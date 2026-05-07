#!/usr/bin/env python3
"""
AutoPilot QA — Knowledge File Validator
Validates an app-knowledge.yaml file against its JSON Schema and
runs semantic checks that go beyond what the schema can express.

Usage:
    python crawler/validate_knowledge.py <knowledge-file> [--schema <schema-path>]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

import yaml
from jsonschema import Draft7Validator

# ---------------------------------------------------------------------------
# ANSI colour helpers
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_http_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

def validate_schema(data: dict, schema: dict) -> list[str]:
    """Return a list of human-readable schema error messages."""
    validator = Draft7Validator(schema)
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path))
    messages = []
    for err in errors:
        path = " → ".join(str(p) for p in err.absolute_path) or "(root)"
        messages.append(f"{path}: {err.message}")
    return messages


# ---------------------------------------------------------------------------
# Semantic checks
# ---------------------------------------------------------------------------

class Issue:
    def __init__(self, severity: str, message: str) -> None:
        # severity: "error" | "warning"
        self.severity = severity
        self.message = message

    @property
    def is_error(self) -> bool:
        return self.severity == "error"

    def __str__(self) -> str:
        if self.is_error:
            return _c(f"  ✗  {self.message}", RED)
        return _c(f"  ⚠  {self.message}", YELLOW)


def semantic_checks(data: dict) -> list[Issue]:
    issues: list[Issue] = []

    # -- application.base_url --------------------------------------------------
    app = data.get("application", {})
    base_url = app.get("base_url", "")
    if base_url and not _is_http_url(str(base_url)):
        issues.append(Issue("error",
            f"[application] base_url '{base_url}' is not a valid http/https URL"))

    # -- auth: login_url required when type != none ---------------------------
    auth = data.get("authentication", {})
    auth_type = auth.get("type", "none")
    if auth_type != "none" and not auth.get("login_url"):
        issues.append(Issue("warning",
            f"[authentication] type is '{auth_type}' but login_url is missing"))

    # -- journeys: at least one smoke -----------------------------------------
    journeys = data.get("journeys", []) or []
    if journeys:
        smoke_journeys = [j for j in journeys if j.get("priority") == "smoke"]
        if not smoke_journeys:
            issues.append(Issue("warning",
                "[journeys] No journey with priority 'smoke' found"))

        # -- each journey should have >= 2 steps --------------------------------
        for j in journeys:
            steps = j.get("steps", []) or []
            name = j.get("name", "<unnamed>")
            if len(steps) < 2:
                issues.append(Issue("warning",
                    f"[journeys] '{name}' has fewer than 2 steps ({len(steps)})"))

    # -- api_endpoints paths start with / ------------------------------------
    for ep in data.get("api_endpoints", []) or []:
        path = ep.get("path", "")
        if path and not str(path).startswith("/"):
            issues.append(Issue("error",
                f"[api_endpoints] path '{path}' does not start with '/'"))

    # -- routes paths start with / -------------------------------------------
    for route in data.get("routes", []) or []:
        path = route.get("path", "")
        if path and not str(path).startswith("/"):
            issues.append(Issue("error",
                f"[routes] path '{path}' does not start with '/'"))

    # -- test_data.environments base_url valid --------------------------------
    test_data = data.get("test_data", {}) or {}
    for env in test_data.get("environments", []) or []:
        env_url = env.get("base_url", "")
        env_name = env.get("name", "<unnamed>")
        if env_url and not _is_http_url(str(env_url)):
            issues.append(Issue("error",
                f"[test_data.environments] '{env_name}' base_url '{env_url}' "
                f"is not a valid http/https URL"))

    # -- domain.entities key_fields non-empty --------------------------------
    domain = data.get("domain", {}) or {}
    for entity in domain.get("entities", []) or []:
        kf = entity.get("key_fields", []) or []
        name = entity.get("name", "<unnamed>")
        if len(kf) == 0:
            issues.append(Issue("error",
                f"[domain.entities] '{name}' has an empty key_fields list"))

    return issues


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _print_section(title: str) -> None:
    print()
    print(_c(title, BOLD))
    print(_hr())


def _print_schema_results(errors: list[str]) -> None:
    _print_section("Schema Validation")
    if not errors:
        print(_c("  ✓  All required fields present and correctly typed", GREEN))
    else:
        for msg in errors:
            print(_c(f"  ✗  {msg}", RED))


def _print_semantic_results(issues: list[Issue]) -> None:
    _print_section("Semantic Checks")
    if not issues:
        print(_c("  ✓  All semantic checks passed", GREEN))
    else:
        for issue in issues:
            print(issue)


def _print_summary(data: dict, schema_errors: list[str],
                   semantic_issues: list[Issue],
                   knowledge_path: str, schema_path: str) -> None:
    _print_section("Summary")

    error_count   = len(schema_errors) + sum(1 for i in semantic_issues if i.is_error)
    warning_count = sum(1 for i in semantic_issues if not i.is_error)

    app = data.get("application", {}) if isinstance(data, dict) else {}
    domain   = data.get("domain", {}) or {}
    routes   = data.get("routes", []) or []
    apis     = data.get("api_endpoints", []) or []
    journeys = data.get("journeys", []) or []

    if error_count == 0:
        print(_c("  ✓ PASSED", GREEN, BOLD))
    else:
        print(_c(f"  ✗ FAILED  ({error_count} error(s))", RED, BOLD))

    print(f"     Application  : {app.get('name', '—')}")
    print(f"     Base URL     : {app.get('base_url', '—')}")
    print(f"     Entities     : {len(domain.get('entities', []) or [])}")
    print(f"     Routes       : {len(routes)}")
    print(f"     API endpoints: {len(apis)}")
    print(f"     Journeys     : {len(journeys)}")
    if warning_count:
        print(_c(f"     Warnings     : {warning_count} (see above)", YELLOW))
    else:
        print(f"     Warnings     : 0")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    repo_root = Path(__file__).parent.parent
    default_schema = repo_root / "knowledge" / "schema" / "app-knowledge.schema.json"

    parser = argparse.ArgumentParser(
        description="AutoPilot QA — validate an app-knowledge.yaml file",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("knowledge_file", help="Path to the app-knowledge.yaml file")
    parser.add_argument(
        "--schema",
        default=str(default_schema),
        metavar="SCHEMA_PATH",
        help=f"Path to the JSON Schema (default: {default_schema})",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    knowledge_path = Path(args.knowledge_file)
    schema_path    = Path(args.schema)

    # Header -----------------------------------------------------------------
    print()
    print(_c("AutoPilot QA — Knowledge File Validator", BOLD, CYAN))
    print(_hr())
    print(f"  File  : {knowledge_path}")
    print(f"  Schema: {schema_path}")

    # 1. File existence checks ------------------------------------------------
    if not knowledge_path.exists():
        print(_c(f"\n✗  Knowledge file not found: {knowledge_path}", RED), file=sys.stderr)
        sys.exit(1)

    if not schema_path.exists():
        print(_c(f"\n✗  Schema file not found: {schema_path}", RED), file=sys.stderr)
        sys.exit(1)

    # 2. Parse YAML -----------------------------------------------------------
    try:
        raw_yaml = knowledge_path.read_text(encoding="utf-8")
        data = yaml.safe_load(raw_yaml)
    except yaml.YAMLError as exc:
        print(_c(f"\n✗  YAML parse error in {knowledge_path}:", RED), file=sys.stderr)
        print(f"   {exc}", file=sys.stderr)
        sys.exit(1)

    # 3. Top-level must be a dict --------------------------------------------
    if not isinstance(data, dict):
        print(_c(
            f"\n✗  Expected a YAML mapping at the top level, got {type(data).__name__}",
            RED,
        ), file=sys.stderr)
        sys.exit(1)

    # 4. Parse JSON Schema ---------------------------------------------------
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(_c(f"\n✗  JSON Schema parse error in {schema_path}:", RED), file=sys.stderr)
        print(f"   {exc}", file=sys.stderr)
        sys.exit(1)

    # 5. JSON Schema validation ----------------------------------------------
    schema_errors = validate_schema(data, schema)
    _print_schema_results(schema_errors)

    # 6. Semantic checks ------------------------------------------------------
    semantic_issues = semantic_checks(data)
    _print_semantic_results(semantic_issues)

    # 7. Summary --------------------------------------------------------------
    _print_summary(data, schema_errors, semantic_issues,
                   str(knowledge_path), str(schema_path))

    # 8. Exit code ------------------------------------------------------------
    error_count = len(schema_errors) + sum(1 for i in semantic_issues if i.is_error)
    sys.exit(0 if error_count == 0 else 1)


if __name__ == "__main__":
    main()
