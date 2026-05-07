#!/usr/bin/env python3
"""
AutoPilot QA — Extract Agent

Two-pass semantic extraction:
  Pass 1 — BS4 structural extraction (tags, selectors, XPath)
  Pass 2 — Claude enrichment (business_action, page_object_method, journey_relevance)

Usage (CLI):
    python crawler/agents/extract_agent.py <crawl_result.json>
        [--knowledge PATH]    default: knowledge/app-knowledge.yaml
        [--output PATH]       default: crawler/output/extract_result.json
        [--model STRING]      default: claude-sonnet-4-6
        [--no-claude]         skip Claude enrichment (BS4 only)

LangGraph node:
    from crawler.agents.extract_agent import extract_node
    state_update = extract_node(state)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

# ---------------------------------------------------------------------------
# ANSI colour helpers (matches crawl_agent.py style)
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

CAPTURE_TAGS = frozenset({"a", "button", "input", "textarea", "select", "form", "h1", "h2", "h3"})

# Tool schema for Claude structured output
EXTRACT_ELEMENTS_TOOL = {
    "name": "extract_elements",
    "description": (
        "Given a list of UI elements from a web page, return semantic annotations "
        "for each element: business action, Page Object method name (Java camelCase), "
        "and the journey names the element participates in."
    ),
    "input_schema": {
        "type": "object",
        "required": ["elements"],
        "properties": {
            "elements": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": [
                        "element_index",
                        "business_action",
                        "page_object_method",
                        "journey_relevance",
                    ],
                    "properties": {
                        "element_index": {
                            "type": "integer",
                            "description": "0-based index of the element in the input list",
                        },
                        "business_action": {
                            "type": "string",
                            "description": (
                                "What the user is doing with this element "
                                "(business language, not technical)"
                            ),
                        },
                        "page_object_method": {
                            "type": "string",
                            "description": (
                                "Java camelCase method name for the Page Object. "
                                "Use prefixes: click*/fill*/select*/assert*. "
                                "Business language. "
                                "e.g. clickWishingCard, fillOccasionField, assertListTitleVisible"
                            ),
                        },
                        "journey_relevance": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Names of journeys from app-knowledge.yaml "
                                "this element participates in"
                            ),
                        },
                    },
                },
            }
        },
    },
}


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class ElementDescriptor:
    # BS4 fields — always populated
    tag:           str
    element_type:  str
    selector:      str
    xpath:         str
    text:          str
    aria_label:    str | None
    href:          str | None
    input_type:    str | None
    placeholder:   str | None
    name_attr:     str | None
    is_disabled:   bool

    # Claude fields — None until enriched; stay None on --no-claude or API failure
    business_action:    str | None = None
    page_object_method: str | None = None
    journey_relevance:  list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ApiEndpointDescriptor:
    method:                  str
    path:                    str
    description:             str
    request_schema:          dict | None
    response_schema:         str | None
    restassured_method_name: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ExtractResult:
    app_name:      str
    timestamp:     str
    model:         str
    use_claude:    bool
    pages:         dict   # url -> {"url","route_path","route_title","elements":[...]}
    api_endpoints: list   # list[ApiEndpointDescriptor]
    errors:        list[str]

    def to_dict(self) -> dict:
        total_elements = sum(len(p["elements"]) for p in self.pages.values())
        return {
            "extraction_metadata": {
                "app_name":               self.app_name,
                "timestamp":              self.timestamp,
                "model":                  self.model,
                "claude_enriched":        self.use_claude,
                "pages_processed":        len(self.pages),
                "total_elements":         total_elements,
                "api_endpoints_extracted": len(self.api_endpoints),
            },
            "pages": self.pages,
            "api_endpoints": [
                e.to_dict() if isinstance(e, ApiEndpointDescriptor) else e
                for e in self.api_endpoints
            ],
            "errors": self.errors,
        }


# ---------------------------------------------------------------------------
# ElementExtractor
# ---------------------------------------------------------------------------

class ElementExtractor:

    def __init__(
        self,
        app_knowledge: dict,
        model: str = "claude-sonnet-4-6",
        use_claude: bool = True,
        output_dir: Path | None = None,
    ) -> None:
        self.app_knowledge = app_knowledge
        self.model = model
        self.use_claude = use_claude
        self.output_dir = output_dir or (REPO_ROOT / "crawler" / "output")
        self._errors: list[str] = []

    # ------------------------------------------------------------------
    # Main run
    # ------------------------------------------------------------------

    def run(
        self,
        dom_snapshots: dict,         # url -> HTML string
        api_calls_intercepted: list, # list of NetworkCall dicts
    ) -> ExtractResult:
        app_name  = self.app_knowledge.get("application", {}).get("name", "Unknown App")
        timestamp = datetime.now(timezone.utc).isoformat()

        # Build route lookup: path -> route dict (O(1) match)
        routes_by_path: dict[str, dict] = {}
        base_url = self.app_knowledge.get("application", {}).get("base_url", "").rstrip("/")
        for route in self.app_knowledge.get("routes", []) or []:
            path = route.get("path", "")
            routes_by_path[path] = route

        pages: dict[str, dict] = {}
        for url, html in dom_snapshots.items():
            # Derive route_path by stripping base_url
            route_path = url
            if route_path.startswith(base_url):
                route_path = route_path[len(base_url):]
            if not route_path:
                route_path = "/"

            route = routes_by_path.get(route_path, {"path": route_path, "title": route_path})
            route_title = route.get("title", route_path)

            print(f"  {_c('→', CYAN)} {route_title:30s} {_c(url, DIM)}")

            # BS4 pass
            elements = self._extract_bs4(url, html, route)
            print(f"    {_c('bs4', DIM)} extracted {len(elements)} element(s)")

            # Claude enrichment
            if self.use_claude and elements:
                try:
                    self._enrich_with_claude(elements, route, url)
                    enriched = sum(1 for e in elements if e.business_action is not None)
                    print(f"    {_c('claude', DIM)} enriched {enriched}/{len(elements)} element(s)")
                except Exception as exc:
                    msg = f"Claude enrichment failed for {url}: {exc}"
                    print(_c(f"    ⚠  {msg}", YELLOW))
                    self._errors.append(msg)

            pages[url] = {
                "url":         url,
                "route_path":  route_path,
                "route_title": route_title,
                "elements":    [e.to_dict() for e in elements],
            }

        api_endpoints = self._extract_api_endpoints(api_calls_intercepted)

        return ExtractResult(
            app_name=app_name,
            timestamp=timestamp,
            model=self.model,
            use_claude=self.use_claude,
            pages=pages,
            api_endpoints=api_endpoints,
            errors=self._errors,
        )

    # ------------------------------------------------------------------
    # Write output
    # ------------------------------------------------------------------

    def write_output(self, result: ExtractResult) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        out_path = self.output_dir / "extract_result.json"
        out_path.write_text(
            json.dumps(result.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return out_path

    # ------------------------------------------------------------------
    # BS4 pre-pass
    # ------------------------------------------------------------------

    def _extract_bs4(self, url: str, html: str, route: dict) -> list[ElementDescriptor]:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        seen_ids: set[int] = set()
        elements: list[ElementDescriptor] = []

        # Collect candidate elements (primary tags + extra CSS queries)
        candidates: list[Any] = []
        for tag_name in CAPTURE_TAGS:
            candidates.extend(soup.find_all(tag_name))
        for tag in soup.select('[role="button"]'):
            candidates.append(tag)
        for tag in soup.select('[role="img"][aria-label]'):
            candidates.append(tag)

        for tag in candidates:
            tag_id = id(tag)
            if tag_id in seen_ids:
                continue
            seen_ids.add(tag_id)

            tag_name = tag.name.lower()

            # Skip rules
            if tag_name == "a":
                href = tag.get("href")
                if not href:
                    continue
                if href.startswith("/_next/") or href.startswith("/favicon"):
                    continue
            elif tag_name == "input":
                input_type = (tag.get("type") or "text").lower()
                if input_type == "hidden":
                    continue

            # Collect attributes
            href_val        = tag.get("href") or None
            aria_label_val  = tag.get("aria-label") or None
            placeholder_val = tag.get("placeholder") or None
            name_attr_val   = tag.get("name") or None
            input_type_val  = tag.get("type") or None

            # is_disabled — keep these; they're valid for assertion methods
            is_disabled = (
                tag.get("disabled") is not None
                or tag.get("aria-disabled") == "true"
            )

            # Normalized visible text (max 120 chars)
            raw_text = tag.get_text(separator=" ", strip=True)
            raw_text = re.sub(r"\s+", " ", raw_text).strip()
            text_val = raw_text[:120]

            element_type = self._get_element_type(tag)
            selector = self._make_css_selector(
                tag, tag_name, href_val, aria_label_val,
                placeholder_val, name_attr_val, input_type_val,
            )
            xpath = self._make_xpath(
                tag, tag_name, href_val, aria_label_val,
                placeholder_val, name_attr_val, input_type_val, text_val,
            )

            elements.append(ElementDescriptor(
                tag=tag_name,
                element_type=element_type,
                selector=selector,
                xpath=xpath,
                text=text_val,
                aria_label=aria_label_val,
                href=str(href_val) if href_val else None,
                input_type=input_type_val,
                placeholder=placeholder_val,
                name_attr=name_attr_val,
                is_disabled=is_disabled,
            ))

        return elements

    def _get_element_type(self, tag: Any) -> str:
        tag_name = tag.name.lower()
        role = (tag.get("role") or "").lower()

        if tag_name == "a":
            return "link"
        if tag_name == "button":
            return "button"
        if role == "button":
            return "button"
        if tag_name == "input":
            input_type = (tag.get("type") or "text").lower()
            type_map = {
                "text":     "text_input",
                "email":    "email_input",
                "password": "password_input",
                "search":   "search_input",
                "url":      "url_input",
                "number":   "number_input",
                "tel":      "tel_input",
                "checkbox": "checkbox",
                "radio":    "radio",
                "submit":   "submit_button",
                "button":   "button",
                "file":     "file_input",
                "date":     "date_input",
            }
            return type_map.get(input_type, "text_input")
        if tag_name == "textarea":
            return "textarea"
        if tag_name == "select":
            return "select"
        if tag_name == "form":
            return "form"
        if tag_name in ("h1", "h2", "h3"):
            return "heading"
        return "element"

    def _make_css_selector(
        self,
        tag: Any,
        tag_name: str,
        href: str | None,
        aria_label: str | None,
        placeholder: str | None,
        name_attr: str | None,
        input_type: str | None,
    ) -> str:
        role = (tag.get("role") or "").lower()

        if tag_name == "a":
            if tag.find_parent("header") is not None and href:
                return f"header a[href='{href}']"
            if href:
                return f"a[href='{href}']"
            if aria_label:
                return f"a[aria-label='{aria_label}']"
            return "a"

        if tag_name == "button":
            button_type = (tag.get("type") or "").lower()
            if tag.find_parent("form") is not None and button_type == "submit":
                return 'form button[type="submit"]'
            if aria_label:
                return f"button[aria-label='{aria_label}']"
            if name_attr:
                return f"button[name='{name_attr}']"
            return "button"

        if tag_name == "input":
            if name_attr:
                return f"input[name='{name_attr}']"
            required = tag.get("required") is not None
            if input_type and required:
                return f"input[type='{input_type}'][required]"
            if placeholder:
                first_word = placeholder.split()[0]
                return f"input[placeholder*='{first_word}']"
            if input_type:
                return f"input[type='{input_type}']"
            return "input"

        if tag_name == "textarea":
            if name_attr:
                return f"textarea[name='{name_attr}']"
            if placeholder:
                first_word = placeholder.split()[0]
                return f"textarea[placeholder*='{first_word}']"
            rows = tag.get("rows")
            if rows:
                return f"textarea[rows='{rows}']"
            return "textarea"

        if tag_name == "form":
            action = tag.get("action")
            if action:
                return f"form[action='{action}']"
            if tag.find_parent("main") is not None:
                return "main form"
            return "form"

        if tag_name in ("h1", "h2", "h3"):
            return tag_name

        if role == "button":
            if aria_label:
                return f'[role="button"][aria-label="{aria_label}"]'
            return '[role="button"]'

        return tag_name

    def _make_xpath(
        self,
        tag: Any,
        tag_name: str,
        href: str | None,
        aria_label: str | None,
        placeholder: str | None,
        name_attr: str | None,
        input_type: str | None,
        text: str,
    ) -> str:
        role = (tag.get("role") or "").lower()

        if tag_name == "a":
            if href:
                return f"//a[@href='{href}']"
            if aria_label:
                return f"//a[@aria-label='{aria_label}']"
            if text:
                return f"//a[normalize-space()='{text[:60]}']"
            return "//a"

        if tag_name == "button":
            if text and len(text) <= 60:
                return f"//button[normalize-space()='{text}']"
            if aria_label:
                return f"//button[@aria-label='{aria_label}']"
            return "//button"

        if tag_name == "input":
            if name_attr:
                return f"//input[@name='{name_attr}']"
            if input_type and placeholder:
                first_word = placeholder.split()[0]
                return f"//input[@type='{input_type}' and contains(@placeholder,'{first_word}')]"
            if input_type:
                return f"//input[@type='{input_type}']"
            return "//input"

        if tag_name == "textarea":
            if name_attr:
                return f"//textarea[@name='{name_attr}']"
            if placeholder:
                first_word = placeholder.split()[0]
                return f"//textarea[contains(@placeholder,'{first_word}')]"
            rows = tag.get("rows")
            if rows:
                return f"//textarea[@rows='{rows}']"
            return "//textarea"

        if tag_name == "form":
            action = tag.get("action")
            if action:
                return f"//form[@action='{action}']"
            return "//form"

        if tag_name in ("h1", "h2", "h3"):
            if text and len(text) <= 60:
                return f"//{tag_name}[normalize-space()='{text}']"
            return f"//{tag_name}"

        if role == "button":
            if aria_label:
                return f"//*[@role='button' and @aria-label='{aria_label}']"
            if text and len(text) <= 60:
                return f"//*[@role='button' and normalize-space()='{text}']"
            return "//*[@role='button']"

        return f"//{tag_name}"

    # ------------------------------------------------------------------
    # Claude enrichment
    # ------------------------------------------------------------------

    def _build_system_prompt(self) -> str:
        app    = self.app_knowledge.get("application", {})
        domain = self.app_knowledge.get("domain", {})
        entities = domain.get("entities", []) or []
        entity_lines = "\n".join(
            f"  - {e['name']}: {e['description']}"
            for e in entities
        )
        return f"""You are an expert test automation engineer specialising in Page Object Model (POM) design.

Application: {app.get('name', 'Web Application')}
Description: {app.get('description', '')}

Domain entities:
{entity_lines}

Your task: Given a list of UI elements from a web page, annotate each with:
1. business_action — what the user is doing with this element (plain English, business language)
2. page_object_method — a Java camelCase method name for the Page Object
   - Prefixes: click* (buttons/links), fill* (text inputs), select* (dropdowns), assert* (headings/labels)
   - Use business language, not technical names
   - Examples: clickWishingCard, fillOccasionField, assertWelcomeHeading, selectGiftCategory
3. journey_relevance — list of exact journey names (from the provided list) this element participates in

Rules:
- Annotate ALL elements, including headings (use assert* prefix for headings/labels)
- Only include journey names that were explicitly provided — do not invent new ones
- For disabled elements, still provide annotations — they are valid for assertion methods
- Skip decoration-only elements by omitting them from the output array"""

    def _build_user_prompt(
        self,
        condensed: list[dict],
        route: dict,
        journeys: list[dict],
    ) -> str:
        route_path  = route.get("path", "/")
        route_title = route.get("title", "")
        route_desc  = route.get("description", "")

        journey_names = [j.get("name", "") for j in journeys]
        journey_summary = "\n".join(
            f"  - {j.get('name', '')}: {', '.join(str(s) for s in j.get('steps', [])[:3])}…"
            for j in journeys[:6]
        )

        return (
            f"Page: {route_title} ({route_path})\n"
            f"Description: {route_desc}\n\n"
            f"Available journey names (use EXACTLY these strings):\n"
            + "\n".join(f"  - {name}" for name in journey_names)
            + f"\n\nJourney steps summary:\n{journey_summary}\n\n"
            f"UI elements to annotate (JSON):\n{json.dumps(condensed, indent=2)}\n\n"
            f"Annotate all {len(condensed)} elements using the extract_elements tool."
        )

    def _enrich_with_claude(
        self,
        elements: list[ElementDescriptor],
        route: dict,
        url: str,
    ) -> None:
        """Enrich elements in-place with Claude annotations. Raises on any failure."""
        import anthropic  # lazy import — mirrors crawl_agent pattern

        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            print(_c("  ⚠  ANTHROPIC_API_KEY not set — skipping Claude enrichment", YELLOW))
            return

        client  = anthropic.Anthropic(api_key=api_key)
        journeys = self.app_knowledge.get("journeys", []) or []

        # Build condensed inventory (~20x smaller than raw HTML)
        condensed: list[dict] = []
        for idx, el in enumerate(elements):
            entry: dict[str, Any] = {
                "idx":  idx,
                "tag":  el.tag,
                "type": el.element_type,
            }
            if el.text:
                entry["text"] = el.text[:80]
            if el.aria_label:
                entry["aria_label"] = el.aria_label
            if el.href:
                entry["href"] = el.href
            if el.placeholder:
                entry["placeholder"] = el.placeholder
            if el.input_type:
                entry["input_type"] = el.input_type
            if el.is_disabled:
                entry["disabled"] = True
            condensed.append(entry)

        response = client.messages.create(
            model=self.model,
            max_tokens=2048,
            system=self._build_system_prompt(),
            tools=[EXTRACT_ELEMENTS_TOOL],
            tool_choice={"type": "tool", "name": "extract_elements"},
            messages=[{
                "role": "user",
                "content": self._build_user_prompt(condensed, route, journeys),
            }],
        )

        # Find tool_use block
        tool_result = None
        for block in response.content:
            if block.type == "tool_use" and block.name == "extract_elements":
                tool_result = block.input
                break

        if not tool_result:
            raise RuntimeError("Claude did not return an extract_elements tool call")

        # Merge annotations back by element_index
        for annotation in tool_result.get("elements", []):
            idx = annotation.get("element_index")
            if idx is None or not isinstance(idx, int) or idx >= len(elements):
                continue
            el = elements[idx]
            el.business_action    = annotation.get("business_action")
            el.page_object_method = annotation.get("page_object_method")
            el.journey_relevance  = annotation.get("journey_relevance") or []

    # ------------------------------------------------------------------
    # API endpoint extraction
    # ------------------------------------------------------------------

    def _extract_api_endpoints(
        self,
        api_calls_intercepted: list,
    ) -> list[ApiEndpointDescriptor]:
        endpoints: list[ApiEndpointDescriptor] = []
        seen: set[str] = set()

        # Primary source: app_knowledge["api_endpoints"] (always present)
        for ep in self.app_knowledge.get("api_endpoints", []) or []:
            method = (ep.get("method") or "GET").upper()
            path   = ep.get("path", "")
            key    = f"{method}:{path}"
            seen.add(key)

            # Normalise request_body → request_schema
            request_body = ep.get("request_body")
            request_schema: dict | None = None
            if isinstance(request_body, dict):
                request_schema = {k: str(v) for k, v in request_body.items()}

            response_schema: str | None = ep.get("response")
            if response_schema is not None:
                response_schema = str(response_schema)

            endpoints.append(ApiEndpointDescriptor(
                method=method,
                path=path,
                description=ep.get("description", ""),
                request_schema=request_schema,
                response_schema=response_schema,
                restassured_method_name=self._make_restassured_method_name(method, path),
            ))

        # Supplement: intercepted calls not in app_knowledge (undocumented)
        for call in api_calls_intercepted:
            method   = (call.get("method") or "GET").upper()
            call_url = call.get("url", "")
            parsed   = urlparse(call_url)
            path     = parsed.path
            key      = f"{method}:{path}"
            if key in seen:
                continue
            seen.add(key)

            endpoints.append(ApiEndpointDescriptor(
                method=method,
                path=path,
                description="Intercepted from crawl (undocumented)",
                request_schema=None,
                response_schema=None,
                restassured_method_name=self._make_restassured_method_name(method, path),
            ))

        return endpoints

    @staticmethod
    def _make_restassured_method_name(method: str, path: str) -> str:
        """
        POST /api/suggest                           → postSuggest
        GET  /api/lists/{id}/items                  → getListsItems
        POST /api/lists/{id}/items/{itemId}/analyze → postListsItemsAnalyze
        GET  /api/shared/{shareId}                  → getShared
        """
        # Strip /api/ prefix
        cleaned = re.sub(r"^/api/", "", path)
        # Split by /, drop path-param segments {param}
        segments = [
            s for s in cleaned.split("/")
            if s and not (s.startswith("{") and s.endswith("}"))
        ]
        if not segments:
            return method.lower()
        return method.lower() + "".join(s.capitalize() for s in segments)


# ---------------------------------------------------------------------------
# LangGraph node
# ---------------------------------------------------------------------------

def extract_node(state: dict) -> dict:
    """
    LangGraph node — reads dom_snapshots + app_knowledge from state,
    runs the extractor, writes extract_result.json, returns partial state update.

    Environment variables:
        ANTHROPIC_API_KEY : required for Claude enrichment
        EXTRACT_MODEL     : model to use (default: claude-sonnet-4-6)
        NO_CLAUDE         : "true" to skip Claude enrichment (BS4 only)
    """
    extractor = ElementExtractor(
        app_knowledge=state["app_knowledge"],
        model=os.environ.get("EXTRACT_MODEL", "claude-sonnet-4-6"),
        use_claude=os.environ.get("NO_CLAUDE", "false").lower() != "true",
    )
    result = extractor.run(
        state["dom_snapshots"],
        state["api_calls_intercepted"],
    )
    extractor.write_output(result)

    return {
        "extracted_elements": {
            url: page["elements"]
            for url, page in result.to_dict()["pages"].items()
        },
        "errors": state.get("errors", []) + result.errors,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    default_output    = REPO_ROOT / "crawler" / "output" / "extract_result.json"
    default_knowledge = REPO_ROOT / "knowledge" / "app-knowledge.yaml"
    parser = argparse.ArgumentParser(
        description="AutoPilot QA — Element extraction agent (BS4 + Claude enrichment)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("crawl_result", help="Path to crawl_result.json")
    parser.add_argument(
        "--knowledge",
        default=str(default_knowledge),
        metavar="PATH",
        help=f"Path to app-knowledge.yaml (default: {default_knowledge})",
    )
    parser.add_argument(
        "--output",
        default=str(default_output),
        metavar="PATH",
        help=f"Output file path (default: {default_output})",
    )
    parser.add_argument(
        "--model",
        default="claude-sonnet-4-6",
        help="Claude model to use (default: claude-sonnet-4-6)",
    )
    parser.add_argument(
        "--no-claude",
        action="store_true",
        help="Skip Claude enrichment — BS4 extraction only",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    crawl_path     = Path(args.crawl_result)
    knowledge_path = Path(args.knowledge)
    output_path    = Path(args.output)

    # Header
    print()
    print(_c("AutoPilot QA — Extract Agent", BOLD, CYAN))
    print(_hr())
    print(f"  Crawl result : {crawl_path}")
    print(f"  Knowledge    : {knowledge_path}")
    print(f"  Model        : {args.model}")
    print(f"  Claude       : {'disabled (--no-claude)' if args.no_claude else 'enabled'}")
    print(f"  Output       : {output_path}")

    # Validate inputs
    if not crawl_path.exists():
        print(_c(f"\n✗  Crawl result not found: {crawl_path}", RED), file=sys.stderr)
        sys.exit(1)

    if not knowledge_path.exists():
        print(_c(f"\n✗  Knowledge file not found: {knowledge_path}", RED), file=sys.stderr)
        sys.exit(1)

    # Load files
    try:
        crawl_data = json.loads(crawl_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(_c(f"\n✗  Failed to parse crawl result: {exc}", RED), file=sys.stderr)
        sys.exit(1)

    try:
        knowledge = yaml.safe_load(knowledge_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        print(_c(f"\n✗  Failed to parse knowledge file: {exc}", RED), file=sys.stderr)
        sys.exit(1)

    # Build dom_snapshots from successfully crawled pages
    dom_snapshots = {
        url: page["dom_html"]
        for url, page in crawl_data.get("pages", {}).items()
        if page.get("dom_html") and page.get("status") == "ok"
    }
    api_calls = crawl_data.get("api_calls", [])

    if not dom_snapshots:
        print(
            _c("\n✗  No successfully crawled pages found in crawl result", RED),
            file=sys.stderr,
        )
        sys.exit(1)

    _print_section("Extracting Elements")
    print(f"  Pages to process : {len(dom_snapshots)}")

    # Run extraction
    extractor = ElementExtractor(
        app_knowledge=knowledge,
        model=args.model,
        use_claude=not args.no_claude,
        output_dir=output_path.parent,
    )
    result   = extractor.run(dom_snapshots, api_calls)
    out_path = extractor.write_output(result)

    # Summary
    _print_section("Summary")
    d    = result.to_dict()
    meta = d["extraction_metadata"]
    pages_processed = meta["pages_processed"]
    total_elements  = meta["total_elements"]

    if pages_processed > 0:
        print(_c(
            f"  ✓ PASSED  ({pages_processed} page(s) extracted, "
            f"{total_elements} element(s) total)",
            GREEN, BOLD,
        ))
    else:
        print(_c("  ✗ FAILED  (0 pages extracted)", RED, BOLD))

    print(f"     App              : {meta['app_name']}")
    print(f"     Pages processed  : {pages_processed}")
    print(f"     Total elements   : {total_elements}")
    print(f"     API endpoints    : {meta['api_endpoints_extracted']}")
    print(f"     Claude enriched  : {meta['claude_enriched']}")

    for url, page in d["pages"].items():
        el_count = len(page["elements"])
        print(f"     {page['route_title']:25s} : {el_count} element(s)  ({url})")

    if result.errors:
        print(_c(f"     Errors           : {len(result.errors)}", YELLOW))
        for err in result.errors:
            print(_c(f"       ⚠  {err}", YELLOW))

    print(f"     Output           : {out_path}")
    print()
    sys.exit(0 if pages_processed > 0 else 1)


if __name__ == "__main__":
    main()
