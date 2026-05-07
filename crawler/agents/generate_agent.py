#!/usr/bin/env python3
"""
AutoPilot QA — Generate Agent

Pure-Python Java code generator (stdlib only).  Consumes extract_result.json
and emits compilable Java source files:
  - One Page Object class per crawled page  (Playwright Java, fluent API)
  - One RestAssured API client class covering all API endpoints

No Claude API calls — all semantic work was done in extract_agent.

Usage (CLI):
    python crawler/agents/generate_agent.py <extract_result.json>
        [--knowledge PATH]   default: knowledge/app-knowledge.yaml
        [--package PKG]      default: com.autopilot
        [--java-out PATH]    default: src/test/java
        [--output PATH]      default: crawler/output/generate_result.json

LangGraph node:
    from crawler.agents.generate_agent import generate_node
    state_update = generate_node(state)
"""
from __future__ import annotations

import argparse
import json
import os
import re
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

REPO_ROOT       = Path(__file__).parent.parent.parent   # Kryptonite/
DEFAULT_JAVA_OUT = REPO_ROOT / "src" / "test" / "java"


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class GeneratedArtifact:
    class_name: str
    package:    str
    file_path:  str
    source:     str
    kind:       str        # "page_object" | "api_client"
    source_url: str        # crawled URL; "" for api_client

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class GenerateResult:
    app_name:     str
    timestamp:    str
    package:      str
    page_objects: list     # list[GeneratedArtifact]
    api_clients:  list     # list[GeneratedArtifact]
    errors:       list

    def to_dict(self) -> dict:
        return {
            "generation_metadata": {
                "app_name":               self.app_name,
                "timestamp":              self.timestamp,
                "package":                self.package,
                "page_objects_generated": len(self.page_objects),
                "api_clients_generated":  len(self.api_clients),
            },
            "page_objects": {
                a.class_name: a.to_dict() for a in self.page_objects
            },
            "api_clients": {
                a.class_name: a.to_dict() for a in self.api_clients
            },
            "errors": self.errors,
        }


# ---------------------------------------------------------------------------
# JavaCodeGenerator
# ---------------------------------------------------------------------------

class JavaCodeGenerator:

    # Prefix map: element_type → Java method prefix
    _PREFIX_MAP: dict[str, str] = {
        "link":           "click",
        "button":         "click",
        "submit_button":  "click",
        "checkbox":       "click",
        "radio":          "click",
        "text_input":     "fill",
        "textarea":       "fill",
        "email_input":    "fill",
        "password_input": "fill",
        "search_input":   "fill",
        "number_input":   "fill",
        "tel_input":      "fill",
        "url_input":      "fill",
        "date_input":     "fill",
        "file_input":     "fill",
        "select":         "select",
        "heading":        "assert",
        "element":        "assert",
    }

    def __init__(
        self,
        app_knowledge: dict,
        package: str = "com.autopilot",
        java_out: Path | None = None,
        output_dir: Path | None = None,
    ) -> None:
        self.app_knowledge = app_knowledge
        self.package       = package
        self.java_out      = java_out or DEFAULT_JAVA_OUT
        self.output_dir    = output_dir or (REPO_ROOT / "crawler" / "output")
        self._errors: list[str] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self, extract_data: dict) -> GenerateResult:
        meta          = extract_data.get("extraction_metadata", {})
        app_name      = meta.get("app_name", "App")
        timestamp     = datetime.now(timezone.utc).isoformat()
        pages         = extract_data.get("pages", {})
        api_endpoints = extract_data.get("api_endpoints", [])

        page_objects: list[GeneratedArtifact] = []
        api_clients:  list[GeneratedArtifact] = []

        # ── Page Objects ──────────────────────────────────────────────
        for url, page_data in pages.items():
            try:
                class_name, source = self._generate_page_object(url, page_data)
                pkg       = f"{self.package}.pages"
                file_path = self._write_java_file(self.java_out, pkg, class_name, source)
                page_objects.append(GeneratedArtifact(
                    class_name=class_name,
                    package=pkg,
                    file_path=str(file_path),
                    source=source,
                    kind="page_object",
                    source_url=url,
                ))
                print(f"  {_c('✓', GREEN)} {class_name:40s} → {file_path}")
            except Exception as exc:
                msg = f"Failed to generate page object for {url}: {exc}"
                print(_c(f"  ✗  {msg}", RED))
                self._errors.append(msg)

        # ── API Client ────────────────────────────────────────────────
        if api_endpoints:
            try:
                class_name, source = self._generate_api_client(api_endpoints)
                pkg       = f"{self.package}.api"
                file_path = self._write_java_file(self.java_out, pkg, class_name, source)
                api_clients.append(GeneratedArtifact(
                    class_name=class_name,
                    package=pkg,
                    file_path=str(file_path),
                    source=source,
                    kind="api_client",
                    source_url="",
                ))
                print(f"  {_c('✓', GREEN)} {class_name:40s} → {file_path}")
            except Exception as exc:
                msg = f"Failed to generate API client: {exc}"
                print(_c(f"  ✗  {msg}", RED))
                self._errors.append(msg)

        return GenerateResult(
            app_name=app_name,
            timestamp=timestamp,
            package=self.package,
            page_objects=page_objects,
            api_clients=api_clients,
            errors=self._errors,
        )

    def write_output(self, result: GenerateResult) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        out_path = self.output_dir / "generate_result.json"
        out_path.write_text(
            json.dumps(result.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return out_path

    # ------------------------------------------------------------------
    # Helpers — naming
    # ------------------------------------------------------------------

    @staticmethod
    def _to_pascal_case(text: str) -> str:
        """Space-separated words → PascalCase (first letter upper, rest preserved)."""
        words = text.split()
        return "".join(w[0].upper() + w[1:] for w in words if w)

    def _class_name_for_page(self, route_title: str) -> str:
        return self._to_pascal_case(route_title) + "Page"

    def _api_client_name(self) -> str:
        name = self.app_knowledge.get("application", {}).get("name", "App")
        # Strip trailing " App" suffix before building name
        if name.endswith(" App"):
            name = name[:-4]
        return self._to_pascal_case(name) + "ApiClient"

    @staticmethod
    def _escape_selector(sel: str) -> str:
        """Escape double quotes for embedding a selector in a Java string literal."""
        return sel.replace("\\", "\\\\").replace('"', '\\"')

    @staticmethod
    def _escape_text(text: str) -> str:
        """Escape text for embedding in a Java string literal."""
        return text.replace("\\", "\\\\").replace('"', '\\"')

    @staticmethod
    def _extract_path_params(path: str) -> list[str]:
        return re.findall(r"\{(\w+)\}", path)

    @staticmethod
    def _build_path_expression(path: str) -> str:
        """
        Convert a path template to a Java string concatenation expression.

        /api/suggest              → "/api/suggest"
        /api/lists/{id}/items     → "/api/lists/" + id + "/items"
        /api/shared/{shareId}     → "/api/shared/" + shareId
        """
        parts = re.split(r"\{(\w+)\}", path)
        result: list[str] = []
        for i, part in enumerate(parts):
            if i % 2 == 0:       # literal segment
                if part:
                    result.append(f'"{part}"')
            else:                # path-param name
                result.append(part)
        return " + ".join(result)

    # ------------------------------------------------------------------
    # Helpers — method name derivation
    # ------------------------------------------------------------------

    def _derive_method_name(self, el: dict) -> str | None:
        """
        Derive a Java camelCase method name from element attributes when
        page_object_method is None (BS4-only extraction mode).

        Returns None when the element should be skipped.
        """
        element_type = el.get("element_type", "")
        text         = (el.get("text")        or "").strip()
        aria_label   = (el.get("aria_label")  or "").strip()
        placeholder  = (el.get("placeholder") or "").strip()
        href         = (el.get("href")        or "").strip()
        tag          = (el.get("tag")         or "").strip()

        # Rule 1: always skip form containers
        if element_type == "form":
            return None

        # Rule 2: skip generic elements with no identifying information
        if element_type == "element" and not aria_label and not text:
            return None

        prefix = self._PREFIX_MAP.get(element_type, "assert")

        # Find the first candidate string that yields at least one word
        # after stripping non-word/non-space characters.
        for candidate in (text, aria_label, placeholder, href, tag):
            if not candidate:
                continue
            sanitized = re.sub(r"[^\w\s]", " ", candidate)
            words = sanitized.split()
            if words:
                # Take at most 4 words; preserve inner capitalisation
                pascal = "".join(w[0].upper() + w[1:] for w in words[:4])
                return prefix + pascal

        return None

    # ------------------------------------------------------------------
    # Helpers — method dispatch
    # ------------------------------------------------------------------

    def _dispatch_method(
        self,
        el: dict,
        seen: set[str],
        class_name: str,
    ) -> str | None:
        """
        Build the complete Java method string for an element, or return None
        to skip the element.

        Updates *seen* in-place with the chosen method name (dedup).
        """
        element_type    = el.get("element_type", "")
        page_method     = el.get("page_object_method")
        is_disabled     = el.get("is_disabled", False)
        text            = (el.get("text") or "").strip()
        selector        = el.get("selector") or ""
        business_action = (el.get("business_action") or "").strip()

        # Always skip form containers
        if element_type == "form":
            return None

        # Resolve method name: prefer Claude annotation, fall back to derived
        method_name = page_method or self._derive_method_name(el)
        if not method_name:
            return None

        # Deduplicate within this page object
        base = method_name
        n = 2
        while method_name in seen:
            method_name = base + str(n)
            n += 1
        seen.add(method_name)

        esc_sel = self._escape_selector(selector)

        # Dispatch on method-name prefix (prefix takes priority over element_type)
        if method_name.startswith("fill"):
            body   = f'page.fill("{esc_sel}", value)'
            params = "String value"

        elif method_name.startswith("click"):
            body   = f'page.click("{esc_sel}")'
            params = ""

        elif method_name.startswith("select"):
            body   = f'page.selectOption("{esc_sel}", value)'
            params = "String value"

        else:
            # assert* prefix or unrecognised — treat as assertion
            params = ""
            if is_disabled:
                body = f'assertThat(page.locator("{esc_sel}")).isDisabled()'
            elif element_type == "heading" and text and len(text) <= 60:
                esc_text = self._escape_text(text)
                body = f'assertThat(page.locator("{esc_sel}")).containsText("{esc_text}")'
            else:
                body = f'assertThat(page.locator("{esc_sel}")).isVisible()'

        comment = business_action or (
            f"{element_type}: {text[:60]}" if text else element_type
        )

        lines = [
            f"    /** {comment} */",
            f"    public {class_name} {method_name}({params}) {{",
            f"        {body};",
            f"        return this;",
            f"    }}",
        ]
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Page Object generation
    # ------------------------------------------------------------------

    def _generate_page_object(self, url: str, page_data: dict) -> tuple[str, str]:
        route_title = page_data.get("route_title", url)
        route_path  = page_data.get("route_path", "/")
        elements    = page_data.get("elements", [])

        class_name = self._class_name_for_page(route_title)
        pages_pkg  = f"{self.package}.pages"

        # ── Build method blocks ───────────────────────────────────────
        seen_methods: set[str] = set()
        body_lines:   list[str] = []
        last_type:    str | None = None

        for el in elements:
            element_type = el.get("element_type", "")
            method_str   = self._dispatch_method(el, seen_methods, class_name)
            if method_str is None:
                continue

            if element_type != last_type:
                # New type group — blank line then group separator comment
                body_lines.append("")
                body_lines.append(f"    // --- {element_type} ---")
                last_type = element_type
            else:
                # Same type — blank line between methods
                body_lines.append("")

            body_lines.extend(method_str.split("\n"))

        # ── Collect unique journey names ──────────────────────────────
        seen_journeys: set[str] = set()
        journeys: list[str] = []
        for el in elements:
            for j in el.get("journey_relevance", []) or []:
                if j and j not in seen_journeys:
                    journeys.append(j)
                    seen_journeys.add(j)
        journeys_str = ", ".join(journeys) if journeys else "General"

        methods_body = "\n".join(body_lines)

        # ── Assemble source ───────────────────────────────────────────
        source = (
            f"package {pages_pkg};\n"
            f"\n"
            f"import com.autopilot.base.BasePage;\n"
            f"import com.microsoft.playwright.Page;\n"
            f"import static com.microsoft.playwright.assertions.PlaywrightAssertions.assertThat;\n"
            f"\n"
            f"/**\n"
            f" * Page Object for {route_title} ({route_path})\n"
            f" * Journeys: {journeys_str}\n"
            f" * Generated by AutoPilot QA \u2014 generate_agent\n"
            f" */\n"
            f"public class {class_name} extends BasePage {{\n"
            f"\n"
            f"    public {class_name}(Page page) {{\n"
            f"        super(page);\n"
            f"    }}\n"
            f"\n"
            f"    public {class_name} navigate() {{\n"
            f"        page.navigate(\"{route_path}\");\n"
            f"        return this;\n"
            f"    }}\n"
            f"{methods_body}\n"
            f"}}\n"
        )

        return class_name, source

    # ------------------------------------------------------------------
    # API Client generation
    # ------------------------------------------------------------------

    def _generate_api_client(self, endpoints: list) -> tuple[str, str]:
        app_name   = self.app_knowledge.get("application", {}).get("name", "App")
        class_name = self._api_client_name()
        api_pkg    = f"{self.package}.api"

        has_any_body = any(
            isinstance(ep.get("request_schema"), dict) and ep["request_schema"]
            for ep in endpoints
        )

        method_strs: list[str] = []

        for ep in endpoints:
            method      = (ep.get("method") or "GET").upper()
            path        = ep.get("path", "/")
            description = ep.get("description", "")
            req_schema  = ep.get("request_schema")    # dict | None
            method_name = ep.get("restassured_method_name", "")

            path_params = self._extract_path_params(path)
            body_params = list(req_schema.keys()) if isinstance(req_schema, dict) else []
            has_body    = bool(body_params)
            path_expr   = self._build_path_expression(path)

            # Parameter list: path params first, then body params (all String)
            all_params = [f"String {p}" for p in path_params]
            all_params += [f"String {p}" for p in body_params]
            params_str = ", ".join(all_params)

            # Build method body
            indent = "        "
            body_lines: list[str] = []

            if has_body:
                body_lines.append(f"{indent}Map<String, Object> body = new HashMap<>();")
                for key in body_params:
                    body_lines.append(f'{indent}body.put("{key}", {key});')

            body_lines.append(f"{indent}return given()")
            body_lines.append(f"{indent}        .baseUri(baseUrl)")
            if has_body:
                body_lines.append(f"{indent}        .contentType(ContentType.JSON)")
                body_lines.append(f"{indent}        .body(body)")
            body_lines.append(f"{indent}        .when()")
            body_lines.append(f"{indent}        .{method.lower()}({path_expr})")
            body_lines.append(f"{indent}        .then()")
            body_lines.append(f"{indent}        .extract().response();")

            method_body = "\n".join(body_lines)

            m = (
                f"    /** {method} {path} \u2014 {description} */\n"
                f"    public Response {method_name}({params_str}) {{\n"
                f"{method_body}\n"
                f"    }}"
            )
            method_strs.append(m)

        methods_body = "\n\n".join(method_strs)

        # Conditional imports
        import_lines = ["import io.restassured.response.Response;"]
        if has_any_body:
            import_lines.append("import io.restassured.http.ContentType;")
            import_lines.append("import java.util.HashMap;")
            import_lines.append("import java.util.Map;")
        import_lines.append("import static io.restassured.RestAssured.given;")
        imports_str = "\n".join(import_lines)

        source = (
            f"package {api_pkg};\n"
            f"\n"
            f"{imports_str}\n"
            f"\n"
            f"/**\n"
            f" * RestAssured API client for {app_name}\n"
            f" * Generated by AutoPilot QA \u2014 generate_agent\n"
            f" */\n"
            f"public class {class_name} {{\n"
            f"\n"
            f"    private final String baseUrl;\n"
            f"\n"
            f"    public {class_name}(String baseUrl) {{\n"
            f"        this.baseUrl = baseUrl;\n"
            f"    }}\n"
            f"\n"
            f"{methods_body}\n"
            f"}}\n"
        )

        return class_name, source

    # ------------------------------------------------------------------
    # File I/O
    # ------------------------------------------------------------------

    def _write_java_file(
        self,
        java_out: Path,
        pkg: str,
        cls: str,
        src: str,
    ) -> Path:
        """Create package directory and write <ClassName>.java; returns the Path."""
        pkg_dir = java_out.joinpath(*pkg.split("."))
        pkg_dir.mkdir(parents=True, exist_ok=True)
        file_path = pkg_dir / f"{cls}.java"
        file_path.write_text(src, encoding="utf-8")
        return file_path


# ---------------------------------------------------------------------------
# LangGraph node
# ---------------------------------------------------------------------------

def generate_node(state: dict) -> dict:
    """
    LangGraph node — reads extract_result.json from disk, generates Java
    artifacts, writes generate_result.json, returns partial state update.

    Reads extract_result.json from disk rather than CrawlState["extracted_elements"]
    because the state field lacks route_title and api_endpoints (by design).

    Environment variables:
        JAVA_PACKAGE : Java base package (default: com.autopilot)
        JAVA_OUT     : Root of Java source tree  (default: <repo>/src/test/java)
    """
    extract_path = REPO_ROOT / "crawler" / "output" / "extract_result.json"
    extract_data = json.loads(extract_path.read_text(encoding="utf-8"))

    generator = JavaCodeGenerator(
        app_knowledge=state["app_knowledge"],
        package=os.environ.get("JAVA_PACKAGE", "com.autopilot"),
        java_out=Path(os.environ.get("JAVA_OUT", str(DEFAULT_JAVA_OUT))),
    )
    result = generator.run(extract_data)
    generator.write_output(result)

    return {
        "generated_page_objects": {a.class_name: a.source for a in result.page_objects},
        "generated_api_clients":  {a.class_name: a.source for a in result.api_clients},
        "errors": state.get("errors", []) + result.errors,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    default_extract   = REPO_ROOT / "crawler" / "output" / "extract_result.json"
    default_knowledge = REPO_ROOT / "knowledge" / "app-knowledge.yaml"
    default_output    = REPO_ROOT / "crawler" / "output" / "generate_result.json"
    default_java_out  = REPO_ROOT / "src" / "test" / "java"

    parser = argparse.ArgumentParser(
        description="AutoPilot QA — Java code generator (Page Objects + API client)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "extract_result",
        nargs="?",
        default=str(default_extract),
        help=f"Path to extract_result.json (default: {default_extract})",
    )
    parser.add_argument(
        "--knowledge",
        default=str(default_knowledge),
        metavar="PATH",
        help=f"Path to app-knowledge.yaml (default: {default_knowledge})",
    )
    parser.add_argument(
        "--package",
        default="com.autopilot",
        metavar="PKG",
        help="Java base package (default: com.autopilot)",
    )
    parser.add_argument(
        "--java-out",
        default=str(default_java_out),
        metavar="PATH",
        help=f"Root of Java source tree (default: {default_java_out})",
    )
    parser.add_argument(
        "--output",
        default=str(default_output),
        metavar="PATH",
        help=f"generate_result.json output path (default: {default_output})",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    extract_path  = Path(args.extract_result)
    knowledge_path = Path(args.knowledge)
    output_path   = Path(args.output)
    java_out      = Path(args.java_out)

    # ── Header ────────────────────────────────────────────────────────
    print()
    print(_c("AutoPilot QA — Generate Agent", BOLD, CYAN))
    print(_hr())
    print(f"  Extract result : {extract_path}")
    print(f"  Knowledge      : {knowledge_path}")
    print(f"  Package        : {args.package}")
    print(f"  Java output    : {java_out}")
    print(f"  JSON output    : {output_path}")

    # ── Validate inputs ───────────────────────────────────────────────
    if not extract_path.exists():
        print(_c(f"\n✗  Extract result not found: {extract_path}", RED), file=sys.stderr)
        sys.exit(1)

    try:
        extract_data = json.loads(extract_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(_c(f"\n✗  Failed to parse extract result: {exc}", RED), file=sys.stderr)
        sys.exit(1)

    # ── Load knowledge (optional — only needed for API client name) ───
    app_knowledge: dict = {}
    if knowledge_path.exists():
        try:
            import yaml
            app_knowledge = yaml.safe_load(knowledge_path.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            print(_c(f"  ⚠  Could not load knowledge file: {exc}", YELLOW))
    else:
        # Fall back to app name from extract metadata
        app_name = extract_data.get("extraction_metadata", {}).get("app_name", "App")
        app_knowledge = {"application": {"name": app_name}}
        print(_c(f"  ⚠  Knowledge file not found — using app name from extract metadata", YELLOW))

    pages = extract_data.get("pages", {})
    if not pages:
        print(
            _c("\n✗  No pages found in extract result", RED),
            file=sys.stderr,
        )
        sys.exit(1)

    # ── Generate ──────────────────────────────────────────────────────
    _print_section("Generating Java Artifacts")
    print(f"  Pages         : {len(pages)}")
    print(f"  API endpoints : {len(extract_data.get('api_endpoints', []))}")
    print()

    generator = JavaCodeGenerator(
        app_knowledge=app_knowledge,
        package=args.package,
        java_out=java_out,
        output_dir=output_path.parent,
    )
    result   = generator.run(extract_data)
    out_path = generator.write_output(result)

    # ── Summary ───────────────────────────────────────────────────────
    _print_section("Summary")
    total = len(result.page_objects) + len(result.api_clients)

    if total > 0:
        print(_c(
            f"  ✓ PASSED  ({len(result.page_objects)} page object(s), "
            f"{len(result.api_clients)} API client(s))",
            GREEN, BOLD,
        ))
    else:
        print(_c("  ✗ FAILED  (0 classes generated)", RED, BOLD))

    print(f"     App               : {result.app_name}")
    print(f"     Package           : {result.package}")
    print(f"     Page objects      : {len(result.page_objects)}")
    for a in result.page_objects:
        print(f"       {a.class_name:35s} {a.file_path}")
    print(f"     API clients       : {len(result.api_clients)}")
    for a in result.api_clients:
        print(f"       {a.class_name:35s} {a.file_path}")

    if result.errors:
        print(_c(f"     Errors            : {len(result.errors)}", YELLOW))
        for err in result.errors:
            print(_c(f"       ⚠  {err}", YELLOW))

    print(f"     JSON output       : {out_path}")
    print()

    sys.exit(0 if total > 0 else 1)


if __name__ == "__main__":
    main()
