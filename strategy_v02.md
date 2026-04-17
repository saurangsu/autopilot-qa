# CLAUDE.md

> Context file for Claude Code. Read this before doing anything.

---

## Project identity

- **Name:** AutoPilot QA
- **Repo name:** `autopilot-qa`
- **Python package name:** `autopilot_qa`
- **GitHub:** `github.com/saurangsu/autopilot-qa`
- **Description:** AI-native test automation platform — describe your app, get manual test scenarios and executable automation scripts.

---

## What this project does

AutoPilot QA reads an **Application Knowledge File (AKF)** — a structured YAML that describes an application's entities, workflows, APIs, and business rules — and uses the Anthropic Claude API to generate comprehensive test artifacts. It uses a two-agent LangGraph pipeline:

1. **Generator Agent** — reads the AKF, generates draft test scenarios optimising for breadth (all routes, APIs, entities, edge cases covered)
2. **Reviewer Agent** — reads both the AKF and the draft, applies a workflow lens and coverage lens, produces the final deliverable

Current output covers: Smoke, Regression, Edge Cases, Negative, and API test categories.

The current output is intentionally **exhaustive** — it is designed as a full regression suite baseline for the application as described in the AKF. This is by design for v0.1.x.

### Tech stack

- Python 3.11+
- `anthropic` SDK (>=0.40) — Claude API client with streaming
- `langgraph` — agent orchestration
- `pyyaml` (>=6.0) — YAML parsing
- Claude Sonnet 4.6 — generation model for both agents

### CLI usage

```bash
# Generate only
python run.py knowledge/app-knowledge.yaml

# Two-agent pipeline (generate + review)
python run.py knowledge/app-knowledge.yaml --review

# Review existing draft
python run.py knowledge/app-knowledge.yaml --review-only output/draft-scenarios.md
```

---

## Current project structure

```
autopilot-qa/
├── CLAUDE.md                  ← this file
├── README.md
├── run.py                     ← CLI entry point
├── requirements.txt
├── knowledge/
│   └── app-knowledge.yaml     ← example AKF
├── autopilot_qa/              ← Python package
│   ├── __init__.py
│   ├── generator.py           ← Generator Agent
│   ├── reviewer.py            ← Reviewer Agent
│   └── prompts.py             ← prompt templates
├── docs/
│   └── design.md              ← architecture decisions, prompt engineering notes
└── output/                    ← generated artifacts (gitignored)
    ├── draft-scenarios.md
    └── test-scenarios-final.md
```

---

## Completed work

### v0.1 ✅ — Generator Agent
- Defined and implemented the **Application Knowledge File (AKF)** standard — a structured YAML schema describing an application's entities, workflows, APIs, roles, and business rules
- Built the **Generator Agent**: reads AKF → generates exhaustive draft manual test scenarios in Markdown
- Output categories: Smoke, Regression, Edge Cases, Negative, API
- CLI entry point: `run.py` with `knowledge/app-knowledge.yaml` as input
- Tech: Python 3.11+, Anthropic SDK, PyYAML, Claude Sonnet

### v0.1.1 ✅ — Reviewer Agent + Rename (current version)
- Built the **Reviewer Agent**: reads AKF + Generator draft → applies workflow lens and coverage lens → produces final polished test scenario deliverable
- Wired both agents into a **two-agent LangGraph pipeline**
- Added `--review` and `--review-only` CLI flags
- **Renamed project from Kryptonite → AutoPilot QA**
  - Python package renamed: `kryptonite/` → `autopilot_qa/`
  - All imports, references, docstrings, README, and docs updated
  - GitHub repo created and pushed: `github.com/saurangsu/autopilot-qa`
- `.gitignore` configured: `output/`, `__pycache__/`, `.env`, `.venv/`, build artifacts

---

## Engineering principles (apply to all future versions)

These are non-negotiable constraints for every new feature built into AutoPilot QA.

### Performance
- Minimize Claude API calls — batch where possible, avoid redundant calls
- Use streaming for all LLM interactions to reduce perceived latency
- Avoid loading the full AKF into memory repeatedly — parse once, pass by reference
- Profile before optimizing — use `cProfile` or `time` instrumentation on new agents before shipping

### Memory efficiency
- Do not accumulate large intermediate outputs in memory — write to disk incrementally for large suites
- LangGraph state should carry only what the next node needs — trim state aggressively between nodes
- For large AKFs (50+ entities), consider chunked processing rather than single-pass generation

### Code quality
- Every new agent gets its own file under `autopilot_qa/`
- Prompts live in `prompts.py` only — no inline prompt strings in agent files
- All new CLI flags documented in README before merging
- Keep `run.py` as a thin orchestrator — no business logic

---

## Roadmap

### v0.1 ✅ — Generator Agent
Knowledge file → manual test scenarios (single agent, Markdown output)

### v0.1.1 ✅ — Reviewer Agent (current)
Two-agent pipeline: Generator → Reviewer. Output is a full exhaustive regression suite baseline.

### v0.2 ✅ — Code Generation (Playwright + RestAssured) — SHIPPED 2026-04-17

Added the **Code Generator Agent** as a third pipeline step after the Reviewer Agent.

**What was built:**

#### `autopilot_qa/code_generator.py` (NEW)
- `generate_playwright()` — single Claude call for all Playwright categories; parses `// ===FILE: name.spec.ts===` markers into per-category files written to `output/playwright/`
- `generate_restassured()` — single Claude call for all API scenarios; writes `output/restassured/ApiTests.java`
- `_split_playwright_files()` — marker parser; fallback to `tests.spec.ts` if Claude omits markers
- `_strip_markdown_fences()` — strips accidental ` ```java ` / ` ```typescript ` wrapping
- `run()` — top-level orchestrator
- `PLAYWRIGHT_MAX_TOKENS = 16384`, `RESTASSURED_MAX_TOKENS = 8192`

#### `autopilot_qa/prompts.py` (EXTENDED)
- `PLAYWRIGHT_CODEGEN_SYSTEM_PROMPT` — TypeScript/Playwright codegen, strict file-marker format, `BASE_URL` env var, one `test()` per TC-ID
- `RESTASSURED_CODEGEN_SYSTEM_PROMPT` — Java/JUnit5/RestAssured, `@BeforeAll` base URI, Hamcrest matchers, self-contained tests
- `build_playwright_prompt()` / `build_restassured_prompt()` — user prompt builders
- Design decision block added: one call per format, file markers, scenarios Markdown as canonical spec

#### `run.py` (EXTENDED)
- `--codegen` — full pipeline: generate → review → codegen (3-step, 4-step with `--build-knowledge`)
- `--codegen-only SCENARIOS_PATH` — skip generation/review, generate code from existing file
- `--codegen-output DIR` — root output directory (default: `output`)
- Step counter adapts dynamically for all pipeline combinations

**Architecture after v0.2:**
```
autopilot-qa/
├── autopilot_qa/
│   ├── generator.py
│   ├── reviewer.py
│   ├── code_generator.py      ← ADDED
│   └── prompts.py
└── output/
    ├── draft-scenarios.md
    ├── test-scenarios-final.md
    ├── playwright/             ← ADDED
    │   ├── smoke.spec.ts
    │   ├── regression.spec.ts
    │   ├── edge-cases.spec.ts
    │   └── negative.spec.ts
    └── restassured/            ← ADDED
        └── ApiTests.java
```

**Key design decisions made:**
1. **One call per output format** — TypeScript and Java conventions are too different to mix in one call without quality loss; two focused calls win.
2. **File markers for multi-file Playwright output** — `// ===FILE: filename.spec.ts===` markers let us produce all spec files in one API call and split client-side.
3. **Scenarios Markdown as input (not the AKF)** — the code agent only needs the finalized scenarios; decoupling means it can run independently on any well-formed scenarios file.

### v0.3 — Crawler Add-on (AKF enrichment)
A web crawler that inspects the live DOM of the application under test and enriches or validates the AKF automatically.

- Input: base URL + existing AKF
- Output: enriched AKF with discovered routes, form fields, API endpoints
- Keeps the AKF as the source of truth — crawler output is additive, not destructive

### v0.4 — Change-Aware Test Regeneration *(key strategic differentiator)*

**This is the most important long-term feature in the roadmap.**

When the application under test changes — new features, modified workflows, API contract changes — the engineer supplies a **diff or changelog** alongside the AKF. AutoPilot QA will:

1. Identify which parts of the AKF are affected by the change
2. Regenerate **only** the impacted test scenarios (not the full suite)
3. Regenerate **only** the impacted automation scripts
4. Produce a **change impact report** — what tests are new, what changed, what was removed

**Why this matters:** The exhaustive baseline generated in v0.1.x is a one-time artifact. In CI/CD reality, engineers need targeted regression on every PR — not a full suite re-run. This feature transforms AutoPilot QA from a one-shot generator into a **living test system** that evolves with the application.

**Input additions:**
- `--diff path/to/changes.yaml` — structured changelog mapped to AKF sections
- `--diff path/to/git.diff` — raw git diff of application source changes
- AKF versioning: AKF carries a `version` field; AutoPilot QA tracks changes between versions

**New LangGraph nodes (do not build yet — for planning only):**
- `change_parser.py` — reads diff, maps changes to affected AKF sections
- `selective_generator.py` — passes only affected AKF sections to Generator Agent
- `impact_reporter.py` — produces change impact report

**Output additions:**
- `output/delta-scenarios.md` — only the changed/new/removed scenarios
- `output/delta-playwright/` — only the affected Playwright tests
- `output/change-impact-report.md` — human-readable summary of what changed

**Performance note:** Selective regeneration is the entire point of this feature — the change parser must be as precise as possible to minimize tokens sent to Claude. This is the most API-call-sensitive feature in the roadmap.

### v0.5 — Test Management Export
Export generated scenarios to test management tools:
- Xray JSON (Jira-native)
- TestRail CSV
- Zephyr Scale JSON

---

## GitHub

- **Repo:** `github.com/saurangsu/autopilot-qa`
- **Visibility:** Public
- **Default branch:** `main`

### .gitignore

```
output/
__pycache__/
*.pyc
.env
.venv/
*.egg-info/
dist/
build/
```

---

## Commit message conventions

```
feat: <what was added>
fix: <what was fixed>
perf: <performance improvement>
refactor: <code restructure, no behavior change>
docs: <documentation only>
chore: <tooling, deps, config>
```

### Example for v0.2
```
feat: add Code Generator Agent (Playwright + RestAssured)

- New LangGraph node: autopilot_qa/code_generator.py
- Playwright TS output: output/playwright/
- RestAssured Java output: output/restassured/
- CLI flags: --codegen, --codegen-only
- Prompts added to prompts.py
```