# Computer-Use Automation System — Credit Union Ops

A computer-use agent that discovers a workflow in a hostile legacy web UI by watching pixels and driving
mouse/keyboard, turns what it did into a **reviewable artifact**, and replays that artifact
deterministically with **no model in the loop**.

The interesting claim is not that a model can click through a form. It is that a model-discovered run
becomes a reviewable contract, that exceptional states have explicit semantics, and that a human can take
and return control of the exact live session the agent is driving.

This file is **setup and one full run**. For the test suites and the free rehearsals, see
[TESTING.md](TESTING.md). For the design and the findings, [REPORT.md](REPORT.md) and
[IMPLEMENTATION.md](IMPLEMENTATION.md).

```
apps/cred_union_sim/     the Credit Union Ops Simulator — the application under test
sandbox/                 the agent's sandboxed desktop (Xvfb + Chromium + VNC + surface agent)
src/
  domain/                typed spine: actions, results, the run-trace and artifact contracts
  surfaces/              x11_computer.py drives pixels; playwright_web.py drives roles and labels
  policy/                the component whose job is to say no; plus redaction
  sessions/              control ownership and the pause barrier
  evidence/              the only component that writes a run to disk
  discovery/             the loop, the model provider, the canonicalizer, the locator ranker
  replay/                resolver, conditions, recovery, extraction, the engine
  cli.py                 everything you can run by hand
artifacts/               committed capabilities, produced by `canonicalize`
evidence/                run folders; named ones are committed, `run_*` is gitignored
discovery-loop-plan.md   the discovery layer, step by step, with findings
canonicalizer-plan.md    the artifact and replay layer, same format
```

---

# Setup

## Prerequisites

- **Docker Desktop**, daemon running. Give the VM ~4 GB — the sandbox runs Chromium.
- **Python 3.12+** and **Poetry 2.x**.
- An **Anthropic API key**, needed only for the discovery half. The canonicalizer, the whole replay
  engine and the entire test suite run without one.

```bash
poetry install
poetry run playwright install chromium    # replay only; discovery drives pixels, not Playwright
cp .env.example .env                      # then put your real key in .env — .env is gitignored
```

The code reads `ANTHROPIC_API_KEY` from the environment, not from `.env` directly, so export it before
any command that talks to the model:

```bash
set -a && source .env && set +a
```

## Start the stack

```bash
poetry run python -m src.cli sandbox up     # docker compose up -d --wait
docker compose ps
```

`--wait` matters: it blocks until the sandbox reports _healthy_, not merely _started_. A `discover` fired
at a container that is up but whose Chromium has not finished launching fails for a reason that has
nothing to do with what you were testing.

Expect three services. The asymmetry in `PORTS` is deliberate:

```
bank-sim   Up (healthy)   127.0.0.1:8001->8001/tcp
relay      Up             127.0.0.1:6080->6080/tcp, 127.0.0.1:8900->8900/tcp
sandbox    Up (healthy)   6080/tcp, 8900/tcp          <- no host binding, on purpose
```

`sandbox` sits alone on an `internal: true` network, so the browser it runs has **no internet access**. A
container on such a network cannot publish ports at all — Docker silently creates no host binding — so
`relay` (a two-line `socat` forwarder) sits on both networks and forwards inbound only.

Confirm the containment rather than trusting it:

```bash
docker compose exec sandbox curl -m 5 https://example.com   # fails — no egress
docker compose exec sandbox curl -s http://bank-sim:8001/   # works — the target
```

| Port | Bound to    | Published by | Service                     |
| ---- | ----------- | ------------ | --------------------------- |
| 8001 | `127.0.0.1` | `bank-sim`   | Credit Union Ops Simulator  |
| 6080 | `127.0.0.1` | `relay`      | noVNC — watch and take over |
| 8900 | `127.0.0.1` | `relay`      | Sandbox surface agent       |

Every port is bound to loopback, so nothing is reachable from the local network. The sandbox's own VNC
server (5900) is bound to container-loopback and never published — noVNC on 6080 is the only way in.

## Watch the desktop the agent drives

```bash
open "http://localhost:6080/vnc.html?autoconnect=true&resize=scale"
```

The servicing portal at 1280x800, no address bar, the workflow inside an iframe. Click around — this is a
real session you _share_ with the agent, which is what makes the handoff genuine.

> `resize=scale` fits the window without touching the remote display. Never use `resize=remote`: it
> resizes the X display itself and invalidates every coordinate in a trace.

## The application under test

Member search → member detail → open sub-account → review. Seeded members include `12345`
(John Martinez) and `23456` (Sarah Chen); `88888` is unseeded and exercises the not-found path. `99999` is
a trap — despite being named `Test, NotFound` it _is_ seeded.

It is deliberately hostile: the workflow is in an iframe, element ids are regenerated every request,
labels carry no `for=` attribute, two buttons share the label "Back", and one control is icon-only with no
accessible name at all.

```bash
open http://localhost:8001/       # the portal, outside the sandbox
```

Fault profiles are switched by the operator, never by the thing under test:

```bash
poetry run python -m src.cli fault set overlay    # default | overlay | dialog | session | tenant_b
poetry run python -m src.cli fault show
```

| Profile    | Effect                                  | Used by                                  |
| ---------- | --------------------------------------- | ---------------------------------------- |
| `default`  | none                                    | The happy path, discovery and replay     |
| `overlay`  | 1200 ms loading overlay                 | Replay run 3 — bounded wait and retry    |
| `dialog`   | unexpected modal after review loads     | Discovery's second handoff; replay run 4 |
| `session`  | session-expiry warning banner           | Reauthentication escalation              |
| `tenant_b` | different theme, "Continue" → "Proceed" | Cross-tenant replay via locator fallback |

---

# A full run

Three layers, in order. Discovery costs about **$0.30–1.10** and needs a human at the keyboard for the
handoffs; the other two are free and unattended.

|       |                                     | Cost        | Model         | Produces                     |
| ----- | ----------------------------------- | ----------- | ------------- | ---------------------------- |
| **A** | [Discover](#a-discovery)            | ~$0.30–1.10 | Opus 5 drives | `evidence/<name>/trace.yaml` |
| **B** | [Canonicalize](#b-canonicalization) | free        | none          | `artifacts/<name>.yaml`      |
| **C** | [Replay](#c-replay)                 | free        | none          | `evidence/replay-*/`         |

Before spending anything, run the free rehearsals in [TESTING.md](TESTING.md#free-rehearsals). Each rules
out a class of failure that would otherwise waste a paid run — and if the scripted loop does not end
`CHECKPOINT_VERIFIED`, a paid run will only tell you the same thing more slowly.

Open **two terminals** and the noVNC window. Discovery will park and wait for you.

---

## A. Discovery

### Go in cold

The sandbox is stateful. A container that has been up for days has a browser sitting on some page from a
previous experiment, and the model will spend paid steps working out where it is.

```bash
poetry run python -m src.cli sandbox down
poetry run python -m src.cli sandbox up
```

### Capture the run

```bash
poetry run python -m src.cli fault set default
set -a && source .env && set +a

poetry run python -m src.cli discover \
  --goal "Find member 12345 and prepare a savings sub-account, opening amount 25.00; stop at review" \
  --target http://bank-sim:8001/ \
  --fault dialog \
  --member-id 12345 --account-type savings --opening-amount 25.00 \
  --max-steps 30 --max-usd 2.00 \
  --evidence-dir evidence/discovery-success \
  --overwrite
```

`--evidence-dir` is what makes it a keeper: `evidence/run_*/` is gitignored and a named folder is not.
Name it for what the run _demonstrates_ — `discovery-success`, `handoff-unknown-dialog` — not for when it
happened. If the folder already holds a run the command refuses **before** doing anything, so a capture
never costs money before failing; pass `--overwrite` to replace it.

Watch it in noVNC. The output is live:

```
-> left_click, type, left_click, wait, screenshot
   "I'll search for member 12345."
    0  left_click     allow
    1  type           allow
    ...
```

### Take control when it asks

The run reaches the sub-account form, types the amount, clicks Continue, and meets the form's own
validation: _"You must accept the account disclosure to continue."_ The model **asks for a human** rather
than ticking the disclosure checkbox itself, and the run **parks** rather than ending:

```
-> request_human
   "The form will not continue without ticking 'I have reviewed the account disclosure...'"
   ESCALATED  MODEL_REQUESTED  intervention=int_3b400bd2
   take over  : http://localhost:6080/vnc.html?autoconnect=true&resize=scale
   then run   : poetry run python -m src.cli session accept run_20260925_003244_4b61 --operator you
   and then   : poetry run python -m src.cli session resume run_20260925_003244_4b61
   waiting for a human (Ctrl-C to abandon)...
```

Leave that terminal alone. In terminal 2, paste what it printed — fixing the screen by hand in noVNC
**between** the two commands:

```bash
poetry run python -m src.cli session accept run_<id> --operator your-name
# tick the disclosure checkbox in the noVNC window
poetry run python -m src.cli session resume run_<id>
```

Terminal 1 picks it up and carries on:

```
   RESUMED  handoff #1 recorded as step 16  screen changed=False
-> left_click, wait, screenshot
   "The operator has ticked the disclosure checkbox. I'll continue to the review screen."
```

With `--fault dialog` armed it parks a **second** time on the System Notice modal (`UNKNOWN_DIALOG`).
Dismiss it with **OK** and resume again. Up to `max_handoffs` (3) — a run that keeps needing a human is
not making progress either, and stops with `MAX_HANDOFFS_EXCEEDED`.

> **The run id is not the folder name**, and does not need to be. `session accept` takes the run id and
> finds the folder by reading the `run_id` recorded inside each `intervention.json`. Mistype it and the
> error lists every run you _can_ take.

### What a good capture looks like

This is the committed capture in `evidence/discovery-success/`:

```
outcome
   SUCCESS  stop_reason=CHECKPOINT_VERIFIED
   checkpoint_verified : True
   steps               : 21
   cost                : ~$0.8366
   policy refusals     : none
   human interventions : 2 at steps [16, 20]
   steps missing probe : none
```

`CHECKPOINT_VERIFIED` rather than the model's word for it: the controller re-observes and checks that
`Review New Account`, the account type and the amount are all on screen. A claim that disagrees with the
screen returns `CHECKPOINT_FAILED` with both values, because a run that reports success on the wrong
screen produces an artifact that replays garbage.

`missing probes` must be empty — a coordinate action with no locator evidence and no stated reason cannot
be canonicalized. The mechanical verification is in
[TESTING.md](TESTING.md#verifying-a-capture-run).

**Write down what went wrong while it is fresh** — which controls the model misread, where it hesitated,
what you fixed by hand. A capture folder with no narrative is evidence without a witness. And resist
fixing a prompt problem by changing the loop: if the model picks the wrong control that is a prompt or an
affordance issue, and the loop's job is only to bound it, record it, and refuse to call it a success.

One escalation a handoff **cannot** clear: `IRREVERSIBLE_REQUIRES_APPROVAL`. Typing `session resume` means
"I have finished looking at the screen", not "I authorize this commit" — that needs an `ApprovalToken`
bound to the digest of the exact action. So a second reach for **Open Account** ends the run rather than
parking it.

---

## B. Canonicalization

A capture is a record of one afternoon: 21 steps at specific coordinates, with a human filling two holes.
A **capability** is what should happen every time, in terms replay can resolve.

### What the reduction removes

**21 recorded steps become 7:**

| Removed                          | Why                                                                                                                                                                                                    |
| -------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 7 `wait` / `screenshot` steps    | The model asked for those to see where it was. Replay waits on conditions, never on a clock                                                                                                            |
| A rejected attempt and its retry | Trace steps 13 and 17 are the same click — the app refused the first. The artifact gets **one** step, and keeps the refusal as a `text_absent` postcondition so the same failure would be caught again |
| Two human steps                  | Neither is a step. One becomes a recovery rule; the other becomes a **gap**                                                                                                                            |

Coordinates survive only under `evidence.discovery_coordinate`, to explain where a locator came from.
Replay resolves roles, labels and attributes — never a pixel.

> **What is derived and what is not.** Steps, targets, postconditions, entry, origins, provenance and gaps
> come from the trace. Input _types_, output extractors, and the capability's name and risk come from
> `src/discovery/specs.py` — deriving a public API from one observed run is overfitting. Nothing in that
> trace says a member id is always five digits.

### 1. Read the gaps before anything else

```bash
poetry run python -m src.cli canonicalize evidence/discovery-success/trace.yaml --report
```

```
reduction
   21 trace steps -> 7 artifact steps
     member-id                fill
     search                   click
     results-panel            click
     open-sub-account         click
     opening-amount           fill
     continue                 click
     verify-outcome           read

gaps
   unresolved gap after step 'opening-amount': A human intervened and no observable state changed, so
   the action they took could not be derived. The message that preceded the intervention names the
   control.
    evidence: 'You must accept the account disclosure to continue.' (trace step 13)
   unresolved gap at a position the author must choose: account_type is a declared input that no step
   enters. Replay with a different value would silently use whatever the form defaults to, and fail
   only at the checkpoint. The authored step's position must be chosen by hand.
    evidence: 'no step enters account_type; the run never set it, so a default was accepted'
   declared input(s) no step sets: account_type
```

**This artifact is not replayable, and that is the point.** It says so, it says why, and it says where. An
artifact that quietly omitted a step a human supplied would look complete and replay into the same wall
the model hit — the one failure mode that destroys an artifact's value.

Two gaps, from two unrelated causes:

- **The disclosure checkbox.** A human ticked it at trace step 16. Ticking a checkbox changes no visible
  text, so the step recorded `dom_changed: false` with both text diffs empty — the canonicalizer
  genuinely cannot see what they did. What localises it is the message three steps earlier.
- **`account_type`.** Declared in the goal, and **no step ever sets it**. `savings` is the first
  `<option>`, so the default coincided with what the run wanted and the model never touched the dropdown.
  Caught because "the checkpoint mentions it" is not the same as "a step sets it".

One came from a person, one from a coincidence. The same mechanism catches both.

### 2. Write it out

```bash
poetry run python -m src.cli canonicalize evidence/discovery-success/trace.yaml \
  --out artifacts/open_subaccount_review.yaml
```

### 3. Author the two missing steps

Open the file and paste each block **immediately before** the step it must precede. Authoring is
hand-editing, deliberately: the artifact is meant to be reviewed by humans, so editing it is the intended
workflow rather than a fallback.

**Paste before `- id: opening-amount`:**

```yaml
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
```

**Paste before `- id: continue`:**

```yaml
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
```

Both locators use the form `name=` attribute, because **that is what the server reads** — a contract, not
a coincidence. The obvious-looking alternatives are traps: `id="sel_{{suffix}}"` and
`id="chk_{{suffix}}"` are regenerated on every request, which is why the ranker records them as
rejections rather than offering them.

### 4. Close both gaps

Add `resolved_by: human` to each entry under `gaps:`:

```yaml
gaps:
  - step_after: opening-amount
    reason: A human intervened and no observable state changed ...
    resolved_by: human # <- add
  - reason: account_type is a declared input that no step enters ...
    resolved_by: human # <- add
```

Two different fields, on purpose. `authored_by` marks the **step** a human wrote; `resolved_by` marks the
**gap** as answered. Adding one without the other leaves the artifact refusing itself — which is correct,
because a step nobody vouched for is not an answer.

### 5. Check it

```bash
poetry run python -c "
from src.domain.artifact import load_artifact
c = load_artifact('artifacts/open_subaccount_review.yaml')
print('steps      :', [s.id for s in c.steps])
print('authored   :', [s.id for s in c.steps if s.authored_by])
print('unbound    :', c.unbound_inputs)
print('replayable :', c.is_replayable)
for r in c.why_not_replayable(): print('  ' + r)"
```

```
steps      : ['member-id', 'search', 'results-panel', 'open-sub-account', 'select-account-type',
              'opening-amount', 'accept-disclosure', 'continue', 'verify-outcome']
authored   : ['select-account-type', 'accept-disclosure']
unbound    : []
replayable : True
```

Before authoring, the same command prints `replayable: False`, `unbound: ['account_type']` and both gaps.
**Same file, same models** — that pair is the entire mechanism, and it is worth running both ways once to
see it. If yours does not match, compare against `tests/fixtures/capability_authored.yaml`, which is this
exact result and is checked by the test suite.

### 6. Keep it beside the run that produced it

```bash
cp artifacts/open_subaccount_review.yaml evidence/discovery-success/capability.yaml
```

Two copies on purpose: `artifacts/` is where a capability lives to be used, and the one in the evidence
folder is part of the record — an artifact next to the trace, screenshots and events it was derived from.
`provenance.source_run_id` points back at that run.

---

## C. Replay

No model, at any point. Replay needs **only the simulator**, not the sandbox — that exists to contain a
model, and there is no model here.

```bash
docker compose up -d bank-sim
poetry run playwright install chromium     # if you have not already
```

Check the artifact opens the app before running it:

```bash
poetry run python -m src.cli replay --smoke artifacts/open_subaccount_review.yaml
```

```
artifact
   corebank.open_subaccount_review v1.0.1
   steps    : 9
   rebind   : http://bank-sim:8001 -> http://127.0.0.1:8001
   origins  : ['http://127.0.0.1:8001']  <- what policy checks
entry
   frame    : ['servicing-frame'] -> http://127.0.0.1:8001/servicing/members/search
   entry check ok: '/servicing/members/search' reached inside the frame
```

The rebind is the one thing in this layer that can disable a safety control without anything appearing to
go wrong. The artifact records `http://bank-sim:8001` — the hostname the _agent_ saw inside the sandbox
network — and replay runs on the host. It maps **one origin to one origin**, and `origins` shows what the
policy engine actually gets: the runtime origin and nothing else. Allowing both, or passing an empty
allowlist, would leave the origin check running and no longer checking.

### The four runs

All four use the **same artifact**, differing only in inputs and fault profile. That is the point.

#### 1. Success, with inputs discovery never saw

```bash
poetry run python -m src.cli replay artifacts/open_subaccount_review.yaml \
  --input member_id=23456 --input account_type=savings --input opening_amount=50.00 \
  --fault default --evidence-dir evidence/replay-success
```

```
run
   inputs   : ['account_type', 'member_id', 'opening_amount']  (1 redacted in evidence)
   fault    : default
   provider : none — no model is called at any point
    0  fill           allow
    1  click          allow
    ...
    8  read           allow

outcome
   SUCCESS  stop_reason=CHECKPOINT_VERIFIED
   checkpoint_verified : True
   outputs
     review: {'member': '***56', 'account_type': 'Savings', 'opening_amount': '50.00',
              'funding_source': '****-0153'}
   steps               : 9
   cost                : $0.00 — no model was called
   policy refusals     : none
```

Member `23456`, not the `12345` discovery used — the artifact is a capability with parameters, not a
recording of one session. Try `--input account_type=checking --input opening_amount=125.50` and the review
panel follows. The declared outputs come back extracted and transformed: the member id is **masked**
because the contract says that field is sensitive.

Inputs are validated against `contract.inputs` **before** a browser opens, so a mistake costs nothing:

```bash
poetry run python -m src.cli replay artifacts/open_subaccount_review.yaml \
  --input member_id=99 --input account_type=savings --input opening_amount=50.00
#   input rejected: member_id='99' does not match ^[0-9]{5}$
```

#### 2. A domain answer is not a crash

```bash
poetry run python -m src.cli replay artifacts/open_subaccount_review.yaml \
  --input member_id=88888 --input account_type=savings --input opening_amount=50.00 \
  --evidence-dir evidence/replay-member-not-found
```

```
    0  fill           allow
    1  click          allow

outcome
   BUSINESS_OUTCOME  stop_reason=TERMINAL_DECLARATION
   code                : MEMBER_NOT_FOUND
   steps               : 2
```

Two steps, then it stops. `88888` is unseeded; the artifact's outcome rule matches _"No members found"_
and returns a business outcome rather than walking the rest of a form that cannot be filled.

#### 3. A bounded recovery

```bash
poetry run python -m src.cli replay artifacts/open_subaccount_review.yaml \
  --input member_id=23456 --input account_type=checking --input opening_amount=125.50 \
  --fault overlay --evidence-dir evidence/replay-recovery
```

Ends `SUCCESS`, and the recovery is in the evidence rather than only in the outcome line:

```bash
python3 -c "
import json
for line in open('evidence/replay-recovery/events.redacted.jsonl'):
    e = json.loads(line)
    if e.get('kind') == 'recovery': print(e['strategy'], e['attempts'], e['detail'])"
#   wait_and_retry 3 the condition cleared on its own
```

Three 500 ms looks to clear a 1200 ms delay. The rule's budget was 2 attempts until a live run showed it
failing 200 ms short — a recovery budget has to be sized against the delay it absorbs.

#### 4. A dialog the artifact learned to handle

```bash
poetry run python -m src.cli replay artifacts/open_subaccount_review.yaml \
  --input member_id=34567 --input account_type=savings --input opening_amount=75.00 \
  --fault dialog --evidence-dir evidence/replay-dialog
```

Also ends `SUCCESS`, by **dismissing** the modal:

```
   wait_and_retry 1 the condition cleared on its own
   dismiss        1 'System Notice' dismissed
```

This is the loop closing. During discovery a human dismissed that modal by hand; the canonicalizer read
the intervention, named the dialog, and wrote a `dismiss` rule with an `else: escalated` for when
dismissal stops working. So replay clears it unattended where discovery needed a person.

A dialog the artifact does **not** name is never dismissed — it escalates as `UNKNOWN_DIALOG`, because
clicking an unidentified modal away might be clicking `Confirm`. The rule is declared in the artifact
_and_ enforced in code. It has no live trigger today, since the simulator has exactly one dialog and the
artifact now knows it; see [TESTING.md](TESTING.md#one-path-with-no-live-coverage).

### Confirming no model was involved

Not a claim in a README — a field in the summary:

```bash
python3 -c "
import json; d = json.load(open('evidence/replay-success/run-summary.json'))
print('provider :', d['provider']['name'])
print('tokens   :', d['budget']['input_tokens'], 'in,', d['budget']['output_tokens'], 'out')"
#   provider : none
#   tokens   : 0 in, 0 out
```

Put that beside `evidence/discovery-success/run-summary.json` for the same workflow and the difference is
one line: `anthropic / claude-opus-5` against `none`, and thousands of tokens against zero.

---

## Where to go next

- [TESTING.md](TESTING.md) — the 396 offline tests, the 22 browser tests, and the free rehearsals to run
  before spending anything.
- [REPORT.md](REPORT.md) — the write-up: the safety model, what the gap finding demonstrates, and what is
  deliberately out of scope.
- [IMPLEMENTATION.md](IMPLEMENTATION.md) — system design.
- `discovery-loop-plan.md` and `canonicalizer-plan.md` — every step as it was built, each with the things
  the implementation contradicted.
