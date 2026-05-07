#!/usr/bin/env python3
"""
AutoPilot QA — Crawl Agent

Playwright-based crawler that drives the AutoPilot QA pipeline.
Produces dom_snapshots and api_calls_intercepted for downstream agents.

Usage (CLI):
    python crawler/agents/crawl_agent.py <knowledge-file>
        [--output PATH]
        [--browser chromium|firefox|webkit]
        [--no-headless]
        [--no-screenshots]
        [--timeout MS]

LangGraph node:
    from crawler.agents.crawl_agent import crawl_node
    state_update = crawl_node(state)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from time import time
from typing import Any
from urllib.parse import urlparse

import yaml

# ---------------------------------------------------------------------------
# ANSI colour helpers (matches validate_knowledge.py style)
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
MAX_RESPONSE_BODY_BYTES = 8 * 1024                # 8 KB
SENSITIVE_HEADERS = frozenset({"cookie", "authorization", "set-cookie"})
DYNAMIC_PATH_RE = re.compile(r"\[|\{|:[a-zA-Z_]")


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class NetworkCall:
    url: str
    method: str
    source_page: str
    request_headers: dict
    request_post_data: str | None
    response_status: int | None
    response_content_type: str | None
    response_body: str | None   # JSON responses only, truncated at 8 KB
    captured_at: str            # ISO 8601

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PageResult:
    url: str
    route_path: str
    title: str
    dom_html: str
    screenshot_path: str | None
    status: str                 # "ok" | "error" | "timeout" | "skipped"
    error_message: str | None
    load_time_ms: int
    is_dynamic: bool
    is_discovered: bool = False  # True when found by BFS, not seeded from knowledge file

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CrawlResult:
    app_name: str
    base_url: str
    timestamp: str
    duration_seconds: float
    pages: dict         # url -> PageResult
    api_calls: list     # list[NetworkCall]
    dynamic_routes: list[dict]
    errors: list[str]

    def to_dict(self) -> dict:
        pages_discovered = sum(
            1 for p in self.pages.values() if p.is_discovered
        )
        return {
            "crawl_metadata": {
                "app_name": self.app_name,
                "base_url": self.base_url,
                "timestamp": self.timestamp,
                "duration_seconds": round(self.duration_seconds, 3),
                "pages_visited": len(self.pages),
                "pages_failed": sum(
                    1 for p in self.pages.values() if p.status != "ok"
                ),
                "pages_discovered": pages_discovered,
                "api_calls_captured": len(self.api_calls),
                "dynamic_routes_skipped": len(self.dynamic_routes),
            },
            "pages": {url: pr.to_dict() for url, pr in self.pages.items()},
            "api_calls": [nc.to_dict() for nc in self.api_calls],
            "dynamic_routes": self.dynamic_routes,
            "errors": self.errors,
        }


# ---------------------------------------------------------------------------
# AppCrawler
# ---------------------------------------------------------------------------

class AppCrawler:
    def __init__(
        self,
        app_knowledge: dict,
        headless: bool = True,
        browser_type: str = "chromium",
        page_timeout: int = 30_000,
        output_dir: Path | None = None,
        screenshots: bool = True,
        monitor: Any = None,      # MonitoringAgent instance — optional
        max_depth: int = 3,       # BFS depth limit from seed URLs
        max_pages: int = 50,      # hard cap on total pages visited
    ) -> None:
        self.app_knowledge = app_knowledge
        self.headless      = headless
        self.browser_type  = browser_type
        self.page_timeout  = page_timeout
        self.output_dir    = output_dir or (REPO_ROOT / "crawler" / "output")
        self.screenshots   = screenshots
        self.monitor       = monitor
        self.max_depth     = max_depth
        self.max_pages     = max_pages

        self._current_source_url: str = ""
        self._api_calls: dict[str, NetworkCall] = {}   # keyed by "METHOD:url"
        self._navigated_urls: list[str] = []           # filled by framenavigated listener
        self._errors: list[str] = []

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _base_url(self) -> str:
        return self.app_knowledge["application"]["base_url"].rstrip("/")

    def _is_same_origin(self, url: str) -> bool:
        parsed_base = urlparse(self._base_url())
        parsed_url  = urlparse(url)
        return (
            parsed_base.scheme == parsed_url.scheme
            and parsed_base.netloc == parsed_url.netloc
        )

    def _is_dynamic(self, path: str) -> bool:
        return bool(DYNAMIC_PATH_RE.search(path))

    def _is_excluded(self, path: str) -> bool:
        exclusions = self.app_knowledge.get("exclusions", []) or []
        for excl in exclusions:
            # exclusion strings like "/api/* — do not test..."
            pattern = excl.split()[0].split("—")[0].strip()
            if pattern.endswith("*"):
                if path.startswith(pattern[:-1]):
                    return True
            elif path == pattern:
                return True
        return False

    def _slug(self, url: str) -> str:
        """Convert a URL to a filesystem-safe slug."""
        parsed = urlparse(url)
        path = parsed.path.strip("/") or "home"
        slug = re.sub(r"[^a-zA-Z0-9_-]", "_", path)
        return slug[:80]

    def _sanitise_headers(self, headers: dict) -> dict:
        return {k: v for k, v in headers.items() if k.lower() not in SENSITIVE_HEADERS}

    # ------------------------------------------------------------------
    # Network interception
    # ------------------------------------------------------------------

    def _attach_network_listener(self, page: Any) -> None:
        """Attach response listener once for the whole crawl session."""

        def on_response(response: Any) -> None:
            try:
                req = response.request
                if req.resource_type not in ("xhr", "fetch"):
                    return
                url = req.url
                if not self._is_same_origin(url):
                    return
                key = f"{req.method}:{url}"
                if key in self._api_calls:
                    return  # deduplicate — first capture wins

                try:
                    req_headers = self._sanitise_headers(dict(req.headers))
                except Exception:
                    req_headers = {}

                try:
                    post_data = req.post_data
                except Exception:
                    post_data = None

                content_type: str | None = None
                response_body: str | None = None
                try:
                    content_type = response.headers.get("content-type", "")
                    if content_type and "json" in content_type:
                        body_bytes = response.body()
                        response_body = body_bytes[:MAX_RESPONSE_BODY_BYTES].decode(
                            "utf-8", errors="replace"
                        )
                except Exception:
                    pass

                self._api_calls[key] = NetworkCall(
                    url=url,
                    method=req.method,
                    source_page=self._current_source_url,
                    request_headers=req_headers,
                    request_post_data=post_data,
                    response_status=response.status,
                    response_content_type=content_type,
                    response_body=response_body,
                    captured_at=datetime.now(timezone.utc).isoformat(),
                )
            except Exception as exc:
                self._errors.append(f"Network listener error: {exc}")

        page.on("response", on_response)

    def _attach_navigation_listener(self, page: Any) -> None:
        """
        Track every main-frame navigation during the session.
        self._navigated_urls is cleared before each page visit in run();
        the listener closure always appends to the same list object.

        This is Source 2 of three link-discovery sources (ADR-014).
        It catches SPA router.push() calls that produce no <a> tag.
        """
        def on_navigate(frame: Any) -> None:
            try:
                if frame == page.main_frame:
                    url = frame.url
                    if url and url not in self._navigated_urls:
                        self._navigated_urls.append(url)
            except Exception:
                pass

        page.on("framenavigated", on_navigate)

    # ------------------------------------------------------------------
    # Link discovery (BFS — three sources, ADR-014)
    # ------------------------------------------------------------------

    def _normalise_link(self, href: str) -> str | None:
        """Return an absolute same-origin URL or None if unusable."""
        href = href.strip()
        if not href:
            return None
        if href.startswith(("#", "javascript:", "mailto:", "tel:")):
            return None

        base = self._base_url()

        if href.startswith("http://") or href.startswith("https://"):
            if not self._is_same_origin(href):
                return None
            return href.rstrip("/") if href != base + "/" else base

        if href.startswith("/"):
            return base + href

        return None   # relative paths without leading / — skip (unpredictable)

    def _is_static_asset(self, path: str) -> bool:
        """True for paths that are assets, not navigable pages."""
        static_prefixes = (
            "/_next/", "/favicon", "/robots.txt", "/sitemap",
            "/static/", "/assets/", "/images/",
        )
        static_extensions = (
            ".js", ".css", ".ico", ".png", ".jpg", ".jpeg",
            ".svg", ".woff", ".woff2", ".ttf", ".map", ".json",
        )
        if any(path.startswith(p) for p in static_prefixes):
            return True
        if any(path.endswith(ext) for ext in static_extensions):
            return True
        return False

    def _filter_links(self, raw: set[str], visited: set[str]) -> set[str]:
        """Remove visited, cross-origin, excluded, and static-asset URLs."""
        base   = self._base_url()
        result = set()
        for link in raw:
            if link in visited:
                continue
            if not self._is_same_origin(link):
                continue
            path = link[len(base):] or "/"
            if self._is_excluded(path):
                continue
            if self._is_static_asset(path):
                continue
            result.add(link)
        return result

    def _extract_page_links(self, dom_html: str, current_url: str) -> set[str]:
        """
        Collect candidate URLs from three sources (ADR-014):
          1. <a href> tags — standard HTML navigation
          2. framenavigated events — SPA router.push() calls (self._navigated_urls)
          3. data-* attributes with URL-like values — share links, deep links
        """
        links: set[str] = set()

        # Source 1 + Source 3: parse the DOM
        try:
            from bs4 import BeautifulSoup

            soup = BeautifulSoup(dom_html, "html.parser")

            for a in soup.find_all("a", href=True):
                norm = self._normalise_link(a["href"])
                if norm:
                    links.add(norm)

            for tag in soup.find_all(True):
                for attr, val in tag.attrs.items():
                    if attr.startswith("data-") and isinstance(val, str):
                        val = val.strip()
                        if val.startswith("/") or val.startswith("http"):
                            norm = self._normalise_link(val)
                            if norm:
                                links.add(norm)

        except Exception as exc:
            self._errors.append(f"Link extraction error for {current_url}: {exc}")

        # Source 2: SPA navigations captured during this page's load + settle
        for nav_url in self._navigated_urls:
            if nav_url != current_url:
                norm = self._normalise_link(nav_url)
                if norm:
                    links.add(norm)

        return links

    # ------------------------------------------------------------------
    # API-response URL derivation (Source 4 — dynamic-route seeding)
    # ------------------------------------------------------------------

    def _seed_from_api_responses(
        self,
        frontier: "deque[str]",
        depth: "dict[str, int]",
        visited: "set[str]",
        route_meta: "dict[str, dict]",
    ) -> None:
        """
        Scan captured API response bodies for UUID/string fields, then check
        whether any dynamic route template in the knowledge file can be
        instantiated with those values.

        Example: share_id="abc123" + dynamic route /shared/[shareId]
                 → seeds http://base/shared/abc123 into the frontier.
        """
        import json as _json
        import re as _re

        UUID_RE = _re.compile(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
            _re.IGNORECASE,
        )
        # Collect dynamic route templates (e.g. /shared/[shareId], /list/[id])
        dynamic_routes = [
            r.get("path", "")
            for r in (self.app_knowledge.get("routes", []) or [])
            if self._is_dynamic(r.get("path", ""))
        ]

        base = self._base_url()

        for nc in list(self._api_calls.values()):
            if not nc.response_body:
                continue
            try:
                body = _json.loads(nc.response_body)
            except Exception:
                continue
            if not isinstance(body, dict):
                continue

            for field_val in body.values():
                if not isinstance(field_val, str):
                    continue
                # Only try UUID-looking values (covers share_id, id, etc.)
                if not UUID_RE.match(field_val):
                    continue
                for tmpl in dynamic_routes:
                    # Replace [param] or {param} with the field value
                    candidate = _re.sub(r"\[[^\]]+\]|\{[^\}]+\}", field_val, tmpl)
                    if _re.search(r"\[|\{", candidate):
                        continue  # not fully resolved
                    url = base + candidate
                    if url not in visited and url not in route_meta:
                        norm = self._normalise_link(url)
                        if norm and norm not in visited:
                            frontier.append(norm)
                            depth.setdefault(norm, 1)
                            print(_c(f"  + seeded from API response ({field_val[:8]}…): {norm}", GREEN))

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def _try_fill_input(self, page: Any, *selectors: str, value: str) -> bool:
        """Try each selector in order; fill the first match."""
        for sel in selectors:
            try:
                page.fill(sel, value, timeout=2_000)
                return True
            except Exception:
                continue
        return False

    def _authenticate(self, context: Any) -> None:
        auth      = self.app_knowledge.get("authentication", {}) or {}
        auth_type = auth.get("type", "none")

        if auth_type == "none":
            return

        if auth_type == "form":
            login_url = auth.get("login_url")
            if not login_url:
                print(_c("  ⚠  [auth] form auth configured but login_url missing — skipping", YELLOW))
                return

            username = os.environ.get("APP_USERNAME")
            password = os.environ.get("APP_PASSWORD")
            if not username or not password:
                print(_c(
                    "  ⚠  [auth] APP_USERNAME / APP_PASSWORD env vars not set — skipping login",
                    YELLOW,
                ))
                return

            page = context.new_page()
            try:
                page.goto(login_url, wait_until="domcontentloaded")
                filled_user = self._try_fill_input(
                    page,
                    'input[type="email"]',
                    'input[name*="user"]',
                    'input[name*="email"]',
                    'input[placeholder*="user" i]',
                    'input[placeholder*="email" i]',
                    value=username,
                )
                filled_pass = self._try_fill_input(
                    page,
                    'input[type="password"]',
                    value=password,
                )
                if not (filled_user and filled_pass):
                    raise RuntimeError("Could not locate login form inputs")

                page.click('button[type="submit"], input[type="submit"]')
                page.wait_for_load_state("networkidle", timeout=10_000)
                print(_c("  ✓  Form auth completed", GREEN))
            except Exception as exc:
                msg = f"[auth] Form login failed: {exc}"
                print(_c(f"  ⚠  {msg}", YELLOW))
                self._errors.append(msg)
            finally:
                page.close()

        else:
            print(_c(
                f"  ⚠  [auth] type '{auth_type}' not yet automated — "
                "ensure session cookies are pre-loaded",
                YELLOW,
            ))

    # ------------------------------------------------------------------
    # Per-page crawl
    # ------------------------------------------------------------------

    def _crawl_page(self, page: Any, url: str, route: dict) -> PageResult:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        self._current_source_url = url
        route_path = route.get("path", "")
        start_ts   = time()
        screenshot_path: str | None = None

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=self.page_timeout)

            # Wait for SPA networkidle — tolerate timeout (SPA may never idle)
            try:
                page.wait_for_load_state("networkidle", timeout=5_000)
            except PlaywrightTimeoutError:
                pass

            # Brief pause for deferred XHR
            page.wait_for_timeout(500)

            load_time_ms = int((time() - start_ts) * 1000)
            title        = page.title() or ""
            dom_html     = page.content() or ""

            if self.screenshots:
                screenshots_dir = self.output_dir / "screenshots"
                screenshots_dir.mkdir(parents=True, exist_ok=True)
                slug            = self._slug(url)
                screenshot_path = str(screenshots_dir / f"{slug}.png")
                try:
                    page.screenshot(path=screenshot_path, full_page=True)
                except Exception as exc:
                    print(_c(f"  ⚠  Screenshot failed for {url}: {exc}", YELLOW))
                    screenshot_path = None

            return PageResult(
                url=url,
                route_path=route_path,
                title=title,
                dom_html=dom_html,
                screenshot_path=screenshot_path,
                status="ok",
                error_message=None,
                load_time_ms=load_time_ms,
                is_dynamic=False,
            )

        except PlaywrightTimeoutError as exc:
            load_time_ms = int((time() - start_ts) * 1000)
            msg = f"Timeout crawling {url}: {exc}"
            self._errors.append(msg)
            return PageResult(
                url=url,
                route_path=route_path,
                title="",
                dom_html="",
                screenshot_path=None,
                status="timeout",
                error_message=str(exc),
                load_time_ms=load_time_ms,
                is_dynamic=False,
            )

        except Exception as exc:
            load_time_ms = int((time() - start_ts) * 1000)
            msg = f"Error crawling {url}: {exc}"
            self._errors.append(msg)
            return PageResult(
                url=url,
                route_path=route_path,
                title="",
                dom_html="",
                screenshot_path=None,
                status="error",
                error_message=str(exc),
                load_time_ms=load_time_ms,
                is_dynamic=False,
            )

    # ------------------------------------------------------------------
    # Pre-flight
    # ------------------------------------------------------------------

    def _preflight(self) -> None:
        base_url = self._base_url()
        try:
            urllib.request.urlopen(base_url, timeout=5)
        except Exception as exc:
            print()
            print(_c(f"✗  App not reachable at {base_url}", RED, BOLD))
            print(_c(f"   {exc}", RED))
            print(_c("   Make sure the app is running and try again.", DIM))
            sys.exit(1)

    # ------------------------------------------------------------------
    # Route partitioning
    # ------------------------------------------------------------------

    def _partition_routes(self) -> tuple[list[dict], list[dict], list[dict]]:
        """Return (static_routes, dynamic_routes, excluded_routes)."""
        routes   = self.app_knowledge.get("routes", []) or []
        static, dynamic, excluded = [], [], []
        for route in routes:
            path = route.get("path", "")
            if self._is_excluded(path):
                excluded.append(route)
            elif self._is_dynamic(path):
                dynamic.append(route)
            else:
                static.append(route)
        return static, dynamic, excluded

    # ------------------------------------------------------------------
    # Main run
    # ------------------------------------------------------------------

    def run(self) -> CrawlResult:
        from playwright.sync_api import sync_playwright

        crawl_start = time()
        timestamp   = datetime.now(timezone.utc).isoformat()
        app_name    = self.app_knowledge.get("application", {}).get("name", "Unknown App")
        base_url    = self._base_url()

        self._preflight()

        static_routes, dynamic_routes, excluded_routes = self._partition_routes()

        _print_section("BFS Crawl — Route Summary")
        print(f"  Seed routes (static)   : {len(static_routes)}")
        print(f"  Dynamic routes         : {len(dynamic_routes)}  (skipped — require runtime ID)")
        print(f"  Excluded routes        : {len(excluded_routes)}")
        print(f"  Max depth              : {self.max_depth}")
        print(f"  Max pages              : {self.max_pages}")
        print(f"  Monitor                : {'enabled' if self.monitor else 'disabled'}")

        # Map absolute URL → route dict for seed routes (preserves metadata)
        route_meta: dict[str, dict] = {
            base_url + r.get("path", ""): r
            for r in static_routes
        }

        # BFS state
        frontier: deque[str]    = deque(route_meta.keys())
        visited:  set[str]      = set()
        depth:    dict[str, int] = {url: 0 for url in frontier}
        pages:    dict[str, PageResult] = {}

        with sync_playwright() as pw:
            browser_launcher = getattr(pw, self.browser_type)
            browser  = browser_launcher.launch(headless=self.headless)
            context  = browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent="AutoPilotQA-Crawler/1.0",
            )
            page = context.new_page()
            page.set_default_timeout(self.page_timeout)

            self._attach_network_listener(page)
            self._attach_navigation_listener(page)
            self._authenticate(context)

            # -- Pre-crawl: run declarative interactions to discover dynamic routes --
            if self.app_knowledge.get("discovery_interactions"):
                _print_section("Discovery Interactions")
                from crawler.agents.interaction_agent import InteractionRunner
                runner = InteractionRunner(app_knowledge=self.app_knowledge, page=page)
                discovered = runner.run_all()
                for disc_url in discovered:
                    norm = self._normalise_link(disc_url)
                    if norm and norm not in route_meta and norm not in visited:
                        frontier.append(norm)
                        depth.setdefault(norm, 1)
                        print(_c(f"  + seeded from interaction: {norm}", GREEN))
                if runner.errors:
                    for err in runner.errors:
                        print(_c(f"  ⚠  {err}", YELLOW))

                # Derive additional URLs from captured API response bodies.
                # Looks for UUID-valued fields and checks if any dynamic route
                # template matches (e.g. share_id → /shared/[shareId]).
                self._seed_from_api_responses(frontier, depth, visited, route_meta)

            _print_section("Crawling")

            while frontier and len(visited) < self.max_pages:
                url        = frontier.popleft()
                page_depth = depth.get(url, 0)

                if url in visited or page_depth > self.max_depth:
                    continue

                visited.add(url)

                route        = route_meta.get(url, {"path": url[len(base_url):] or "/", "title": url})
                is_discovered = url not in route_meta
                label        = route.get("title", url)
                depth_tag    = _c(f"d={page_depth}", DIM)

                print(f"  {_c('→', CYAN)} [{depth_tag}] {label:28s} {_c(url, DIM)}")

                # Clear SPA navigation tracking for this page
                self._navigated_urls.clear()

                result             = self._crawl_page(page, url, route)
                result.is_discovered = is_discovered
                pages[url]         = result

                status_str = _c("✓ ok", GREEN) if result.status == "ok" else _c(f"✗ {result.status}", RED)
                disc_tag   = _c(" [discovered]", DIM) if is_discovered else ""
                print(f"    {status_str}  ({result.load_time_ms} ms){disc_tag}")

                if result.status != "ok" or page_depth >= self.max_depth:
                    continue

                # Discover links from three sources
                raw_links   = self._extract_page_links(result.dom_html, url)
                new_links   = self._filter_links(raw_links, visited)

                if self.monitor and result.dom_html:
                    recent_api = [
                        nc.to_dict() for nc in self._api_calls.values()
                        if nc.source_page == url
                    ]
                    decision = self.monitor.observe(
                        url=url,
                        title=result.title,
                        dom_html=result.dom_html,
                        discovered_links=sorted(new_links),
                        api_calls=recent_api,
                    )
                    self.monitor.apply_patches(decision.patches)

                    # Remove skipped links, add prioritized to front
                    skip_set  = set(decision.links_to_skip)
                    new_links -= skip_set

                    for link in reversed(decision.links_to_prioritize):
                        norm = self._normalise_link(link)
                        if norm and norm not in visited:
                            frontier.appendleft(norm)
                            depth.setdefault(norm, page_depth + 1)
                            new_links.discard(norm)

                for link in new_links:
                    if link not in visited:
                        frontier.append(link)
                        depth.setdefault(link, page_depth + 1)

            page.close()
            context.close()
            browser.close()

        duration = time() - crawl_start

        dynamic_route_dicts = [
            {
                "path":  r.get("path", ""),
                "title": r.get("title", ""),
                "note":  "Skipped — requires runtime ID",
            }
            for r in dynamic_routes
        ]

        return CrawlResult(
            app_name=app_name,
            base_url=base_url,
            timestamp=timestamp,
            duration_seconds=round(duration, 3),
            pages=pages,
            api_calls=list(self._api_calls.values()),
            dynamic_routes=dynamic_route_dicts,
            errors=self._errors,
        )

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------

    def write_output(self, result: CrawlResult) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        out_path = self.output_dir / "crawl_result.json"
        out_path.write_text(
            json.dumps(result.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return out_path


# ---------------------------------------------------------------------------
# LangGraph node
# ---------------------------------------------------------------------------

def crawl_node(state: dict) -> dict:
    """
    LangGraph node — reads app_knowledge from state, runs the crawler,
    and returns a partial state update.

    Environment variables:
        HEADLESS : "false" to run headed (default: headless)
        BROWSER  : "chromium" | "firefox" | "webkit" (default: chromium)
    """
    headless     = os.environ.get("HEADLESS", "true").lower() != "false"
    browser_type = os.environ.get("BROWSER", "chromium")

    crawler = AppCrawler(
        app_knowledge=state["app_knowledge"],
        headless=headless,
        browser_type=browser_type,
    )
    result = crawler.run()
    crawler.write_output(result)

    ok_pages = {url: pr for url, pr in result.pages.items() if pr.status == "ok"}
    last_url = list(result.pages.keys())[-1] if result.pages else ""

    return {
        "visited_urls":          list(result.pages.keys()),
        "dom_snapshots":         {url: pr.dom_html for url, pr in ok_pages.items()},
        "api_calls_intercepted": [nc.to_dict() for nc in result.api_calls],
        "errors":                result.errors,
        "current_url":           last_url,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    default_output = REPO_ROOT / "crawler" / "output" / "crawl_result.json"
    parser = argparse.ArgumentParser(
        description="AutoPilot QA — Playwright-based web crawler",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("knowledge_file", help="Path to app-knowledge.yaml")
    parser.add_argument(
        "--output",
        default=str(default_output),
        metavar="PATH",
        help=f"Output file path (default: {default_output})",
    )
    parser.add_argument(
        "--browser",
        choices=["chromium", "firefox", "webkit"],
        default="chromium",
        help="Browser engine (default: chromium)",
    )
    parser.add_argument(
        "--no-headless",
        action="store_true",
        help="Run browser in headed mode",
    )
    parser.add_argument(
        "--no-screenshots",
        action="store_true",
        help="Skip screenshot capture",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=30_000,
        metavar="MS",
        help="Page load timeout in milliseconds (default: 30000)",
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
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    knowledge_path = Path(args.knowledge_file)
    output_path    = Path(args.output)

    # Header
    print()
    print(_c("AutoPilot QA — Crawl Agent", BOLD, CYAN))
    print(_hr())
    print(f"  Knowledge : {knowledge_path}")
    print(f"  Browser   : {args.browser}")
    print(f"  Headless  : {not args.no_headless}")
    print(f"  Timeout   : {args.timeout} ms")
    print(f"  Max depth : {args.max_depth}")
    print(f"  Max pages : {args.max_pages}")
    print(f"  Output    : {output_path}")

    # Load knowledge file
    if not knowledge_path.exists():
        print(_c(f"\n✗  Knowledge file not found: {knowledge_path}", RED), file=sys.stderr)
        sys.exit(1)

    try:
        data = yaml.safe_load(knowledge_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        print(_c(f"\n✗  YAML parse error: {exc}", RED), file=sys.stderr)
        sys.exit(1)

    if not isinstance(data, dict):
        print(_c("\n✗  Knowledge file must be a YAML mapping", RED), file=sys.stderr)
        sys.exit(1)

    # Run crawler
    crawler = AppCrawler(
        app_knowledge=data,
        headless=not args.no_headless,
        browser_type=args.browser,
        page_timeout=args.timeout,
        output_dir=output_path.parent,
        screenshots=not args.no_screenshots,
        max_depth=args.max_depth,
        max_pages=args.max_pages,
    )
    result  = crawler.run()
    out_path = crawler.write_output(result)

    # Summary
    _print_section("Summary")
    meta       = result.to_dict()["crawl_metadata"]
    ok_count   = meta["pages_visited"] - meta["pages_failed"]
    fail_count = meta["pages_failed"]

    if ok_count > 0:
        print(_c(f"  ✓ PASSED  ({ok_count} page(s) crawled successfully)", GREEN, BOLD))
    else:
        print(_c("  ✗ FAILED  (0 pages successfully crawled)", RED, BOLD))

    print(f"     App              : {meta['app_name']}")
    print(f"     Base URL         : {meta['base_url']}")
    print(f"     Pages visited    : {meta['pages_visited']}")
    print(f"     Pages discovered : {meta['pages_discovered']}")
    print(f"     Pages failed     : {fail_count}")
    print(f"     API calls        : {meta['api_calls_captured']}")
    print(f"     Dynamic skipped  : {meta['dynamic_routes_skipped']}")
    print(f"     Duration         : {meta['duration_seconds']:.1f} s")
    print(f"     Output           : {out_path}")

    if result.errors:
        print(_c(f"     Errors           : {len(result.errors)}", YELLOW))
        for err in result.errors:
            print(_c(f"       ⚠  {err}", YELLOW))

    print()
    sys.exit(0 if ok_count > 0 else 1)


if __name__ == "__main__":
    main()
