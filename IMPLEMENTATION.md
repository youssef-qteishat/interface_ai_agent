# Interface.ai Computer-Use Take-Home: Implementation Plan

## Recommended direction

Build a **small local “Credit Union Operations Simulator” plus a computer-use automation service**, not a generic browser agent and not a polished SaaS product. The strongest submission is one deep vertical slice:

> Given `member_id` and `account_type`, find the member, begin opening a sub-account, and stop at the review/confirmation screen; return the normalized review details.

The simulator should intentionally resemble a hostile legacy application: server-rendered pages, an iframe, nested tables, duplicated labels, generated element IDs, inconsistent load times, and no test IDs. Add deterministic fault-injection controls for member-not-found, validation failure, session expiry, permission denial, transient slowness, an unexpected dialog, and a forced human intervention.

Use a **hybrid discovery strategy**: let the LLM observe screenshots plus a compact accessibility/visible-control snapshot and request UI actions, while the automation layer executes and records them. On successful discovery, canonicalize the run into a typed capability containing parameter placeholders, locator bundles, conditions, outputs, policy metadata, and checkpoints. Replay uses only that artifact and the surface adapter—never the LLM.

A practical default stack is:

- Python 3.12
- FastAPI for the orchestrator, mock bank application, and minimal operator endpoints
- Playwright for browser control, browser-session ownership, screenshots, traces, and deterministic replay
- Pydantic v2 for discriminated-union models and JSON Schema generation
- Anthropic or OpenAI computer-use/tool-calling API behind a small provider interface
- SQLite only for simulator data and run metadata; JSON/YAML files for reviewable capability artifacts and evidence
- Jinja/HTMX or plain HTML for the simulator and operator page; avoid building a React application unless it materially improves the handoff demo
- Pytest for schema, policy, locator-resolution, replay, and error-taxonomy tests

This fits the evaluator’s emphasis: artifact design, deterministic behavior, runtime outcomes, control transfer, and clear trade-offs—not infrastructure breadth.

## Why this approach

Playwright is suitable for the implemented web surface without forcing the design to become DOM-specific. Its locators support accessibility roles, labels, text, and other strategies, and locator actions include retryability and actionability checks such as visibility, stability, event reception, and enabled state. Playwright can also represent accessibility structure as YAML ARIA snapshots, including roles, names, attributes, values, and hierarchy.[^1][^2][^3]

For discovery, both major computer-use patterns place the model in an observe/action loop while the application owns execution. Anthropic describes an agent loop in which the application receives tool requests, performs actions, captures results, and sends them back; OpenAI similarly describes screenshot-driven browser or desktop control with application-enforced action handling and verification. That separation supports a clean `ModelProvider` boundary while keeping policy enforcement under local control.[^4][^5]

Pydantic can generate JSON Schema from typed models, while JSON Schema provides a declarative way to validate document structure, constraints, and data types. Use semantic versioning for capability contracts: incompatible contract changes increment major, backward-compatible additions increment minor, and compatible fixes increment patch.[^6][^7][^8]

## Concepts to research

### Priority 0: before coding

| Concept | What to understand | Concrete design decision |
|---|---|---|
| Observe–decide–act loops | Tool calls, screenshots, state summaries, completion criteria, max steps, timeout, cancellation, repeated-state detection | Define one structured `AgentAction` union and explicit stopping rules before integrating a model |
| Computer-use threat model | Prompt injection from page content, confused-deputy behavior, secret transmission, risky actions, domain/action restrictions | Treat all rendered content as untrusted; enforce policy outside the prompt; require approval for consequential actions. OpenAI explicitly recommends isolation, site/action allowlists, confirmation for consequential actions, bounded runs, and outcome verification.[^4] |
| Surface abstraction | Separate semantic intent from browser-specific execution | Define `SurfaceAdapter.observe`, `act`, `resolve_target`, `capture_evidence`, `pause`, and `resume` |
| Capability/artifact design | Typed parameters, outputs, action union, target identity, checkpoints, conditions, outcomes, policy, compatibility, provenance | Write the Pydantic models and two hand-authored example artifacts before building the recorder |
| Locator robustness | Accessibility role/name, label, text-with-context, frame path, CSS/XPath fallback, uniqueness, fingerprints, coordinates | Store an ordered locator bundle, not one selector; coordinates are discovery evidence only |
| Deterministic replay | State machine, explicit waits, pre/postconditions, idempotency, retry boundaries, output validation | Every step gets preconditions, action, postconditions, timeout, and retry policy |
| Error taxonomy | Business outcome vs recoverable condition vs hard technical/policy failure | Model results as a tagged union; never use one generic exception for all failures |
| Human control transfer | Session identity, lease/owner, pause barrier, same-session takeover, resume token, audit trail | Implement `AUTOMATION → HUMAN_PENDING → HUMAN → AUTOMATION` transitions with compare-and-set ownership |

### Priority 1: during implementation

| Concept | What to research or prototype | Why it matters |
|---|---|---|
| Playwright BrowserContext lifecycle | Context/page ownership, traces, frames, popup handling, non-persistent sessions | Browser contexts provide independent sessions, and non-persistent contexts do not write browsing data to disk.[^9] Keeping the context alive is the basis of same-session handoff |
| Accessibility-tree observation | Roles, accessible names, states, ARIA snapshots, limitations on poorly accessible pages | It supplies compact semantic observations but must fall back to screenshots/coordinates when controls are inaccessible |
| Frame and legacy-page handling | Frame paths, nested frame resolution, table-relative anchors | Playwright’s `FrameLocator` encapsulates entering an iframe and locating controls within it.[^10] |
| Wait semantics | Auto-waiting, explicit state assertions, navigation/load ambiguity, backoff with jitter | Prevent arbitrary sleeps and distinguish slow success from timeout |
| Evidence design | JSONL event log, screenshot policy, trace retention, redacted snapshots, correlation IDs | Playwright traces can preserve actions and be inspected after a run; its tracing APIs save trace files for Trace Viewer.[^11][^12] |
| Sensitive-data redaction | Field classification, deny-by-default logging, hashing identifiers, screenshot risks, retention | OWASP recommends removing, masking, sanitizing, hashing, or encrypting session identifiers and personal data rather than recording them directly.[^13] |
| Structured model output | JSON/tool schemas, validation and repair, invalid-action handling, provider portability | The model may propose actions, but only schema-valid and policy-valid actions should execute |
| Replay idempotency | Safe restart points, commit boundaries, duplicate-submit avoidance | Especially important once a flow approaches irreversible actions |

### Priority 2: write-up only

- Windows UI Automation and desktop accessibility. Microsoft UI Automation provides programmatic access to desktop controls and is usable by automated tests, making it a credible future `DesktopSurfaceAdapter` backend.[^14]
- Visual anchoring, OCR, template matching, and coordinate transforms for controls unavailable through accessibility APIs.
- Tenant/vendor inheritance: base capability, variant profiles, tenant overrides, compatibility fingerprints, rollout rings, and override promotion.
- Capability lifecycle: draft, reviewed, approved, deprecated, rollback, replay-health score.
- Distributed session workers, queues, encrypted evidence storage, and remote operator streaming. Explain these seams but do not implement them.

## Application to build

### Credit Union Ops Simulator

Build one local app with four screens:

1. **Member Search** — accepts a member ID.
2. **Member Detail** — shows identity summary and account table.
3. **Open Sub-Account Form** — selects account type, funding source, and opening amount.
4. **Review Screen** — shows normalized values and a final `Open Account` button.

The automated goal stops when the review checkpoint is verified. The final commit button is visibly present but classified `irreversible`; discovery and replay must refuse to click it unless a human-approved policy token is supplied. This demonstrates consequential-action reasoning without creating a second full workflow.

Make the surface intentionally difficult but fair:

- Put the account workflow inside an iframe.
- Use server-generated IDs that change on restart.
- Use table layouts and duplicated text such as multiple “Continue” controls.
- Keep accessible names on most controls so semantic targeting can succeed.
- Include one icon-only or poorly exposed control to exercise screenshot/coordinate discovery.
- Add a tenant theme flag that changes branding and one label while preserving workflow semantics.
- Provide a fault selector through environment variables or a development-only route, never through the automation artifact.

### Runs to demonstrate

| Run | Inputs/fault | Expected result | What it proves |
|---|---|---|---|
| Discovery | Valid member, no fault | `success`, saved capability, review outputs | A genuine LLM-driven UI run and artifact generation |
| Replay happy path | Different valid member | `success`, validated outputs, no model calls | Parameterization and deterministic replay |
| Replay business outcome | Unknown member | `business_outcome: MEMBER_NOT_FOUND` | Legitimate domain result is not treated as a crash |
| Replay recovery | One injected timeout/interstitial | Retry/dismiss, then `success` | Bounded recoverability and evidence |
| Replay escalation | Unknown dialog or policy-gated step | `escalated`, same session taken by human, resumed | Real control transfer and preserved context |
| Optional variant | Tenant B theme/label | Success via locator fallback or explicit override | Cross-tenant reuse without building multi-tenant infrastructure |

The first four are essential. The optional variant is the best stretch goal because it directly addresses a central business constraint rather than adding an unrelated feature.

## System architecture

Use a modular monolith with these boundaries:

```text
CLI / FastAPI
    |
Run Orchestrator ---- Intervention Service / Operator Page
    |
    +---- Discovery Controller ---- ModelProvider
    |
    +---- Replay Engine
    |
Policy Engine ---- Capability Registry ---- Evidence Store
    |
SurfaceAdapter
    +---- PlaywrightWebAdapter       (implemented)
    +---- DesktopUIAAdapter          (interface/design only)
    +---- VisualDesktopAdapter       (interface/design only)
```

### Core modules

- **Run orchestrator:** owns run lifecycle, limits, cancellation, session ID, and control ownership.
- **Discovery controller:** obtains observations, asks the model for a typed action, validates it, executes it, and detects completion or stuck states.
- **Recorder/canonicalizer:** converts executed actions into a clean artifact; raw model chain-of-thought or transcript is not the artifact.
- **Replay engine:** interprets only artifact steps, resolves targets, checks pre/postconditions, extracts outputs, and emits a tagged result.
- **Policy engine:** authorizes target URLs, routes, action types, data classes, and risk levels before every action in both modes.
- **Surface adapter:** hides browser/desktop mechanics behind observations, targets, actions, and evidence primitives.
- **Session/control manager:** keeps the live Playwright context and page, enforces one control owner, pauses automation, and resumes it.
- **Evidence store:** writes redacted JSONL logs plus trace/screenshot files under `/evidence/<run_id>/`.

Avoid microservices, Redis, Kubernetes, a durable queue, or a full capability marketplace. A modular monolith makes ownership and failure semantics visible without spending the take-home on deployment plumbing.

## Artifact schema

The artifact should be declarative enough to review yet operational enough to replay. Keep **schema version** separate from **capability version**: the first versions the artifact format; the second versions the public input/output behavior of one capability.

```yaml
schema_version: "1.0.0"
capability:
  id: "corebank.open_subaccount_review"
  version: "1.0.0"
  title: "Prepare a new sub-account for review"
  risk: "reversible"
  approval_state: "draft"
compatibility:
  app_family: "credit-union-ops-sim"
  app_versions: [">=1.0,<2.0"]
  variant: "base"
  surface_kinds: ["web"]
contract:
  inputs:
    member_id: {type: "string", pattern: "^[0-9]{5}$", sensitive: true}
    account_type: {type: "enum", values: ["savings", "checking"]}
    opening_amount: {type: "decimal", minimum: 0}
  outputs:
    member_ref: {type: "string", sensitive: true}
    review:
      type: "object"
      properties:
        account_type: {type: "string"}
        opening_amount: {type: "decimal"}
  result_variants: ["success", "business_outcome", "failure", "escalated"]
policy:
  allowed_origins: ["http://bank-sim:8001"]
  allowed_actions: ["navigate", "click", "fill", "select", "read", "wait"]
  forbidden_actions: ["download", "upload", "submit_irreversible"]
steps:
  - id: "search-member"
    preconditions:
      - {kind: "url", matches: "/members/search"}
    action:
      kind: "fill"
      value: "${inputs.member_id}"
      target:
        frame_path: []
        candidates:
          - {kind: "role", role: "textbox", name: "Member ID"}
          - {kind: "label", text: "Member ID"}
          - {kind: "contextual_text", anchor: "Member Search", relative: "input"}
    postconditions:
      - {kind: "value", equals: "${inputs.member_id}"}
    timeout_ms: 5000
    retry: {max_attempts: 2, backoff_ms: 250}
  - id: "verify-review"
    action: {kind: "read", target: {ref: "review-panel"}}
    checkpoint:
      all:
        - {kind: "heading", text: "Review New Account"}
        - {kind: "text", contains: "${inputs.account_type}"}
outcome_rules:
  - when: {kind: "text", matches: "Member not found"}
    return: {status: "business_outcome", code: "MEMBER_NOT_FOUND"}
  - when: {kind: "dialog", matches: "Session expired"}
    recover: {strategy: "reauth_or_escalate", max_attempts: 1}
outputs:
  member_ref: {extract: {target_ref: "member-number", transform: "mask_member_id"}}
  review: {extract: {target_ref: "review-panel", schema_ref: "#/contract/outputs/review"}}
provenance:
  discovered_at: "<timestamp>"
  model_provider: "<provider/model>"
  source_run_id: "<run-id>"
  content_hash: "<hash>"
```

### Locator strategy

Store an ordered set of locator candidates with scope and evidence:

1. Role + accessible name.
2. Label + control type.
3. Stable visible text plus a semantic ancestor/region.
4. Frame path plus one of the above.
5. Attribute/CSS selector only when an attribute is shown stable across runs.
6. Visual anchor or coordinates only as an explicitly lower-confidence adapter-specific fallback.

Playwright recommends user-facing locators such as roles, labels, text, placeholders, and test IDs, while warning that CSS/XPath can be used when necessary. Because this mock intentionally has no test IDs, the artifact should prioritize semantic locators and record uniqueness checks, expected role/text, and a lightweight element fingerprint. Never replay the raw discovery coordinate without first resolving and validating the target.[^2]

### Result contract

Use a discriminated union:

```json
{"status":"success","outputs":{},"run_id":"..."}
{"status":"business_outcome","code":"MEMBER_NOT_FOUND","details":{},"run_id":"..."}
{"status":"failure","code":"TARGET_NOT_RESOLVED","step_id":"...","expected":{},"observed":{},"evidence":[],"run_id":"..."}
{"status":"escalated","intervention_id":"...","reason":"UNKNOWN_DIALOG","step_id":"...","run_id":"..."}
```

Do not report validation errors, absent members, permission denial, and selector failures through the same exception path. The caller needs stable machine-readable semantics.

## Discovery and recording

### Agent loop

1. Create an isolated browser context and start tracing.
2. Validate the target against the allowlist.
3. Capture screenshot, URL/title, dialogs, and compact accessibility/visible-control state.
4. Ask the model for exactly one typed action or a terminal declaration: `act`, `goal_complete`, `business_outcome`, `request_human`, or `cannot_proceed`.
5. Validate schema, policy, target origin, risk, and current control owner.
6. Execute through `SurfaceAdapter`; never let generated code run directly.
7. Capture before/after evidence and derive locator candidates from the actual hit element.
8. Detect loops using repeated observation hashes, repeated failed targets, no-progress count, max steps, and wall-clock timeout.
9. On success, canonicalize placeholders and outputs, validate the capability against its schema, and save it as `draft`.

The prompt may ask for a short action rationale, but the durable log should record only a concise operational reason and observable state—not hidden reasoning. This keeps the artifact decoupled from provider-specific transcripts.

### Canonicalization

- Replace literal member IDs and amounts with `${inputs.*}` references.
- Convert a raw click point into the resolved element’s semantic locator bundle.
- Remove redundant navigation and waits.
- Add postconditions from observed state changes.
- Convert known error screens into outcome/recovery rules.
- Mark data fields with sensitivity classes.
- Validate that every declared input is used and every output extractor matches its schema.
- Reject artifacts containing credentials, cookies, authorization headers, or unredacted sensitive values.

## Replay and errors

Implement replay as a state-machine interpreter, not generated Playwright code. For each step:

1. Verify policy and control ownership.
2. Evaluate preconditions.
3. Scan for global exception states such as session expiry or dialogs.
4. Resolve locator candidates in priority order.
5. Require uniqueness and verify the expected element signature.
6. Execute with bounded timeout and retry rules.
7. Evaluate postconditions/checkpoints.
8. Extract and type-check outputs.
9. Emit one structured event per transition.

Prefer condition-based waits over `sleep`. Playwright already performs actionability checks and fails with a timeout if they are not met, which can be mapped into a recoverable or hard replay condition.[^3]

### Error matrix

| Condition | Classification | Behavior |
|---|---|---|
| Member ID absent | Business outcome | Return `MEMBER_NOT_FOUND`; do not retry |
| Invalid amount | Business outcome | Return `VALIDATION_REJECTED` with redacted field-level codes |
| Known loading overlay | Recoverable | Wait with bounded exponential backoff |
| Known informational dialog | Recoverable | Dismiss only if policy permits, then retry current step |
| Session expired | Recoverable/escalate | Attempt one safe reauthentication hook without storing credentials; otherwise escalate |
| Permission denied | Business outcome or failure | Use `NOT_AUTHORIZED` if this is an expected domain response; otherwise configuration failure |
| Locator has zero matches | Hard failure after fallbacks | Return `TARGET_NOT_RESOLVED` with candidate diagnostics |
| Locator has multiple matches | Hard failure | Return `TARGET_AMBIGUOUS`; never pick the first silently |
| Checkpoint missing | Hard failure | Return `CHECKPOINT_FAILED` with expected/observed evidence |
| Unknown modal | Escalation | Pause and transfer control |
| Irreversible submit | Policy escalation | Require an approval token or human action |

## Human handoff

Use a small, real ownership model:

```text
AUTOMATION --blocked--> HUMAN_PENDING --accepted--> HUMAN
HUMAN --resume--> AUTOMATION
HUMAN --complete--> COMPLETED
any owner --cancel--> CANCELLED
```

Each transition requires the current `control_version`; update it atomically to prevent both human and automation acting simultaneously. The automation loop waits on an async pause barrier before every action.

For the implementation, launch a headed Playwright browser and keep the same `BrowserContext` and `Page` alive. The operator page lists the intervention, reason, step, screenshot, and buttons for `Accept control`, `Resume automation`, `Mark complete`, and `Cancel`. After acceptance, the operator interacts directly with the already-open headed browser window; this is the same live session, not a recreated one.

Capture manual activity by injecting a narrowly scoped event observer into the local simulator that records click targets, input-field names with redacted values, navigation, and timestamps. Label these events `actor: human`. Do not attempt a production co-browsing product. In the report, explain that production would stream a containerized display through a remote-view protocol; noVNC is an HTML VNC client/application and is a reasonable example of that future operator seam.[^15]

## Safety model

Enforce safety in code before every action in discovery, replay, and human-resume transitions:

- **Origin/route allowlist:** exact origins and route patterns; reject popups or navigation outside them.
- **Action allowlist:** only recognized typed actions; no shell, arbitrary JavaScript, file upload, download, clipboard, or new-window actions by default.
- **Risk classes:** `read_only`, `reversible`, `consequential`, `irreversible`.
- **Approval tokens:** short-lived and scoped to run, capability, step, action digest, and operator identity.
- **Secret references:** artifacts may contain `${secret_ref.bank_user}` but never the secret value.
- **Redaction at source:** redact before serialization, not in a later cleanup pass.
- **Screenshot discipline:** capture failure/transition evidence, blur or mask known sensitive regions, and use fake data only.
- **Prompt-injection defense:** rendered instructions cannot change the goal, policy, permissions, or allowed tools.
- **Bounded execution:** max steps, timeout, cost budget, retry count, cancellation, and loop detection.
- **Checkpoint verification:** trust observed postconditions, not the model’s claim that a task succeeded.

## Tests and evidence

Prioritize tests where judgment lives:

- Artifact schema accepts valid capabilities and rejects unknown action kinds, missing outputs, unbound parameters, and secrets.
- Policy blocks disallowed origins, routes, action kinds, popups, and irreversible actions without approval.
- Locator resolver selects unique semantic candidates, falls back deliberately, and fails on ambiguity.
- Replay returns each tagged result variant correctly.
- Recovery retries only permitted steps and respects limits.
- Control ownership prevents automation actions while a human owns the session.
- Redactor masks member IDs, amounts if configured, cookies, tokens, and input values.
- One integration test exercises discovery with a fake model; the committed `/evidence/` run must use a real model.

Suggested evidence tree:

```text
evidence/
  discovery-success/
    capability.yaml
    events.redacted.jsonl
    final.png
    trace.zip
    run-summary.json
  replay-success/
    events.redacted.jsonl
    final.png
    trace.zip
    result.json
  replay-member-not-found/
    events.redacted.jsonl
    failure-or-outcome.png
    result.json
  handoff-unknown-dialog/
    intervention.json
    events.redacted.jsonl
    before-human.png
    after-human.png
    result.json
```

## Repository layout

```text
README.md
REPORT.md
pyproject.toml
.env.example
src/
  cli.py
  api.py
  domain/
    artifact.py
    actions.py
    results.py
    interventions.py
  discovery/
    controller.py
    model_provider.py
    recorder.py
    canonicalizer.py
  replay/
    engine.py
    locator_resolver.py
    conditions.py
    recovery.py
  surfaces/
    base.py
    playwright_web.py
  policy/
    engine.py
    redaction.py
  sessions/
    manager.py
    ownership.py
  evidence/
    writer.py
  operator/
    routes.py
    templates/
apps/
  bank_sim/
    server.py
    templates/
    static/
tests/
artifacts/
evidence/
```

Keep `README.md` operational: setup, environment variables, exact discovery command, exact replay command, fault-injection command, operator-handoff steps, test command, and a “works without live model” path using a fake provider plus committed evidence.

## Development timeline

A realistic time box is **24 calendar days, 60–80 focused hours**, assuming roughly 15–20 hours per week and AI-assisted scaffolding. Stop at the review gate if a phase exceeds its budget; preserve the seam and document the cut rather than omitting a required capability.

| Days | Hours | Deliverable | Exit criterion |
|---|---:|---|---|
| 1–2 | 5–7 | Scope, architecture sketch, artifact/result models | Two example artifacts validate; decisions recorded |
| 3–5 | 8–10 | Legacy-style bank simulator and fault injection | Primary flow works manually; all fault states are reproducible |
| 6–8 | 8–11 | Playwright surface adapter and real model loop | One real LLM run reaches the review screen under limits |
| 9–11 | 8–10 | Recorder and canonicalizer | Successful run emits a readable, parameterized artifact |
| 12–14 | 8–11 | Deterministic replay and output extraction | New valid input replays with zero model calls |
| 15–16 | 5–7 | Error taxonomy and recovery | Not-found plus one recoverable and one hard condition produce correct contracts |
| 17–18 | 5–7 | Policy and redaction | Disallowed/risky actions are blocked; committed logs contain no raw sensitive values |
| 19–20 | 6–8 | Human escalation and same-session resume | Human accepts control, acts in the same session, resumes, and actions are audited |
| 21–22 | 4–6 | Tests and evidence capture | Core schema/policy/replay/control tests pass; evidence folders are complete |
| 23 | 3–5 | README and seven-heading REPORT | Reviewer can reproduce discovery/replay from exact commands |
| 24 | 2–4 | Clean-machine rehearsal and buffer | Fresh clone demo succeeds; public repo contains no secrets |

### Scope checkpoints

- **End of day 8:** If model integration is unstable, constrain the action vocabulary and observation size; do not add another app.
- **End of day 14:** If replay is not reliable, stop UI polish and deepen locator/checkpoint/error behavior.
- **End of day 20:** If operator UI is delayed, use the headed-browser handoff plus a plain server-rendered control page; preserve the real ownership mechanism.
- **End of day 22:** Freeze features. Spend the remaining time on evidence, documentation, and a clean-clone rehearsal.

## Cuts and stretch goal

Explicitly cut:

- Real bank integrations and real PII
- Native desktop implementation
- Production remote desktop/co-browsing
- Distributed workers, queues, autoscaling, and multi-region persistence
- Full authentication/RBAC system
- Automatic general-purpose self-healing
- Multiple unrelated capabilities
- Polished operator UX

If the core is solid by day 21, implement **one stretch goal only**: a second tenant variant with different branding, one renamed field, and one wrapper-frame change. Reuse the base capability through an explicit variant override and report which locator candidates succeeded. This directly demonstrates the intended app-family/tenant model and is more valuable than code generation or a decorative capability catalog.

## Decisions to defend

Be prepared to explain these choices in the review:

- Why discovery may use visual/model reasoning while replay forbids model decisions.
- Why coordinates are useful evidence but poor primary replay targets.
- Why a locator bundle and element signature are safer than silently selecting the first match.
- Why business outcomes are first-class result variants rather than exceptions.
- Why recovery is bounded and step-specific.
- Why safety checks execute outside the model prompt.
- Why session ownership is a state machine with an atomic lease/version.
- Why schema and capability versions are separate.
- Why a modular monolith is enough for the take-home while surface, provider, policy, and storage interfaces preserve future scale.
- Why the primary automated flow stops at review and treats final commit as a human-approved boundary.

The highest-value demonstration is not that the model can click through a form. It is that a real model-discovered run becomes a reviewable contract, that contract replays without model judgment, exceptional states have explicit semantics, and a human can safely take and return control of the exact live session.

---

## References

1. [Snapshot testing | Playwright Python](https://playwright.dev/python/docs/aria-snapshots) - Overview

2. [Locators | Playwright Python](https://playwright.dev/python/docs/locators) - Introduction

3. [Auto-waiting | Playwright Python](https://playwright.dev/python/docs/actionability) - Introduction

4. [Computer use | OpenAI API](https://developers.openai.com/api/docs/guides/tools-computer-use) - Computer use lets a model operate browser and desktop interfaces. Use it to fill out forms, test use...

5. [Computer use tool - Claude Platform Docs](https://docs.anthropic.com/en/docs/agents-and-tools/computer-use) - Claude can interact with computer environments through the computer use tool, which provides screens...

6. [JSON Schema](https://pydantic.dev/docs/validation/dev/concepts/json_schema/)

7. [Semantic Versioning 2.0.0 | Semantic Versioning](https://semver.org/) - Semantic Versioning spec and website

8. [JSON Schema](https://json-schema.org/docs) - JSON Schema - a declarative language for validating JSON documents

9. [BrowserContext | Playwright Python](https://playwright.dev/python/docs/api/class-browsercontext) - BrowserContexts provide a way to operate multiple independent browser sessions.

10. [FrameLocator | Playwright Python](https://playwright.dev/python/docs/api/class-framelocator) - FrameLocator represents a view to the iframe on the page. It captures the logic sufficient to retrie...

11. [Trace viewer - Playwright](https://playwright.dev/docs/trace-viewer) - Introduction

12. [Tracing](https://playwright.dev/docs/api/class-tracing) - API for collecting and saving Playwright traces. Playwright traces can be opened in Trace Viewer aft...

13. [Logging Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html) - Never log data unless it is legally sanctioned. For example, intercepting some communications, monit...

14. [UI Automation Overview - Win32 apps](https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-uiautomationoverview) - Microsoft UI Automation is an accessibility framework for Windows.

15. [noVNC: HTML VNC client library and application](https://github.com/novnc/novnc) - noVNC is both a HTML VNC client JavaScript library and an application built on top of that library. ...

