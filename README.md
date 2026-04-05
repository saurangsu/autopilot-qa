# AutoPilot QA

> AI-native test scenario generator — describe your app, get manual test cases.

AutoPilot QA reads an **Application Knowledge File** and uses the Claude API to generate comprehensive **manual test scenarios** in Markdown via a two-agent pipeline.

The AKF (Application Knowledge File) is the backbone for this utility. It's essentially passing over accplication knowledge as part of context to the LLM.

No browser. No live app access. No boilerplate to write.

---

## Get started in 4 steps

```bash
# 1. Copy the config template and fill in your app details
cp knowledge-config.example.yaml knowledge-config.yaml
# → edit knowledge-config.yaml (add your app description, repo path, or docs)

# 2. Install dependencies and set your API key
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...

# 3. Run the full pipeline
python run.py --build-knowledge knowledge-config.yaml --review

# 4. Open your test scenarios
open output/test-scenarios-final.md
```

---

## How it works

```
knowledge-config.yaml       ← you configure this (single control point)
        │
        ▼
 Knowledge Builder     ──►  knowledge/app-knowledge.yaml
 (Claude reads your          (generated from your sources)
  prompt + code + docs)
        │
        ▼
 Generator Agent       ──►  output/draft-scenarios.md
 (breadth-first coverage)
        │
        ▼
 Reviewer Agent        ──►  output/test-scenarios-final.md  ✓
 (workflow + coverage lens)      (this is your deliverable)
```

**Knowledge Builder** — collects context from up to three sources you configure: a human description of your app, your source code repository (local or GitHub), and any supporting documentation. Feeds it all to Claude to produce the `app-knowledge.yaml`.

**Generator Agent** — reads the knowledge file and generates draft scenarios covering all routes, API endpoints, and entity states.

**Reviewer Agent** — reads both the knowledge file and the draft. Applies a workflow lens (can a real user complete their goal end-to-end?) and a coverage lens (is every documented behaviour tested?). Outputs a Review Summary and the finalized scenario set.

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

```bash
# Full pipeline: build knowledge → generate → review  (recommended)
python run.py --build-knowledge knowledge-config.yaml --review

# Build knowledge file only
python run.py --build-knowledge knowledge-config.yaml

# Generate + review from an existing knowledge file
python run.py knowledge/app-knowledge.yaml --review

# Generate only (single agent, faster)
python run.py knowledge/app-knowledge.yaml

# Review an existing draft without regenerating
python run.py knowledge/app-knowledge.yaml --review-only output/draft-scenarios.md

# Options
  --output PATH           Override default output file path
  --draft-output PATH     Where to save the generator draft (default: output/draft-scenarios.md)
  --model MODEL           Claude model for all agents (default: claude-sonnet-4-6)
  --no-stream             Wait for full response before printing
```

---

## Example output

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
│   └── prompts.py                 ← all prompt templates with design rationale
├── docs/
│   └── design.md                  ← ADRs, prompt engineering notes, tool choices
├── output/                        ← generated artifacts (gitignored)
│   ├── draft-scenarios.md         ← Generator Agent output
│   └── test-scenarios-final.md    ← Reviewer Agent output (the deliverable)
├── run.py                         ← CLI entry point
└── requirements.txt
```

---

## Design decisions

See [`docs/design.md`](docs/design.md) for:
- Why a config file as the single control point (not CLI flags)
- Source trust hierarchy (prompt > code > docs) and why
- Why the knowledge file is generated, not hand-authored
- Two-agent pipeline rationale (generator vs reviewer)
- Prompt engineering notes — what worked, what didn't

---

## Roadmap

| Version | Feature |
|---------|---------|
| **v0.1** ✅ | Knowledge file → manual test scenarios (Generator Agent) |
| **v0.1.1** ✅ | Reviewer Agent — two-agent generate → review pipeline |
| **v0.1.2** ✅ | Knowledge Builder — generate knowledge file from prompt + code + docs |
| v0.2 | Crawler add-on — enrich knowledge with live DOM via Playwright |
| v0.3 | Code generation — Java Page Objects + RestAssured API clients |
| v0.4 | Test management export — Xray JSON, TestRail CSV |

---

## Requirements

- Python 3.11+
- `anthropic>=0.40`, `pyyaml>=6.0`
- `gh` CLI (optional, for GitHub repo source)
- Anthropic API key (`ANTHROPIC_API_KEY`)
