#!/usr/bin/env python3
"""
AutoPilot QA — Validate Agent

Compiles generated Java artifacts via Maven (mvn test-compile) and reports
per-class pass/fail status.  Acts as the final gate in the LangGraph pipeline
before tests can be run.

The agent:
  1. Reads generate_result.json to learn which files were generated.
  2. Deletes the .class file for each generated source so Maven always
     recompiles it, even when the class cache appears up to date.
  3. Runs: mvn [clean] test-compile -B --no-transfer-progress
  4. Parses javac error lines (embedded in Maven [ERROR] output) and maps
     each one to the generated file whose absolute path appears in the line.
  5. Writes validate_result.json with per-class status and raw compiler output.

Usage (CLI):
    python crawler/agents/validate_agent.py [generate_result.json]
        [--pom PATH]        pom.xml location (default: <repo>/pom.xml)
        [--output PATH]     default: crawler/output/validate_result.json
        [--force]           run mvn clean test-compile (full recompile)

LangGraph node:
    from crawler.agents.validate_agent import validate_node
    state_update = validate_node(state)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# ANSI colour helpers (matches extract_agent.py style)
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


def _print_section(title: str) -> None:
    print()
    print(_c(title, BOLD))
    print(_hr())


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent.parent   # Kryptonite/

# Maven error pattern:  [ERROR] /abs/path/File.java:[line,col] error: message
# (Columns may be absent in some javac versions.)
_MAVEN_ERROR_RE = re.compile(
    r"\[ERROR\]\s+(?P<path>/[^\[]+\.java)"   # absolute path ending in .java
    r"(?::\[\d+,\d+\])?"                     # optional [line,col]
    r"[: ]+error:"                           # "error:"
)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class ClassValidation:
    class_name: str
    file_path:  str
    kind:       str        # "page_object" | "api_client"
    status:     str        # "ok" | "error" | "missing"
    errors:     list[str]  # compiler error lines attributed to this class

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ValidateResult:
    app_name:          str
    timestamp:         str
    tool:              str        # "maven"
    tool_version:      str
    force_clean:       bool
    classes_validated: int
    classes_passed:    int
    classes_failed:    int
    validations:       list[ClassValidation]
    raw_output:        str        # trimmed Maven compiler output
    errors:            list[str]  # agent-level errors (Maven not found, etc.)

    @property
    def passed(self) -> bool:
        return self.classes_failed == 0 and not self.errors

    def to_dict(self) -> dict:
        return {
            "validation_metadata": {
                "app_name":          self.app_name,
                "timestamp":         self.timestamp,
                "tool":              self.tool,
                "tool_version":      self.tool_version,
                "force_clean":       self.force_clean,
                "classes_validated": self.classes_validated,
                "classes_passed":    self.classes_passed,
                "classes_failed":    self.classes_failed,
                "overall_status":    "PASSED" if self.passed else "FAILED",
            },
            "validations": {
                v.class_name: v.to_dict() for v in self.validations
            },
            "raw_output": self.raw_output,
            "errors":     self.errors,
        }


# ---------------------------------------------------------------------------
# JavaValidator
# ---------------------------------------------------------------------------

class JavaValidator:

    def __init__(
        self,
        generate_data: dict,
        repo_root:   Path = REPO_ROOT,
        output_dir:  Path | None = None,
        force_clean: bool = False,
        pom_path:    Path | None = None,
    ) -> None:
        self.generate_data = generate_data
        self.repo_root   = repo_root
        self.output_dir  = output_dir or (REPO_ROOT / "crawler" / "output")
        self.force_clean = force_clean
        self.pom_path    = pom_path or (repo_root / "pom.xml")
        self._errors: list[str] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> ValidateResult:
        meta      = self.generate_data.get("generation_metadata", {})
        app_name  = meta.get("app_name", "App")
        timestamp = datetime.now(timezone.utc).isoformat()

        artifacts = self._collect_artifacts()

        # ── Find Maven ────────────────────────────────────────────────
        mvn_cmd = self._find_maven()
        if not mvn_cmd:
            msg = (
                "Maven executable ('mvn') not found.  "
                "Install Maven and ensure it is on PATH, "
                "or set MAVEN_HOME / M2_HOME."
            )
            self._errors.append(msg)
            return ValidateResult(
                app_name=app_name,
                timestamp=timestamp,
                tool="maven",
                tool_version="not found",
                force_clean=self.force_clean,
                classes_validated=0,
                classes_passed=0,
                classes_failed=0,
                validations=[],
                raw_output="",
                errors=self._errors,
            )

        tool_version = self._get_tool_version(mvn_cmd)

        # ── Guard: pom.xml must exist ─────────────────────────────────
        if not self.pom_path.exists():
            self._errors.append(f"pom.xml not found at {self.pom_path}")
            return ValidateResult(
                app_name=app_name,
                timestamp=timestamp,
                tool="maven",
                tool_version=tool_version,
                force_clean=self.force_clean,
                classes_validated=0,
                classes_passed=0,
                classes_failed=0,
                validations=[],
                raw_output="",
                errors=self._errors,
            )

        # ── Guard: generated files must exist on disk ─────────────────
        missing: list[dict] = []
        for art in artifacts:
            if not Path(art["file_path"]).exists():
                missing.append(art)
        for art in missing:
            self._errors.append(
                f"Generated file not on disk: {art['file_path']}  "
                f"(run generate_agent first)"
            )

        # ── Delete .class files for generated sources ─────────────────
        # Maven's incremental check compares source vs class file timestamps.
        # touch() is unreliable when source modification and class creation
        # both land in the same second (equal timestamps → "up to date").
        # Deleting the class file forces a recompile unconditionally.
        for art in artifacts:
            self._delete_class_file(art)

        # ── Compile ───────────────────────────────────────────────────
        print(f"  Maven   : {mvn_cmd}")
        print(f"  Version : {tool_version.splitlines()[0]}")
        print(f"  pom.xml : {self.pom_path}")
        print(f"  Force   : {'yes (clean)' if self.force_clean else 'no'}")
        print(f"  Classes : {len(artifacts)}")

        exit_code, raw_output = self._compile(mvn_cmd)
        trimmed_output = self._trim_output(raw_output)

        # ── Parse errors ──────────────────────────────────────────────
        errors_by_path: dict[str, list[str]] = {}
        if exit_code != 0:
            errors_by_path = self._parse_errors(raw_output, artifacts)

            # If build failed but no errors mapped to generated files,
            # there's a non-generated-code problem (pom/deps/base-class).
            if not any(errors_by_path.values()):
                general = [
                    re.sub(r"^\[ERROR\]\s*", "", ln).strip()
                    for ln in raw_output.splitlines()
                    if "[ERROR]" in ln and "BUILD" not in ln
                ][:10]
                self._errors.extend(
                    general or ["Compilation failed — see raw_output for details"]
                )

        # ── Build per-class result ────────────────────────────────────
        validations: list[ClassValidation] = []
        for art in artifacts:
            fp   = art["file_path"]
            errs = errors_by_path.get(fp, [])

            if not Path(fp).exists():
                status = "missing"
            elif errs or exit_code != 0 and not errors_by_path:
                # If compilation failed with general errors, mark all as failed.
                status = "error"
            else:
                status = "ok"

            validations.append(ClassValidation(
                class_name=art["class_name"],
                file_path=fp,
                kind=art["kind"],
                status=status,
                errors=errs,
            ))

        passed = sum(1 for v in validations if v.status == "ok")
        failed = sum(1 for v in validations if v.status != "ok")

        return ValidateResult(
            app_name=app_name,
            timestamp=timestamp,
            tool="maven",
            tool_version=tool_version.splitlines()[0],
            force_clean=self.force_clean,
            classes_validated=len(validations),
            classes_passed=passed,
            classes_failed=failed,
            validations=validations,
            raw_output=trimmed_output,
            errors=self._errors,
        )

    def write_output(self, result: ValidateResult) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        out_path = self.output_dir / "validate_result.json"
        out_path.write_text(
            json.dumps(result.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return out_path

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _collect_artifacts(self) -> list[dict]:
        """Flatten page_objects + api_clients from generate_result into a list."""
        artifacts: list[dict] = []
        for kind_key in ("page_objects", "api_clients"):
            for art in self.generate_data.get(kind_key, {}).values():
                artifacts.append(art)
        return artifacts

    def _find_maven(self) -> str | None:
        """Locate the mvn executable, checking PATH then common env vars."""
        cmd = shutil.which("mvn")
        if cmd:
            return cmd

        for env_var in ("MAVEN_HOME", "M2_HOME"):
            home = os.environ.get(env_var)
            if home:
                candidate = Path(home) / "bin" / "mvn"
                if candidate.exists():
                    return str(candidate)

        return None

    def _get_tool_version(self, mvn_cmd: str) -> str:
        try:
            result = subprocess.run(
                [mvn_cmd, "--version"],
                capture_output=True, text=True, timeout=15,
            )
            return (result.stdout or result.stderr).strip()
        except Exception as exc:
            return f"unknown ({exc})"

    def _compile(self, mvn_cmd: str) -> tuple[int, str]:
        """Run Maven compilation; returns (exit_code, combined_stdout+stderr)."""
        cmd: list[str] = [mvn_cmd]
        if self.force_clean:
            cmd.append("clean")
        cmd += ["test-compile", "-B", "--no-transfer-progress"]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=str(self.repo_root),
                timeout=300,
            )
            return result.returncode, result.stdout + result.stderr
        except subprocess.TimeoutExpired:
            return 1, "[ERROR] mvn test-compile timed out (300 s)"
        except Exception as exc:
            return 1, f"[ERROR] Failed to run Maven: {exc}"

    def _parse_errors(
        self,
        output: str,
        artifacts: list[dict],
    ) -> dict[str, list[str]]:
        """
        Map javac error lines from Maven output to generated file paths.

        Maven batch-mode format:
            [ERROR] /abs/path/to/File.java:[line,col] error: message
            [ERROR]   symbol:   class MissingType
            [ERROR]   location: class HomePage

        Only lines that contain an absolute path matching a generated file are
        attributed to that file.  Continuation lines (symbol/location) that
        follow immediately are grouped with the previous error.
        """
        errors_by_path: dict[str, list[str]] = {}
        path_set = {art["file_path"] for art in artifacts}

        current_path: str | None = None

        for line in output.splitlines():
            if "[ERROR]" not in line:
                current_path = None   # reset context on non-error lines
                continue

            clean = re.sub(r"^\[ERROR\]\s*", "", line).strip()
            if not clean:
                continue

            # Does this line reference one of our generated files?
            matched: str | None = None
            for fp in path_set:
                if fp in clean:
                    matched = fp
                    break

            if matched:
                current_path = matched
                errors_by_path.setdefault(current_path, []).append(clean)
            elif current_path:
                # Continuation of the previous error (symbol/location/caret)
                if clean and not clean.startswith("Failed to execute"):
                    errors_by_path[current_path].append("  " + clean)

        return errors_by_path

    @staticmethod
    def _trim_output(raw: str) -> str:
        """
        Keep only the compiler section and error/summary lines from Maven output.

        Drops the project banner and any lines that are only INFO separators,
        keeping the output compact for inclusion in validate_result.json.
        """
        keep: list[str] = []
        in_compiler = False

        for line in raw.splitlines():
            if "--- compiler:" in line or "--- resources:" in line:
                in_compiler = True
            if in_compiler:
                # Skip blank [INFO]  separator lines
                if re.fullmatch(r"\[INFO\]\s*", line):
                    continue
                # Stop at the Maven footer timestamp line
                if "Finished at:" in line:
                    break
                keep.append(line)

        return "\n".join(keep) if keep else raw

    def _delete_class_file(self, art: dict) -> None:
        """
        Delete the compiled .class file for a generated artifact.

        Maven's incremental compiler compares source vs class file mtime.
        If both timestamps fall in the same second, Maven considers the
        class "up to date" and skips recompilation — producing a false
        PASSED result even after source corruption.  Deleting the class
        file forces an unconditional recompile.

        Mapping:  package="com.autopilot.pages", class_name="HomePage"
              →   target/test-classes/com/autopilot/pages/HomePage.class
        """
        pkg_path   = art["package"].replace(".", "/")
        class_name = art["class_name"]
        class_file = (
            self.repo_root
            / "target"
            / "test-classes"
            / pkg_path
            / f"{class_name}.class"
        )
        try:
            class_file.unlink()
        except FileNotFoundError:
            pass   # not yet compiled — nothing to delete


# ---------------------------------------------------------------------------
# LangGraph node
# ---------------------------------------------------------------------------

def validate_node(state: dict) -> dict:
    """
    LangGraph node — reads generate_result.json from disk, compiles via Maven,
    writes validate_result.json, returns partial state update.

    Environment variables:
        FORCE_RECOMPILE : "true" to run mvn clean test-compile (default: false)
    """
    generate_path = REPO_ROOT / "crawler" / "output" / "generate_result.json"
    if not generate_path.exists():
        return {
            "validated_artifacts": {},
            "errors": state.get("errors", [])
            + [f"generate_result.json not found: {generate_path}"],
        }

    generate_data = json.loads(generate_path.read_text(encoding="utf-8"))
    force = os.environ.get("FORCE_RECOMPILE", "false").lower() == "true"

    validator = JavaValidator(
        generate_data=generate_data,
        force_clean=force,
    )
    result = validator.run()
    validator.write_output(result)

    # Flatten per-class errors into the pipeline error list
    compile_errors = [
        f"{v.class_name}: {err}"
        for v in result.validations
        for err in v.errors
    ]

    return {
        "validated_artifacts": {
            v.class_name: v.status for v in result.validations
        },
        "errors": state.get("errors", []) + result.errors + compile_errors,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    default_generate = REPO_ROOT / "crawler" / "output" / "generate_result.json"
    default_output   = REPO_ROOT / "crawler" / "output" / "validate_result.json"
    default_pom      = REPO_ROOT / "pom.xml"

    parser = argparse.ArgumentParser(
        description="AutoPilot QA — Java compilation validator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "generate_result",
        nargs="?",
        default=str(default_generate),
        help=f"Path to generate_result.json (default: {default_generate})",
    )
    parser.add_argument(
        "--pom",
        default=str(default_pom),
        metavar="PATH",
        help=f"Path to pom.xml (default: {default_pom})",
    )
    parser.add_argument(
        "--output",
        default=str(default_output),
        metavar="PATH",
        help=f"Output JSON path (default: {default_output})",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run 'mvn clean test-compile' (always recompiles; slower)",
    )
    return parser.parse_args()


def main() -> None:
    args         = _parse_args()
    gen_path     = Path(args.generate_result)
    output_path  = Path(args.output)
    pom_path     = Path(args.pom)

    # ── Header ────────────────────────────────────────────────────────
    print()
    print(_c("AutoPilot QA — Validate Agent", BOLD, CYAN))
    print(_hr())
    print(f"  Generate result : {gen_path}")
    print(f"  pom.xml         : {pom_path}")
    print(f"  Force clean     : {args.force}")
    print(f"  Output          : {output_path}")

    # ── Load generate_result.json ─────────────────────────────────────
    if not gen_path.exists():
        print(_c(f"\n✗  generate_result.json not found: {gen_path}", RED), file=sys.stderr)
        sys.exit(1)

    try:
        generate_data = json.loads(gen_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(_c(f"\n✗  Failed to parse generate_result.json: {exc}", RED), file=sys.stderr)
        sys.exit(1)

    if not generate_data.get("page_objects") and not generate_data.get("api_clients"):
        print(_c("\n✗  No generated artifacts found in generate_result.json", RED), file=sys.stderr)
        sys.exit(1)

    # ── Validate ──────────────────────────────────────────────────────
    _print_section("Compiling Generated Artifacts")

    validator = JavaValidator(
        generate_data=generate_data,
        repo_root=pom_path.parent,
        output_dir=output_path.parent,
        force_clean=args.force,
        pom_path=pom_path,
    )
    result   = validator.run()
    out_path = validator.write_output(result)

    # ── Per-class results ─────────────────────────────────────────────
    print()
    for v in result.validations:
        icon = _c("✓", GREEN) if v.status == "ok" else _c("✗", RED)
        label = _c(v.status, GREEN if v.status == "ok" else RED)
        print(f"  {icon} {v.class_name:40s} {label}")
        for err in v.errors[:5]:          # cap inline display at 5 lines
            print(f"       {_c(err, YELLOW)}")
        if len(v.errors) > 5:
            print(f"       {_c(f'… {len(v.errors) - 5} more error(s)', DIM)}")

    # ── Summary ───────────────────────────────────────────────────────
    _print_section("Summary")

    if result.passed:
        print(_c(
            f"  ✓ PASSED  ({result.classes_passed}/{result.classes_validated} class(es) compiled OK)",
            GREEN, BOLD,
        ))
    else:
        print(_c(
            f"  ✗ FAILED  ({result.classes_failed} class(es) with errors, "
            f"{result.classes_passed} OK)",
            RED, BOLD,
        ))

    print(f"     App           : {result.app_name}")
    print(f"     Tool          : {result.tool_version}")
    print(f"     Validated     : {result.classes_validated}")
    print(f"     Passed        : {result.classes_passed}")
    print(f"     Failed        : {result.classes_failed}")

    if result.errors:
        print(_c(f"     Agent errors  : {len(result.errors)}", YELLOW))
        for err in result.errors:
            print(_c(f"       ⚠  {err}", YELLOW))

    print(f"     Output        : {out_path}")
    print()

    sys.exit(0 if result.passed else 1)


if __name__ == "__main__":
    main()
