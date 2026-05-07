# AutoPilot QA

> AI-native test automation platform — describe your app, get manual test scenarios and executable automation scripts.

AutoPilot QA reads an **Application Knowledge File** and uses the Claude API to generate comprehensive **manual test scenarios** and **executable Playwright + RestAssured tests** via a multi-agent pipeline. An optional crawler enriches the knowledge file by inspecting the live DOM before generation.

The AKF (Application Knowledge File) is the backbone for this utility. It's essentially passing over application knowledge as part of context to the LLM.

No boilerplate to write. Live app access is optional — the crawler adds it when you need DOM-grounded accuracy.

---

## Get started in 4 steps

```bash
# 1. Copy the config template and fill in your app details
cp knowledge-config.example.yaml knowledge-config.yaml
# → edit knowledge-config.yaml (add your app description, repo path, or docs)

# 2. Install dependencies and set your API key
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...

# 3. Run the full pipeline (scenarios + code generation)
python run.py --build-knowledge knowledge-config.yaml --codegen

# 4. Open your outputs
open output/test-scenarios-final.md   # manual test scenarios
open output/playwright/               # Playwright TypeScript spec files
open output/restassured/ApiTests.java # RestAssured Java test class
```

---

## How it works (Workflow)

```
knowledge-config.yaml       ← you configure this (single control point)
        │
        ▼
 Knowledge Builder     ──►  knowledge/app-knowledge.yaml
 (Claude reads your          (generated from your sources)
  prompt + code + docs)
        │
        ▼ (optional)
 Crawler Pipeline      ──►  knowledge/app-knowledge.yaml  (enriched)
 (live DOM + API capture)    crawler/output/crawl_result.json
        │
        ▼
 Generator Agent       ──►  output/draft-scenarios.md
 (breadth-first coverage)
        │
        ▼
 Reviewer Agent        ──►  output/test-scenarios-final.md  ✓
 (workflow + coverage lens)      (human-readable deliverable)
        │
        ▼
 Code Generator        ──►  output/playwright/*.spec.ts
 (Playwright + RestAssured)  output/restassured/ApiTests.java  ✓
```

**Knowledge Builder** — collects context from up to three sources you configure: a human description of your app, your source code repository (local or GitHub), and any supporting documentation. Feeds it all to Claude to produce the `app-knowledge.yaml`.

**Crawler Pipeline** *(optional)* — launches a Playwright browser, crawls the live app, and enriches the knowledge file with discovered routes, form fields, selectors, and intercepted API calls. Run with `python run_crawler.py`. Four internal agents: Crawl → Extract → Generate Java artifacts → Validate. The knowledge file is updated in-place (original backed up as `.yaml.bak`).

**Generator Agent** — reads the knowledge file and generates draft scenarios covering all routes, API endpoints, and entity states.

**Reviewer Agent** — reads both the knowledge file and the draft. Applies a workflow lens (can a real user complete their goal end-to-end?) and a coverage lens (is every documented behaviour tested?). Outputs a Review Summary and the finalized scenario set.

**Code Generator Agent** — reads the finalized scenarios and generates executable automation code:
- **Playwright (TypeScript)** — one `.spec.ts` per test category (smoke, regression, edge-cases, negative)
- **RestAssured (Java)** — a single `ApiTests.java` class for all API scenarios

The final output covers:
- **Smoke** — critical path, broken-build-blocking only
- **Regression** — full happy-path journeys with precise steps and test data
- **Edge cases** — boundary conditions, optional fields, unusual but valid flows
- **Negative** — invalid inputs, missing required fields, error states
- **API** — direct API contract tests (request/response shape, error codes)

---

## Prerequisites

- **Python 3.11+** — check with `python --version`
- **Anthropic API key** — get one at [console.anthropic.com](https://console.anthropic.com)
- **`gh` CLI** (optional) — only needed if reading from a GitHub repo
- **Node.js + `@playwright/test`** (optional) — only needed to *run* the generated Playwright specs
- **Java 11+ + Maven/Gradle** (optional) — only needed to *run* the generated RestAssured tests

---

## Configuring your sources

`knowledge-config.yaml` is the single place you control what AutoPilot QA knows about your app. Enable any combination of the three sources:

```yaml
output: knowledge/app-knowledge.yaml

# Source 1 — describe your app in plain language
prompt:
  enabled: true
  text: |
    A Next.js gift list app. Wishers create lists with AI-powered suggestions
    and share them with gifters. Gifters can claim items from the shared list.
    No authentication. Stack: Next.js 14, TypeScript, SQLite.

# Source 2 — point at your source code (local or GitHub)
repo:
  enabled: true
  type: local               # or: github
  path: ../my-app
  # github_url: https://github.com/owner/repo
  patterns:
    - "src/app/api/**/*.ts"
    - "src/app/**/*.tsx"
    - "src/lib/**/*.ts"
  max_files: 40

# Source 3 — any supporting docs (README, API spec, Jira stories, etc.)
docs:
  enabled: false
  files:
    - path: README.md
```

Copy `knowledge-config.example.yaml` to get started — it has all options documented with comments.

---

## CLI reference

### Scenario pipeline (`run.py`)

```bash
# Full pipeline: build knowledge → generate → review → codegen  (recommended)
python run.py --build-knowledge knowledge-config.yaml --codegen

# Full pipeline without code generation
python run.py --build-knowledge knowledge-config.yaml --review

# Build knowledge file only
python run.py --build-knowledge knowledge-config.yaml

# Generate + review + codegen from an existing knowledge file
python run.py knowledge/app-knowledge.yaml --codegen

# Generate + review from an existing knowledge file
python run.py knowledge/app-knowledge.yaml --review

# Generate only (single agent, fastest)
python run.py knowledge/app-knowledge.yaml

# Review an existing draft without regenerating
python run.py knowledge/app-knowledge.yaml --review-only output/draft-scenarios.md

# Code generation only (from an existing finalized scenarios file)
python run.py --codegen-only output/test-scenarios-final.md

# Options
  --output PATH           Override default output file path
  --draft-output PATH     Where to save the generator draft (default: output/draft-scenarios.md)
  --codegen-output DIR    Root directory for generated code (default: output)
  --model MODEL           Claude model for all agents (default: claude-sonnet-4-6)
  --no-stream             Wait for full response before printing
```

### Crawler pipeline (`run_crawler.py`)

```bash
# Full crawler pipeline: crawl → extract → generate Java artifacts → validate
python run_crawler.py knowledge/app-knowledge.yaml

# Crawl + extract only (skip Java codegen)
python run_crawler.py knowledge/app-knowledge.yaml --no-codegen

# Run without MonitoringAgent (no Claude calls during crawl — faster)
python run_crawler.py knowledge/app-knowledge.yaml --no-monitor

# Run in headed mode (watch the browser)
python run_crawler.py knowledge/app-knowledge.yaml --no-headless

# Skip Claude enrichment in extract step (BeautifulSoup only)
python run_crawler.py knowledge/app-knowledge.yaml --no-claude

# Options
  --browser chromium|firefox|webkit   Browser engine (default: chromium)
  --max-depth N                        BFS depth limit (default: 3)
  --max-pages N                        Hard cap on pages visited (default: 50)
  --timeout MS                         Page load timeout in ms (default: 30000)
  --output DIR                         Output directory (default: crawler/output)
```

---

## Example output

**Manual test scenario (`output/test-scenarios-final.md`):**

```markdown
## TC-001: Create a Wish List (Happy Path)
**Priority**: High — Smoke
**Type**: E2E
**Component**: Wisher Flow

### Preconditions
- App is running at http://localhost:3000
- No authentication required

### Steps
| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Navigate to http://localhost:3000 | Home page loads; two cards visible |
| 2 | Click "I'm wishing" card | Redirected to /wisher; Phase 1 form visible |
| 3 | Enter "30th Birthday" in occasion field | Text appears in field |
| 4 | Click "Find Gifts" | Loading state shown; /api/suggest called |
| 5 | Wait up to 30s for AI suggestions grid | Grid of gift cards appears |

### Test Data
- Occasion: "30th Birthday"
- Context: "Loves hiking, budget around £50"
```

**Generated Playwright spec (`output/playwright/smoke.spec.ts`):**

```typescript
import { test, expect } from '@playwright/test';

const BASE_URL = process.env.BASE_URL ?? 'http://localhost:3000';

test.describe('Smoke Tests', () => {
  test('TC-001: Create a Wish List (Happy Path)', async ({ page }) => {
    // Navigate to home page
    await page.goto(BASE_URL);
    await expect(page.locator('text=I\'m wishing')).toBeVisible();

    // Start wisher flow
    await page.locator('text=I\'m wishing').click();
    await expect(page).toHaveURL(`${BASE_URL}/wisher`);

    // Fill occasion and trigger AI suggestions
    await page.locator('[placeholder*="occasion"]').fill('30th Birthday');
    await page.locator('text=Find Gifts').click();

    // Wait for AI suggestions
    await page.waitForSelector('[data-testid="suggestions-grid"]', { timeout: 30000 });
  });
});
```

**Generated RestAssured test (`output/restassured/ApiTests.java`):**

```java
// TC-015: POST /api/lists returns 201 with valid payload
@Test
@DisplayName("TC-015: POST /api/lists returns 201 with valid payload")
void postListsReturns201() {
    given()
        .contentType(ContentType.JSON)
        .body("{ \"name\": \"30th Birthday\", \"occasion\": \"Birthday\" }")
    .when()
        .post("/api/lists")
    .then()
        .statusCode(201)
        .body("id", notNullValue())
        .body("name", equalTo("30th Birthday"));
}
```

---

## Project structure

```
autopilot-qa/
├── knowledge-config.example.yaml  ← copy this → knowledge-config.yaml (gitignored)
├── knowledge/
│   ├── app-knowledge.yaml         ← generated (or hand-authored) app description
│   └── schema/
│       └── app-knowledge.schema.json
├── autopilot_qa/
│   ├── knowledge_builder.py       ← Knowledge Builder Agent
│   ├── generator.py               ← Generator Agent
│   ├── reviewer.py                ← Reviewer Agent
│   ├── code_generator.py          ← Code Generator Agent (Playwright + RestAssured)
│   └── prompts.py                 ← all prompt templates with design rationale
├── crawler/
│   ├── agents/
│   │   ├── crawl_agent.py         ← BFS Playwright crawler
│   │   ├── extract_agent.py       ← DOM element + API endpoint extractor
│   │   ├── generate_agent.py      ← Java Page Object + API client generator
│   │   ├── validate_agent.py      ← Maven compile validator
│   │   ├── monitor_agent.py       ← Claude-guided crawl monitor
│   │   └── interaction_agent.py   ← Interactive element handler
│   ├── state.py                   ← Shared pipeline state models
│   ├── validate_knowledge.py      ← Knowledge file validator
│   └── requirements.txt           ← Crawler-specific dependencies
├── docs/
│   └── design.md                  ← ADRs, prompt engineering notes, tool choices
├── output/                        ← generated artifacts (gitignored)
│   ├── draft-scenarios.md         ← Generator Agent output
│   ├── test-scenarios-final.md    ← Reviewer Agent output
│   ├── playwright/                ← Playwright spec files (one per category)
│   │   ├── smoke.spec.ts
│   │   ├── regression.spec.ts
│   │   ├── edge-cases.spec.ts
│   │   └── negative.spec.ts
│   └── restassured/
│       └── ApiTests.java          ← RestAssured test class
├── run.py                         ← Scenario pipeline CLI entry point
├── run_crawler.py                 ← Crawler pipeline CLI entry point
└── requirements.txt
```

---

## Design decisions

See [`docs/design.md`](docs/design.md) for:
- Why a config file as the single control point (not CLI flags)
- Source trust hierarchy (prompt > code > docs) and why
- Two-agent pipeline rationale (generator vs reviewer)
- Why code generation uses one Claude call per output format (not one per test case)
- File marker pattern for multi-file Playwright output
- Why the code generator takes scenarios Markdown as input, not the knowledge file
- Prompt engineering notes — what worked, what didn't

---

## Roadmap

| Version | Feature |
|---------|---------|
| **v0.1** ✅ | Knowledge file → manual test scenarios (Generator Agent) |
| **v0.1.1** ✅ | Reviewer Agent — two-agent generate → review pipeline |
| **v0.2** ✅ | Knowledge Builder — generate knowledge file from prompt + code + docs |
| **v0.3** ✅ | Code Generator Agent (Playwright + RestAssured) + Crawler Pipeline (live DOM enrichment) |
| v0.4 | Change-aware regeneration — diff-driven targeted test updates |
| v0.5 | Test management export — Xray JSON, TestRail CSV |

---

## Requirements

- Python 3.11+
- `anthropic>=0.40`, `pyyaml>=6.0`
- `gh` CLI (optional, for GitHub repo source)
- Anthropic API key (`ANTHROPIC_API_KEY`)
