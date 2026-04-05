"""
reviewer.py — Reviewer agent: draft scenarios + knowledge file → finalized scenarios.

DESIGN DECISION: Mirror the generator's structure deliberately
──────────────────────────────────────────────────────────────
The reviewer module is structured identically to generator.py:
  load_knowledge() — same, reused from generator
  review_scenarios() — analogous to generate_scenarios()
  save_output()     — same pattern, different header
  run()             — same signature, takes draft_path instead of None

This is intentional. Consistent structure makes the codebase more readable and
lowers the bar for adding a third agent later (e.g., a test-data agent or a
prioritisation agent) — you just copy the pattern.

DESIGN DECISION: The reviewer's output IS the source of truth
──────────────────────────────────────────────────────────────
The reviewer does not annotate the draft — it rewrites the complete scenario
set. The final output file replaces the draft as the deliverable. The draft is
kept at output/draft-scenarios.md for comparison/diff, but it is NOT the output
engineers should act on. Only the finalized file is.

This means if you run `--review` twice you get two independently reviewed sets,
not an accumulation of reviews on reviews. That is the correct behaviour.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import anthropic

from .generator import load_knowledge, DEFAULT_MODEL
from .prompts import REVIEWER_SYSTEM_PROMPT, build_reviewer_prompt

# The reviewer is given more tokens than the generator.
# It needs to output the Review Summary + the complete finalized set.
# 25-30 scenarios + summary ≈ 6-8k tokens; 12k gives comfortable headroom.
REVIEWER_MAX_TOKENS = 12000


def review_scenarios(
    knowledge: dict,
    draft_content: str,
    *,
    model: str = DEFAULT_MODEL,
    api_key: str | None = None,
    stream: bool = True,
) -> str:
    """Call Claude with the knowledge file + draft and return the finalized markdown.

    Args:
        knowledge: Parsed app-knowledge dict (the ground truth).
        draft_content: The draft scenarios as a markdown string.
        model: Claude model ID.
        api_key: Anthropic API key. Falls back to ANTHROPIC_API_KEY env var.
        stream: Stream the response to stdout while collecting.

    Returns:
        The review summary + finalized scenarios as a markdown string.
    """
    user_prompt = build_reviewer_prompt(knowledge, draft_content)
    client = anthropic.Anthropic(api_key=api_key)

    app_name = knowledge.get("application", {}).get("name", "the application")
    print(f"\n  Reviewer Agent — reviewing scenarios for {app_name}...")
    print(f"  Draft size: {len(draft_content):,} chars")
    print("  (streaming output)\n")
    print("─" * 70)

    collected: list[str] = []

    if stream:
        with client.messages.stream(
            model=model,
            max_tokens=REVIEWER_MAX_TOKENS,
            system=REVIEWER_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        ) as stream_ctx:
            for text in stream_ctx.text_stream:
                print(text, end="", flush=True)
                collected.append(text)
        print()
    else:
        response = client.messages.create(
            model=model,
            max_tokens=REVIEWER_MAX_TOKENS,
            system=REVIEWER_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        content = response.content[0].text
        print(content)
        collected.append(content)

    print("─" * 70)
    return "".join(collected)


def save_output(content: str, knowledge: dict, output_path: str | Path) -> Path:
    """Write the finalized reviewed scenarios to a markdown file."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    app_name = knowledge.get("application", {}).get("name", "Application")
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    header = "\n".join([
        f"# Manual Test Scenarios (Reviewed) — {app_name}",
        f"> Reviewed by AutoPilot QA Reviewer Agent on {timestamp} using `{DEFAULT_MODEL}`  ",
        f"> Source: `knowledge/app-knowledge.yaml`  ",
        f"> Draft: `output/draft-scenarios.md`  ",
        f"> To regenerate: `python run.py knowledge/app-knowledge.yaml --review`",
        "",
        "---",
        "",
    ])

    output_path.write_text(header + content, encoding="utf-8")
    return output_path


def run(
    knowledge_path: str | Path,
    draft_path: str | Path,
    output_path: str | Path = "output/test-scenarios-final.md",
    model: str = DEFAULT_MODEL,
    stream: bool = True,
) -> Path:
    """End-to-end: load knowledge + draft → review → save → return output path."""
    knowledge = load_knowledge(knowledge_path)

    draft_path = Path(draft_path)
    if not draft_path.exists():
        raise FileNotFoundError(f"Draft scenarios file not found: {draft_path}")
    draft_content = draft_path.read_text(encoding="utf-8")

    content = review_scenarios(knowledge, draft_content, model=model, stream=stream)
    out = save_output(content, knowledge, output_path)
    return out
