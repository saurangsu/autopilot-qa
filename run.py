#!/usr/bin/env python3
"""
AutoPilot QA — CLI entry point.

Modes:
  Generate only (default):
    python run.py knowledge/app-knowledge.yaml

  Generate + Review (two-agent pipeline):
    python run.py knowledge/app-knowledge.yaml --review

  Review only (skip generation, review an existing draft):
    python run.py knowledge/app-knowledge.yaml --review-only output/draft-scenarios.md

Options:
  --output PATH        Where to save the final output (default varies by mode)
  --draft-output PATH  Where to save the draft when using --review
                       (default: output/draft-scenarios.md)
  --model MODEL        Claude model for both agents (default: claude-sonnet-4-6)
  --no-stream          Wait for full response before printing

Environment:
  ANTHROPIC_API_KEY  (required) — your Anthropic API key
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from autopilot_qa.generator import run as generate, DEFAULT_MODEL
from autopilot_qa.reviewer import run as review


def main() -> None:
    parser = argparse.ArgumentParser(
        description="AutoPilot QA — AI-native test scenario generator.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "knowledge",
        help="Path to your app-knowledge.yaml file",
    )

    # ── Mode flags (mutually exclusive) ──────────────────────────────────────
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--review",
        action="store_true",
        help="Run the full two-agent pipeline: generate draft → review → finalize",
    )
    mode.add_argument(
        "--review-only",
        metavar="DRAFT_PATH",
        help="Skip generation; review an existing draft file at DRAFT_PATH",
    )

    # ── Output paths ──────────────────────────────────────────────────────────
    parser.add_argument(
        "--output",
        default=None,
        help=(
            "Output file path. "
            "Default: output/test-scenarios.md (generate-only) or "
            "output/test-scenarios-final.md (review modes)"
        ),
    )
    parser.add_argument(
        "--draft-output",
        default="output/draft-scenarios.md",
        help="Where to save the generator draft when using --review "
             "(default: output/draft-scenarios.md)",
    )

    # ── Model / streaming ─────────────────────────────────────────────────────
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Claude model for both agents (default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--no-stream",
        action="store_true",
        help="Disable streaming — wait for full response before printing",
    )

    args = parser.parse_args()
    stream = not args.no_stream

    # ── Determine mode and resolve defaults ───────────────────────────────────
    if args.review_only:
        mode_label = "review-only"
        default_output = "output/test-scenarios-final.md"
    elif args.review:
        mode_label = "generate + review"
        default_output = "output/test-scenarios-final.md"
    else:
        mode_label = "generate"
        default_output = "output/test-scenarios.md"

    final_output = args.output or default_output

    # ── Header ────────────────────────────────────────────────────────────────
    print("\nAutoPilot QA — AI Test Scenario Pipeline")
    print("=" * 50)
    print(f"  Mode           : {mode_label}")
    print(f"  Knowledge file : {args.knowledge}")
    print(f"  Model          : {args.model}")

    try:
        # ── Generate-only ─────────────────────────────────────────────────────
        if mode_label == "generate":
            print(f"  Output         : {final_output}")
            out = generate(
                knowledge_path=args.knowledge,
                output_path=final_output,
                model=args.model,
                stream=stream,
            )
            print(f"\n  Saved to: {out}")

        # ── Generate + Review ─────────────────────────────────────────────────
        elif mode_label == "generate + review":
            print(f"  Draft output   : {args.draft_output}")
            print(f"  Final output   : {final_output}")
            print()

            # Step 1 — Generator Agent
            print("[ STEP 1 / 2 ]  Generator Agent")
            print("-" * 50)
            draft_path = generate(
                knowledge_path=args.knowledge,
                output_path=args.draft_output,
                model=args.model,
                stream=stream,
            )
            print(f"\n  Draft saved to: {draft_path}")

            # Step 2 — Reviewer Agent
            print()
            print("[ STEP 2 / 2 ]  Reviewer Agent")
            print("-" * 50)
            final_path = review(
                knowledge_path=args.knowledge,
                draft_path=draft_path,
                output_path=final_output,
                model=args.model,
                stream=stream,
            )
            print(f"\n  Final saved to: {final_path}")

        # ── Review-only ───────────────────────────────────────────────────────
        elif mode_label == "review-only":
            print(f"  Draft          : {args.review_only}")
            print(f"  Final output   : {final_output}")
            print()
            print("[ Reviewer Agent ]")
            print("-" * 50)
            final_path = review(
                knowledge_path=args.knowledge,
                draft_path=args.review_only,
                output_path=final_output,
                model=args.model,
                stream=stream,
            )
            print(f"\n  Final saved to: {final_path}")

        print("\nDone.\n")

    except FileNotFoundError as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
