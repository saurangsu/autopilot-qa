#!/usr/bin/env python3
"""
AutoPilot QA — CLI entry point.

Modes:
  Build knowledge file from sources:
    python run.py --build-knowledge knowledge-config.yaml

  Build knowledge + full pipeline:
    python run.py --build-knowledge knowledge-config.yaml --review

  Generate only (from existing knowledge file):
    python run.py knowledge/app-knowledge.yaml

  Generate + Review (two-agent pipeline):
    python run.py knowledge/app-knowledge.yaml --review

  Review only (skip generation, review an existing draft):
    python run.py knowledge/app-knowledge.yaml --review-only output/draft-scenarios.md

Options:
  --output PATH        Where to save the final output (default varies by mode)
  --draft-output PATH  Where to save the draft when using --review
                       (default: output/draft-scenarios.md)
  --model MODEL        Claude model for all agents (default: claude-sonnet-4-6)
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
from autopilot_qa.knowledge_builder import run as build_knowledge


def main() -> None:
    parser = argparse.ArgumentParser(
        description="AutoPilot QA — AI-native test scenario generator.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # ── Knowledge input (positional or via --build-knowledge) ─────────────────
    parser.add_argument(
        "knowledge",
        nargs="?",           # optional — not needed when --build-knowledge is used
        help="Path to app-knowledge.yaml (omit when using --build-knowledge)",
    )
    parser.add_argument(
        "--build-knowledge",
        metavar="CONFIG_PATH",
        help="Build app-knowledge.yaml from sources defined in a knowledge-config.yaml",
    )

    # ── Pipeline mode flags (mutually exclusive) ──────────────────────────────
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--review",
        action="store_true",
        help="Run the full pipeline: generate draft → review → finalize",
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
        help=f"Claude model for all agents (default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--no-stream",
        action="store_true",
        help="Disable streaming — wait for full response before printing",
    )

    args = parser.parse_args()
    stream = not args.no_stream

    # ── Validate arg combinations ─────────────────────────────────────────────
    if not args.build_knowledge and not args.knowledge:
        parser.error("Provide a knowledge file path, or use --build-knowledge <config>")

    # ── Determine mode ────────────────────────────────────────────────────────
    building = bool(args.build_knowledge)

    if args.review_only:
        scenario_mode = "review-only"
        default_output = "output/test-scenarios-final.md"
    elif args.review:
        scenario_mode = "generate + review"
        default_output = "output/test-scenarios-final.md"
    elif args.knowledge:
        scenario_mode = "generate"
        default_output = "output/test-scenarios.md"
    else:
        scenario_mode = None   # --build-knowledge only, no scenario generation
        default_output = None

    final_output = args.output or default_output

    # ── Header ────────────────────────────────────────────────────────────────
    print("\nAutoPilot QA — AI Test Scenario Pipeline")
    print("=" * 50)
    if building:
        print(f"  Knowledge config : {args.build_knowledge}")
    if scenario_mode:
        print(f"  Scenario mode    : {scenario_mode}")
    print(f"  Model            : {args.model}")

    try:
        knowledge_path = args.knowledge

        # ── Step 0: Build knowledge file (optional) ───────────────────────────
        if building:
            print()
            step_label = "[ STEP 0 ]" if scenario_mode else "[ Knowledge Builder ]"
            print(f"{step_label}  Knowledge Builder Agent")
            print("-" * 50)
            generated_knowledge = build_knowledge(
                config_path=args.build_knowledge,
                model=args.model,
                stream=stream,
            )
            print(f"\n  Knowledge file saved to: {generated_knowledge}")
            # Use the generated file for subsequent scenario steps
            knowledge_path = str(generated_knowledge)

        if not scenario_mode:
            print("\nDone.\n")
            return

        # ── Scenario steps ────────────────────────────────────────────────────
        print()

        if scenario_mode == "generate":
            print(f"  Output           : {final_output}")
            print()
            print("[ Generator Agent ]")
            print("-" * 50)
            out = generate(
                knowledge_path=knowledge_path,
                output_path=final_output,
                model=args.model,
                stream=stream,
            )
            print(f"\n  Saved to: {out}")

        elif scenario_mode == "generate + review":
            total = "3" if building else "2"
            step_gen = "2" if building else "1"
            step_rev = "3" if building else "2"

            print(f"  Draft output     : {args.draft_output}")
            print(f"  Final output     : {final_output}")
            print()
            print(f"[ STEP {step_gen} / {total} ]  Generator Agent")
            print("-" * 50)
            draft_path = generate(
                knowledge_path=knowledge_path,
                output_path=args.draft_output,
                model=args.model,
                stream=stream,
            )
            print(f"\n  Draft saved to: {draft_path}")

            print()
            print(f"[ STEP {step_rev} / {total} ]  Reviewer Agent")
            print("-" * 50)
            final_path = review(
                knowledge_path=knowledge_path,
                draft_path=draft_path,
                output_path=final_output,
                model=args.model,
                stream=stream,
            )
            print(f"\n  Final saved to: {final_path}")

        elif scenario_mode == "review-only":
            print(f"  Draft            : {args.review_only}")
            print(f"  Final output     : {final_output}")
            print()
            print("[ Reviewer Agent ]")
            print("-" * 50)
            final_path = review(
                knowledge_path=knowledge_path,
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
