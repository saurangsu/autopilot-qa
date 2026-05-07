#!/usr/bin/env python3
"""
AutoPilot QA — Crawler Pipeline Orchestrator (v0.3)

Chains: crawl → extract [→ generate → validate]
Optionally runs a MonitoringAgent alongside the BFS crawler.
Writes enriched knowledge file back to disk atomically (ADR-017).

Usage:
    python run_crawler.py knowledge/app-knowledge.yaml
        [--no-monitor]              skip MonitoringAgent (BFS without Claude guidance)
        [--no-codegen]              stop after extract; skip Java generation + validation
        [--no-validate]             skip Maven compile validation
        [--no-claude]               skip Claude enrichment in extract step (BS4 only)
        [--no-headless]             run browser in headed mode
        [--browser chromium|firefox|webkit]
        [--timeout MS]              page load timeout (default: 30000)
        [--max-depth N]             BFS depth limit (default: 3)
        [--max-pages N]             hard cap on pages visited (default: 50)
        [--output DIR]              output directory (default: crawler/output)

Environment:
    ANTHROPIC_API_KEY   required for MonitoringAgent and Claude enrichment in extract
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from time import time

import yaml

# ---------------------------------------------------------------------------
# Path bootstrap — makes `crawler` package importable from repo root
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent
sys.path.insert(0, str(REPO_ROOT))

# ---------------------------------------------------------------------------
# ANSI colour helpers (matches agent style)
# ---------------------------------------------------------------------------

RESET  = "\033[0m"
BOLD   = "\033[1m"
RED    = "\033[31m"
YELLOW = "\033[33m"
GREEN  = "\033[32m"
CYAN   = "\033[36m"
DIM    = "\033[2m"


def _c(text: str, *codes: str) -> str:
    return "".join(codes) + text + RESET


def _hr() -> str:
    return DIM + "─" * 60 + RESET


def _step_banner(n: int, total: int, label: str) -> None:
    print()
    print(_c(f"Step {n}/{total}  —  {label}", BOLD, CYAN))
    print(_hr())


def _section(title: str) -> None:
    print()
    print(_c(title, BOLD))
    print(_hr())


def _print_errors(errors: list[str]) -> None:
    for err in errors[:5]:
        print(_c(f"    ⚠  {err}", YELLOW))
    if len(errors) > 5:
        print(_c(f"    … {len(errors) - 5} more", DIM))


# ---------------------------------------------------------------------------
# Knowledge loader
# ---------------------------------------------------------------------------

def _load_knowledge(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        print(_c(f"\n✗  YAML parse error: {exc}", RED), file=sys.stderr)
        sys.exit(1)
    if not isinstance(data, dict):
        print(_c("\n✗  Knowledge file must be a YAML mapping", RED), file=sys.stderr)
        sys.exit(1)
    return data


# ---------------------------------------------------------------------------
# Safe knowledge file write (ADR-017)
#
# Sequence:  backup → tmp write → atomic rename
# Max 2 instances on disk: current + one .bak
#
# Note: yaml.dump strips YAML comments (pyyaml limitation).
# The .bak always preserves the previous version including comments.
# ---------------------------------------------------------------------------

def _write_knowledge(knowledge: dict, path: Path) -> None:
    bak  = path.with_suffix(".yaml.bak")
    tmp  = path.with_suffix(".yaml.tmp")

    # Step 1 — backup current (overwrites previous .bak)
    if path.exists():
        import shutil
        shutil.copy2(path, bak)

    # Step 2 — write enriched content to .tmp
    tmp.write_text(
        yaml.dump(
            knowledge,
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    # Step 3 — atomic rename (POSIX: either lands fully or original survives)
    tmp.replace(path)

    print(_c(f"  ✓  Knowledge file updated: {path}", GREEN))
    if bak.exists():
        print(_c(f"     Previous version backed up: {bak}", DIM))


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def main() -> None:
    args = _parse_args()

    knowledge_path = Path(args.knowledge_file)
    output_dir     = Path(args.output)
    run_validate   = not args.no_codegen and not args.no_validate

    if not knowledge_path.exists():
        print(_c(f"\n✗  Knowledge file not found: {knowledge_path}", RED), file=sys.stderr)
        sys.exit(1)

    # ── Header ────────────────────────────────────────────────────────────
    print()
    print(_c("AutoPilot QA — Crawler Pipeline", BOLD, CYAN))
    print(_hr())
    print(f"  Knowledge : {knowledge_path}")
    print(f"  Browser   : {args.browser}  (headless={not args.no_headless})")
    print(f"  Monitor   : {'disabled (--no-monitor)' if args.no_monitor else 'enabled'}")
    print(f"  Max depth : {args.max_depth}  |  Max pages : {args.max_pages}")
    print(f"  Claude    : {'disabled (--no-claude)' if args.no_claude else 'enabled'}")
    print(f"  Codegen   : {'disabled (--no-codegen)' if args.no_codegen else 'enabled'}")
    print(f"  Validate  : {'disabled' if not run_validate else 'enabled'}")
    print(f"  Output    : {output_dir}")

    knowledge      = _load_knowledge(knowledge_path)
    pipeline_start = time()
    errors: list[str] = []

    total_steps = 2 + (0 if args.no_codegen else 1) + (1 if run_validate else 0)
    step = 0

    # ── MonitoringAgent (optional) ────────────────────────────────────────
    monitor = None
    if not args.no_monitor:
        import os
        if not os.environ.get("ANTHROPIC_API_KEY"):
            print(_c("  ⚠  ANTHROPIC_API_KEY not set — monitor disabled", YELLOW))
        else:
            from crawler.agents.monitor_agent import MonitoringAgent
            monitor = MonitoringAgent(app_knowledge=knowledge)

    # ── Step 1: Crawl ─────────────────────────────────────────────────────
    step += 1
    _step_banner(step, total_steps, "Crawl")

    from crawler.agents.crawl_agent import AppCrawler

    crawler = AppCrawler(
        app_knowledge=knowledge,
        headless=not args.no_headless,
        browser_type=args.browser,
        page_timeout=args.timeout,
        output_dir=output_dir,
        monitor=monitor,
        max_depth=args.max_depth,
        max_pages=args.max_pages,
    )
    t0           = time()
    crawl_result = crawler.run()
    crawler.write_output(crawl_result)
    crawl_secs   = time() - t0

    crawl_meta   = crawl_result.to_dict()["crawl_metadata"]
    ok_count     = crawl_meta["pages_visited"] - crawl_meta["pages_failed"]
    disc_count   = crawl_meta["pages_discovered"]

    if ok_count == 0:
        print(_c("  ✗  Crawl failed — 0 pages successfully loaded", RED, BOLD))
        _print_errors(crawl_result.errors)
        sys.exit(1)

    print(_c(
        f"  ✓  {ok_count} page(s) crawled "
        f"({disc_count} discovered by BFS), "
        f"{crawl_meta['api_calls_captured']} API call(s) "
        f"({crawl_secs:.1f} s)",
        GREEN,
    ))
    errors.extend(crawl_result.errors)

    # ── Write enriched knowledge file ─────────────────────────────────────
    if monitor and monitor._patches_applied:
        _section("Updating Knowledge File")
        try:
            _write_knowledge(knowledge, knowledge_path)
        except Exception as exc:
            msg = f"Knowledge file write failed: {exc}"
            print(_c(f"  ⚠  {msg}", YELLOW))
            errors.append(msg)

    # Build the minimal in-memory payload the extract step needs.
    # Pages that errored are excluded — no point extracting broken DOM.
    dom_snapshots: dict[str, str] = {
        url: pr.dom_html
        for url, pr in crawl_result.pages.items()
        if pr.status == "ok"
    }
    api_calls: list[dict] = [nc.to_dict() for nc in crawl_result.api_calls]

    # ── Step 2: Extract ───────────────────────────────────────────────────
    step += 1
    _step_banner(step, total_steps, "Extract")

    from crawler.agents.extract_agent import ElementExtractor

    extractor = ElementExtractor(
        app_knowledge=knowledge,
        use_claude=not args.no_claude,
        output_dir=output_dir,
    )
    t0             = time()
    extract_result = extractor.run(
        dom_snapshots=dom_snapshots,
        api_calls_intercepted=api_calls,
    )
    extractor.write_output(extract_result)
    extract_secs   = time() - t0

    # Release DOM HTML now — large strings no longer needed in memory.
    # extract_result.json is on disk for the generate step.
    del dom_snapshots
    del api_calls

    extract_meta = extract_result.to_dict()["extraction_metadata"]
    if extract_meta["pages_processed"] == 0:
        print(_c("  ✗  Extraction failed — 0 pages processed", RED, BOLD))
        _print_errors(extract_result.errors)
        sys.exit(1)

    print(_c(
        f"  ✓  {extract_meta['pages_processed']} page(s), "
        f"{extract_meta['total_elements']} element(s), "
        f"{extract_meta['api_endpoints_extracted']} API endpoint(s) "
        f"({extract_secs:.1f} s)",
        GREEN,
    ))
    errors.extend(extract_result.errors)

    if args.no_codegen:
        _final_summary(errors, pipeline_start, output_dir, step, total_steps)
        sys.exit(0 if not errors else 0)  # errors here are non-fatal warnings

    # ── Step 3: Generate ──────────────────────────────────────────────────
    step += 1
    _step_banner(step, total_steps, "Generate Java Artifacts")

    from crawler.agents.generate_agent import JavaCodeGenerator

    # generate_agent reads from disk by design (route_title + api_endpoints
    # are not carried in the extract state field).
    extract_path = output_dir / "extract_result.json"
    extract_data = json.loads(extract_path.read_text(encoding="utf-8"))

    generator  = JavaCodeGenerator(app_knowledge=knowledge, output_dir=output_dir)
    t0         = time()
    gen_result = generator.run(extract_data)
    generator.write_output(gen_result)
    gen_secs   = time() - t0

    total_classes = len(gen_result.page_objects) + len(gen_result.api_clients)
    if total_classes == 0:
        print(_c("  ✗  Generation failed — 0 classes produced", RED, BOLD))
        _print_errors(gen_result.errors)
        sys.exit(1)

    print(_c(
        f"  ✓  {len(gen_result.page_objects)} page object(s), "
        f"{len(gen_result.api_clients)} API client(s) "
        f"({gen_secs:.1f} s)",
        GREEN,
    ))
    errors.extend(gen_result.errors)

    if args.no_validate:
        _final_summary(errors, pipeline_start, output_dir, step, total_steps)
        sys.exit(0 if not errors else 0)

    # ── Step 4: Validate ──────────────────────────────────────────────────
    step += 1
    _step_banner(step, total_steps, "Validate (Maven compile)")

    from crawler.agents.validate_agent import JavaValidator

    gen_path  = output_dir / "generate_result.json"
    gen_data  = json.loads(gen_path.read_text(encoding="utf-8"))

    validator  = JavaValidator(generate_data=gen_data, output_dir=output_dir)
    t0         = time()
    val_result = validator.run()
    validator.write_output(val_result)
    val_secs   = time() - t0

    if val_result.passed:
        print(_c(
            f"  ✓  {val_result.classes_passed}/{val_result.classes_validated} "
            f"class(es) compiled OK ({val_secs:.1f} s)",
            GREEN,
        ))
    else:
        print(_c(
            f"  ✗  {val_result.classes_failed} class(es) failed "
            f"({val_secs:.1f} s)",
            RED,
        ))

    errors.extend(val_result.errors)
    errors.extend(
        f"{v.class_name}: {e}"
        for v in val_result.validations
        for e in v.errors
    )

    _final_summary(errors, pipeline_start, output_dir, step, total_steps)
    sys.exit(0 if val_result.passed and not errors else 1)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def _final_summary(
    errors: list[str],
    start: float,
    output_dir: Path,
    steps_run: int,
    total_steps: int,
) -> None:
    total = time() - start
    _section("Pipeline Summary")

    if errors:
        print(_c(f"  ⚠  Completed {steps_run}/{total_steps} step(s) with {len(errors)} warning(s)", YELLOW, BOLD))
        _print_errors(errors)
    else:
        print(_c(f"  ✓  All {steps_run} step(s) passed", GREEN, BOLD))

    print(f"     Output     : {output_dir}")
    print(f"     Total time : {total:.1f} s")
    print()


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="AutoPilot QA — Crawler Pipeline Orchestrator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python run_crawler.py knowledge/app-knowledge.yaml\n"
            "  python run_crawler.py knowledge/app-knowledge.yaml --no-codegen\n"
            "  python run_crawler.py knowledge/app-knowledge.yaml --no-claude --no-headless\n"
        ),
    )
    parser.add_argument(
        "knowledge_file",
        help="Path to app-knowledge.yaml",
    )
    parser.add_argument(
        "--no-monitor",
        action="store_true",
        help="Disable MonitoringAgent — BFS without Claude guidance (no API calls during crawl)",
    )
    parser.add_argument(
        "--max-depth",
        type=int,
        default=3,
        metavar="N",
        help="BFS depth limit from seed URLs (default: 3)",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=50,
        metavar="N",
        help="Hard cap on total pages visited (default: 50)",
    )
    parser.add_argument(
        "--no-codegen",
        action="store_true",
        help="Stop after extract — skip Java generation and validation",
    )
    parser.add_argument(
        "--no-validate",
        action="store_true",
        help="Skip Maven compile validation after generation",
    )
    parser.add_argument(
        "--no-claude",
        action="store_true",
        help="Skip Claude enrichment in extract (BS4 only — faster, no API calls)",
    )
    parser.add_argument(
        "--no-headless",
        action="store_true",
        help="Run browser in headed (visible) mode",
    )
    parser.add_argument(
        "--browser",
        choices=["chromium", "firefox", "webkit"],
        default="chromium",
        help="Browser engine (default: chromium)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=30_000,
        metavar="MS",
        help="Page load timeout in milliseconds (default: 30000)",
    )
    parser.add_argument(
        "--output",
        default="crawler/output",
        metavar="DIR",
        help="Output directory for all intermediate and final files (default: crawler/output)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    main()
