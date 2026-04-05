"""
prompts.py — Prompt templates for the AutoPilot QA generator and reviewer agents.

DESIGN DECISION: Why prompts live in their own module
─────────────────────────────────────────────────────
Prompts are first-class artifacts in an AI-native tool — as important as the
code that calls them. Keeping them in a dedicated module means:
  - They can be versioned, reviewed, and improved independently of the plumbing
  - Engineers building on top of AutoPilot QA can swap or extend them without
    touching generator logic
  - They serve as living documentation of what we're asking the model to do

DESIGN DECISION: System prompt vs user prompt split
────────────────────────────────────────────────────
  System prompt  → defines the *role*, *output format*, and *quality bar*
                   (stable across all apps)
  User prompt    → injects the *application-specific context*
                   (changes per knowledge file)

This split matters because:
  1. Claude respects system-level instructions strongly — putting format rules
     there prevents the model from wandering to prose when you want tables.
  2. The user prompt stays clean: just the app context, not mixed with format
     directives. Easier to debug when something looks wrong.

DESIGN DECISION: Ask for a fixed output template per scenario
──────────────────────────────────────────────────────────────
We specify a precise markdown template (TC-number, table of steps, etc.) rather
than letting Claude choose its own structure. This is intentional:
  - Consistent format = parseable downstream (future: ingest into TestRail, Xray)
  - Testers can scan the output predictably
  - The template makes Claude's job clearer → fewer hallucinated or vague steps

The trade-off: we lose some of Claude's natural flow. We accept that because
machine-readable > aesthetically varied for a test management tool.

DESIGN DECISION: Two-agent pattern (generator + reviewer)
──────────────────────────────────────────────────────────
We use two separate Claude calls rather than asking one call to both generate
AND review. The reasons:

  1. Different cognitive modes. Generating scenarios requires optimising for
     breadth — covering all the things. Reviewing requires optimising for
     coherence — do these scenarios form a strategy that reflects how a real
     user experiences the app? Asking the same call to do both means it does
     neither as well as it could.

  2. The critic pattern. A classic LLM technique: generate a draft, then ask
     a separate call to critique it with fresh eyes and explicit review criteria.
     The reviewer's system prompt puts it in a different mental frame (QA lead
     reading a junior's work) than the generator (junior writing from scratch).

  3. The reviewer can catch what the generator normalises. The generator is
     immersed in the app structure — it can miss "obvious" cross-cutting concerns.
     The reviewer reads the draft as an outsider with a checklist.

  Why not a self-critique loop?
  We could ask the generator to review its own output. We don't because the
  generator has already "committed" to a framing. A new call with a reviewer
  system prompt produces meaningfully different scrutiny.
"""

# ─────────────────────────────────────────────────────────────────────────────
# SYSTEM PROMPT
# Tells Claude what role to play and exactly how to format every scenario.
# Keep this stable — change it only when the output format needs to change.
# ─────────────────────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are a senior QA engineer with deep experience in manual test design for web and API applications. You are meticulous, domain-aware, and write test scenarios that are immediately executable by another tester who has never seen the app before.

## Your task
Given an Application Knowledge document, generate comprehensive manual test scenarios. These scenarios will be used by:
  - Human testers for exploratory and structured testing
  - Automation engineers as the specification for test automation

## Coverage requirements
Produce scenarios in this order:
  1. SMOKE (2–4 scenarios) — the bare minimum that must pass before anything else runs
  2. REGRESSION (6–10 scenarios) — core user journeys and happy paths in full detail
  3. EDGE CASES (4–6 scenarios) — boundary conditions, optional fields, unusual but valid inputs
  4. NEGATIVE (4–6 scenarios) — invalid inputs, missing required fields, broken flows
  5. API (4–6 scenarios) — direct API contract tests (request/response shape, error codes)

Skip any category that the knowledge file explicitly excludes.

## Output format — follow this template EXACTLY for every scenario

---

## TC-{number}: {concise title}
**Priority**: {High / Medium / Low} — {Smoke / Regression / Edge Case / Negative / API}
**Type**: {UI | API | E2E}
**Component**: {page name or API resource}

### Preconditions
- {bullet — what must be true before this test starts}

### Steps
| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | {precise user action} | {observable outcome} |

### Test Data
- {field}: {value}

### Notes
> {warnings, timing quirks, known gotchas from the app notes — omit this section entirely if empty}

---

## Rules for writing good steps
- Every action must be precise: include button label, field name, URL, or value
- Every expected result must be observable: what the tester sees, hears, or can measure
- Use real test data values from the knowledge file — never say "enter valid data"
- Reference actual API paths when relevant (e.g. "POST /api/lists is called")
- Prefer "Verify {thing} is displayed" over "Check that it works"
- For async operations (AI calls, network requests), add an explicit wait step
- Number TC IDs sequentially starting at TC-001

## What NOT to do
- Do not generate scenarios for features explicitly listed in the exclusions section
- Do not invent fields, flows, or behaviours not described in the knowledge file
- Do not merge multiple independent behaviours into one scenario
- Do not produce vague steps like "fill in the form" — spell out each field
"""


# ─────────────────────────────────────────────────────────────────────────────
# USER PROMPT BUILDER
# Converts the parsed knowledge YAML into structured context for Claude.
# We render it as plain text (not raw YAML) because:
#   - Plain text is more token-efficient for the parts Claude actually needs
#   - We can highlight what matters (journeys, notes) over boilerplate schema fields
#   - Easier to read during prompt debugging
# ─────────────────────────────────────────────────────────────────────────────
def build_user_prompt(knowledge: dict) -> str:
    """Render an app-knowledge dict into the user prompt for Claude."""
    lines = []

    # ── Application overview ──────────────────────────────────────────────────
    app = knowledge.get("application", {})
    lines += [
        "# Application Knowledge Document",
        "",
        f"**App name**: {app.get('name', 'Unknown')}",
        f"**Type**: {app.get('type', 'web')}",
        f"**Base URL**: {app.get('base_url', '')}",
        f"**Description**: {app.get('description', '')}",
    ]
    if app.get("swagger_url"):
        lines.append(f"**Swagger / OpenAPI**: {app['swagger_url']}")
    lines.append("")

    # ── Authentication ────────────────────────────────────────────────────────
    auth = knowledge.get("authentication", {})
    auth_type = auth.get("type", "none")
    lines += ["## Authentication", f"- Type: {auth_type}"]
    if auth_type != "none":
        if auth.get("login_url"):
            lines.append(f"- Login URL: {auth['login_url']}")
        if auth.get("credentials_env"):
            for role, env_var in auth["credentials_env"].items():
                lines.append(f"- {role}: read from env var `{env_var}`")
    lines.append("")

    # ── Domain entities ───────────────────────────────────────────────────────
    domain = knowledge.get("domain", {})
    if domain:
        lines += ["## Domain", f"{domain.get('description', '')}", ""]
        for entity in domain.get("entities", []):
            lines.append(f"### Entity: {entity['name']}")
            lines.append(f"  {entity.get('description', '')}")
            if entity.get("key_fields"):
                lines.append(f"  Key fields: {', '.join(entity['key_fields'])}")
            if entity.get("valid_states"):
                for field, states in entity["valid_states"].items():
                    lines.append(f"  Valid values for `{field}`: {', '.join(str(s) for s in states)}")
            lines.append("")

    # ── Pages / Routes ────────────────────────────────────────────────────────
    routes = knowledge.get("routes", [])
    if routes:
        lines += ["## Pages & Routes", ""]
        for route in routes:
            lines.append(f"- **{route['path']}** — {route.get('title', '')}")
            lines.append(f"  {route.get('description', '')}")
        lines.append("")

    # ── API Endpoints ─────────────────────────────────────────────────────────
    endpoints = knowledge.get("api_endpoints", [])
    if endpoints:
        lines += ["## API Endpoints", ""]
        for ep in endpoints:
            lines.append(f"- **{ep['method']} {ep['path']}**")
            lines.append(f"  {ep.get('description', '')}")
            if ep.get("request_body"):
                body_fields = ", ".join(
                    f"{k}: {v}" for k, v in ep["request_body"].items()
                )
                lines.append(f"  Request body: {{ {body_fields} }}")
            if ep.get("query_params"):
                params = ", ".join(
                    f"{k}: {v}" for k, v in ep["query_params"].items()
                )
                lines.append(f"  Query params: {params}")
            if ep.get("response"):
                lines.append(f"  Response: {ep['response']}")
        lines.append("")

    # ── User Journeys ─────────────────────────────────────────────────────────
    journeys = knowledge.get("journeys", [])
    if journeys:
        lines += ["## User Journeys", ""]
        for journey in journeys:
            priority_tag = f"[{journey.get('priority', 'regression').upper()}]"
            lines.append(f"### {priority_tag} {journey['name']}")
            lines.append(journey.get("description", ""))
            for i, step in enumerate(journey.get("steps", []), 1):
                lines.append(f"  {i}. {step}")
            lines.append("")

    # ── Test Data ─────────────────────────────────────────────────────────────
    test_data = knowledge.get("test_data", {})
    if test_data:
        lines += ["## Test Data", ""]
        for env in test_data.get("environments", []):
            lines.append(
                f"- Environment `{env['name']}`: {env.get('base_url', '')} "
                f"(data reset: {env.get('data_reset', False)})"
            )
        sample = test_data.get("sample_inputs", {})
        if sample:
            lines.append("")
            lines.append("Sample inputs available for test data:")
            for key, values in sample.items():
                lines.append(f"  - {key}: {values}")
        lines.append("")

    # ── Exclusions ────────────────────────────────────────────────────────────
    exclusions = knowledge.get("exclusions", [])
    if exclusions:
        lines += ["## Exclusions (DO NOT generate tests for these)", ""]
        for exc in exclusions:
            lines.append(f"- {exc}")
        lines.append("")

    # ── App Notes ─────────────────────────────────────────────────────────────
    notes = knowledge.get("notes", [])
    if notes:
        lines += ["## App Notes (important for test design)", ""]
        for note in notes:
            lines.append(f"- {note}")
        lines.append("")

    # ── Generation instruction ────────────────────────────────────────────────
    lines += [
        "---",
        "",
        "Using the application knowledge above, generate manual test scenarios covering:",
        "Smoke, Regression, Edge Cases, Negative, and API categories.",
        "Follow the output format template exactly. Use real values from the test data section.",
        "Do not generate scenarios for excluded areas.",
    ]

    return "\n".join(lines)


# ═════════════════════════════════════════════════════════════════════════════
# REVIEWER AGENT PROMPTS
# ─────────────────────────────────────────────────────────────────────────────
# The reviewer reads the knowledge file + draft scenarios and produces a
# finalized set. It operates with two explicit lenses:
#
#   WORKFLOW LENS   — thinks like a real user, not a feature checklist.
#                     Asks: can a user actually complete their goal? What
#                     breaks at handoffs between pages and API calls?
#
#   COVERAGE LENS   — audits the draft against the knowledge file.
#                     Every route, every API endpoint, every entity state,
#                     every app note should surface in at least one scenario.
#
# DESIGN DECISION: Critic-then-rewrite, not annotate-and-patch
# ──────────────────────────────────────────────────────────────
# The reviewer produces (1) a Review Summary of what it found, then (2) the
# complete finalized scenario set — not a patch or annotation on the draft.
#
# Why rewrite rather than patch?
#   - Patches (e.g., "add step 3a to TC-007") require the reader to mentally
#     merge the patch with the original. A complete rewrite is always readable.
#   - When the reviewer reorders, renumbers, and rewrites, it signals clearly
#     that the output IS the source of truth — not "the draft + some changes."
#   - For tutorial purposes, seeing the before (draft) vs after (final) as two
#     complete files is more instructive than a diff.
# ═════════════════════════════════════════════════════════════════════════════

REVIEWER_SYSTEM_PROMPT = """You are a principal QA engineer and test strategy lead. You are reviewing a draft set of manual test scenarios written by another engineer.

Your job is not to rubber-stamp the draft — it is to make it genuinely better. You approach this review with two explicit lenses:

## WORKFLOW LENS — think like a real user
Before reading a single scenario, mentally walk through the full application as a user would:
- What does a brand-new user do first? Can they complete that journey end-to-end?
- What are all the meaningful state transitions? (e.g., empty list → list with items → shared list)
- Where are the handoffs between pages? Between UI actions and API calls?
- What happens when async operations (AI calls, network requests) are slow or fail?
- What does the app look like in empty states? What does it look like with data?
- What can a user undo or redo? What is irreversible?
- Are there implicit journeys the knowledge file doesn't name but the app clearly supports?

## COVERAGE LENS — audit against the knowledge file
Run through this checklist systematically:
- Every page/route has at least one scenario (even read-only pages)
- Every API endpoint is exercised — either through the UI or directly
- Every domain entity's key fields are tested (required vs optional behaviour differs)
- Every `valid_states` value for every entity appears in at least one scenario
- Every note in the "App Notes" section informs at least one scenario
- No scenario tests anything listed in the "Exclusions" section
- Smoke scenarios are minimal — only the absolute critical path (not every happy path)
- Negative scenarios test actual error conditions, not just "leave field blank"

## Your review process
1. Study the application knowledge document
2. Walk through user workflows with the WORKFLOW LENS
3. Run the COVERAGE LENS checklist against the draft
4. Enumerate your findings: what is missing, what is vague, what is wrong, what is redundant
5. Write the finalized scenario set

## Output format
Produce two sections in order:

### Section 1 — Review Summary
Use this structure:
```
## Review Summary

### Added
- TC-NNN: {title} — {one-line reason}

### Improved
- TC-NNN (was TC-NNN): {title} — {what was wrong and what changed}

### Removed
- {original TC-NNN}: {title} — {reason: duplicate / excluded feature / incorrect}

### Coverage gaps closed
- {description of what workflow or knowledge-file item was not covered in the draft}
```

### Section 2 — Finalized Test Scenarios
The complete scenario set, renumbered from TC-001. Use the EXACT same per-scenario template as the draft:

---

## TC-{number}: {concise title}
**Priority**: {High / Medium / Low} — {Smoke / Regression / Edge Case / Negative / API}
**Type**: {UI | API | E2E}
**Component**: {page name or API resource}

### Preconditions
- {bullet}

### Steps
| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | {precise action} | {observable outcome} |

### Test Data
- {field}: {value}

### Notes
> {warnings, timing quirks, gotchas — omit section entirely if empty}

---

## Rules you must follow
- Every step action must name the specific button, field, URL, or value
- Every expected result must be something a tester can observe (visible on screen, in a URL, in a response)
- Never say "enter valid data" — use specific values from the test data section
- For async operations add an explicit "Wait for X to appear" step with a time budget
- Keep Smoke to ≤4 scenarios — if it's not broken-build-blocking, it's not Smoke
- Do not invent behaviour not described in the knowledge file
- Do not test anything in the Exclusions section
"""


def build_reviewer_prompt(knowledge: dict, draft_content: str) -> str:
    """Build the reviewer user prompt from the knowledge file and draft scenarios.

    The reviewer gets both the original ground truth (knowledge file) AND the
    draft to review. It needs both because:
      - The knowledge file is the specification (what the app IS SUPPOSED to do)
      - The draft is the work product to critique (what the generator SAID about it)
    The reviewer cross-references between the two.
    """
    from .prompts import build_user_prompt  # reuse the same context renderer

    knowledge_context = build_user_prompt(knowledge)

    # Replace the generation instruction at the bottom with a review instruction
    knowledge_context = knowledge_context.rsplit("---", 1)[0].rstrip()

    return "\n".join([
        "# PART 1 — Application Knowledge Document (ground truth)",
        "",
        knowledge_context,
        "",
        "---",
        "",
        "# PART 2 — Draft Test Scenarios (to review)",
        "",
        draft_content,
        "",
        "---",
        "",
        "Review the draft test scenarios above against the application knowledge document.",
        "Apply both the WORKFLOW LENS and the COVERAGE LENS.",
        "Output the Review Summary first, then the complete finalized scenario set.",
        "Renumber all scenarios starting from TC-001 in the finalized set.",
    ])
