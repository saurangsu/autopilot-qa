# AutoPilot QA

> AI-native test scenario generator — describe your app, get manual test cases.

AutoPilot QA reads an **Application Knowledge File** (a YAML description of your
app's pages, APIs, user journeys, and domain entities) and uses the Claude API
to generate comprehensive **manual test scenarios** in Markdown.

No browser. No live app access. No boilerplate to write.

---

## How it works

```
app-knowledge.yaml
       │
       ▼
 Generator Agent  ──►  draft-scenarios.md  (breadth-first, all coverage)
       │
       ▼ (reads knowledge + draft)
 Reviewer Agent   ──►  test-scenarios-final.md  ✓ (reviewed, coherent, deliverable)
```

The **Generator Agent** writes scenarios optimised for breadth: all routes, all
API endpoints, all entity states covered. The **Reviewer Agent** then reads both
the knowledge file and the draft, applies a workflow lens (can a real user
complete their journey end-to-end?) and a coverage lens (is every documented
behaviour tested?), and produces the finalized output.

The final output covers:
- **Smoke** — the critical path that must pass before anything else
- **Regression** — full happy-path journeys with precise steps and test data
- **Edge cases** — boundary conditions, optional fields, unusual but valid flows
- **Negative** — invalid inputs, missing required fields, error states
- **API** — direct API contract tests (request/response, error codes)

Each scenario follows a consistent format: TC ID, priority, preconditions, a
step table with expected results, test data, and notes.

---

## Quick start

```bash
# 1. Clone the repo
git clone https://github.com/saurangsu/autopilot-qa.git
cd autopilot-qa

# 2. Install dependencies (Python 3.11+)
pip install -r requirements.txt

# 3. Set your Anthropic API key
export ANTHROPIC_API_KEY=sk-ant-...

# 4. Write (or use the example) knowledge file
#    See: knowledge/app-knowledge.yaml

# 5a. Generate only
python run.py knowledge/app-knowledge.yaml
# → output/test-scenarios.md

# 5b. Full pipeline — generate + review (recommended)
python run.py knowledge/app-knowledge.yaml --review
# → output/draft-scenarios.md       (raw generator output)
# → output/test-scenarios-final.md  (reviewed — use this one)
```

---

## The Application Knowledge File

The knowledge file is the only thing you need to provide. It's a YAML document
that captures:

```yaml
application:
  name: "My App"
  base_url: "https://my-app.example.com"
  description: "What the app does"

authentication:
  type: form   # or: none, oauth2, saml, basic, token
  login_url: "/login"

domain:
  entities:
    - name: Order
      description: "A customer purchase order"
      key_fields: [id, customerId, status, total]

routes:
  - path: "/orders"
    title: "Orders List"
    description: "Paginated list of all orders with search and filter"

api_endpoints:
  - method: POST
    path: "/api/orders"
    description: "Create a new order"
    request_body:
      customerId: string
      items: array

journeys:
  - name: "Place an order"
    steps:
      - navigate to /orders/new
      - fill in customer and items
      - click Submit
    priority: smoke

test_data:
  sample_inputs:
    customer_ids: ["CUST-001", "CUST-002"]
```

See [`knowledge/app-knowledge.yaml`](knowledge/app-knowledge.yaml) for a
complete example using the Gift List App.

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
| 5 | Wait for AI suggestions grid (up to 30s) | Grid of gift cards appears |
...

### Test Data
- Occasion: "30th Birthday"
- Context: "Loves hiking, budget around £50"
```

---

## CLI options

```bash
python run.py <knowledge-file> [options]

Modes:
  (default)                    Generate only → output/test-scenarios.md
  --review                     Generate + Review → output/test-scenarios-final.md
  --review-only <draft-path>   Review an existing draft (skip generation)

Options:
  --output PATH           Output file (overrides mode default)
  --draft-output PATH     Where to save the draft when using --review
                          (default: output/draft-scenarios.md)
  --model MODEL           Claude model for both agents (default: claude-sonnet-4-6)
  --no-stream             Wait for full response instead of streaming
```

---

## Project structure

```
autopilot_qa/
├── knowledge/
│   └── app-knowledge.yaml     ← your app description (edit this)
├── autopilot_qa/
│   ├── generator.py           ← Generator Agent: load → prompt → Claude → draft
│   ├── reviewer.py            ← Reviewer Agent: knowledge + draft → final
│   └── prompts.py             ← all prompt templates with design rationale
├── docs/
│   └── design.md              ← ADRs, prompt engineering notes, tool choices
├── output/                    ← generated artifacts (gitignored)
│   ├── draft-scenarios.md     ← generator output
│   └── test-scenarios-final.md ← reviewer output (the deliverable)
├── run.py                     ← CLI entry point
└── requirements.txt
```

---

## Design decisions

See [`docs/design.md`](docs/design.md) for:
- Why YAML as the knowledge format
- Why Markdown output (not TestRail/Xray)
- Why `claude-sonnet-4-6` over Haiku or Opus
- System vs user prompt split rationale
- Prompt engineering notes (what worked, what didn't)

---

## Roadmap

| Version | Feature |
|---------|---------|
| **v0.1** ✅ | Knowledge file → manual test scenarios (Generator Agent) |
| **v0.1.1** ✅ | Reviewer Agent — two-agent generate → review pipeline |
| v0.2 | Crawler add-on — enrich knowledge with live DOM before generation |
| v0.3 | Code generation — output Java Page Objects + RestAssured API clients |
| v0.4 | Test management export — Xray JSON, TestRail CSV |

---

## Requirements

- Python 3.11+
- `anthropic>=0.40`, `pyyaml>=6.0`
- Anthropic API key (`ANTHROPIC_API_KEY`)
