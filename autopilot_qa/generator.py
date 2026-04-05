"""
generator.py — Core agent: knowledge file → manual test scenarios.

Flow:
  1. Load app-knowledge.yaml
  2. Build prompt (via prompts.py)
  3. Call Claude API (claude-sonnet-4-6)
  4. Write output/test-scenarios.md

DESIGN DECISION: Single-agent, single-call for v0.1
────────────────────────────────────────────────────
The full AutoPilot QA vision uses a LangGraph multi-agent pipeline (crawl →
extract → generate → validate). For this MVP we collapse everything into one
Claude call: the knowledge file IS the extracted context, so no crawling or
extraction step is needed.

This means v0.1 is:
  - Zero external dependencies beyond `anthropic` and `pyyaml`
  - Runnable without a browser or a live app
  - Fast: single round-trip to Claude (~15–25 seconds for 20-30 scenarios)

Future add-on hooks are marked with # FUTURE-HOOK comments so it's clear
where the crawler and code-gen agents will plug in later.

DESIGN DECISION: Why claude-sonnet-4-6
────────────────────────────────────────
sonnet-4-6 hits the right balance of:
  - Instruction-following (critical for the strict output template)
  - Reasoning quality (catches non-obvious edge cases)
  - Cost (viable for running on every CI pipeline run)
  Opus would produce marginally richer scenarios but at 5x the cost.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

import yaml

import anthropic

from .prompts import SYSTEM_PROMPT, build_user_prompt


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────
DEFAULT_MODEL = "claude-sonnet-4-6"

# Max tokens for the response. 20-30 scenarios with step tables ~ 4-6k tokens.
# 8192 gives headroom without triggering unnecessary truncation.
MAX_TOKENS = 8192


# ─────────────────────────────────────────────────────────────────────────────
# 1. Load knowledge file
# ─────────────────────────────────────────────────────────────────────────────
def load_knowledge(path: str | Path) -> dict:
    """Load and parse an app-knowledge.yaml file.

    Raises FileNotFoundError if the path doesn't exist.
    Raises yaml.YAMLError if the file is malformed.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Knowledge file not found: {path}")
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"Knowledge file must be a YAML mapping, got: {type(data)}")
    return data


# ─────────────────────────────────────────────────────────────────────────────
# 2. Call Claude
# ─────────────────────────────────────────────────────────────────────────────
def generate_scenarios(
    knowledge: dict,
    *,
    model: str = DEFAULT_MODEL,
    api_key: str | None = None,
    stream: bool = True,
) -> str:
    """Call Claude with the knowledge file context and return the markdown output.

    Args:
        knowledge: Parsed app-knowledge dict.
        model: Claude model ID.
        api_key: Anthropic API key. Falls back to ANTHROPIC_API_KEY env var.
        stream: If True, stream the response to stdout while collecting it.
                Gives the user visible progress for what is a ~15-25s call.

    Returns:
        The generated test scenarios as a markdown string.
    """
    # FUTURE-HOOK: crawl_agent output could be merged into `knowledge` here
    # before building the prompt — enriching it with live DOM + intercepted calls.

    user_prompt = build_user_prompt(knowledge)
    client = anthropic.Anthropic(api_key=api_key)  # api_key=None → reads env var

    app_name = knowledge.get("application", {}).get("name", "the application")
    print(f"\n  Calling {model} to generate test scenarios for {app_name}...")
    print("  (streaming output)\n")
    print("─" * 70)

    collected: list[str] = []

    if stream:
        with client.messages.stream(
            model=model,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        ) as stream_ctx:
            for text in stream_ctx.text_stream:
                print(text, end="", flush=True)
                collected.append(text)
        print()  # final newline after stream ends
    else:
        response = client.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        content = response.content[0].text
        print(content)
        collected.append(content)

    print("─" * 70)
    return "".join(collected)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Save output
# ─────────────────────────────────────────────────────────────────────────────
def save_output(content: str, knowledge: dict, output_path: str | Path) -> Path:
    """Write the generated scenarios to a markdown file with a header.

    The header records the app name, generation timestamp, and model used so the
    output file is self-describing — important when it gets committed to source
    control alongside the knowledge file.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    app_name = knowledge.get("application", {}).get("name", "Application")
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    header = "\n".join([
        f"# Manual Test Scenarios — {app_name}",
        f"> Generated by AutoPilot QA v0.1 on {timestamp} using `{DEFAULT_MODEL}`  ",
        f"> Source: `knowledge/app-knowledge.yaml`  ",
        f"> To regenerate: `python run.py knowledge/app-knowledge.yaml`",
        "",
        "---",
        "",
    ])

    output_path.write_text(header + content, encoding="utf-8")
    return output_path


# ─────────────────────────────────────────────────────────────────────────────
# 4. Top-level orchestrator
# ─────────────────────────────────────────────────────────────────────────────
def run(
    knowledge_path: str | Path,
    output_path: str | Path = "output/test-scenarios.md",
    model: str = DEFAULT_MODEL,
    stream: bool = True,
) -> Path:
    """End-to-end: load knowledge → generate → save → return output path."""
    knowledge = load_knowledge(knowledge_path)
    content = generate_scenarios(knowledge, model=model, stream=stream)
    out = save_output(content, knowledge, output_path)
    return out
