"""
knowledge_builder.py — Knowledge Builder Agent.

Reads a knowledge-config.yaml (the single user control point), collects context
from up to three sources (human prompt, source repo, documentation files), and
calls Claude to produce an app-knowledge.yaml file.

Flow:
  knowledge-config.yaml
        │
        ├── Source 1: prompt.text       (human description)
        ├── Source 2: repo (local/GH)   (source code files)
        └── Source 3: docs[]            (free-form documentation)
        │
        ▼ (assembled + sent to Claude)
  knowledge/app-knowledge.yaml

DESIGN DECISION: Config file as the single control point
─────────────────────────────────────────────────────────
All three sources are configured in one YAML file rather than as CLI flags.
Reasons:
  - The config is version-controllable alongside the knowledge file — you can
    see exactly what inputs produced a given knowledge file.
  - Multiple doc files would be unwieldy as CLI args.
  - The config can be shared with a team; anyone can re-run the same build.
  - enable/disable per source is cleaner in YAML than with flag combinatorics.

DESIGN DECISION: Source priority ordering
──────────────────────────────────────────
When all three sources are present, they are assembled in this order:
  1. Human prompt   — highest trust; sets the intent and framing
  2. Source code    — ground truth for actual routes, APIs, entities
  3. Documentation  — supplementary context, may be outdated

Claude is told about this ordering explicitly in the prompt so it weights
the human description when resolving ambiguities between sources.

DESIGN DECISION: Repo file prioritisation
──────────────────────────────────────────
When reading a repo, files are sorted by relevance:
  1. API route files first  (contain endpoint definitions)
  2. Page/view files        (contain UI routes and interactions)
  3. Model/lib/db files     (contain domain entities and schemas)
  4. Everything else

This prioritisation ensures the most important files make it within the
max_files budget, even for large repos.

DESIGN DECISION: Output is raw YAML, validated after generation
────────────────────────────────────────────────────────────────
Claude is instructed to output only valid YAML (no markdown fences).
We parse it immediately after generation to catch malformed output before
writing the file. If parsing fails, we surface the error clearly rather
than writing a broken knowledge file.
"""

from __future__ import annotations

import base64
import fnmatch
import json
import re
import subprocess
from datetime import datetime
from pathlib import Path

import yaml
import anthropic

from .prompts import KNOWLEDGE_BUILDER_SYSTEM_PROMPT, build_knowledge_prompt
from .generator import DEFAULT_MODEL

MAX_FILE_BYTES = 60_000   # ~15k tokens per file — safety cap


# ─────────────────────────────────────────────────────────────────────────────
# 1. Load config
# ─────────────────────────────────────────────────────────────────────────────
def load_config(path: str | Path) -> dict:
    """Load and parse a knowledge-config.yaml file."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Knowledge config not found: {path}")
    with open(path, "r", encoding="utf-8") as fh:
        config = yaml.safe_load(fh)
    if not isinstance(config, dict):
        raise ValueError("knowledge-config.yaml must be a YAML mapping")
    return config


# ─────────────────────────────────────────────────────────────────────────────
# 2. Collect sources
# ─────────────────────────────────────────────────────────────────────────────
def _matches_patterns(file_path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(file_path, p) for p in patterns)


def _collect_local_repo(repo_cfg: dict, config_dir: Path) -> str:
    """Read source files from a local directory."""
    raw_path = repo_cfg.get("path", ".")
    repo_root = (config_dir / raw_path).resolve()

    if not repo_root.exists():
        raise FileNotFoundError(f"Repo path not found: {repo_root}")

    patterns = repo_cfg.get("patterns", ["**/*.py", "**/*.ts", "**/*.tsx"])
    excludes = repo_cfg.get("exclude", ["node_modules/**", ".next/**"])
    max_files = int(repo_cfg.get("max_files", 40))

    # Collect all matching files
    candidates: list[Path] = []
    for pattern in patterns:
        candidates.extend(repo_root.glob(pattern))

    # Deduplicate and filter excluded patterns
    seen: set[Path] = set()
    filtered: list[Path] = []
    for f in candidates:
        if f in seen or not f.is_file():
            continue
        seen.add(f)
        rel = str(f.relative_to(repo_root))
        if not _matches_patterns(rel, excludes):
            filtered.append(f)

    # Sort by relevance: API routes → pages/views → lib/models → rest
    def priority(p: Path) -> int:
        s = str(p).lower()
        if "/api/" in s or "route" in s:
            return 0
        if "/app/" in s or "/pages/" in s or "/views/" in s:
            return 1
        if "/lib/" in s or "/models/" in s or "/db" in s or "schema" in s:
            return 2
        return 3

    filtered.sort(key=priority)
    filtered = filtered[:max_files]

    blocks: list[str] = []
    for f in filtered:
        rel = str(f.relative_to(repo_root))
        try:
            content = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        if len(content.encode()) > MAX_FILE_BYTES:
            content = content[: MAX_FILE_BYTES] + "\n... [truncated]"
        ext = f.suffix.lstrip(".")
        blocks.append(f"### {rel}\n```{ext}\n{content}\n```")

    print(f"  Repo: read {len(filtered)} files from {repo_root}")
    return "\n\n".join(blocks)


def _collect_github_repo(repo_cfg: dict) -> str:
    """Read source files from a GitHub repo via the gh CLI."""
    github_url = repo_cfg.get("github_url", "")
    # Parse owner/repo from https://github.com/owner/repo
    match = re.search(r"github\.com[/:]([^/]+)/([^/\s]+?)(?:\.git)?$", github_url)
    if not match:
        raise ValueError(f"Cannot parse GitHub URL: {github_url}")
    owner, repo = match.group(1), match.group(2)

    patterns = repo_cfg.get("patterns", ["**/*.py", "**/*.ts", "**/*.tsx"])
    excludes = repo_cfg.get("exclude", ["node_modules/**"])
    max_files = int(repo_cfg.get("max_files", 40))

    # Get full file tree
    print(f"  Repo: fetching file tree for {owner}/{repo} ...")
    try:
        result = subprocess.run(
            ["gh", "api", f"repos/{owner}/{repo}/git/trees/HEAD?recursive=1"],
            capture_output=True, text=True, check=True,
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"gh api failed: {e.stderr}") from e

    tree = json.loads(result.stdout).get("tree", [])
    blob_paths = [item["path"] for item in tree if item.get("type") == "blob"]

    # Filter by patterns and excludes
    filtered = [
        p for p in blob_paths
        if _matches_patterns(p, patterns) and not _matches_patterns(p, excludes)
    ]

    # Sort by priority
    def priority(p: str) -> int:
        s = p.lower()
        if "/api/" in s or "route" in s:
            return 0
        if "/app/" in s or "/pages/" in s or "/views/" in s:
            return 1
        if "/lib/" in s or "/models/" in s or "db" in s or "schema" in s:
            return 2
        return 3

    filtered.sort(key=priority)
    filtered = filtered[:max_files]

    blocks: list[str] = []
    for path in filtered:
        try:
            res = subprocess.run(
                ["gh", "api", f"repos/{owner}/{repo}/contents/{path}"],
                capture_output=True, text=True, check=True,
            )
            data = json.loads(res.stdout)
            content = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
        except Exception as e:
            print(f"  Warning: could not read {path}: {e}")
            continue
        if len(content.encode()) > MAX_FILE_BYTES:
            content = content[:MAX_FILE_BYTES] + "\n... [truncated]"
        ext = Path(path).suffix.lstrip(".")
        blocks.append(f"### {path}\n```{ext}\n{content}\n```")

    print(f"  Repo: read {len(blocks)} files from {owner}/{repo}")
    return "\n\n".join(blocks)


def collect_sources(config: dict, config_path: Path) -> dict[str, str]:
    """Collect content from all enabled sources. Returns dict with keys:
    'prompt', 'repo', 'docs' — each a string or empty string if disabled.
    """
    config_dir = config_path.parent
    sources: dict[str, str] = {"prompt": "", "repo": "", "docs": ""}

    # Source 1 — Human prompt
    prompt_cfg = config.get("prompt", {})
    if prompt_cfg.get("enabled", False):
        text = prompt_cfg.get("text", "").strip()
        if text:
            sources["prompt"] = text
            print("  Prompt: collected")

    # Source 2 — Source repo
    repo_cfg = config.get("repo", {})
    if repo_cfg.get("enabled", False):
        repo_type = repo_cfg.get("type", "local")
        if repo_type == "local":
            sources["repo"] = _collect_local_repo(repo_cfg, config_dir)
        elif repo_type == "github":
            sources["repo"] = _collect_github_repo(repo_cfg)
        else:
            raise ValueError(f"Unknown repo type: {repo_type!r} — must be 'local' or 'github'")

    # Source 3 — Documentation files
    docs_cfg = config.get("docs", {})
    if docs_cfg.get("enabled", False):
        doc_blocks: list[str] = []
        for entry in docs_cfg.get("files", []):
            doc_path = (config_dir / entry["path"]).resolve()
            if not doc_path.exists():
                print(f"  Warning: doc file not found, skipping: {doc_path}")
                continue
            try:
                content = doc_path.read_text(encoding="utf-8", errors="replace")
            except Exception as e:
                print(f"  Warning: could not read {doc_path}: {e}")
                continue
            if len(content.encode()) > MAX_FILE_BYTES:
                content = content[:MAX_FILE_BYTES] + "\n... [truncated]"
            doc_blocks.append(f"### {entry['path']}\n{content}")
        sources["docs"] = "\n\n---\n\n".join(doc_blocks)
        print(f"  Docs: collected {len(doc_blocks)} file(s)")

    return sources


# ─────────────────────────────────────────────────────────────────────────────
# 3. Call Claude
# ─────────────────────────────────────────────────────────────────────────────
def build_knowledge(
    sources: dict[str, str],
    *,
    model: str = DEFAULT_MODEL,
    api_key: str | None = None,
    stream: bool = True,
) -> str:
    """Call Claude with assembled sources and return the generated YAML string."""
    user_prompt = build_knowledge_prompt(sources)
    client = anthropic.Anthropic(api_key=api_key)

    enabled = [k for k, v in sources.items() if v.strip()]
    print(f"\n  Calling {model} to generate app-knowledge.yaml ...")
    print(f"  Sources in use: {', '.join(enabled) if enabled else 'none'}")
    print("  (streaming output)\n")
    print("─" * 70)

    collected: list[str] = []

    if stream:
        with client.messages.stream(
            model=model,
            max_tokens=4096,
            system=KNOWLEDGE_BUILDER_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        ) as stream_ctx:
            for text in stream_ctx.text_stream:
                print(text, end="", flush=True)
                collected.append(text)
        print()
    else:
        response = client.messages.create(
            model=model,
            max_tokens=4096,
            system=KNOWLEDGE_BUILDER_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        content = response.content[0].text
        print(content)
        collected.append(content)

    print("─" * 70)
    return "".join(collected)


# ─────────────────────────────────────────────────────────────────────────────
# 4. Validate + save
# ─────────────────────────────────────────────────────────────────────────────
def save_knowledge(content: str, output_path: str | Path) -> Path:
    """Validate the YAML is parseable, then write to disk with a header comment."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Strip accidental markdown fences if Claude added them
    stripped = re.sub(r"^```ya?ml\s*\n", "", content.strip(), flags=re.MULTILINE)
    stripped = re.sub(r"\n```\s*$", "", stripped.strip())

    # Validate it's parseable YAML before writing
    try:
        parsed = yaml.safe_load(stripped)
    except yaml.YAMLError as e:
        raise ValueError(f"Claude returned malformed YAML: {e}\n\nRaw output:\n{content}") from e

    if not isinstance(parsed, dict):
        raise ValueError(f"Generated YAML is not a mapping (got {type(parsed).__name__})")

    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    header = (
        f"# app-knowledge.yaml\n"
        f"# Generated by AutoPilot QA Knowledge Builder on {timestamp}\n"
        f"# Source: knowledge-config.yaml\n"
        f"# Review and adjust before running the test scenario pipeline.\n\n"
    )

    output_path.write_text(header + stripped, encoding="utf-8")
    return output_path


# ─────────────────────────────────────────────────────────────────────────────
# 5. Orchestrator
# ─────────────────────────────────────────────────────────────────────────────
def run(
    config_path: str | Path,
    model: str = DEFAULT_MODEL,
    stream: bool = True,
) -> Path:
    """End-to-end: load config → collect sources → build → save → return path."""
    config_path = Path(config_path)
    config = load_config(config_path)
    output_path = Path(config.get("output", "knowledge/app-knowledge.yaml"))

    sources = collect_sources(config, config_path)

    if not any(v.strip() for v in sources.values()):
        raise ValueError(
            "No sources are enabled in knowledge-config.yaml. "
            "Enable at least one of: prompt, repo, docs."
        )

    content = build_knowledge(sources, model=model, stream=stream)
    out = save_knowledge(content, output_path)
    return out
