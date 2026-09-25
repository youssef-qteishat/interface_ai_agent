# Testing

Everything here is about **checking that the system works**. For setup and for doing a real
discovery → canonicalize → replay run, see [README.md](README.md).

Three kinds of check, in the order you would reach for them:

| Kind                                      | Cost | Needs                    | Answers                                             |
| ----------------------------------------- | ---- | ------------------------ | --------------------------------------------------- |
| [The offline suite](#the-offline-suite)   | free | nothing                  | Does the logic hold?                                |
| [Browser tests](#browser-tests)           | free | `bank-sim`               | Does the artifact still resolve against the app?    |
| [Free rehearsals](#free-rehearsals)       | free | the sandbox              | Is the plumbing alive before I spend anything?      |

---

## The offline suite

```bash
poetry run pytest
```

**396 tests**, no network, no Docker, no API key. Browser tests are excluded by
`addopts = "-q -m 'not integration'"` in `pyproject.toml`, so this is the default and it stays fast.

| File                              | Tests | What it pins                                                                            |
| --------------------------------- | ----- | --------------------------------------------------------------------------------------- |
| `test_discovery_fake_provider.py` | 37    | Request/result shapes; the five API fields that must stay absent                        |
| `test_domain_models.py`           | 35    | The action union rejects disabled members; the trace round-trips                        |
| `test_controller.py`              | 35    | Every stopping rule by name; a wrong-screen `goal_complete` fails; handoffs resume       |
| `test_replay_engine.py`           | 29    | The irreversible gate at replay time; bounds; recovery; a crash still writes a summary   |
| `test_canonicalizer.py`           | 27    | 21 steps → 7; the dedupe keeps its refusal message; both gaps; the leak gate             |
| `test_policy.py`                  | 26    | The irreversible gate; refused actions never reach the adapter                           |
| `test_conditions.py`              | 26    | Which haystack each condition reads; an absence cannot be polled for                     |
| `test_redaction.py`               | 25    | Nothing sensitive serializes; validated against captured live payloads                   |
| `test_artifact.py`                | 24    | The artifact refuses a typo, a missing extractor, a wrong major version, a scoped `url`  |
| `test_ownership.py`               | 23    | Stale versions refused; a parked loop cannot act; a resume releases the adapter           |
| `test_evidence.py`                | 20    | The leak gate; a killed run still parses                                                 |
| `test_scaling.py`                 | 20    | Coordinate conversion; clamp-vs-refuse behaviour                                         |
| `test_recorder.py`                | 19    | The trace refuses to be written incomplete; `match_count=2` survives to disk             |
| `test_locator_resolver.py`        | 14    | The `contextual_text` walk; ambiguity is a hard stop; a late candidate still wins         |
| `test_locator_ranking.py`         | 13    | The four exclusions, against the real probes; an unlocatable element yields nothing       |
| `test_cli.py`                     | 12    | A parked run is found whatever its folder is called; a capture folder is never merged     |
| `test_replay_surface.py`          | 11    | The base-URL rebind maps one way; an unmapped origin and `/dev/` are still refused        |

### The four tests worth reading

If you only look at a few, these are the ones carrying the safety argument rather than checking
plumbing.

**`test_policy.py` — a refused action never reaches the adapter.** It is not enough that the engine
returns `deny`; the test asserts the surface was never called. A decision that is recorded but not
enforced is the failure mode that looks like safety and is not.

**`test_replay_engine.py::test_a_danger_button_is_escalated_and_never_clicked`** — the same gate, at
replay time, where it is easiest to skip. The artifact was reviewed, so it is tempting to trust it — but
the artifact was reviewed against a page where `Continue` was safe and cannot know the button now carries
`danger-button`. The engine reads the **resolved element** and refuses. Asserted on both signals
(`danger-button`, and the name `Open Account`) separately, because a tenant can rename the label and a
refactor can rename the class.

**`test_conditions.py::test_a_scoped_text_condition_can_fail_where_an_unscoped_one_cannot`** — the
checkpoint's account-type assertion read the whole frame, where the submitted form's `<select>` still
renders every option. It passed for `savings`, for `checking`, and for `money_market`, which is not even
a legal value, while the review panel said `Savings`. A checkpoint that cannot fail is not a check.

**`test_replay_engine.py::test_a_dialog_the_artifact_does_not_name_is_never_dismissed`** — clicking an
unidentified modal away might be clicking `Confirm`. The test asserts the exception *and* that nothing
was clicked.

---

## Browser tests

```bash
docker compose up -d bank-sim              # bank-sim only — replay does not use the sandbox
poetry run playwright install chromium
poetry run pytest -m integration
```

**22 tests.** These are the ones that execute the committed artifact against the running application, so
they are where a drifted locator or a changed page is caught.

| File                       | Tests | What it proves                                                              |
| -------------------------- | ----- | --------------------------------------------------------------------------- |
| `test_replay_live.py`      | 8     | The four demonstration runs, plus redaction and the zero-model summary       |
| `test_conditions.py`       | 6     | Postconditions hold live; overlay detection both ways; the dialog rules      |
| `test_locator_resolver.py` | 4     | All 8 targets resolve on their first candidate; `tenant_b` falls through     |
| `test_replay_surface.py`   | 4     | The workflow is only reachable inside the frame; a paused surface refuses    |

`bank-sim` publishes 8001 to the host and has no `depends_on`, so this needs neither the sandbox nor
Xvfb nor VNC. That asymmetry is the point: the sandbox exists to contain a **model**, and replay has no
model to contain.

### What the live tests establish

```
test_the_artifact_replays_for_inputs_discovery_never_saw[savings-50.00]
test_the_artifact_replays_for_inputs_discovery_never_saw[checking-125.50]
test_an_unseeded_member_is_a_business_outcome
test_a_loading_overlay_is_waited_out_and_recorded
test_a_dialog_discovery_learned_to_dismiss_is_dismissed
test_the_run_summary_records_that_no_model_was_called
test_a_sensitive_input_does_not_reach_the_evidence
test_the_dialog_the_plan_expected_to_escalate_is_now_handled
```

Two are worth calling out.

**`checking-125.50` is the parameterization proof.** Discovery ran with member `12345` and `savings`.
This replays member `23456` with `checking` and `125.50`, and checks the **review panel directly** rather
than only through the checkpoint — an extracted value that agrees with a checkpoint reading the same
region could still both be wrong about what the app did.

**`test_a_sensitive_input_does_not_reach_the_evidence`** greps `trace.yaml`,
`events.redacted.jsonl` and `run-summary.json` for the member id. The same `Redactor` discovery uses,
wired to the artifact's `sensitive: true` inputs.

### One path with no live coverage

The replay **handoff** — park → `barrier()` blocks → `session resume` → the same step retried — is
exercised only offline, with a fake session. The real `SessionManager`, the cross-process file handshake
and `ReplaySurface.pause`/`resume` have never run together at replay time.

That is not an oversight in the tests; there is nothing to trigger it. The simulator has exactly one
dialog (`System Notice`), and the artifact now **knows** it — discovery watched a human dismiss it, so
the canonicalizer wrote a `dismiss` rule. `--fault dialog` therefore succeeds unattended. Demonstrating
the escalation live needs a second dialog the artifact has never seen, which is a fault-profile addition.

Worth knowing because the discovery layer's handoff broke the first time it was tried live, for reasons
no test caught, and this is the same mechanism.

---

## Free rehearsals

Run these before spending anything on a model. Each rules out a class of failure that would otherwise
waste a paid run.

### Does the sandbox answer?

```bash
poetry run python -m src.cli sandbox status
```

```
agent      : 0.3.0 (up 23.2s)
display    : 1280x800 on :99
chromium   : alive
cdp        : Chrome/153.0.8010.52
scale      : 1.0 (screenshot px per display px)
fault      : default
noVNC      : http://localhost:6080/vnc.html?autoconnect=true&resize=scale
```

`scale: 1.0` is deliberate — 1280x800 fits inside the model's image limits, so no scaling is needed. The
conversion code still runs, and `test_scaling.py` proves a 2560x1600 display would scale correctly.

### Do the hands work?

```bash
poetry run python -m src.cli drive
```

Watch the cursor move in noVNC. `drive` searches for member 12345, opens their detail page, and prints
what the probe records at each step — including an **ambiguous** control:

```
6. record an ambiguous control (two 'Back' buttons)
   <button> role=button name='Back'
     - role             match_count=2  <- ambiguous
     - text             match_count=2  <- ambiguous
     - css              match_count=2  <- ambiguous
   is_ambiguous    : True
   recorded as     : step 5 in trace.yaml
```

That count is the signal the canonicalizer needs to refuse a locator rather than silently pick the first
match — and it is **recorded**, not just printed. An earlier version probed this control outside the
recorded-step path, which meant the one signal the trace exists to carry appeared in no artifact at all,
only in a terminal someone had to be watching.

`drive` answers one question cheaply: **do the hands work?** When a model-driven run misbehaves later,
this separates "the model chose badly" from "the plumbing is broken" in ten seconds and for nothing. It
exits non-zero if the screen does not actually change, and it runs the full per-action path the
controller uses, so it produces a real evidence folder.

### Does the whole loop work, without a model?

```bash
poetry run python -m src.cli discover --provider fake --fault default
```

```
   provider : fake (tests/fixtures/scripts/full_workflow.yaml)
   bounds   : 20 steps, $2.00, 600s, 3 handoffs
outcome
   SUCCESS  stop_reason=CHECKPOINT_VERIFIED
   checkpoint_verified : True
   steps               : 18
   cost                : $0.00 — scripted, no model was called
```

**This is not a mock.** It is the same loop, sandbox, policy engine, probe, evidence writer and
checkpoint — only the thing choosing the actions differs, and nothing below the provider can tell. So it
proves the plumbing end to end and any failure it reports is real.

If this does not end `CHECKPOINT_VERIFIED`, stop. Something about the sandbox or the simulator has moved,
and a paid run will tell you the same thing more slowly and less clearly.

> The script ticks the disclosure checkbox. The real model declines to and asks for a human instead —
> that difference is the subject of the handoff, not a discrepancy.

### Is the API shape accepted?

See the exact request without sending it:

```bash
poetry run python -m src.cli smoke --dry-run
```

Then send it. About five cents, and it **executes nothing**:

```bash
set -a && source .env && set +a
poetry run python -m src.cli smoke --round-trip
```

```
what it asked for (NOT executed)
  left_click (549, 96)  ·  type "12345"  ·  left_click (1070, 96)
  wait 2.0              ·  screenshot            <- ended the batch as instructed
send results back
  5 result blocks, 5 carrying toolset_name  ->  accepted
cost: in 8,908  out 345  ~$0.057 (2 turns) · cache 6,586 read (43% of billed input)
```

Three things this proves:

1. **The request shape is accepted** — toolset type, `configs`, betas, `output_config`.
2. **The result shape is accepted.** `--round-trip` sends the `tool_result` blocks back, which is the
   other place the API returns a 400 (a result missing `toolset_name` is rejected). Without the second
   call the riskier half stays unproven.
3. **The coordinate space is right.** The model chose (549, 96) and (1070, 96) purely from the
   screenshot — within one pixel of coordinates measured by hand for `drive`, from a source with no
   access to those constants.

> The sandbox is stateful, so what the model sees depends on where the browser is. Run `drive` first if
> you want the search page as the starting point. A transient `APIConnectionError` is labelled as
> transient rather than printing a traceback — just retry.

### Rehearsing the handoff

The control transfer is the fiddliest part to do live, and there is no reason to practise it on a paid
run.

**Model-free, scripted:**

```bash
# terminal 1 — parks, exactly as a real capture does
poetry run python -m src.cli discover --provider fake \
  --script tests/fixtures/scripts/handoff.yaml \
  --evidence-dir evidence/handoff-fake --overwrite --max-steps 30

# terminal 2 — the commands terminal 1 printed, verbatim
poetry run python -m src.cli session accept <run_id> --operator you
# tick the disclosure checkbox in noVNC
poetry run python -m src.cli session resume <run_id>
```

**The ownership mechanics, in isolation:**

```bash
poetry run python -m src.cli fault set dialog
poetry run python -m src.cli handoff-demo
```

It drives to the review screen, hits a modal it has no rule for, and parks. In a second terminal:

```bash
poetry run python -m src.cli session status handoff-demo     # owner=HUMAN_PENDING v1
poetry run python -m src.cli session accept handoff-demo --operator you
```

Terminal 1 is **still parked**. Accepting is not resuming — the interval between them is the whole point.
Prove the version check works:

```bash
poetry run python -m src.cli session resume handoff-demo --control-version 1
#   refused: control version 1 is stale; current version is 2
```

Dismiss the modal by hand in noVNC, then `session resume handoff-demo`. Terminal 1 unblocks, re-observes,
and reports `dialogs: []`. The audit trail:

```bash
python3 -m json.tool evidence/handoff-demo/intervention.json
#   v0->v1 AUTOMATION->HUMAN_PENDING · v1->v2 ->HUMAN by you · v2->v3 ->AUTOMATION
```

> **An honest boundary:** while parked, `curl POST 127.0.0.1:8900/act` still works. The surface agent is
> a deliberately dumb executor; ownership is enforced in the orchestrator above it. That is also exactly
> how *you* act during a handoff — moving enforcement into the agent would lock the human out.

Reset afterwards: `poetry run python -m src.cli fault set default`

---

## Poking the surface agent by hand

Useful when a locator stops resolving and you need to know what the page actually offers.

```bash
open http://localhost:8900/docs            # Swagger: every endpoint, clickable
open http://localhost:8900/screenshot.png  # exactly what the agent sees
```

The endpoint that matters is `POST /probe`, which answers *what element is under this pixel, and how
would you find it again?*

**The sandbox is stateful**, so a coordinate only means something on the screen it was measured on. Get to
Member Search first — the coordinate below is the Member ID field there, and on the sub-account form the
same pixel lands on the account-type dropdown and reports `nearby_label: "Savings Checking"`:

```bash
# click Members in the left nav, then probe
curl -s -X POST 127.0.0.1:8900/act -H 'content-type: application/json' \
  -d '{"kind":"left_click","coordinate":[104,60]}'

curl -s -X POST 127.0.0.1:8900/probe -H 'content-type: application/json' \
  -d '{"x":550,"y":96}' | python3 -m json.tool
```

You should see `accessible_name: null` — the app's labels carry no `for=`
attribute, so ARIA gives nothing — but `nearby_label: "Member ID"` from a table-relative heuristic, a
stable `input[name="member_id"]` selector, and `dom_id_stability: "generated"` explaining why the id
(`inp_0a8b8b80`, different every request) was not offered as a locator.

To find coordinates without guessing, hover a control in noVNC then:

```bash
curl -s -X POST 127.0.0.1:8900/act -H 'content-type: application/json' \
  -d '{"kind":"cursor_position"}'
```

### Resolving the artifact's locators against the live app

```bash
poetry run python -m src.cli replay --probe artifacts/open_subaccount_review.yaml
```

Walks the artifact's steps, printing which candidate won for each target and every fall-through, then
each step's condition results. On an unchanged app every target resolves on its **first** candidate:

```
   member-id              contextual_text  Member ID -> input
     ok   value(23456): ok — saw 23456
   search                 role             button/Search
   results-panel          css              #results-panel table.data-table > tbody > tr.member-row
     ok   url(/servicing/members/23456): ok — saw .../servicing/members/23456
     ok   heading(Member Detail — 23456): ok — saw Member Detail — 23456
   ...
```

A fall-through is the signal that a locator has stopped working. Under `fault set tenant_b` exactly one
appears, because that theme renames the submit button:

```
   continue               contextual_text  Back -> button
     fell through: role(button/Continue): matched nothing
```

---

## Verifying an evidence folder

Any run — test or capture — leaves a folder that should stand on its own.

```bash
RUN=$(ls -td evidence/run_*/ | head -1)
find "$RUN" -type f | sort
python3 -m json.tool "$RUN/run-summary.json"
```

```
evidence/run_20260923_205936_dbf1/
  events.redacted.jsonl   one line per transition, flushed as it happens
  trace.yaml              the run-trace contract
  run-summary.json        goal, outcome, counts, budget
  final.png
  steps/000-before.png … 004-after.png
```

### Nothing sensitive reached disk

```bash
# grep exits 1 on zero matches, which is the result you want — hence `|| echo`,
# so the check reads as a pass rather than a failure.
grep -l 12345 "$RUN"/{events.redacted.jsonl,trace.yaml,run-summary.json} \
  || echo "clean: no raw member id in any text artifact"

# ...and what replaced it
grep -o 'inputs\.[a-z_]*' "$RUN/trace.yaml" | sort -u     # inputs.member_id
```

A declared input redacts to its **own placeholder** rather than a mask, so the trace is both safe and
already half-canonicalized — `/servicing/members/${inputs.member_id}` is exactly the shape the capability
artifact wants.

Screenshots are **not** redacted, by design: a blurred screenshot cannot corroborate the log beside it,
which is the whole reason a reviewer opens one. The simulator holds synthetic data, so this is safe here;
it is a decision to revisit before pointing this at anything real.

### A crashed run still leaves a readable folder

```bash
# SIGKILL rather than Ctrl-C: a drive takes ~4s and SIGINT usually lets it finish,
# which would test tidiness rather than crash safety. `pkill -P` is needed because
# $! is the poetry wrapper, not the python process doing the work.
poetry run python -m src.cli drive >/dev/null 2>&1 &
PID=$!; sleep 2; pkill -9 -P $PID; kill -9 $PID 2>/dev/null

RUN=$(ls -td evidence/run_*/ | head -1)
poetry run python -c "
from src.domain.trace import RunTrace
print('steps that survived:', len(RunTrace.from_yaml(open('$RUN/trace.yaml').read()).steps))"
find "$RUN" -name '*.tmp' | grep -q . && echo "HALF-WRITTEN" || echo "no partial writes"
```

Events are flushed per line and `trace.yaml` is rewritten atomically after every step, so the run that
died is still the one you can read.

### Verifying a capture run

A capture is the folder that gets committed, so it is worth four mechanical criteria.

```bash
RUN=evidence/discovery-success

poetry run python -c "
from src.domain.trace import RunTrace
t = RunTrace.from_yaml(open('$RUN/trace.yaml').read())
ok = True

# 1. It reached the review screen and the CHECKPOINT agreed — not the model's word.
print('outcome        :', t.outcome.status, t.outcome.stop_reason)
ok &= t.outcome.stop_reason == 'CHECKPOINT_VERIFIED'

# 2. Every click carries locator evidence, or says why it does not.
print('missing probes :', t.steps_missing_probe() or 'none')
print('missing policy :', t.steps_missing_policy() or 'none')
ok &= not t.steps_missing_probe() and not t.steps_missing_policy()

# 3. Within budget. An UNRECORDED cost must fail, not pass by default —
#    'we do not know what this cost' is not 'it was cheap'.
spend = t.budget.usd_estimate
print('cost           :', '~\$%.4f' % spend if spend is not None else 'NOT RECORDED')
ok &= spend is not None and spend < 2.0

# 4. Provenance: which model, which fault profile.
print('provider       :', t.provider.name, t.provider.model)
print('fault_profile  :', t.fault_profile or 'NOT RECORDED')
print('human steps    :', t.human_steps() or 'none')
ok &= t.provider.name == 'anthropic' and t.fault_profile is not None

print()
print('CAPTURE OK' if ok else 'NOT A KEEPER — see above')
"
```

`missing probes` must be empty: a coordinate action with no locator evidence and no stated reason is a
step that cannot be canonicalized later.

`policy = None` on a human step is deliberate, not an omission — nobody ran the allowlist against what a
person did with their own hands, and a synthesised `allow` would claim the policy engine authorized
something it never saw. The model validator enforces both directions: an automation step must carry a
decision, a human step must not.

### Verifying a replay run

```bash
RUN=evidence/replay-success

# No model was called. Not a claim in a README — a field in the summary.
python3 -c "
import json; d = json.load(open('$RUN/run-summary.json'))
print('provider :', d['provider']['name'])
print('tokens   :', d['budget']['input_tokens'], 'in,', d['budget']['output_tokens'], 'out')
print('outcome  :', d['outcome']['status'], d['outcome'].get('checkpoint_verified'))"

# What recovered, and how many attempts it took
python3 -c "
import json
for line in open('$RUN/events.redacted.jsonl'):
    e = json.loads(line)
    if e.get('kind') == 'recovery':
        print(' ', e['strategy'], e['attempts'], 'attempt(s):', e['detail'])"
```

Put a replay summary next to a discovery summary for the same workflow and the difference is one line:
`anthropic / claude-opus-5` against `none`, and thousands of tokens against zero.
