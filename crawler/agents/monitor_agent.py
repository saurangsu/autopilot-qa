#!/usr/bin/env python3
"""
AutoPilot QA — Monitoring Agent (v0.3)

Runs alongside the BFS crawler. After each page crawl it:
  1. Receives the page result + discovered links + intercepted API calls
  2. Sends a single-turn structured observation to Claude (tool use)
  3. Returns a MonitorDecision: links to prioritize/skip + knowledge patches

Context store:
  A rolling plain-text history (self._history) accumulated across the session.
  Each call is single-turn (user → tool_use) to avoid API conversation
  structure constraints. The history is injected into each user message so
  Claude retains session memory without requiring multi-turn bookkeeping.

Prompt caching:
  The system prompt is built once from the initial knowledge file state and
  marked with cache_control. It stays stable for the full crawl session;
  discovered routes/endpoints appear in the per-call context summary instead.

Patch rules (ADR-016 — additive only):
  add_route           → append to routes[], never touch existing entries
  add_test_data       → append to test_data.observed_ids
  add_note            → append to notes[]
  add_observation     → add monitor_observations block to an existing route
  add_undocumented_api→ append to api_endpoints[] with discovered=true

Usage:
    from crawler.agents.monitor_agent import MonitoringAgent

    monitor = MonitoringAgent(app_knowledge=knowledge)
    decision = monitor.observe(url, title, dom_html, links, api_calls)
    monitor.apply_patches(decision.patches)
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# ANSI colour helpers
# ---------------------------------------------------------------------------

RESET  = "\033[0m"
YELLOW = "\033[33m"
DIM    = "\033[2m"


def _c(text: str, *codes: str) -> str:
    return "".join(codes) + text + RESET


# ---------------------------------------------------------------------------
# Tool schema — forces Claude to return structured decisions
# ---------------------------------------------------------------------------

MONITOR_TOOL: dict = {
    "name": "monitor_decision",
    "description": (
        "Record what was observed on a crawled page and make BFS decisions. "
        "Called once per page. Return all fields even if empty."
    ),
    "input_schema": {
        "type": "object",
        "required": [
            "observation_summary",
            "links_to_prioritize",
            "links_to_skip",
            "patches",
        ],
        "properties": {
            "observation_summary": {
                "type": "string",
                "description": (
                    "One or two sentences summarising what this page revealed "
                    "about the application. Used as running context in future calls."
                ),
            },
            "links_to_prioritize": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "URLs to move to the front of the BFS queue. "
                    "Use for novel pages, dynamic instances (/list/real-id), "
                    "and pages that unlock further journeys."
                ),
            },
            "links_to_skip": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "URLs to drop entirely. "
                    "Use for static assets (/_next/, .js, .css), "
                    "API routes (/api/*), and already-documented known routes."
                ),
            },
            "patches": {
                "type": "array",
                "description": "Ordered list of knowledge file patches to apply.",
                "items": {
                    "type": "object",
                    "required": ["op", "data"],
                    "properties": {
                        "op": {
                            "type": "string",
                            "enum": [
                                "add_route",
                                "add_test_data",
                                "add_note",
                                "add_observation",
                                "add_undocumented_api",
                            ],
                        },
                        "data": {
                            "type": "object",
                            "description": (
                                "add_route: {path, title, description?, discovered:true}\n"
                                "add_test_data: {list_ids?:[...], share_ids?:[...], ...}\n"
                                "add_note: {text}\n"
                                "add_observation: {route, text}  — route must be an existing path\n"
                                "add_undocumented_api: {method, path, description?, discovered:true}"
                            ),
                        },
                    },
                },
            },
        },
    },
}


# ---------------------------------------------------------------------------
# MonitorDecision
# ---------------------------------------------------------------------------

@dataclass
class MonitorDecision:
    links_to_prioritize: list[str]
    links_to_skip:       list[str]
    patches:             list[dict]
    observation_summary: str

    @classmethod
    def empty(cls, reason: str = "") -> "MonitorDecision":
        return cls([], [], [], reason)


# ---------------------------------------------------------------------------
# MonitoringAgent
# ---------------------------------------------------------------------------

class MonitoringAgent:

    def __init__(
        self,
        app_knowledge: dict,
        model: str = "claude-sonnet-4-6",
        summarise_after: int = 15,
    ) -> None:
        self.app_knowledge  = app_knowledge   # mutable — apply_patches mutates this
        self.model          = model
        self.summarise_after = summarise_after

        self._client: Any = None              # lazy — avoid import at module load

        # Rolling session state for context building
        self._history:         list[str]  = []   # "url: summary" per page
        self._patches_applied: list[dict] = []   # all patches ever applied
        self._pages_visited:   int        = 0

        # Frozen system prompt — built once, never updated mid-session
        self._system_prompt: list[dict] | None = None

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def observe(
        self,
        url:              str,
        title:            str,
        dom_html:         str,
        discovered_links: list[str],
        api_calls:        list[dict],
    ) -> MonitorDecision:
        """Safe wrapper — returns an empty decision on any failure."""
        try:
            return self._observe(url, title, dom_html, discovered_links, api_calls)
        except Exception as exc:
            print(_c(f"    ⚠  [monitor] {exc}", YELLOW))
            return MonitorDecision.empty(f"monitor error: {exc}")

    def apply_patches(self, patches: list[dict]) -> None:
        """Apply patches to self.app_knowledge in-place (ADR-016 — additive only)."""
        for patch in patches:
            op   = patch.get("op", "")
            data = patch.get("data") or {}
            try:
                self._apply_one_patch(op, data)
            except Exception as exc:
                print(_c(f"    ⚠  [monitor] patch '{op}' failed: {exc}", YELLOW))

    # ------------------------------------------------------------------
    # Core observe (raises on error — caller wraps with observe())
    # ------------------------------------------------------------------

    def _observe(
        self,
        url:              str,
        title:            str,
        dom_html:         str,
        discovered_links: list[str],
        api_calls:        list[dict],
    ) -> MonitorDecision:
        import anthropic

        if self._client is None:
            api_key = os.environ.get("ANTHROPIC_API_KEY")
            if not api_key:
                raise RuntimeError("ANTHROPIC_API_KEY not set")
            self._client = anthropic.Anthropic(api_key=api_key)

        if self._system_prompt is None:
            self._system_prompt = self._build_system_prompt()

        user_content = self._build_user_message(
            url, title, dom_html, discovered_links, api_calls
        )

        response = self._client.messages.create(
            model=self.model,
            max_tokens=1024,
            system=self._system_prompt,
            tools=[MONITOR_TOOL],
            tool_choice={"type": "tool", "name": "monitor_decision"},
            messages=[{"role": "user", "content": user_content}],
        )

        decision = self._parse_decision(response)

        # Update rolling state
        self._pages_visited += 1
        self._history.append(f"{url}: {decision.observation_summary}")
        self._patches_applied.extend(decision.patches)

        # Trim history to window
        if len(self._history) > self.summarise_after:
            self._history = self._history[-self.summarise_after:]

        return decision

    # ------------------------------------------------------------------
    # Patch application (ADR-016)
    # ------------------------------------------------------------------

    def _apply_one_patch(self, op: str, data: dict) -> None:
        if op == "add_route":
            routes = self.app_knowledge.setdefault("routes", [])
            path   = data.get("path", "")
            if not path:
                return
            existing_paths = {r.get("path") for r in routes}
            if path not in existing_paths:
                routes.append(data)

        elif op == "add_test_data":
            td  = self.app_knowledge.setdefault("test_data", {})
            obs = td.setdefault("observed_ids", {})
            for key, values in data.items():
                bucket   = obs.setdefault(key, [])
                new_vals = values if isinstance(values, list) else [values]
                for v in new_vals:
                    if v and v not in bucket:
                        bucket.append(v)

        elif op == "add_note":
            text  = (data.get("text") or "").strip()
            notes = self.app_knowledge.setdefault("notes", [])
            if text and text not in notes:
                notes.append(text)

        elif op == "add_observation":
            route_path = data.get("route", "")
            text       = (data.get("text") or "").strip()
            if not route_path or not text:
                return
            for route in self.app_knowledge.get("routes", []):
                if route.get("path") == route_path:
                    obs_list = route.setdefault("monitor_observations", [])
                    if text not in obs_list:
                        obs_list.append(text)
                    break

        elif op == "add_undocumented_api":
            endpoints = self.app_knowledge.setdefault("api_endpoints", [])
            method    = (data.get("method") or "GET").upper()
            path      = data.get("path", "")
            if not path:
                return
            key      = f"{method}:{path}"
            existing = {
                f"{(e.get('method') or 'GET').upper()}:{e.get('path','')}"
                for e in endpoints
            }
            if key not in existing:
                entry = dict(data)
                entry["method"] = method
                endpoints.append(entry)

    # ------------------------------------------------------------------
    # System prompt (built once, cached via cache_control)
    # ------------------------------------------------------------------

    def _build_system_prompt(self) -> list[dict]:
        app      = self.app_knowledge.get("application", {})
        routes   = self.app_knowledge.get("routes", [])
        endpoints = self.app_knowledge.get("api_endpoints", [])
        journeys = self.app_knowledge.get("journeys", [])

        known_routes = "\n".join(
            f"  {r.get('path', '')}  —  {r.get('title', '')}"
            for r in routes
        ) or "  (none)"

        known_endpoints = "\n".join(
            f"  {(e.get('method') or 'GET').upper()}  {e.get('path', '')}"
            for e in endpoints
        ) or "  (none)"

        known_journeys = "\n".join(
            f"  - {j.get('name', '')}"
            for j in journeys
        ) or "  (none)"

        text = f"""\
You are a QA Monitoring Agent running alongside a BFS web crawler.
Your role: review each crawled page and decide which links to follow and what to add to the knowledge file.

APPLICATION
  Name        : {app.get('name', 'Web Application')}
  Base URL    : {app.get('base_url', '')}
  Description : {app.get('description', '')}

KNOWN ROUTES (do not add these again)
{known_routes}

KNOWN API ENDPOINTS (do not add these again)
{known_endpoints}

KNOWN JOURNEYS
{known_journeys}

PATCH RULES — additive only, never modify existing fields:
  add_route           — new UI page not in known routes. Set discovered=true. Ignore /api/* and /_next/*.
  add_test_data       — real runtime IDs observed (list IDs, share IDs, UUIDs). data keys: list_ids, share_ids, etc.
  add_note            — short factual observation worth recording for test authors.
  add_observation     — additional detail about an EXISTING route. data must include route (exact path) and text.
  add_undocumented_api— API call seen in network traffic not in known endpoints. Set discovered=true.

LINK RULES:
  links_to_prioritize — novel UI pages and dynamic route instances (e.g. /list/real-uuid). Go to front of queue.
  links_to_skip       — static assets (/_next/, .js, .css, .ico, .png), API paths (/api/*), already-known routes.

Be conservative. Only patch when information is genuinely new."""

        return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]

    # ------------------------------------------------------------------
    # User message (per-call — not cached)
    # ------------------------------------------------------------------

    def _build_context_summary(self) -> str:
        if not self._history:
            return ""

        patch_ops: dict[str, int] = {}
        for p in self._patches_applied:
            op = p.get("op", "unknown")
            patch_ops[op] = patch_ops.get(op, 0) + 1

        patch_str   = ", ".join(f"{n} {op}" for op, n in patch_ops.items()) or "none"
        recent      = self._history[-10:]
        history_str = "\n".join(f"  - {h}" for h in recent)

        return (
            f"[CRAWL CONTEXT — {self._pages_visited} pages visited, "
            f"{len(self._patches_applied)} patches applied ({patch_str})]\n"
            f"Recent observations:\n{history_str}"
        )

    def _build_user_message(
        self,
        url:              str,
        title:            str,
        dom_html:         str,
        discovered_links: list[str],
        api_calls:        list[dict],
    ) -> str:
        parts: list[str] = []

        context = self._build_context_summary()
        if context:
            parts.append(context)

        dom_summary = self.condense_dom(dom_html)

        api_lines = []
        for call in api_calls[:8]:
            method  = call.get("method", "GET")
            path    = call.get("url", "").split("?")[0]
            status  = call.get("response_status", "?")
            api_lines.append(f"  {method} {path} → {status}")
        api_str = "\n".join(api_lines) or "  (none)"

        link_str = "\n".join(f"  {l}" for l in discovered_links[:20]) or "  (none)"

        parts.append(
            f"[CURRENT PAGE]\n"
            f"URL   : {url}\n"
            f"Title : {title or '(no title)'}\n\n"
            f"DOM summary:\n{dom_summary}\n\n"
            f"Intercepted API calls:\n{api_str}\n\n"
            f"Discovered links ({len(discovered_links)} total, showing ≤20):\n{link_str}"
        )

        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # DOM condensation (static — avoids loading full HTML into monitor context)
    # ------------------------------------------------------------------

    @staticmethod
    def condense_dom(html: str) -> str:
        """
        Reduce raw HTML to a compact summary for the monitor prompt.
        Extracts headings, inputs, buttons, and data-* URL attributes only.
        """
        try:
            from bs4 import BeautifulSoup

            soup  = BeautifulSoup(html, "html.parser")
            lines: list[str] = []

            # Headings
            headings = [
                h.get_text(strip=True)[:60]
                for h in soup.find_all(["h1", "h2", "h3"])[:5]
                if h.get_text(strip=True)
            ]
            if headings:
                lines.append(f"Headings: {', '.join(headings)}")

            # Form inputs
            inputs: list[str] = []
            for tag in soup.find_all(["input", "textarea", "select"])[:12]:
                label    = (
                    tag.get("placeholder")
                    or tag.get("aria-label")
                    or tag.get("name")
                    or tag.get("type")
                    or "field"
                )
                itype    = tag.get("type", "text")
                required = "(required)" if tag.get("required") is not None else ""
                inputs.append(f"{label}:{itype} {required}".strip())
            if inputs:
                lines.append(f"Inputs: {', '.join(inputs)}")

            # Buttons
            buttons = [
                b.get_text(strip=True)[:30]
                for b in soup.find_all("button")[:8]
                if b.get_text(strip=True)
            ]
            if buttons:
                lines.append(f"Buttons: {', '.join(buttons)}")

            # Anchor count
            link_count = len(soup.find_all("a", href=True))
            if link_count:
                lines.append(f"Anchor tags: {link_count}")

            # data-* attributes with URL-like values (share links, deep links)
            data_urls: list[str] = []
            for tag in soup.find_all(True):
                for attr, val in tag.attrs.items():
                    if attr.startswith("data-") and isinstance(val, str):
                        val = val.strip()
                        if val.startswith("/") or val.startswith("http"):
                            data_urls.append(f"{attr}={val[:60]}")
            if data_urls:
                lines.append(f"Data URL attrs: {', '.join(data_urls[:5])}")

            return "\n".join(lines) if lines else "(no notable elements)"

        except Exception:
            return "(DOM condensation failed)"

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    def _parse_decision(self, response: Any) -> MonitorDecision:
        for block in response.content:
            if (
                hasattr(block, "type")
                and block.type == "tool_use"
                and block.name == "monitor_decision"
            ):
                inp = block.input or {}
                return MonitorDecision(
                    links_to_prioritize = inp.get("links_to_prioritize") or [],
                    links_to_skip       = inp.get("links_to_skip") or [],
                    patches             = inp.get("patches") or [],
                    observation_summary = inp.get("observation_summary") or "",
                )
        return MonitorDecision.empty("no tool_use block in response")
