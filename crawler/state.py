"""
AutoPilot QA — Shared LangGraph state.

All agents in the pipeline read from and write to CrawlState.
"""
from __future__ import annotations

from typing import TypedDict


class CrawlState(TypedDict):
    app_knowledge: dict           # loaded from app-knowledge.yaml
    visited_urls: list[str]
    dom_snapshots: dict           # url -> HTML string
    api_calls_intercepted: list   # list of NetworkCall dicts
    extracted_elements: dict      # url -> list of element descriptors (extract_agent fills)
    generated_page_objects: dict  # class_name -> java code (generate_agent fills)
    generated_api_clients: dict   # class_name -> java code (generate_agent fills)
    errors: list[str]
    current_url: str
