# Canonicalizer, Versioning and Replay — Implementation Plan

The layer after discovery. Turns the captured run into a reviewable contract, then executes that
contract with no model in the loop.

Companion to `discovery-loop-plan.md` (Steps 1–14, complete). Where this document and
`IMPLEMENTATION.md` disagree, this one is right: it is written against the trace that actually exists.

---

## 0. What this layer inherits

### The captured run, exactly as it is

`evidence/discovery-success/` — a real Opus 5 run, `CHECKPOINT_VERIFIED`, 21 steps, $0.84, fault
profile `dialog`, two human interventions:

| #      | actor      | action               | target                     | probe name / nearby label                |
| ------ | ---------- | -------------------- | -------------------------- | ---------------------------------------- |
| 0      | automation | `left_click`         | (549, 96)                  | `None` / `'Member ID'`                   |
| 1      | automation | `type`               | `${inputs.member_id}`      | —                                        |
| 2      | automation | `left_click`         | (1069, 96)                 | `'Search'`                               |
| 3–4    | automation | `wait`, `screenshot` | —                          | —                                        |
| 5      | automation | `left_click`         | (500, 148)                 | `'${inputs.member_id}'` (the result row) |
| 6–7    | automation | `wait`, `screenshot` | —                          | —                                        |
| 8      | automation | `left_click`         | (756, 215)                 | `'Open Sub-Account'`                     |
| 9–10   | automation | `wait`, `screenshot` | —                          | —                                        |
| 11     | automation | `left_click`         | (738, 124)                 | `'$0.00'` / `'Opening Amount'`           |
| 12     | automation | `type`               | `${inputs.opening_amount}` | —                                        |
| 13     | automation | `left_click`         | (694, 212)                 | `'Continue'` → **rejected by the app**   |
| 14–15  | automation | `wait`, `screenshot` | —                          | —                                        |
| **16** | **human**  | `human_intervention` | `MODEL_REQUESTED`          | **no observable change**                 |
| 17     | automation | `left_click`         | (694, 212)                 | `'Continue'` → succeeds, reveals a modal |
| 18–19  | automation | `wait`, `screenshot` | —                          | —                                        |
| **20** | **human**  | `human_intervention` | `UNKNOWN_DIALOG`           | `removed_text: ["⚠ System Notice …"]`    |

**Only seven of the twenty-one steps carry workflow meaning.** Everything else is `wait`,
`screenshot`, a rejected attempt, or a human.

### Three things this trace makes hard, and they are the interesting part

1. **Two human steps that are not the same kind of thing.**
   - Step 20 is a **recoverable condition**: the diff shows exactly what left the screen, so it
     becomes an `outcome_rules` entry — dismiss a known dialog and retry.
   - Step 16 is a **missing action**. The operator ticked the disclosure checkbox; ticking a checkbox
     changes no visible text, so `dom_changed: false` and both diffs are empty. The canonicalizer
     cannot see what happened. An artifact that silently omits it replays straight into the same
     validation error the model hit.

2. **A rejected attempt and its retry.** Step 13's `new_text` is
   `["You must accept the account disclosure to continue."]`; step 17 repeats the identical click and
   its `removed_text` clears that message. The artifact needs **one** Continue step, and the
   validation text is the evidence that names the missing precondition from (1).

3. **A locator that looks authoritative and identifies nothing.** Step 11's accessible name is
   `'$0.00'` with `accessible_name_source: placeholder` — Chromium computed it from placeholder text.
   It would match every empty currency field on the page. The ladder must demote it below CSS.

### What already exists and is reused unchanged

| Component                 | Reused for                                                                                                                                       |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| `src/domain/results.py`   | `TARGET_NOT_RESOLVED`, `TARGET_AMBIGUOUS`, `CHECKPOINT_FAILED` are already defined — "mostly replay-time, defined here so the taxonomy is whole" |
| `src/domain/trace.py`     | The canonicalizer's input; `LocatorCandidate` is already the six-variant union                                                                   |
| `src/policy/engine.py`    | `check_action` / `check_target` run in replay too, unchanged                                                                                     |
| `src/policy/redaction.py` | Already produced `${inputs.*}` in the trace — parameterization is **done**                                                                       |
| `src/sessions/`           | Ownership and the pause barrier work for replay escalation with no changes                                                                       |
| `src/evidence/writer.py`  | Replay writes the same run folders; `assert_recordable` needs a replay-mode sibling                                                              |

**Parameterization is already solved.** Redaction replaced literals with `${inputs.*}` at discovery
time, so the canonicalizer does not need to guess which `12345` was a member id.

---

## 1. Scope

**In:** the artifact schema and its models; the canonicalizer; gap detection; minimal versioning; a
Playwright web adapter; locator resolution; the replay state machine; recovery and outcome rules;
replay-time policy; a `replay` CLI; four demonstration runs and their evidence.

**Out:** a desktop replay adapter (design only, per `interface-ai-plan-v2.md`), a capability registry
or marketplace, `approval_state` lifecycle machinery, tenant inheritance, self-healing locators, and
code generation. Replay is an **interpreter**, never generated Playwright code — an interpreter can be
told to stop.

---

## 2. Decisions locked before coding

| Decision                    | Choice                                                                                                       | Why                                                                                                                                                                                                                                                |
| --------------------------- | ------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Replay substrate            | **Playwright on the host**, hitting `127.0.0.1:8001`, headless by default and headed for the escalation demo | Auto-waiting and actionability checks _are_ the determinism argument; rebuilding them on CDP is most of the work. It also makes the headline contrast real: discovery drives pixels in a contained OS-level driver, replay drives roles and labels |
| The sandbox in replay       | **Not used.** Replay needs no Xvfb, VNC, xdotool or the surface agent                                        | The sandbox exists to contain a _model_. Replay has no model to contain                                                                                                                                                                            |
| Step 16's gap               | **Detect, report, refuse to claim replayable**                                                               | Silently dropping a required step produces an artifact that looks complete and is not — the one failure mode that destroys the artifact's value                                                                                                    |
| Coordinates in the artifact | **Never a primary locator.** Kept under `evidence:` only                                                     | The whole point of canonicalization                                                                                                                                                                                                                |
| Versioning                  | `schema_version` + `capability.version`, one enforcement rule, nothing else                                  | §4                                                                                                                                                                                                                                                 |
| Replay result               | The existing `RunResult` union, unchanged                                                                    | Discovery and replay returning different shapes would make the contract a lie                                                                                                                                                                      |

---

## 3. The artifact contract

Derived from the trace above, not from a sketch — and this is the exact text of
`tests/fixtures/capability_authored.yaml`, which validates against the Step 1 models. The two
`authored_by: human` steps are the gaps, written in by hand; `tests/fixtures/capability_draft.yaml`
is the same file without them and with both gaps open.

```yaml
schema_version: 1.0.0
capability:
  id: corebank.open_subaccount_review
  version: 1.0.0
  title: Prepare a new sub-account for review
  risk: reversible
contract:
  inputs:
    member_id:
      type: string
      pattern: ^[0-9]{5}$
      sensitive: true
    account_type:
      type: enum
      values:
        - savings
        - checking
      sensitive: false
    opening_amount:
      type: decimal
      minimum: 0.0
      sensitive: false
  outputs:
    review:
      type: object
      properties:
        member:
          type: string
          sensitive: true
        account_type:
          type: string
        opening_amount:
          type: decimal
        funding_source:
          type: string
          sensitive: true
      extract:
        scope:
          kind: css
          selector: "#review-container table.review-table"
          stability: unverified
        fields:
          member:
            row_label: "Member:"
            transform: mask_member_id
          account_type:
            row_label: "Account Type:"
          opening_amount:
            row_label: "Opening Amount:"
            transform: strip_currency
          funding_source:
            row_label: "Funding Source:"
policy:
  allowed_origins:
    - http://bank-sim:8001
  forbidden_actions:
    - submit_irreversible
    - navigate_external
    - download
    - upload
entry:
  url: http://bank-sim:8001/
  frame_path:
    - servicing-frame
  expect_url_contains: /servicing/members/search
steps:
  - id: fill-member-id
    action:
      kind: fill
      value: ${inputs.member_id}
      target:
        frame_path:
          - servicing-frame
        candidates:
          - match_count: 1
            kind: contextual_text
            anchor: Member ID
            relative: input
          - match_count: 1
            kind: attribute
            selector: input[name="member_id"]
            attribute: name
            value: member_id
          - match_count: 1
            kind: css
            selector: table.form-table > tbody > tr > td:nth-of-type(2) > input
            stability: unverified
        evidence:
          discovery_coordinate:
            - 549
            - 96
          expected_tag: input
          expected_role: textbox
          rejected:
            - kind: dom_id
              value: inp_7676796a
              reason: "dom_id_stability: generated — a new id is issued on every request"
    preconditions: []
    postconditions:
      - kind: value
        equals: ${inputs.member_id}
    timeout_ms: 5000
  - id: submit-search
    action:
      kind: click
      target:
        frame_path:
          - servicing-frame
        candidates:
          - match_count: 1
            kind: role
            role: button
            name: Search
            name_source: visible_text
          - match_count: 1
            kind: contextual_text
            anchor: Member ID
            relative: button
          - match_count: 1
            kind: text
            tag: button
            text: Search
          - match_count: 1
            kind: css
            selector: table.form-table > tbody > tr > td:nth-of-type(3) > button
            stability: unverified
        evidence:
          discovery_coordinate:
            - 1069
            - 96
          expected_tag: button
          expected_role: button
          rejected: []
    preconditions: []
    postconditions:
      - any_of:
          - Member
          - No members found
        kind: text
    timeout_ms: 5000
  - id: open-member
    action:
      kind: click
      target:
        frame_path:
          - servicing-frame
        candidates:
          - match_count: 1
            kind: css
            selector: "#results-panel table.data-table > tbody > tr.member-row"
            stability: unverified
        evidence:
          discovery_coordinate:
            - 500
            - 148
          expected_tag: tr
          expected_role: cell
          rejected:
            - kind: role
              value: cell/${inputs.member_id}
              reason: "match_count: null — not counted, so it cannot be relied on to be unique"
    preconditions: []
    postconditions:
      - contains: /servicing/members/
        any_of: []
        kind: url
      - contains: Member Detail
        any_of: []
        kind: heading
    timeout_ms: 5000
  - id: start-sub-account
    action:
      kind: click
      target:
        frame_path:
          - servicing-frame
        candidates:
          - match_count: 1
            kind: role
            role: button
            name: Open Sub-Account
            name_source: visible_text
          - match_count: 1
            kind: text
            tag: button
            text: Open Sub-Account
          - match_count: 1
            kind: css
            selector: a > button.action-button
            stability: unverified
        evidence:
          discovery_coordinate:
            - 756
            - 215
          expected_tag: button
          expected_role: button
          rejected: []
    preconditions: []
    postconditions:
      - contains: /servicing/accounts/open
        any_of: []
        kind: url
      - contains: Open Sub-Account
        any_of: []
        kind: heading
    timeout_ms: 5000
  - id: select-account-type
    action:
      kind: select
      value: ${inputs.account_type}
      target:
        frame_path:
          - servicing-frame
        candidates:
          - match_count: 1
            kind: attribute
            selector: select[name="account_type"]
            attribute: name
            value: account_type
          - match_count: 1
            kind: contextual_text
            anchor: Account Type
            relative: select
    preconditions: []
    postconditions: []
    timeout_ms: 5000
    authored_by: human
  - id: fill-opening-amount
    action:
      kind: fill
      value: ${inputs.opening_amount}
      target:
        frame_path:
          - servicing-frame
        candidates:
          - match_count: 1
            kind: contextual_text
            anchor: Opening Amount
            relative: input
          - match_count: 1
            kind: attribute
            selector: input[name="opening_amount"]
            attribute: name
            value: opening_amount
          - match_count: 1
            kind: css
            selector:
              "#open-account-form table.form-table > tbody > tr:nth-of-type(2) > td:nth-of-type(2)
              > input"
            stability: unverified
        evidence:
          discovery_coordinate:
            - 738
            - 124
          expected_tag: input
          expected_role: textbox
          rejected:
            - kind: role
              value: textbox/$0.00
              reason:
                "accessible_name_source: placeholder — the name is a formatting hint, not an identity,
                and matches any element displaying the same one"
            - kind: dom_id
              value: amt_d955786b
              reason: "dom_id_stability: generated — a new id is issued on every request"
    preconditions: []
    postconditions:
      - kind: value
        equals: ${inputs.opening_amount}
    timeout_ms: 5000
  - id: accept-disclosure
    action:
      kind: check
      target:
        frame_path:
          - servicing-frame
        candidates:
          - match_count: 1
            kind: attribute
            selector: input[name="disclosure_accepted"]
            attribute: name
            value: disclosure_accepted
          - match_count: 1
            kind: label
            text: I have reviewed the account disclosure and agree to the terms.
            control: checkbox
    preconditions: []
    postconditions:
      - kind: checked
        equals: true
    timeout_ms: 5000
    authored_by: human
  - id: continue-to-review
    action:
      kind: click
      target:
        frame_path:
          - servicing-frame
        candidates:
          - match_count: 1
            kind: role
            role: button
            name: Continue
            name_source: visible_text
          - match_count: 1
            kind: contextual_text
            anchor: Back
            relative: button
          - match_count: 1
            kind: text
            tag: button
            text: Continue
          - match_count: 1
            kind: css
            selector:
              "#open-account-form table.form-table > tbody > tr:nth-of-type(5) > td.right-align:nth-of-type(2)
              > button.action-button"
            stability: unverified
        evidence:
          discovery_coordinate:
            - 694
            - 212
          expected_tag: button
          expected_role: button
          rejected: []
    preconditions: []
    postconditions:
      - contains: Review New Account
        any_of: []
        kind: heading
      - contains: You must accept the account disclosure
        any_of: []
        kind: text_absent
    timeout_ms: 5000
    retry:
      max_attempts: 1
      backoff_ms: 250
  - id: verify-review
    action:
      kind: read
    preconditions: []
    postconditions: []
    checkpoint:
      all:
        - contains: Review New Account
          any_of: []
          kind: heading
        - contains: ${inputs.account_type}
          any_of: []
          kind: text
        - contains: ${inputs.opening_amount}
          any_of: []
          kind: text
    timeout_ms: 5000
outcome_rules:
  - when:
      contains: No members found
      any_of: []
      kind: text
    return:
      status: business_outcome
      code: MEMBER_NOT_FOUND
  - when:
      contains: You must accept the account disclosure
      any_of: []
      kind: text
    return:
      status: failure
      code: VALIDATION_REJECTED
      field: disclosure_accepted
  - when:
      kind: overlay
      present: true
    recover:
      strategy: wait_and_retry
      max_attempts: 2
      backoff_ms: 500
  - when:
      kind: dialog
      contains: System Notice
      unmatched: false
    recover:
      strategy: dismiss
      target:
        frame_path:
          - servicing-frame
        candidates:
          - match_count: 1
            kind: role
            role: button
            name: OK
            name_source: visible_text
      max_attempts: 1
      backoff_ms: 500
    else:
      status: escalated
      reason: UNKNOWN_DIALOG
  - when:
      kind: dialog
      unmatched: true
    return:
      status: escalated
      reason: UNKNOWN_DIALOG
gaps:
  - step_after: fill-opening-amount
    detected_from:
      trace_step: 13
      human_step: 16
      evidence: You must accept the account disclosure to continue.
    reason:
      A human intervened and no observable state changed, so the action they took could not be derived.
      The validation message that preceded the intervention names the control.
    resolved_by: human
  - step_after: start-sub-account
    detected_from:
      evidence: no step sets account_type; savings is the first <option> so the default coincided
    reason:
      account_type is a declared input that no step enters. The run never touched the dropdown, so
      replay with checking would open a savings account and fail only at the final checkpoint.
    resolved_by: human
provenance:
  source_run_id: run_20260925_013816_266d
  source_evidence: evidence/discovery-success/
  discovered_at: "2026-09-25T01:38:16Z"
  model: anthropic/claude-opus-5
  fault_profile: dialog
```

Four things `IMPLEMENTATION.md`'s sketch had are **not** here, each because it could never fail or
never vary: a `wait` step kind (that is the arbitrary sleep replay avoids), `policy.allowed_actions`
(the `StepAction` union makes an unlisted action unexpressible), `contract.result_variants` (the
`RunResult` union restated), and a separate top-level `outputs:` block (merged into
`contract.outputs`, so a declared output and its extractor cannot drift apart).

Text matching is **always case-insensitive** and every text condition takes exactly one of
`contains` or `any_of`. `savings` renders `Savings` and `25.00` renders `$25.00`; making that a
per-condition flag invites forgetting it precisely where it matters.

### Locator ladder, and the three demotions this trace forces

Emit in this order; the resolver tries them in order:

1. `role` + accessible name — **only** when `name_source ∈ {computed, visible_text, label}`
2. `label` + control type
3. `contextual_text` (anchor + relative)
4. `attribute`, when the attribute is a form `name=` — the **server reads it**, so it is a contract,
   not a coincidence. This is why it outranks generic CSS
5. `text` + tag
6. `css` — always `stability: unverified`, always last

Never emitted as a candidate:

| Signal                          | Rule                                                                                                                                                                                | Seen at                                                                                                                                                   |
| ------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `name_source: "placeholder"`    | Demote below CSS, record in `rejected` with the reason                                                                                                                              | Step 11 (`$0.00`)                                                                                                                                         |
| `match_count: null`             | Cannot be primary — "not counted" is not "matched once"                                                                                                                             | Step 5                                                                                                                                                    |
| `match_count > 1`               | Never primary, and never a fallback either — Step 7 turns a multi-match into an immediate `TARGET_AMBIGUOUS`, so keeping one would convert a clean fall-through into a hard failure | Member Detail's two `Back` buttons, where **all three** candidates match 2 and nothing survives: that element is _unlocatable_, not merely low-confidence |
| `dom_id_stability: "generated"` | Never emitted at all                                                                                                                                                                | Steps 0, 11                                                                                                                                               |
| a coordinate                    | Never a locator; `evidence.discovery_coordinate` only                                                                                                                               | every click                                                                                                                                               |

---

## 4. Versioning — the minimum that is a mechanism

Two fields, one rule, one test. Everything else is deliberately deferred with a reason.

```yaml
schema_version: "1.0.0" # the ARTIFACT FORMAT
capability:
  id: "corebank.open_subaccount_review"
  version: "1.0.0" # this capability's INPUT/OUTPUT BEHAVIOUR
```

**The one rule, in `load_artifact()`:**

> Refuse to load an artifact whose `schema_version` **major** differs from the interpreter's. Minor and
> patch load.

That single check is what turns a version field from a label into a mechanism. The trace's existing
`SCHEMA_VERSION` ([`src/domain/trace.py:33`](src/domain/trace.py#L33)) is currently **written and never
read** — do not repeat that here.

**Why the two are separate**, which the brief asks you to defend: adding an optional field to the
artifact format bumps `schema_version` minor and breaks nothing. Changing `opening_amount` from
optional to required changes what a _caller_ must pass, and bumps `capability.version` major — even
though the file format did not move. Conflating them means callers cannot tell "the file looks
different" from "my integration broke".

**Deliberately not built, and say so in the report:**

| Deferred                                     | Why it is safe to defer here                                                                                                                                                                                                                             |
| -------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `compatibility.app_versions: [">=1.0,<2.0"]` | The simulator exposes **no version endpoint** (verified: its routes are `/`, `/dev/*`, and three servicing pages). A range constraint would be unverifiable at replay time — decoration, not a check. Add the endpoint first if you want this to be real |
| `provenance.content_hash`                    | The artifact is committed to git, which already provides integrity and history. A hash inside a git-tracked file is redundant at this scale                                                                                                              |
| `approval_state` lifecycle                   | Needs a registry and a reviewer workflow; neither exists, and the take-home does not ask for them                                                                                                                                                        |
| `compatibility.variant`                      | Only earns its place with the `tenant_b` stretch goal                                                                                                                                                                                                    |

---

## 5. File layout

```text
src/
  domain/
    artifact.py          NEW  the models in §3, + JSON Schema export
  discovery/
    canonicalizer.py     NEW  trace.yaml -> capability.yaml, and gap detection
    locator_ranking.py   NEW  candidate ladder + the demotions
    specs.py             NEW  CapabilitySpec — the half a trace cannot derive
  replay/                NEW
    __init__.py
    engine.py            the state machine
    locator_resolver.py  candidate ladder -> a Playwright Locator, with uniqueness
    conditions.py        preconditions / postconditions / checkpoints
    recovery.py          outcome_rules: retry, dismiss, escalate
  surfaces/
    playwright_web.py    NEW  ReplaySurface: goto, resolve, act, read, evidence, pause/resume
artifacts/
  open_subaccount_review.yaml   NEW  the committed artifact
tests/
  test_artifact.py       NEW
  test_canonicalizer.py  NEW
  test_locator_ranking.py NEW
  test_replay.py         NEW
  test_conditions.py     NEW
```

Add `playwright (>=1.48)` to `pyproject.toml`; `poetry run playwright install chromium` is a setup
step for the README.

---

## 6. Implementation steps

Each step is independently verifiable. Phases A and C need no browser.

### Phase A — the artifact and the canonicalizer (offline, no Playwright)

---

#### Step 1 — Artifact models · `src/domain/artifact.py` ✅ DONE

- [x] Pydantic v2 models for §3, `extra="forbid"` throughout, mirroring `src/domain/trace.py`'s style.
- [x] Reuse `LocatorCandidate` from `trace.py` — asserted by a test, not just intended.
- [x] Discriminated unions for `StepAction` (`click` / `fill` / `select` / `check` / `read`) and
      `Condition` (`url` / `heading` / `text` / `text_absent` / `value` / `checked` / `dialog` /
      `overlay`).
- [x] `Capability.to_yaml()` / `from_yaml()` in declaration order, `exclude_none` so a reviewer reads
      assertions rather than a field of `null`s.
- [x] `load_artifact(path)` enforcing the §4 major-version rule, raising `IncompatibleSchemaVersion`.
- [x] `unbound_inputs` / `open_gaps` / `is_replayable` / `why_not_replayable()`.

**Four cuts, applied** (ponytail): `wait` is not a `StepAction` — a wait _step_ is the arbitrary sleep
Step 8 forbids, and waiting belongs to condition evaluation and `recover: wait_and_retry`;
`capability_json_schema()` is dropped because `Capability.model_json_schema()` already is it;
`policy.allowed_actions` is dropped because the union makes an unlisted action unexpressible, so the
check could never fire (`forbidden_actions` stays — it can); `contract.result_variants` is dropped as a
restatement of the `RunResult` union. §3's `outputs:` block was merged into `contract.outputs`, so a
declared output and its extractor live in one place and a missing extractor is a required-field error.

**One inconsistency fixed.** §3 spelled the same predicate three ways (`heading` with both `matches:`
and `text:`; `text` with `contains:`, `any_of:` and `matches:`). One spelling: `contains` **or**
`any_of`, exactly one, enforced. No `matches` — it implies a regex and nothing needs one. Matching is
**always case-insensitive** rather than a per-condition `ignore_case` flag: `savings` renders `Savings`
and `25.00` renders `$25.00`, and a flag invites forgetting it precisely where it matters.

##### What building it found: a second gap

**`account_type` is a declared input that no step sets.** The run never touched the Account Type
dropdown — `savings` is the first `<option>`, so the default coincided with the goal and the model had
no reason to click it. Every probe in the trace confirms it: member field, Search, result row, Open
Sub-Account, amount field, Continue, Continue. No select.

Replay with `account_type=checking` would open a **Savings** account and fail only at the final
checkpoint. Structurally the same hole as the disclosure checkbox, arrived at by a different route: one
came from a human intervention, this one from a coincidence.

`unbound_inputs` is deliberately narrower than `referenced_inputs` and that distinction is the whole
catch — the checkpoint _asserts on_ `account_type`, which is not the same as _setting_ it. A naive
"is it mentioned anywhere?" scan reports it bound and misses the bug.

So the draft carries **two** gaps, and §3 gains a `select-account-type` step alongside
`accept-disclosure`. Both are `authored_by: "human"`.

**Verification**

_1 — the draft loads, round-trips, and refuses itself_

```bash
poetry run python -c "
from src.domain.artifact import Capability, load_artifact
c = load_artifact('tests/fixtures/capability_draft.yaml')
print('steps      :', [s.id for s in c.steps])
print('unbound    :', c.unbound_inputs)
print('replayable :', c.is_replayable)
for r in c.why_not_replayable(): print('  ' + r)
assert Capability.from_yaml(c.to_yaml()) == c
print('round-trip : ok')"
```

Actual output — this is the message `replay` will print when it refuses:

```
steps      : ['fill-member-id', 'submit-search', 'open-member', 'start-sub-account',
              'fill-opening-amount', 'continue-to-review', 'verify-review']
unbound    : ['account_type']
replayable : False
  unresolved gap after step 'fill-opening-amount': A human intervened and no observable state
  changed, so the action they took could not be derived. ...
    evidence: 'You must accept the account disclosure to continue.' (trace step 13)
  unresolved gap after step 'start-sub-account': account_type is a declared input that no step
  enters. ...
    evidence: 'no step sets account_type; savings is the first <option> so the default coincided'
  declared input(s) no step sets: account_type
round-trip : ok
```

Each gap prints its **evidence**, because that is what localises it — "a human acted" tells a reader
nothing; the validation message names the control they have to author.

_2 — one union, not two_

```bash
poetry run python -c "
from src.domain import artifact, trace
assert artifact.LocatorCandidate is trace.LocatorCandidate
print('LocatorCandidate reused, not redefined')"
```

_3 — the version rule, both directions_

```bash
poetry run python -c "
from src.domain.artifact import load_artifact, IncompatibleSchemaVersion
import pathlib, tempfile, re
base = pathlib.Path('tests/fixtures/capability_draft.yaml').read_text()
for v, ok in [('1.0.0',1), ('1.9.9',1), ('1.0.7',1), ('0.9.0',0), ('2.0.0',0)]:
    p = pathlib.Path(tempfile.mkstemp(suffix='.yaml')[1])
    p.write_text(re.sub(r'schema_version:.*', f'schema_version: \"{v}\"', base, count=1))
    try: load_artifact(p); got = 1
    except IncompatibleSchemaVersion as e: got = 0; print(' ', e)
    assert got == ok, v
    print(f'  {v:<8} {\"loaded\" if got else \"refused\"}')"
```

Minor and patch load; a different major in **either** direction is refused, and the version is checked
_before_ field validation so a future-major artifact says so rather than emitting a pile of
`extra fields not permitted` about fields that will make sense to the interpreter that understands them.

_4 — authoring flips it_

`tests/fixtures/capability_authored.yaml` is the draft with both steps written in and both gaps
resolved. Same models, nothing else changed:

```bash
poetry run python -c "
from src.domain.artifact import load_artifact
d, a = load_artifact('tests/fixtures/capability_draft.yaml'), load_artifact('tests/fixtures/capability_authored.yaml')
assert d.is_replayable is False and a.is_replayable is True
assert a.unbound_inputs == []
print('authored:', [s.id for s in a.steps if s.authored_by])"
```

_5 — `tests/test_artifact.py`, 21 tests, offline_

| Refuses                                           | Message names                      |
| ------------------------------------------------- | ---------------------------------- |
| `{kind: "wait"}` as an action                     | the invalid kind                   |
| a `click` with no target                          | `target`                           |
| `${inputs.membr_id}`                              | `membr_id`, and what _is_ declared |
| an output with no `extract`                       | which output                       |
| a duplicate step id                               | the repeated id                    |
| a `coordinate` candidate, or zero candidates      | — (unexpressible by construction)  |
| a text condition with both or neither predicate   | "exactly one"                      |
| an outcome rule that neither returns nor recovers | `return`/`recover`                 |
| `schema_version` major mismatch                   | the found and supported versions   |

Does **not** refuse: an unbound input or an open gap. A draft has to stay loadable to be authored;
`is_replayable` is what refuses it.

`poetry run pytest -q` → **273 passed**, no browser, no network, no sim.

---

#### Step 2 — Locator ranking · `src/discovery/locator_ranking.py` ✅ DONE

- [x] `rank_candidates(probe) -> tuple[list[LocatorCandidate], list[Rejection]]` — the ladder and the
      four exclusions.
- [x] Every rejection carries a reason and a short identifier, both of which land in
      `evidence.rejected`.

**The probe was already doing half of this, in the wrong layer.** `probe.js` emits a rough ladder
(`text → contextual_text → attribute → label → css`), and
[`surface_agent.py:576`](sandbox/surface_agent.py#L576) inserts the role candidate at position 0 —
_unless_ its name came from a placeholder, in which case it appends it last. So the `$0.00` demotion
§3 assigns to this layer already happens inside the sandbox.

That is a reason to build the ranker, not to skip it: the artifact's locator priority should not be
decided by JavaScript running inside the application being observed, and the rule should be readable
at artifact-build time. It does mean the module is a **filter and a stable re-sort — about 40 lines**,
not a scoring engine. Two real differences from the emitted order: `text` sorts below `attribute`
(probe.js puts it first), and a placeholder-derived role is _excluded_, not merely last.

**Exclusion precedence matters**, because a candidate can fail two ways at once and the reader wants
the specific reason. The amount field's role candidate is placeholder-derived _and_ uncounted;
"matches any element showing the same formatting hint" explains it, "not counted" does not.

##### What building it found: an element nothing can locate

`tests/fixtures/live_probe_ambiguous.json` — Member Detail's two identical `Back` buttons — has **all
three** candidates at `match_count: 2`. After filtering, **zero candidates survive**.

That is the correct answer, and it is stronger than §3's original framing. On this element the
ambiguity signal does not mean "lower confidence", it means **unlocatable**: no `Target` can be built,
because `Target` requires at least one candidate. Keeping an ambiguous candidate as a last resort would
be worse than useless — Step 7 treats `count() > 1` as an immediate `TARGET_AMBIGUOUS`, so it would
convert a clean fall-through into a hard replay failure.

**The Step 1 fixtures were abbreviated, and are now machine-derived.** The hand-written bundles in
`capability_draft.yaml` dropped real candidates (step 11 lost its `css` fallback, step 13 lost `css`,
step 2 lost `contextual_text` and `css`). The fallback chain _is_ the point of a bundle, so the draft's
`candidates` and `evidence.rejected` blocks are now regenerated from `rank_candidates`, and
`capability_authored.yaml` re-derived from that. Step 3's "matches the draft" check depends on it.

**Verification**

_1 — every probed step in the real run_

```bash
poetry run python -c "
from src.domain.trace import RunTrace
from src.discovery.locator_ranking import rank_candidates
t = RunTrace.from_yaml(open('evidence/discovery-success/trace.yaml').read())
for s in t.steps:
    if not s.probe: continue
    kept, rejected = rank_candidates(s.probe)
    print(f'step {s.index:>2}: {[c.kind for c in kept]}')
    for r in rejected: print(f'         rejected {r.kind}({r.value}): {r.reason[:72]}')"
```

Actual output — each line is a rule doing its job:

```
step  0: ['contextual_text', 'attribute', 'css']
         rejected dom_id(inp_7676796a): dom_id_stability: generated — a new id is issued every request
step  2: ['role', 'contextual_text', 'text', 'css']
step  5: ['css']
         rejected role(cell/${inputs.member_id}): match_count: null — not counted, so it cannot be...
step  8: ['role', 'text', 'css']
step 11: ['contextual_text', 'attribute', 'css']
         rejected role(textbox/$0.00): accessible_name_source: placeholder — the name is a formatting...
         rejected dom_id(amt_d955786b): dom_id_stability: generated — a new id is issued every request
step 13: ['role', 'contextual_text', 'text', 'css']
step 17: ['role', 'contextual_text', 'text', 'css']
```

Step 5 keeping **only `css`** is the `open-member` fragility §8 predicts. The ranker surfaces it rather
than papering over it — there is genuinely nothing semantic to hold onto for that row.

_2 — the unlocatable element_

```bash
poetry run python -c "
import json
from src.domain.trace import ProbeResult
from src.discovery.locator_ranking import rank_candidates
p = ProbeResult.model_validate(json.load(open('tests/fixtures/live_probe_ambiguous.json')))
kept, rejected = rank_candidates(p)
assert kept == [], 'an element nothing can uniquely identify must yield no candidates'
print('kept:', kept, '| rejected:', [r.kind for r in rejected])"
```

→ `kept: [] | rejected: ['role', 'text', 'css']`

_3 — the fixtures still hold their Step 1 properties_

```bash
poetry run python -c "
from src.domain.artifact import load_artifact
d = load_artifact('tests/fixtures/capability_draft.yaml')
a = load_artifact('tests/fixtures/capability_authored.yaml')
assert d.is_replayable is False and a.is_replayable is True
assert d.unbound_inputs == ['account_type'] and a.unbound_inputs == []
for s in d.steps:
    t = getattr(s.action, 'target', None)
    if t: print(f'  {s.id:<22} {[c.kind for c in t.candidates]}')"
```

Every coordinate-derived step now carries its full chain, `css` included.

_4 — `tests/test_locator_ranking.py`, 13 tests, offline_

| Test                                                | Pins                                                                                 |
| --------------------------------------------------- | ------------------------------------------------------------------------------------ |
| placeholder / null-count / zero-match / multi-match | each against the real element that exhibits it                                       |
| precedence                                          | step 11's role is rejected for _placeholder_, not _null count_, though both are true |
| the whole ladder                                    | all seven probed steps asserted as one dict — a single diff if the ladder moves      |
| `text` below `attribute`                            | asserted against probe.js's opposite order, so the re-sort is provably happening     |
| `css` always last                                   | every step                                                                           |
| stable sort                                         | two `text` candidates keep probe order                                               |
| generated `dom_id`                                  | present in `rejected` though it was never a candidate                                |
| every rejection                                     | non-empty `reason` **and** `value`                                                   |
| nothing double-counted                              | `kept + rejected` accounts for every input candidate                                 |
| no coordinate candidate                             | structural, asserted anyway — it is the claim canonicalization rests on              |

`poetry run pytest -q` → **286 passed**, offline.

---

#### Steps 3 + 4 — The canonicalizer and gap detection · `src/discovery/canonicalizer.py` ✅ DONE

Rolled together: gaps are detected _during_ canonicalization, so splitting them would have built a
seam for no reason. Step 4's other three boxes were already satisfied — the `Gap` model, `open_gaps`,
`is_replayable` and `why_not_replayable()` shipped in Step 1; `replay` refusing belongs to Step 10,
which owns the command; authoring-by-hand is a decision `capability_authored.yaml` already exercises.

- [x] Drop the noise (`wait`, `screenshot`, `zoom`, `cursor_position`) — 7 of 21.
- [x] Collapse click-then-type into `fill`.
- [x] Dedupe a rejected attempt and its retry, keeping the refusal message.
- [x] Rank every target through Step 2's `rank_candidates`.
- [x] Derive postconditions — three rules only.
- [x] Classify every human step: recovery rule, or `Gap`.
- [x] Build `outcome_rules`; build `outputs` (from the spec — see below).
- [x] Refuse to emit an artifact that leaks.
- [x] `canonicalize --report` prints the gaps, using `why_not_replayable()` rather than a second
      formatter.

##### The finding: the contract is authored, the procedure is derived

The plan's proposed signature, `canonicalize(trace, *, capability_id, version)`, implies everything
else is derivable. Reading the trace for what it can actually supply shows it is not:

| Section                                                          | Derivable? | From                                                                     |
| ---------------------------------------------------------------- | ---------- | ------------------------------------------------------------------------ |
| `steps`, `entry`, `policy.allowed_origins`, `provenance`, `gaps` | **yes**    | the trace                                                                |
| `contract.inputs` — **names**                                    | **yes**    | every `${inputs.*}` reference, _including the goal string_               |
| `contract.inputs` — **types**                                    | no         | nothing says `member_id` is five digits or `account_type` an enum of two |
| `contract.outputs`                                               | no         | nothing probed the review panel; there is no row-label mapping to derive |
| `capability.id` / `title` / `risk`                               | no         | naming and risk are judgments                                            |
| `outcome_rules` for paths never taken                            | no         | the run found its member, so "No members found" was never on screen      |

So `canonicalize(trace, spec)` takes a `CapabilitySpec` (`src/discovery/specs.py`) for that half.
**Deriving a public API from one observed run is overfitting** — every input would come out
`type: string`, and `account_type` admitting exactly two values is a fact about the domain, not about
the afternoon this run happened.

`${inputs.account_type}` appears in the trace **exactly once, in the goal string**, because the model
never touched the dropdown. That single mention is what makes the second gap detectable at all: the
goal declares intent, the steps record what happened, and the gap is the difference.

##### Four things building it got wrong first

1. **A step named `0-00`.** The slug used the accessible name, which for the amount field is the
   placeholder `$0.00` — the same junk the locator ladder already demotes. The slug now applies the
   same judgment: skip a placeholder-derived name, skip one that is itself an input reference (or the
   member id ends up in a step id), fall back to the enclosing region's id, then the index.
2. **The intervention gap anchored _after_ the step that was rejected.** The human ticked the box
   because `continue` failed, so the missing step has to happen _earlier_ than that click — anchoring
   to it would tell an author to insert the fix after the thing it was supposed to enable. It now
   anchors to the step preceding the rejected one: `opening-amount`.
3. **The unbound-input gap guessed a position.** Nothing in the run touched the dropdown, so nothing
   says which screen it is on. `step_after` is now `None`, and `why_not_replayable()` says _"at a
   position the author must choose"_ rather than _"after step None"_.
4. **URL postconditions read `main_frame_url`.** That never changes — the shell holds an iframe and
   the workflow navigates inside it, so the assertion would have been true on every screen. They read
   the innermost frame, which also means they come out already parameterized:
   `/servicing/members/${inputs.member_id}` asserts the _right_ member.

##### Postconditions are derived narrowly on purpose

Three rules — a navigation, a new heading, and "the value I typed is in the field" — plus a
`text_absent` from a deduped attempt. Nothing else. A postcondition derived from incidental text
becomes a false failure on the next run: `search` swaps a results panel via htmx with no navigation
and no new heading, so it correctly asserts **nothing** rather than baking this member's name in.

**Verification**

_1 — the reduction_

```bash
poetry run python -m src.cli canonicalize evidence/discovery-success/trace.yaml --report
```

```
reduction
   21 trace steps -> 7 artifact steps
     member-id / search / results-panel / open-sub-account / opening-amount / continue / verify-outcome

gaps
   unresolved gap after step 'opening-amount': A human intervened and no observable state changed...
    evidence: 'You must accept the account disclosure to continue.' (trace step 13)
   unresolved gap at a position the author must choose: account_type is a declared input that no
    step enters...
   declared input(s) no step sets: account_type
```

That gap block is the same text Step 10's `replay` prints when it refuses — one formatter, not two.

_2 — the fixture is the output, not a transcription_

```bash
poetry run python -c "
from src.domain.artifact import load_artifact
from src.discovery.canonicalizer import canonicalize
from src.discovery.specs import OPEN_SUBACCOUNT
from src.domain.trace import RunTrace
t = RunTrace.from_yaml(open('evidence/discovery-success/trace.yaml').read())
assert canonicalize(t, OPEN_SUBACCOUNT).to_yaml() == load_artifact('tests/fixtures/capability_draft.yaml').to_yaml()
print('canonicalize() reproduces the draft exactly')"
```

Both fixtures are regenerated from this function now. Hand-writing the expected artifact is how the
first two attempts went wrong — a fixture I typed cannot check the code that should produce it.

_3 — the gap pair still holds_

```bash
poetry run python -c "
from src.domain.artifact import load_artifact
d, a = load_artifact('tests/fixtures/capability_draft.yaml'), load_artifact('tests/fixtures/capability_authored.yaml')
assert d.is_replayable is False and a.is_replayable is True
assert d.unbound_inputs == ['account_type'] and a.unbound_inputs == []
print('draft refuses itself; authored does not')"
```

_4 — `tests/test_canonicalizer.py`, 25 tests, offline_

| Test                              | Pins                                                                                                                                          |
| --------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| output == the committed draft     | the whole reduction, in one assertion                                                                                                         |
| 21 → 7, ids listed                | the step list, so a naming change shows as one diff                                                                                           |
| no `wait`/`screenshot` survives   | replay waits on conditions, never a clock                                                                                                     |
| steps 0+1 → one `fill`            | the target came from the click, the value from the type                                                                                       |
| steps 13/17 → one step            | and its refusal message became a `text_absent` postcondition                                                                                  |
| URL postconditions                | read the **inner** frame, and are parameterized                                                                                               |
| `search` asserts nothing          | htmx swap: no navigation, no heading, no invented assertion                                                                                   |
| gap A                             | localised by step 13's message, anchored to `opening-amount`                                                                                  |
| gap B                             | no anchor, and `unbound_inputs == ['account_type']`                                                                                           |
| step 20                           | a recovery rule, not a gap, matching `System Notice` and **not** the per-request `ERR-` code                                                  |
| no human steps / all inputs bound | neither gap fires spuriously                                                                                                                  |
| derived vs authored               | entry, origins, provenance derived; `^[0-9]{5}$` and the enum from the spec                                                                   |
| the leak gate                     | a generated id smuggled into a _candidate_ is refused, while the same id in a _rejection_ is fine — that is the record of why it was not used |

`poetry run pytest -q` → **311 passed**, offline.

---

#### Step 5 — Commit the artifact

- [x] Run the canonicalizer over `evidence/discovery-success/trace.yaml` — `canonicalize <trace> --out
artifacts/open_subaccount_review.yaml`. The draft is now this command's output end to end.
- [x] Author **both** missing steps by hand, using the simulator's real markup:
      `accept-disclosure` → `input[name="disclosure_accepted"]` (note `id="chk_{{suffix}}"` is
      generated and must not be used), and `select-account-type` → `select[name="account_type"]`
      (`id="sel_{{suffix}}"`, likewise generated). Both get `authored_by: "human"`, and both gaps get
      `resolved_by: "human"`. `tests/fixtures/capability_authored.yaml` is exactly this result.
- [x] Commit `artifacts/open_subaccount_review.yaml` and `evidence/discovery-success/capability.yaml`
      (the same file, beside the run that produced it).

---

### Phase B — replay (needs Playwright)

---

#### Step 6 — Playwright surface · `src/surfaces/playwright_web.py`

- [x] `ReplaySurface` with `goto`, `frame`, `resolve`, `click`, `fill`, `check`, `read`, `observe`,
      `capture_evidence`, `pause`, `resume` — deliberately the same shape as `SurfaceAdapter` so the
      cross-surface argument holds in code, not just prose.
- [x] Frame handling: `frame_path: ["servicing-frame"]` →
      `page.frame_locator('iframe[name="servicing-frame"]')`. Every step's target is frame-scoped;
      the workflow is never in the main document.
- [x] **Base-URL rebinding.** The artifact records `http://bank-sim:8001` — the hostname _the agent_
      saw inside `sandbox_net`. Replay runs on the host, where the sim is `http://127.0.0.1:8001`.
      `replay --base-url` rebinds it, and the policy check runs against the **rebound** origin, with
      the mapping recorded in the replay trace. Getting this wrong silently disables the origin
      allowlist.
- [x] Headless by default; `--headed` for the escalation demo, where the human uses the open window.

**Verify:** `poetry run python -m src.cli replay --smoke` opens the shell, enters the iframe, and
prints the search heading.

---

#### Step 7 — Locator resolver · `src/replay/locator_resolver.py`

- [x] Try candidates in artifact order. For each, build the Playwright locator:
      `role` → `get_by_role(role, name=...)`; `label` → `get_by_label`;
      `text` → `get_by_text`; `contextual_text` → anchor-scoped;
      `attribute`/`css` → `locator(selector)`.
- [x] **Require uniqueness.** `count() == 0` → try the next candidate. `count() > 1` → `TARGET_AMBIGUOUS`
      **immediately**, without trying more candidates and without picking the first. Ambiguity is a
      different fact from absence and must not be silently resolved.
- [x] Verify the resolved element against `evidence.expected_tag` / `expected_role` before acting. A
      unique match on the wrong element is the failure mode a locator bundle exists to prevent.
- [x] Exhausting all candidates → `TARGET_NOT_RESOLVED` with per-candidate diagnostics: which was
      tried, what it matched, why it was rejected.

**Verify:** all five distinct targets resolve against the live sim; a deliberately corrupted artifact
(`name: "Continue"` → `"Proceed"`) falls through to the next candidate and still resolves; an artifact
whose only candidate is `{kind: role, role: button, name: "Back"}` on Member Detail returns
`TARGET_AMBIGUOUS` rather than clicking one of the two.

---

#### Step 8 — Conditions · `src/replay/conditions.py`

- [ ] Evaluate each `Condition` kind against a live frame, with `${inputs.*}` substituted.
- [ ] **Condition-based waits only** — no `sleep`. Playwright's auto-waiting does the work; a timeout
      maps to a recoverable condition or a hard failure per the step's `retry`.
- [ ] `${inputs.opening_amount}` is `25.00` while the page renders `$25.00`, and `savings` renders as
      `Savings`. Reuse the **case-insensitive substring** rule from `Checkpoint`
      ([`src/discovery/controller.py`](src/discovery/controller.py)) — it was written for exactly this
      and is already proven against the real screen.

---

#### Step 9 — The replay engine · `src/replay/engine.py`

Per step, in this order — the same shape as the discovery loop, for the same reasons:

1. `session.barrier()`; assert control ownership
2. Evaluate preconditions
3. Scan global exception states (dialog, overlay, session banner) → `outcome_rules`
4. Resolve the locator bundle (Step 7)
5. `policy.check_action` → `policy.check_target` on the **resolved element**
6. Execute with the step's timeout
7. Evaluate postconditions
8. Record one event per transition through the existing `EvidenceWriter`

- [ ] Bounded: per-step `timeout_ms` and `retry`, a whole-run wall clock, and a max-steps guard.
- [ ] Returns the existing `RunResult` union. No new result type.
- [ ] **The irreversible gate still applies.** `Open Account` carries `danger-button`; replay must
      refuse it exactly as discovery did, and `forbidden_actions: [submit_irreversible]` in the
      artifact is a second, declarative expression of the same rule.

---

#### Step 10 — Recovery, outcomes, and the replay CLI

- [ ] `src/replay/recovery.py`: `wait_and_retry`, `dismiss` (only when the dialog is one the artifact
      names), `escalate`. Bounded by `max_attempts` — an unbounded dismiss loop against a modal that
      keeps returning is worse than an escalation.
- [ ] An **unmatched** dialog is always `escalated: UNKNOWN_DIALOG`, never dismissed. Dismissing a
      dialog you cannot identify is the exact failure the handoff exists to prevent.
- [ ] `replay` CLI:

```bash
poetry run python -m src.cli replay artifacts/open_subaccount_review.yaml \
  --input member_id=23456 --input account_type=savings --input opening_amount=50.00 \
  --base-url http://127.0.0.1:8001 \
  --fault default \
  --evidence-dir evidence/replay-success \
  [--headed] [--overwrite]
```

- [ ] Validate inputs against `contract.inputs` **before** opening a browser — a bad `member_id`
      should cost nothing.
- [ ] Reuse `_resolve_session`, `--overwrite`, `--fault` and the event printer from `src/cli.py`.

---

### Phase C — evidence and tests

---

#### Step 11 — The four demonstration runs

All four use the **same artifact**, differing only in inputs and fault profile. That is the point.

| Run                       | Command                                                                   | Expected                                                                                  | Proves                                                                                                                                                                                   |
| ------------------------- | ------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `replay-success`          | `--input member_id=23456 --input account_type=savings`, `--fault default` | `success`, outputs validated, **zero model calls**                                        | Parameterization and determinism — a _different member_ than discovery used. `account_type` must be passed: it is a bound input now, and the `select-account-type` step actually sets it |
| `replay-member-not-found` | `--input member_id=88888`                                                 | `business_outcome: MEMBER_NOT_FOUND`                                                      | A domain answer is not a crash                                                                                                                                                           |
| `replay-recovery`         | `--fault overlay`                                                         | bounded wait, then `success`                                                              | Recoverability with evidence                                                                                                                                                             |
| `replay-escalation`       | `--fault dialog`, `--headed`                                              | `escalated: UNKNOWN_DIALOG`, human takes the open window, `session resume`, run completes | Real control transfer at replay time                                                                                                                                                     |

**The actual seed data** (`apps/cred_union_sim/seed.sql`), because one of these is a trap:

| Member  | Name           | Status     | Use for                                                                             |
| ------- | -------------- | ---------- | ----------------------------------------------------------------------------------- |
| `12345` | Martinez, J.   | active     | What discovery used — avoid, so replay proves parameterization                      |
| `23456` | Chen, S.       | active     | **`replay-success`**                                                                |
| `34567` | O'Brien, R.    | active     | Spare                                                                               |
| `45678` | Santos, M.     | **frozen** | A candidate extra business outcome, if you want one                                 |
| `99999` | Test, NotFound | inactive   | **A trap — despite the name, it IS seeded** and will not produce the not-found path |
| `88888` | —              | —          | **Not seeded. Use this for `replay-member-not-found`**                              |

The not-found response is `_search_results.html`'s
`<div class="info-banner">No members found. Check the Member ID and try again.</div>`, which is what
the `MEMBER_NOT_FOUND` outcome rule matches on.

> Rehearse the escalation handoff first with the free scripted path documented in the README. The
> discovery-layer handoff broke the first time it was tried live for a reason no test could have
> caught; assume the replay one will too.

---

#### Step 12 — Tests

| File                      | Pins                                                                                                                                                                                                                                                                                            |
| ------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `test_artifact.py`        | ✅ 21 tests. Round-trip; `wait`/no-target/typo/missing-extractor/duplicate-id/coordinate-candidate/both-predicates/inert-outcome-rule and a `schema_version` major mismatch all rejected by message; a draft stays loadable and `is_replayable` is what refuses it                              |
| `test_locator_ranking.py` | ✅ 13 tests. The four exclusions and their precedence, the full ladder for all seven probed steps, `text` below `attribute`, stable sort, the synthesized `dom_id` rejection, and the `Back` button's zero survivors — all against real probes                                                  |
| `test_canonicalizer.py`   | ✅ 25 tests. 21 → 7; click+type collapses; 13/17 dedupe with the refusal kept as a `text_absent`; step 20 → recovery rule; step 16 → `Gap` anchored before the rejected step; unbound `account_type` → anchorless `Gap`; inner-frame URLs; output equals `capability_draft.yaml`; the leak gate |
| `test_conditions.py`      | `savings` matches `Savings`; `25.00` matches `$25.00`; `text_absent`                                                                                                                                                                                                                            |
| `test_replay.py`          | Each result variant; `TARGET_AMBIGUOUS` on the two `Back` buttons; `TARGET_NOT_RESOLVED` diagnostics; an unresolved gap refuses to run; bounded retries                                                                                                                                         |

Phase A tests need no browser. Phase B tests need a running sim — mark them
`@pytest.mark.integration` so `poetry run pytest` stays offline and fast.

---

#### Step 13 — Documentation

- [ ] README: the replay command, the four runs, `playwright install chromium`, and a short
      "canonicalization in one page" section using the real before/after (21 trace steps → 7 artifact
      steps + 2 gaps).
- [ ] REPORT: the gap finding is the strongest material here — _a real run produced an artifact that
      could not be completed automatically, the system detected exactly where and why, and refused to
      claim otherwise._

---

## 7. Acceptance criteria

1. The committed artifact validates, contains no coordinate as a primary locator, no generated
   `dom_id`, and no unredacted input.
2. `replay-success` succeeds with a **different member id** than discovery used, with zero model calls.
   Run it once with `account_type=checking` too: it must open a _Checking_ account, which is the bug
   the second gap existed to prevent and the only way to prove the `select` step does anything.
3. The other three runs return their correct tagged variants.
4. An artifact with an unresolved gap refuses to replay, naming the gap and its evidence.
5. Ambiguity returns `TARGET_AMBIGUOUS`; it never silently picks the first match.
6. `schema_version: "2.0.0"` is refused on load.
7. Replay refuses `Open Account` exactly as discovery did.
8. `poetry run pytest` stays offline and green.

---

## 8. Failure modes to expect

| Likely problem                                                                                                   | Where it bites                  | Do this                                                                                                                                                                                              |
| ---------------------------------------------------------------------------------------------------------------- | ------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **The base-URL rebind silently voids the origin allowlist**                                                      | Step 6                          | Check the rebound origin, record the mapping, and test that an unmapped origin is refused                                                                                                            |
| The member row's only unique candidate is CSS — **confirmed**, the ranker keeps exactly one candidate for step 5 | `open-member`                   | It is structural and fragile by nature. Record it `unverified`, give the step a postcondition that fails loudly, and say so in the report rather than pretending the ladder found something semantic |
| `htmx` swaps the results panel; a stale locator resolves against a detached node                                 | `submit-search` → `open-member` | Re-resolve after every navigation or swap; never cache a `Locator` across steps                                                                                                                      |
| The amount field's placeholder name tempts the ranker                                                            | `fill-opening-amount`           | Already excluded — but assert it, because the candidate _looks_ like the best one                                                                                                                    |
| Postconditions derived from one run overfit to it                                                                | Step 3                          | Derive only URL, heading and value assertions. Do not turn incidental text into a postcondition                                                                                                      |
| The escalation handoff breaks on first live use                                                                  | Step 11                         | It did in the discovery layer. Rehearse free before spending a real run                                                                                                                              |

---

## 9. What this layer deliberately does not do

- **No desktop replay adapter.** `interface-ai-plan-v2.md` scopes it to design only; the locator
  ladder is written so a UIA/AT-SPI adapter would slot in behind the same `resolve` interface.
- **No self-healing.** A locator that stops resolving is a `TARGET_NOT_RESOLVED` a human reviews, not
  something the system silently repairs. Silent repair is how an artifact stops meaning anything.
- **No model at replay time, in any capacity** — including "just to fix a locator".
- **No capability registry, approval workflow, or tenant inheritance.** Named in the report as seams.
