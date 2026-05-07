#!/usr/bin/env python3
"""
AutoPilot QA — Interaction Runner (v0.3)

Runs declarative interaction sequences against the live app before BFS starts.
Purpose: discover dynamic route instances (e.g. /list/{id}) that BFS can never
reach from static HTML alone — they only exist after a user creates a resource.

The interactions are defined in app-knowledge.yaml under `discovery_interactions`.
Each interaction is a list of typed steps. The runner executes them in order,
stops on first failure, and returns any URLs captured via `capture_url` steps.

Step types:
  goto            path: /wisher
  fill            target: "occasion" (label/placeholder/name match), value_key: occasions
  click           text: "Find Gifts"  OR  selector: '.some-css'
  wait_response   path: /api/suggest, timeout: 35000
  wait_url        pattern: /list/, timeout: 15000
  select_first    selector: '[class*="card"]'
  capture_url     (captures page.url at this point)
  get_dom_urls    pattern: /shared/  (scans DOM for matching hrefs / data-* attrs)
  wait_element    selector: '.some-css', timeout: 10000

Usage (called from AppCrawler.run() before the BFS loop):
    from crawler.agents.interaction_agent import InteractionRunner
    runner = InteractionRunner(app_knowledge=knowledge, page=page)
    urls   = runner.run_all()
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# ANSI colour helpers
# ---------------------------------------------------------------------------

RESET  = "\033[0m"
BOLD   = "\033[1m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
CYAN   = "\033[36m"
DIM    = "\033[2m"


def _c(text: str, *codes: str) -> str:
    return "".join(codes) + text + RESET


# ---------------------------------------------------------------------------
# InteractionRunner
# ---------------------------------------------------------------------------

class InteractionRunner:
    """
    Executes knowledge-file-defined interaction sequences against the live app.

    Each call to run_all() returns a deduplicated list of URLs discovered
    during the interactions, ready to be seeded into the BFS frontier.
    """

    def __init__(self, app_knowledge: dict, page: Any) -> None:
        self.app_knowledge = app_knowledge
        self.page          = page
        self.base_url      = (
            app_knowledge.get("application", {})
            .get("base_url", "")
            .rstrip("/")
        )
        self.test_data = (
            app_knowledge.get("test_data", {})
            .get("sample_inputs", {})
        )
        self.errors: list[str] = []
        self._discovered: list[str] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run_all(self) -> list[str]:
        """Run every discovery_interaction defined in the knowledge file."""
        interactions = self.app_knowledge.get("discovery_interactions", [])
        if not interactions:
            return []

        for interaction in interactions:
            name = interaction.get("name", "unnamed")
            print(f"  {_c('◆', CYAN)} {name}")
            try:
                self._run_one(interaction)
            except Exception as exc:
                msg = f"Interaction '{name}' aborted: {exc}"
                print(_c(f"    ⚠  {msg}", YELLOW))
                self.errors.append(msg)

        # Deduplicate, preserve order
        seen: set[str] = set()
        result: list[str] = []
        for url in self._discovered:
            if url and url not in seen:
                seen.add(url)
                result.append(url)
        return result

    # ------------------------------------------------------------------
    # Single interaction
    # ------------------------------------------------------------------

    def _run_one(self, interaction: dict) -> None:
        steps = interaction.get("steps", [])
        name  = interaction.get("name", "unnamed")

        for i, step in enumerate(steps):
            action = step.get("action", "")
            try:
                found = self._execute(action, step)
                if found:
                    for url in found:
                        print(_c(f"    ✓ captured: {url}", GREEN))
                    self._discovered.extend(found)
            except Exception as exc:
                msg = f"[{name}] step {i+1} '{action}' failed: {exc}"
                print(_c(f"    ✗  {msg}", YELLOW))
                self.errors.append(msg)
                return   # stop this interaction on first failure

    # ------------------------------------------------------------------
    # Step executor
    # ------------------------------------------------------------------

    def _execute(self, action: str, step: dict) -> list[str]:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        if action == "goto":
            path = step.get("path", "/")
            url  = self.base_url + path
            print(_c(f"    → goto {url}", DIM))
            self.page.goto(url, wait_until="domcontentloaded")
            try:
                self.page.wait_for_load_state("networkidle", timeout=5_000)
            except PlaywrightTimeoutError:
                pass

        elif action == "fill":
            target    = step.get("target", "")
            value_key = step.get("value_key")
            value     = step.get("value") or ""
            if value_key:
                pool  = self.test_data.get(value_key) or []
                value = pool[0] if pool else value
            if not value:
                raise RuntimeError(f"No value for fill target '{target}'")
            print(_c(f"    → fill '{target}' = {value!r}", DIM))
            self._smart_fill(target, value)

        elif action == "click":
            text     = step.get("text", "")
            selector = step.get("selector", "")
            timeout  = step.get("timeout", 8_000)
            print(_c(f"    → click {text or selector!r}", DIM))
            if text:
                # Try role-button first, then fallback to visible text
                try:
                    self.page.get_by_role("button", name=text, exact=False).first.click(timeout=timeout)
                except Exception:
                    self.page.locator(f'button:has-text("{text}"), [role="button"]:has-text("{text}")').first.click(timeout=timeout)
            elif selector:
                self.page.locator(selector).first.click(timeout=timeout)
            else:
                raise RuntimeError("click step requires 'text' or 'selector'")

        elif action == "wait_response":
            path    = step.get("path", "")
            timeout = step.get("timeout", 35_000)
            print(_c(f"    → wait_response {path} (up to {timeout//1000}s)", DIM))
            self.page.wait_for_event(
                "response",
                predicate=lambda r: path in r.url and r.status < 500,
                timeout=timeout,
            )
            try:
                self.page.wait_for_load_state("networkidle", timeout=8_000)
            except PlaywrightTimeoutError:
                pass

        elif action == "select_first":
            selector = step.get("selector", "")
            timeout  = step.get("timeout", 15_000)
            print(_c(f"    → select_first {selector!r}", DIM))
            if not selector:
                raise RuntimeError("select_first requires 'selector'")
            loc = self.page.locator(selector).first
            loc.wait_for(state="visible", timeout=timeout)
            loc.click()

        elif action == "wait_url":
            pattern = step.get("pattern", "")
            timeout = step.get("timeout", 15_000)
            print(_c(f"    → wait_url *{pattern}* (up to {timeout//1000}s)", DIM))
            self.page.wait_for_url(f"**{pattern}**", timeout=timeout)

        elif action == "wait_element":
            selector = step.get("selector", "")
            timeout  = step.get("timeout", 10_000)
            print(_c(f"    → wait_element {selector!r}", DIM))
            self.page.locator(selector).first.wait_for(state="visible", timeout=timeout)

        elif action == "capture_url":
            url = self.page.url
            print(_c(f"    → capture_url: {url}", DIM))
            return [url]

        elif action == "get_dom_urls":
            pattern = step.get("pattern", "")
            print(_c(f"    → get_dom_urls pattern={pattern!r}", DIM))
            return self._extract_dom_urls(pattern)

        else:
            raise RuntimeError(f"Unknown action: {action!r}")

        return []

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _smart_fill(self, target: str, value: str) -> None:
        """
        Try several strategies to locate and fill a field.
        target is matched case-insensitively against label text, placeholder,
        name, id, and aria-label.
        """
        # Strategy 0: Playwright get_by_label (handles <label for> + aria-labelledby)
        try:
            loc = self.page.get_by_label(target, exact=False).first
            if loc.count() > 0:
                loc.fill(value, timeout=3_000)
                return
        except Exception:
            pass

        # Strategy 1: label sibling selector (label has no `for` attribute)
        for tag in ("input", "textarea"):
            try:
                loc = self.page.locator(
                    f'label:has-text("{target}") + {tag}, '
                    f'label:has-text("{target}") ~ {tag}'
                ).first
                if loc.count() > 0:
                    loc.fill(value, timeout=3_000)
                    return
            except Exception:
                continue

        css_strategies = [
            f'input[placeholder*="{target}" i]',
            f'textarea[placeholder*="{target}" i]',
            f'input[name*="{target}" i]',
            f'input[id*="{target}" i]',
            f'input[aria-label*="{target}" i]',
            f'textarea[name*="{target}" i]',
            f'textarea[aria-label*="{target}" i]',
        ]
        for sel in css_strategies:
            try:
                loc = self.page.locator(sel).first
                if loc.count() > 0:
                    loc.fill(value, timeout=3_000)
                    return
            except Exception:
                continue
        raise RuntimeError(
            f"Could not locate field for target '{target}' — "
            f"tried get_by_label + {len(css_strategies)} CSS selectors"
        )

    def _extract_dom_urls(self, pattern: str) -> list[str]:
        """
        Scan the current page's DOM for URLs containing `pattern`.
        Checks <a href>, and any attribute whose value looks like a URL.
        """
        try:
            from bs4 import BeautifulSoup

            html  = self.page.content()
            soup  = BeautifulSoup(html, "html.parser")
            found: list[str] = []
            seen:  set[str]  = set()

            def _add(val: str) -> None:
                val = val.strip()
                if not val or val in seen:
                    return
                if pattern and pattern not in val:
                    return
                full = val if val.startswith("http") else self.base_url + val
                if full not in seen:
                    seen.add(full)
                    found.append(full)

            for a in soup.find_all("a", href=True):
                _add(a["href"])

            for tag in soup.find_all(True):
                for attr, val in tag.attrs.items():
                    if isinstance(val, str):
                        v = val.strip()
                        if v.startswith("/") or v.startswith("http"):
                            _add(v)

            return found
        except Exception as exc:
            self.errors.append(f"get_dom_urls failed: {exc}")
            return []
