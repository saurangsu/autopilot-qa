# AutoPilot QA — Design Document

> This doc records the architecture decisions, prompt engineering choices, and
> tool selections made while building AutoPilot QA. It's written for people
> following along as a tutorial, not just as internal reference.

---

## What is AutoPilot QA?

AutoPilot QA is an AI-native test automation framework. The core idea: instead of
writing test scenarios by hand or recording clicks in a tool, you describe your
application in a structured YAML file (the "Application Knowledge File") and let
an AI agent generate the test scenarios for you.

**v0.1 scope:** Knowledge file → Manual test scenarios (Markdown)
**v0.1.1:** + Reviewer Agent — two-agent generate → review pipeline
**v0.2 (this version):** + Code Generator Agent — Playwright TypeScript + RestAssured Java output
**Roadmap:** + Crawler → + Change-aware test regeneration

---

## Architecture (v0.2 — four-agent pipeline)

```
┌─────────────────────────────────────┐
│       app-knowledge.yaml            │  ← Human-authored ground truth
│   (routes, APIs, journeys, data)    │
└───────────────┬─────────────────────┘
                │
                ▼
┌─────────────────────────────────────┐
│      Generator Agent                │  autopilot_qa/generator.py
│                                     │
│  1. load_knowledge()                │  Parse YAML
│  2. build_user_prompt()             │  Render context for Claude
│  3. generate_scenarios()            │  Claude API call (streaming)
│  4. save_output()                   │  Write draft markdown
└───────────────┬─────────────────────┘
                │
                ▼
┌─────────────────────────────────────┐
│   output/draft-scenarios.md         │  ← Draft (breadth-first, not yet reviewed)
└───────────────┬─────────────────────┘
                │  (knowledge + draft both fed in)
                ▼
┌─────────────────────────────────────┐
│      Reviewer Agent                 │  autopilot_qa/reviewer.py
│                                     │
│  WORKFLOW LENS: thinks like a user  │  Are full journeys testable end-to-end?
│  COVERAGE LENS: audits vs knowledge │  Every route / API / entity / note covered?
│                                     │
│  Outputs: Review Summary + Final    │
└───────────────┬─────────────────────┘
                │
                ▼
┌─────────────────────────────────────┐
│  output/test-scenarios-final.md     │  ← Human-readable deliverable
│  (Review Summary + TC-001 … TC-N)   │     also input to Code Generator
└───────────────┬─────────────────────┘
                │  (finalized scenarios only — not the knowledge file)
                ▼
┌─────────────────────────────────────┐
│      Code Generator Agent           │  autopilot_qa/code_generator.py
│                                     │
│  Call 1: Playwright TypeScript      │  All UI/E2E categories, one spec per category
│          File markers split output  │  // ===FILE: smoke.spec.ts===
│                                     │
│  Call 2: RestAssured Java           │  API scenarios → ApiTests.java
└───────────────┬─────────────────────┘
                │
                ▼
┌──────────────────────────────────────────────────┐
│  output/playwright/smoke.spec.ts                 │
│  output/playwright/regression.spec.ts            │  ← Executable Playwright tests
│  output/playwright/edge-cases.spec.ts            │
│  output/playwright/negative.spec.ts              │
│  output/restassured/ApiTests.java                │  ← Executable RestAssured tests
└──────────────────────────────────────────────────┘
```

**CLI modes:**
```bash
# Generate only
python run.py knowledge/app-knowledge.yaml

# Two-agent pipeline (generate + review)
python run.py knowledge/app-knowledge.yaml --review

# Review an existing draft without regenerating
python run.py knowledge/app-knowledge.yaml --review-only output/draft-scenarios.md

# Full pipeline including code generation (generate + review + codegen)
python run.py knowledge/app-knowledge.yaml --codegen

# Code generation only (from existing finalized scenarios)
python run.py --codegen-only output/test-scenarios-final.md

# Full four-agent pipeline (build knowledge + generate + review + codegen)
python run.py --build-knowledge knowledge-config.yaml --codegen
```

---

## ADR-001: Application Knowledge File as the single input

**Decision:** The only required input is an `app-knowledge.yaml` file. No live
app access, no browser, no OpenAPI spec required.

**Why:**
- Most teams can produce a YAML description of their app in 30 minutes; they
  cannot always give a crawler access to a staging environment.
- A human-authored knowledge file captures *intent* (what the app is supposed
  to do) not just structure (what HTML elements exist). That intent is exactly
  what good test scenarios need.
- The file is version-controllable, reviewable, and becomes a living document
  alongside the test suite.

**Trade-off:** The quality of generated tests is bounded by the quality of the
knowledge file. Garbage in, garbage out. The crawler add-on (v0.2) addresses
this by grounding the knowledge file in actual DOM structure.

---

## ADR-002: Output format is Markdown, not a test management tool format

**Decision:** Generate human-readable Markdown, not Xray JSON, TestRail CSV, or
any other tool-specific format.

**Why:**
- Markdown is readable without any tool. A tester with no access to TestRail
  can still execute the scenarios.
- It can be committed to source control alongside the knowledge file, making
  the generated scenarios part of the repo's history.
- It's trivially parseable downstream — a second agent can convert Markdown
  tables to any target format. Start generic, specialise later.

**Trade-off:** Can't one-click import into TestRail. That's a v0.3 problem.

---

## ADR-003: claude-sonnet-4-6 as the generation model

**Decision:** Use `claude-sonnet-4-6` (not Haiku, not Opus).

**Why:**
- **vs Haiku**: Haiku is fast and cheap but struggles with strict template
  adherence when the template is complex (step tables + multiple sections per
  scenario × 25 scenarios). We observed more format drift with Haiku.
- **vs Opus**: Opus produces marginally richer scenarios (better edge case
  intuition) but at ~5x the cost and ~2x the latency. For a tool that runs on
  every sprint cycle, sonnet-4-6 is the pragmatic choice.
- Sonnet's instruction-following is strong enough to maintain the TC-number
  template consistently across 25+ scenarios in one call.

**How to change it:** Pass `--model claude-opus-4-6` to `run.py`.

---

## ADR-004: System prompt vs user prompt split

**Decision:** Put format rules and role definition in the *system* prompt; put
all application-specific context in the *user* prompt.

**Why:**

The Anthropic API gives system and user prompts different weight in Claude's
attention. System-level instructions act like standing orders — Claude treats
them as constraints rather than suggestions. By putting the output template
(TC-number format, step table, Notes section) in the system prompt, we get
more reliable template adherence than if we embedded them in the user message.

The user prompt stays clean: just the app context. This is easier to debug.
When a scenario looks wrong, you can usually tell whether the issue is in the
format rules (system prompt) or the app context (user prompt).

**The prompts are in `autopilot_qa/prompts.py`** — they're first-class artifacts,
versioned alongside the code.

---

## Prompt Engineering Notes

### System prompt design choices

**Role framing:**
> "You are a senior QA engineer with deep experience in manual test design..."

Explicitly asking Claude to adopt the role of a senior QA engineer (not just
"an AI assistant") meaningfully improves the domain-specificity of the output.
Claude generates "verify the share URL is copied to clipboard" rather than
"check that the feature works."

**Coverage specification:**
We explicitly list the 5 scenario categories (Smoke, Regression, Edge Case,
Negative, API) with approximate counts. Without this, Claude over-indexes on
happy-path regression tests and under-generates negative/edge scenarios.

**Output template with a worked format:**
We provide a complete template showing every section header and the exact
markdown table format. This is necessary because:
1. Claude will vary the format slightly on each call if not constrained
2. Consistent format = parseable by downstream tools
3. It removes ambiguity: "what goes in Notes vs Steps" is answered by example

**Negative rules ("What NOT to do"):**
Telling Claude what *not* to do is as important as what to do. Without explicit
exclusion rules, Claude sometimes generates tests for features listed in the
`exclusions` section of the knowledge file, or writes vague steps like "fill in
the form."

### User prompt design choices

**Render YAML as structured text, not raw YAML:**
We convert the YAML to a human-readable text format in `build_user_prompt()`.
Raw YAML uses more tokens on repetitive schema structure (indentation, dashes,
colons) and Claude has to parse it before reasoning about it. Plain text is
more token-efficient and removes that parsing step.

**Section ordering: app overview → domain → pages → APIs → journeys → data → exclusions → notes:**
This mirrors how a QA engineer would read an app spec — high-level first, then
the details that inform test data and edge cases. The journeys section is
deliberately placed *after* the structural sections so Claude has full context
when it reads them.

**Explicit generation instruction at the end:**
The user prompt ends with:
> "Using the application knowledge above, generate manual test scenarios
>  covering Smoke, Regression, Edge Cases, Negative, and API categories."

This closing instruction signals clearly that the context section is over and
the task begins. Without it, Claude occasionally continues summarising the app
rather than generating scenarios.

---

## ADR-005: Two-agent pattern (generator + reviewer)

**Decision:** Use two separate Claude calls — a Generator Agent and a Reviewer
Agent — rather than one call that both generates and reviews.

**Why:**

The two agents are optimising for different things:

| Agent | Optimises for | Mental frame |
|-------|--------------|--------------|
| Generator | **Breadth** — cover all routes, APIs, entities | Junior engineer writing from a spec |
| Reviewer | **Coherence** — do these scenarios form a real test strategy? | Principal QA lead reading a colleague's work |

Asking one call to do both means it does neither fully. The generator is
immersed in the structure; the reviewer needs to step back and ask "can a user
actually accomplish their goal with these scenarios?"

**The critic pattern:**
Generate a draft, then critique it. A classic LLM technique that works because:
1. The reviewer reads the draft as an outsider — it hasn't "committed" to the
   generator's framing.
2. The reviewer system prompt puts Claude in a different role: it's looking for
   problems, not producing content.
3. Different prompts surface different blind spots.

**Why not a self-critique loop (generate → self-review → regenerate)?**
We could ask the generator to review its own output. In practice, the model
has already normalised its own choices and tends to validate them. A separate
call with an explicit reviewer persona produces meaningfully more critical
scrutiny, especially for workflow coherence and cross-page handoffs.

**Critic-then-rewrite, not annotate-and-patch:**
The reviewer outputs the complete finalized scenario set (renumbered from TC-001),
not a patch on the draft. This is intentional — the final file is always
self-contained and readable without referencing the draft.

---

## Reviewer Prompt Engineering Notes

### The two lenses

The reviewer system prompt introduces two explicit cognitive modes:

**WORKFLOW LENS:**
> "Walk through the full application as a user would... what are the meaningful
> state transitions? What happens at handoffs between pages and API calls?"

This surfaces scenarios the generator misses because it works feature-by-feature.
The reviewer asks: "is there a test for the list in empty state?" or "what
happens after the user shares a list and then adds an item to it?" — cross-cutting
concerns that don't map cleanly to a single route or API endpoint.

**COVERAGE LENS:**
A checklist the reviewer runs against the draft:
- Every route has at least one scenario
- Every API endpoint is exercised
- Every entity's `valid_states` values appear in at least one scenario
- Every note in the knowledge file informs a scenario
- No scenarios test excluded features
- Smoke is ≤4 scenarios (broken-build-blocking only)

The two lenses are explicit in the prompt rather than implicit because Claude
responds better to structured thinking modes than to vague instructions like
"review for quality."

### Review Summary first, then finalized scenarios

The prompt requires the reviewer to enumerate its findings *before* writing the
final scenarios:
```
## Review Summary
### Added / Improved / Removed / Coverage gaps closed
```

This serves two purposes:
1. Forces structured thinking before rewriting — the reviewer can't skip the
   audit by going straight to output.
2. Makes the output educational. Engineers reading the final file can see exactly
   what changed from the draft and why.

### Giving the reviewer both the knowledge file and the draft

The reviewer user prompt has two parts:
```
PART 1 — Application Knowledge Document (ground truth)
PART 2 — Draft Test Scenarios (to review)
```

The reviewer cross-references between them. It needs the knowledge file because
the draft might be correct but incomplete, or it might contain scenarios that
contradict the documented app behaviour. The knowledge file is the source of
truth; the draft is the thing being evaluated against it.

---

---

## ADR-009: One Claude call per output format (Playwright vs RestAssured)

**Decision:** Make exactly two Claude calls for code generation — one for all
Playwright TypeScript files and one for the RestAssured Java class. Not one call
per test case, not one call per category, not one combined call for both languages.

**Why:**

TypeScript and Java have very different structural conventions (test runner APIs,
import styles, class vs module layout, assertion libraries). A single call asked
to produce both would need to context-switch between paradigms mid-response and
typically produces lower-quality output in both. Two focused calls, each with a
role-specific system prompt ("senior Playwright engineer" vs "senior Java automation
engineer"), produce cleaner, more idiomatic code.

One call per test case would scale linearly with test count — a 30-scenario suite
would require 30 API calls. One pass for all categories is an explicit performance
constraint enforced in `strategy_v02.md`.

**Trade-off:** A single combined call would be simpler to orchestrate. We accept
the extra call because code quality is more important than call count here.

---

## ADR-010: File markers for multi-file Playwright output

**Decision:** Ask Claude to delimit each spec file with a `// ===FILE: filename.spec.ts===`
marker in its response; parse and split client-side.

**Why:**

We want one `.spec.ts` per test category (smoke, regression, edge-cases, negative)
but making one API call per category would multiply cost linearly. The file marker
pattern gives us multi-file output from a single call.

The marker format (`// ===FILE:` ... `===`) is a TypeScript comment — syntactically
valid even if not split correctly, which means the fallback (write everything to
`tests.spec.ts`) still produces runnable code rather than broken files.

The parser lives in `_split_playwright_files()` in `code_generator.py`. It falls
back to a single `tests.spec.ts` if no markers are found, so no content is ever
silently dropped if Claude doesn't follow the format.

---

## ADR-011: Finalized scenarios Markdown as the code generator's input (not the AKF)

**Decision:** The Code Generator Agent takes `output/test-scenarios-final.md` as
its input — not `app-knowledge.yaml`.

**Why:**

The finalized scenarios are the human-readable specification for what the tests
should do. If a scenario is written correctly (precise steps, observable results,
real test data), the code agent can implement it mechanically without re-reading
the knowledge file.

This decoupling means:
1. The code agent can run independently on **any** well-formed scenarios file —
   including hand-written ones, not just AutoPilot-generated ones (`--codegen-only`).
2. The knowledge file is not re-parsed at this stage — each agent in the pipeline
   has a single, clear input.
3. The scenarios file is already the Reviewer Agent's polished output — it's the
   closest thing to a human-authored spec that the pipeline produces.

**Trade-off:** If the scenarios file omits detail that was in the AKF (e.g. a
specific error code), the generated code won't know about it. The scenarios file
must be complete for code quality to be high — which is exactly what the Reviewer
Agent is designed to ensure.

---

## Code Generator Prompt Engineering Notes

### Playwright system prompt design

**Role framing:** "Senior automation engineer specialising in Playwright with TypeScript"
gives Claude a precise persona that strongly influences idiom choices (e.g.
`page.locator()` over `page.$()`, `expect().toBeVisible()` over manual assertions).

**`BASE_URL` env var pattern:**
```typescript
const BASE_URL = process.env.BASE_URL ?? 'http://localhost:3000';
```
Explicitly specified in the prompt so every generated spec is environment-portable
without manual editing after generation.

**TC-ID as test name:**
```typescript
test('TC-001: Create a new wish list', async ({ page }) => {
```
Embedding the TC-ID in the test name creates a direct traceability link between
the Markdown scenario and the executable test. When a test fails in CI, the TC-ID
in the failure message maps directly back to the scenario document.

**"Do not mock network calls"** is explicit in the prompt because Claude has a
strong prior toward mocking in generated tests. We want tests against the real app.

### RestAssured system prompt design

**`@BeforeAll` base URI pattern:**
```java
RestAssured.baseURI = System.getProperty("base.url", "http://localhost:3000");
```
System property allows CI to override the URL without code changes.

**`@DisplayName` with TC-ID:**
```java
@DisplayName("TC-015: POST /api/lists returns 201 with valid payload")
```
Same traceability rationale as Playwright — test runner output maps to the scenario doc.

**"No Spring Boot context, no Mockito"** prevents Claude from generating heavyweight
test infrastructure. These are integration tests; all they need is RestAssured + JUnit5.

---

## Tools Used

| Tool | Version | Purpose |
|------|---------|---------|
| Python | 3.11+ | Orchestration language |
| `anthropic` SDK | >=0.40 | Claude API client — streaming, typed |
| `pyyaml` | >=6.0 | YAML parsing for knowledge files |
| Claude Sonnet 4.6 | - | All four agents (Knowledge Builder, Generator, Reviewer, Code Generator) |

**Generated artifacts (not runtime dependencies — output only):**
| Output format | Runtime requirements |
|--------------|---------------------|
| Playwright TypeScript (`.spec.ts`) | `@playwright/test` npm package, Node.js |
| RestAssured Java (`ApiTests.java`) | RestAssured + JUnit 5, Java 11+, Maven/Gradle |

**Future add-ons (not yet wired):**
| Tool | Purpose |
|------|---------|
| `playwright` | Browser crawler for live app DOM capture (v0.3) |
| `beautifulsoup4` | HTML element extraction (v0.3) |
| `jsonschema` | Knowledge file schema validation |

---

## ADR-006: Knowledge Builder — config file as single control point

**Decision:** The Knowledge Builder is driven by a `knowledge-config.yaml` file
(the "single control point"), not by CLI flags.

**Why:**

The builder takes up to three sources: a human prompt, a source repo, and
multiple documentation files. That's too many inputs to express cleanly as CLI
flags. A config file is:
- **Version-controllable** — you can see exactly what inputs produced a given
  knowledge file, and re-run it identically.
- **Shareable** — a team can agree on the config and each member regenerates
  locally.
- **Readable** — enabling/disabling sources is a one-line `enabled: true/false`,
  not a flag combinatoric.

The `knowledge-config.yaml` is gitignored (contains local paths and personal
descriptions). The `knowledge-config.example.yaml` is committed — same pattern
as `.env` / `.env.example`.

---

## ADR-007: Source trust hierarchy

**Decision:** When sources conflict, Claude prefers them in this order:
1. Human prompt (highest trust)
2. Source code (ground truth)
3. Documentation (supplementary, may be outdated)

**Why:**

The human prompt captures *intent* — what the app is supposed to do, including
known quirks and things not visible in code. Source code is the authoritative
ground truth for what actually exists. Documentation is often the least reliable
(can lag behind the code by months).

This hierarchy is stated explicitly in the Knowledge Builder system prompt so
Claude applies it when sources conflict — e.g., if the README says "no auth"
but the source code has a login route, Claude flags it rather than silently
picking one.

---

## ADR-008: Repo file prioritisation

**Decision:** When reading a source repo, files are sorted before reading:
API routes → page/view components → lib/model/db files → everything else.

**Why:**

Repos can have hundreds of files. The `max_files` cap (default: 40) means we
must choose which files make it into the context window. API routes and page
components contain the most test-relevant information (endpoints, routes, request
shapes, UI interactions). Sorting by relevance ensures these always make it in,
even for large monorepos.

This is implemented in `knowledge_builder.py` via the `priority()` function that
scores file paths by keyword matching.

---

## Running AutoPilot QA

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Set your Anthropic API key
export ANTHROPIC_API_KEY=sk-ant-...

# 3. Copy the config template
cp knowledge-config.example.yaml knowledge-config.yaml
# Edit knowledge-config.yaml — enable your sources, fill in paths/descriptions

# 4a. Full three-agent pipeline (recommended)
python run.py --build-knowledge knowledge-config.yaml --review
# → knowledge/app-knowledge.yaml   (Knowledge Builder output)
# → output/draft-scenarios.md      (Generator output)
# → output/test-scenarios-final.md (Reviewer output — use this)

# 4b. Build knowledge only
python run.py --build-knowledge knowledge-config.yaml

# 4c. Skip knowledge building (use existing knowledge file)
python run.py knowledge/app-knowledge.yaml --review
```

---

## What's Next (Roadmap)

### v0.2 ✅ — Code Generator Agent (SHIPPED 2026-04-17)
Added `autopilot_qa/code_generator.py` as the fourth pipeline step. Generates
Playwright TypeScript spec files and a RestAssured Java test class from the
Reviewer Agent's finalized scenarios. See ADR-009 through ADR-011.

### v0.3 — Crawler Add-on (AKF enrichment)
Add an optional crawler step before the Generator Agent that inspects the live
DOM of the application under test and enriches the `app-knowledge.yaml` with
discovered routes, form fields, and API endpoints. The crawler output is additive
— the AKF remains the source of truth.

CLI target: `python run.py --crawl http://localhost:3000 knowledge/app-knowledge.yaml --codegen`

### v0.4 — Change-Aware Test Regeneration *(key strategic differentiator)*
When the application changes, supply a diff or changelog alongside the AKF.
AutoPilot QA identifies which parts of the AKF are affected and regenerates
**only** the impacted scenarios and automation scripts — not the full suite.

Outputs: `output/delta-scenarios.md`, `output/delta-playwright/`, `output/change-impact-report.md`

This transforms AutoPilot QA from a one-shot generator into a living test system
that evolves with the application.

### v0.5 — Test Management Export
Convert generated scenarios to Xray JSON, TestRail CSV, or Zephyr Scale JSON
for one-click import into popular test management tools.
