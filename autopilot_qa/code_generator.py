"""
code_generator.py — Code Generator Agent: test scenarios → automation scripts.

Takes the finalized test scenarios (Markdown) and generates executable automation code:
  - Playwright (TypeScript) — one .spec.ts per category, written to output/playwright/
  - RestAssured (Java)      — ApiTests.java for API scenarios, written to output/restassured/

Flow:
  1. Read the finalized scenarios markdown
  2. Call Claude for Playwright codegen (single pass, all categories)
  3. Parse file markers and write output/playwright/*.spec.ts
  4. Call Claude for RestAssured codegen (single pass, API scenarios only)
  5. Write output/restassured/ApiTests.java

DESIGN DECISION: One call per output format
────────────────────────────────────────────
TypeScript and Java have very different structural conventions. A single call
asked to produce both would context-switch between paradigms and produce lower-
quality output in both. Two focused calls — each with a role-specific system
prompt — produce cleaner, more idiomatic code.

DESIGN DECISION: File marker parsing for Playwright
─────────────────────────────────────────────────────
Playwright output is a single Claude response containing all spec files
delimited by "// ===FILE: filename.spec.ts===" markers. We collect the full
stream, then split on markers client-side. This keeps it one API call instead
of one per category.

DESIGN DECISION: Scenarios Markdown as input (not the knowledge file)
───────────────────────────────────────────────────────────────────────
The code generator's input is the FINALIZED scenarios (Reviewer Agent output).
This decoupling means:
  - The code agent can run independently on any well-formed scenarios file
  - The knowledge file does not need to be re-parsed in this stage
  - Each agent in the pipeline has a single, clear input
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import anthropic

from .prompts import (
    PLAYWRIGHT_CODEGEN_SYSTEM_PROMPT,
    RESTASSURED_CODEGEN_SYSTEM_PROMPT,
    build_playwright_prompt,
    build_restassured_prompt,
)
from .generator import DEFAULT_MODEL

# Max tokens for code generation — spec files for 20+ test cases can be large.
# 16384 gives headroom for full Playwright output across all categories.
PLAYWRIGHT_MAX_TOKENS = 16384
RESTASSURED_MAX_TOKENS = 8192

# Marker that Claude uses to delimit separate spec files in its Playwright output.
_FILE_MARKER_RE = re.compile(r"^//\s*===FILE:\s*(.+?\.spec\.ts)===\s*$", re.MULTILINE)


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _stream_response(
    client: anthropic.Anthropic,
    *,
    model: str,
    max_tokens: int,
    system: str,
    user: str,
    stream: bool,
    label: str,
) -> str:
    """Call Claude and return the full text response.

    Streams to stdout while collecting when stream=True, giving visible progress
    for what are typically 30–60 second calls for code generation.
    """
    print(f"\n  Calling {model} for {label}...")
    print("  (streaming output)\n")
    print("─" * 70)

    collected: list[str] = []

    if stream:
        with client.messages.stream(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        ) as stream_ctx:
            for text in stream_ctx.text_stream:
                print(text, end="", flush=True)
                collected.append(text)
        print()
    else:
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        content = response.content[0].text
        print(content)
        collected.append(content)

    print("─" * 70)
    return "".join(collected)


def _split_playwright_files(raw: str) -> dict[str, str]:
    """Parse file marker delimiters and return a {filename: content} dict.

    If no markers are found (Claude didn't follow the format), falls back to
    writing the entire output as 'tests.spec.ts' so no content is lost.
    """
    markers = list(_FILE_MARKER_RE.finditer(raw))

    if not markers:
        # Fallback: write all content to a single file
        return {"tests.spec.ts": raw.strip()}

    files: dict[str, str] = {}
    for i, match in enumerate(markers):
        filename = match.group(1).strip()
        start = match.end()
        end = markers[i + 1].start() if i + 1 < len(markers) else len(raw)
        content = raw[start:end].strip()
        if content:
            files[filename] = content

    return files


def _strip_markdown_fences(text: str) -> str:
    """Remove leading/trailing ```java or ```typescript fences if Claude added them."""
    text = re.sub(r"^```[\w]*\n", "", text.strip())
    text = re.sub(r"\n```$", "", text)
    return text.strip()


# ─────────────────────────────────────────────────────────────────────────────
# Core generation functions
# ─────────────────────────────────────────────────────────────────────────────

def generate_playwright(
    scenarios_content: str,
    output_dir: Path,
    *,
    model: str = DEFAULT_MODEL,
    api_key: str | None = None,
    stream: bool = True,
) -> list[Path]:
    """Generate Playwright TypeScript spec files from finalized scenarios.

    Returns the list of files written.
    """
    client = anthropic.Anthropic(api_key=api_key)
    user_prompt = build_playwright_prompt(scenarios_content)

    raw = _stream_response(
        client,
        model=model,
        max_tokens=PLAYWRIGHT_MAX_TOKENS,
        system=PLAYWRIGHT_CODEGEN_SYSTEM_PROMPT,
        user=user_prompt,
        stream=stream,
        label="Playwright TypeScript codegen",
    )

    files = _split_playwright_files(raw)

    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for filename, content in files.items():
        out_path = output_dir / filename
        out_path.write_text(content + "\n", encoding="utf-8")
        written.append(out_path)
        print(f"  Written: {out_path}")

    return written


def generate_restassured(
    scenarios_content: str,
    output_dir: Path,
    *,
    model: str = DEFAULT_MODEL,
    api_key: str | None = None,
    stream: bool = True,
) -> Path:
    """Generate RestAssured Java test class from finalized scenarios.

    Returns the path to the written file.
    """
    client = anthropic.Anthropic(api_key=api_key)
    user_prompt = build_restassured_prompt(scenarios_content)

    raw = _stream_response(
        client,
        model=model,
        max_tokens=RESTASSURED_MAX_TOKENS,
        system=RESTASSURED_CODEGEN_SYSTEM_PROMPT,
        user=user_prompt,
        stream=stream,
        label="RestAssured Java codegen",
    )

    java_content = _strip_markdown_fences(raw)

    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / "ApiTests.java"
    out_path.write_text(java_content + "\n", encoding="utf-8")
    print(f"  Written: {out_path}")

    return out_path


# ─────────────────────────────────────────────────────────────────────────────
# Top-level orchestrator
# ─────────────────────────────────────────────────────────────────────────────

def run(
    scenarios_path: str | Path,
    output_base: str | Path = "output",
    model: str = DEFAULT_MODEL,
    stream: bool = True,
) -> dict[str, list[Path]]:
    """End-to-end: read scenarios → generate Playwright + RestAssured → return paths.

    Args:
        scenarios_path: Path to the finalized scenarios Markdown file.
        output_base:    Root output directory. Playwright files go to
                        {output_base}/playwright/, RestAssured to
                        {output_base}/restassured/.
        model:          Claude model ID.
        stream:         Stream responses to stdout while collecting.

    Returns:
        Dict with keys 'playwright' and 'restassured', each a list of Path objects.
    """
    scenarios_path = Path(scenarios_path)
    if not scenarios_path.exists():
        raise FileNotFoundError(f"Scenarios file not found: {scenarios_path}")

    scenarios_content = scenarios_path.read_text(encoding="utf-8")
    output_base = Path(output_base)

    playwright_files = generate_playwright(
        scenarios_content,
        output_base / "playwright",
        model=model,
        stream=stream,
    )

    restassured_files = generate_restassured(
        scenarios_content,
        output_base / "restassured",
        model=model,
        stream=stream,
    )

    return {
        "playwright": playwright_files,
        "restassured": [restassured_files],
    }
