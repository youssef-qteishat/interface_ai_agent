# Computer-Use Automation System — Credit Union Ops

A computer-use agent that discovers a workflow in a hostile legacy web UI by watching pixels and
driving mouse/keyboard, records what it did as a reviewable artifact, and replays that artifact
deterministically without the model in the loop.

The interesting claim is not that a model can click through a form. It is that a model-discovered run
becomes a **reviewable contract**, that exceptional states have explicit semantics, and that a human
can take and return control of the exact live session the agent is driving.

## Repository map

```
apps/cred_union_sim/     the Credit Union Ops Simulator — the application under test
sandbox/                 the agent's sandboxed desktop (Xvfb + Chromium + VNC + surface agent)
src/
  domain/                typed spine: actions, results, the run-trace contract
  surfaces/              host-side adapter; coordinate scaling lives here
  policy/                the component whose job is to say no; plus redaction
  sessions/              control ownership and the pause barrier
  evidence/              the only component that writes a run to disk
  discovery/             the loop itself, plus the model provider and prompts
  cli.py                 everything you can run by hand
tests/                   252 tests, all offline — no API key, no Docker
discovery-loop-plan.md   the implementation plan, step by step, with findings
IMPLEMENTATION.md        system design · interface-ai-plan-v2.md  cross-surface update
REPORT.md                write-up
```

## Status

**Steps 1–13 of `discovery-loop-plan.md` are complete.** What exists today:

| Step | What it gives you                                                                                          |
| ---- | ---------------------------------------------------------------------------------------------------------- |
| 1–2  | The simulator and the sandbox desktop, in Docker, on a no-egress network                                   |
| 3    | A surface agent in the sandbox: `screenshot` / `act` / `probe` over HTTP                                   |
| 4    | The typed spine — action union, result variants, the run-trace contract                                    |
| 5    | The host-side adapter and `drive`, a model-free walkthrough                                                |
| 6    | The policy engine: origin/route allowlist, typed-input guard, irreversible-action gate                     |
| 7    | Redaction, applied before anything reaches disk                                                            |
| 8    | Real control transfer — compare-and-set ownership, pause barrier, audit trail                              |
| 9    | The evidence writer: crash-safe run folders with a redaction gate                                          |
| 10   | The model provider — validated against the live API                                                        |
| 11   | The discovery loop and `discover`: Claude drives the simulator, bounded, with every ending named           |
| 12   | The recorder: a trace that refuses to be written incomplete, and the human as a recorded actor             |
| 13   | The CLI: `sandbox up/down/status`, `fault set/show`, and a `discover` that can run the whole loop for free |

**Not built yet:** the captured evidence runs committed as artifacts (14 — see
[Capturing a real run](#capturing-a-real-run) below, which is done by hand). The loop runs end to end
against the real model today, and a human can take the screen mid-run and hand it back; what is missing
is the canonicalized, replayable artifact it will emit.

## Setup

Prerequisites:

- **Docker Desktop** (Apple Silicon build on an M-series Mac), daemon running. Give the VM ~4 GB:
  the sandbox runs Chromium.
- **Python 3.12+** and **Poetry 2.x** for the host-side orchestrator.
- An **Anthropic API key** — needed only for the model-driven runs (walkthrough steps 8 and 9).
  Everything else runs without one, including the entire test suite **and** a complete end-to-end run
  of the discovery loop via `--provider fake`.

```bash
poetry install            # includes dev deps: pytest, pytest-asyncio
cp .env.example .env      # then put your real key in .env — .env is gitignored
poetry run pytest         # 252 passed, offline
```

The code reads `ANTHROPIC_API_KEY` from the environment, not from `.env` directly, so export it
before any command that talks to the model:

```bash
set -a && source .env && set +a
```

---

# Full walkthrough

Everything implemented so far, in the order it was built. Roughly fifteen minutes. **Steps 1–7 and 9
cost nothing and need no API key** — including step 9, which runs the complete discovery loop. Step 8
makes a single call costing about five cents; step 10 lets the model drive for real, and you choose the
bound.

Open **two terminals** and a browser window — the handoffs in steps 7 and 10 need them.

## 1. Start the stack

```bash
poetry run python -m src.cli sandbox up     # docker compose up -d --wait
docker compose ps
```

`--wait` matters: it blocks until the sandbox reports _healthy_, not merely _started_. A `discover`
fired at a container that is up but whose Chromium has not finished launching fails for a reason that
has nothing to do with the thing you were testing.

Expect three services. Note the asymmetry in `PORTS`, which is deliberate:

```
bank-sim   Up (healthy)   127.0.0.1:8001->8001/tcp
relay      Up             127.0.0.1:6080->6080/tcp, 127.0.0.1:8900->8900/tcp
sandbox    Up (healthy)   6080/tcp, 8900/tcp          <- no host binding, on purpose
```

`sandbox` sits alone on an `internal: true` network so the browser it runs has **no internet access**.
A container on such a network cannot publish ports at all — Docker silently creates no host binding —
so `relay` (a two-line `socat` forwarder) sits on both networks and forwards inbound only.

## 2. Watch the desktop the agent drives

```bash
open "http://localhost:6080/vnc.html?autoconnect=true&resize=scale"
```

The servicing portal at 1280x800, no address bar, the workflow inside an iframe. Click around — this
is a real session you share with the agent, which is what makes the handoff in step 7 genuine.

> `resize=scale` fits the window without touching the remote display. Never use `resize=remote`: it
> resizes the X display itself and invalidates every coordinate in a trace.

## 3. Check the sandbox

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

`scale: 1.0` is deliberate — 1280x800 fits inside the model's image limits, so no scaling is needed.
The conversion code still runs, and a test proves a 2560x1600 display would scale correctly.

## 4. Poke the surface agent by hand

```bash
open http://localhost:8900/docs            # Swagger: every endpoint, clickable
open http://localhost:8900/screenshot.png  # exactly what the agent sees
```

The endpoint that matters is `POST /probe {"x":…,"y":…}`, which answers _what element is under this
pixel, and how would you find it again?_ Try it on the Member ID field:

```bash
curl -s -X POST 127.0.0.1:8900/probe -H 'content-type: application/json' \
  -d '{"x":550,"y":96}' | python3 -m json.tool
```

You should see `accessible_name: null` — the app's labels carry no `for=` attribute, so ARIA gives
nothing — but `nearby_label: "Member ID"` from a table-relative heuristic, a stable
`input[name="member_id"]` selector, and `dom_id_stability: "generated"` explaining why the id
(`inp_0a8b8b80`, different every request) was not offered as a locator.

To find coordinates without guessing: hover a control in the noVNC window, then

```bash
curl -s -X POST 127.0.0.1:8900/act -H 'content-type: application/json' \
  -d '{"kind":"cursor_position"}'
```

## 5. Drive the app with no model at all

```bash
poetry run python -m src.cli drive
```

Watch the cursor move in the noVNC window. `drive` searches for member 12345, opens their detail page,
and prints what the probe records at each step — including an **ambiguous** control:

```
6. record an ambiguous control (two 'Back' buttons)
   <button> role=button name='Back'
     - role             match_count=2  <- ambiguous
     - text             match_count=2  <- ambiguous
     - css              match_count=2  <- ambiguous
   is_ambiguous    : True
   recorded as     : step 5 in trace.yaml
```

That count is the signal the canonicalizer needs to refuse a locator rather than silently pick the
first match — and it is **recorded**, not just printed. An earlier version probed this control outside
the recorded-step path, which meant the one signal the trace exists to carry appeared in no artifact at
all, only in a terminal someone had to be watching.

`drive` exists to answer one question cheaply: **do the hands work?** When a model-driven run
misbehaves later, this separates "the model chose badly" from "the plumbing is broken" in ten seconds
and for nothing. It exits non-zero if the screen does not actually change.

It also runs the full per-action path the controller will use — observe → policy → probe → policy
again → act → observe → record — so it produces a **real evidence folder**.

## 6. Read the evidence it produced

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

Two properties worth checking yourself:

```bash
# Nothing sensitive reached disk. (grep exits 1 on zero matches, which is the result
# you want here — hence `|| echo`, so the check reads as a pass rather than a failure.)
grep -l 12345 "$RUN"/{events.redacted.jsonl,trace.yaml,run-summary.json} \
  || echo "clean: no raw member id in any text artifact"

# ...and what replaced it
grep -o 'inputs\.[a-z_]*' "$RUN/trace.yaml" | sort -u     # inputs.member_id
```

A declared input redacts to its **own placeholder** rather than a mask, so the trace is both safe and
already half-canonicalized — `/servicing/members/${inputs.member_id}` is exactly the shape the
capability artifact wants.

```bash
# A crashed run still leaves a readable folder.
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

## 7. Take control away from the automation

This is the piece the brief weighs most heavily. Arm the unexpected-dialog fault, then run the
model-free escalation demo.

**Terminal 1:**

```bash
poetry run python -m src.cli fault set dialog
poetry run python -m src.cli handoff-demo
```

It drives to the review screen, hits a modal it has no rule for, and **parks**:

```
ESCALATED  intervention=int_1453fcbb  reason=UNKNOWN_DIALOG
take over  : http://localhost:6080/vnc.html?autoconnect=true&resize=scale
parked — automation has stopped
```

**Terminal 2:**

```bash
poetry run python -m src.cli session status handoff-demo     # owner=HUMAN_PENDING v1
poetry run python -m src.cli session accept handoff-demo --operator you
```

Terminal 1 is **still parked**. Accepting is not resuming — the interval between them is the whole
point. Prove the version check works:

```bash
poetry run python -m src.cli session resume handoff-demo --control-version 1
#   refused: control version 1 is stale; current version is 2
```

**In the noVNC window:** dismiss the modal by hand. This is the same live session the agent was
driving, so the page you fix is the page it sees next.

**Terminal 2:**

```bash
poetry run python -m src.cli session resume handoff-demo
```

Terminal 1 unblocks, re-observes, and reports `dialogs: []` and `changed: True`. The audit trail:

```bash
python3 -m json.tool evidence/handoff-demo/intervention.json
#   v0->v1 AUTOMATION->HUMAN_PENDING · v1->v2 ->HUMAN by you · v2->v3 ->AUTOMATION
```

> **An honest boundary:** while parked, `curl POST 127.0.0.1:8900/act` still works. The surface agent
> is a deliberately dumb executor; ownership is enforced in the orchestrator above it. That is also
> exactly how _you_ act during a handoff — moving enforcement into the agent would lock the human out.

Reset the fault when you are done: `poetry run python -m src.cli fault set default`

## 8. One real model call

First, see the exact request without sending it:

```bash
poetry run python -m src.cli smoke --dry-run
```

Then send it. This costs about five cents and **executes nothing**:

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
2. **The result shape is accepted** — `--round-trip` sends the `tool_result` blocks back, which is the
   other place the API returns a 400 (a result missing `toolset_name` is rejected). Without the second
   call, the riskier half stays unproven.
3. **The coordinate space is right.** The model chose (549, 96) and (1070, 96) purely from the
   screenshot — within one pixel of coordinates measured by hand for `drive`, from a source with no
   access to those constants.

> **What you see depends on where the browser is.** The sandbox is stateful, so if you run this right
> after the handoff demo the model starts from the review screen instead of search. In one such run it
> zoomed to read the panel, then called `goal_complete` with
> `Finalised: "No — 'Open Account' not clicked"` — respecting the stop-at-review rule and returning
> exactly the structured outputs a capability artifact wants. Run `drive` first if you want the search
> page as the starting point.
>
> A transient `APIConnectionError` is also possible; the CLI labels it as transient rather than
> printing a traceback. Just retry.

## 9. Run the whole loop for free

Before spending anything, run the entire discovery loop with a scripted provider instead of a model:

```bash
poetry run python -m src.cli discover --provider fake --fault default
```

```
   provider : fake (tests/fixtures/scripts/full_workflow.yaml)
   fault    : default
   bounds   : 20 steps, $2.00, 600s, 3 handoffs

-> left_click, type, left_click, left_click, wait, screenshot
   "fill the opening amount, accept the disclosure, continue to review"
   12  left_click     allow
   ...
outcome
   SUCCESS  stop_reason=CHECKPOINT_VERIFIED
   checkpoint_verified : True
   steps               : 18
   cost                : $0.00 — scripted, no model was called
```

This is not a mock. It is the **same loop, the same sandbox, the same policy engine, probe, evidence
writer and checkpoint** — only the thing choosing the actions differs, and everything below the
provider cannot tell. So it proves the plumbing end to end, produces a real evidence folder, and any
failure it reports is a real failure.

Run it before every paid run. A broken sandbox, a moved control or a stale container costs nothing to
discover here and a whole run to discover afterwards.

> The script ticks the disclosure checkbox. The real model declines to, and asks for a human instead —
> that difference is the subject of the handoff section below, not a discrepancy.

## 10. Let the model actually drive

Everything up to here was either model-free or a single call that executed nothing. This is the loop.

The sandbox is stateful, so `discover` clicks **Members** first to get back to a known screen — pass
`--no-reset` if you want it to start from wherever the browser happens to be. Keep `--max-steps` low
at first: every step is a real call.

```bash
set -a && source .env && set +a
poetry run python -m src.cli discover --fault default --max-steps 8
```

Watch it in noVNC while it runs. The output is live:

```
-> left_click, type, left_click, wait, screenshot
   "I'll search for member 12345."
    0  left_click     allow
    1  type           allow
    2  left_click     allow
    3  wait           allow
    4  screenshot     allow

-> left_click, wait, screenshot
   "Member 12345 (Martinez, J.) was found. Opening the member record."
    5  left_click     allow
    ...

outcome
   FAILURE  stop_reason=MAX_STEPS
   code                : MAX_STEPS_EXCEEDED
   steps               : 8
   cost                : ~$0.1002 over 2 turn(s)  (cache 19% of billed input)
   policy refusals     : none
   steps missing probe : none
```

**That is a pass.** `MAX_STEPS_EXCEEDED` means the loop bounded itself, which is the behaviour being
tested; it is not a crash. Every ending has its own name — `CHECKPOINT_VERIFIED`, `CHECKPOINT_FAILED`,
`NO_PROGRESS`, `BUDGET_EXCEEDED`, `WALL_CLOCK_EXCEEDED`, `INVALID_ACTIONS_EXCEEDED`, `CANCELLED`,
`ESCALATION` — so "it stopped" is never the whole story. Note that 8 steps cost only 2 calls: the model
batches four or five actions per turn.

Read the evidence the same way as section 6, then check the things that matter most:

```bash
RUN=$(ls -td evidence/run_*/ | head -1)

poetry run python -c "
from src.domain.trace import RunTrace
t = RunTrace.from_yaml(open('$RUN/trace.yaml').read())
print('outcome        :', t.outcome.status, t.outcome.stop_reason)
# s.policy is None on a human step — a person's actions were never policy-checked.
print('policy denials :', [s.policy.code for s in t.steps
                           if s.policy and s.policy.decision != 'allow'])
print('missing probes :', t.steps_missing_probe())
print('human steps    :', t.human_steps())
"

# grep exits 1 when it finds nothing, so the || is load-bearing:
grep -l 12345 "$RUN"/*.jsonl "$RUN"/*.yaml "$RUN"/*.json 2>/dev/null \
  || echo "clean: nothing raw on disk"
```

`missing probes` must be empty — a coordinate action with no locator evidence and no stated reason is
a step that cannot be canonicalized later.

### Going further: a human rescuing the run

```bash
poetry run python -m src.cli discover --max-steps 30     # ~$0.80
```

A run of this length reaches the sub-account form, types the amount, clicks Continue, and meets the
form's own validation: _"You must accept the account disclosure to continue."_ The model asks for a
human rather than ticking the disclosure checkbox itself — and the run **parks** rather than ending:

```
-> request_human
   "The form will not continue without ticking "I have reviewed the account disclosure..."
   ESCALATED  MODEL_REQUESTED  intervention=int_3b400bd2
   take over  : http://localhost:6080/vnc.html?autoconnect=true&resize=scale
   then run   : python -m src.cli session accept run_20260924_222419_9317
   and then   : python -m src.cli session resume run_20260924_222419_9317
   waiting for a human (Ctrl-C to abandon)...
```

Leave that terminal alone. In a second terminal on the host, take the screen, fix it by hand in the
noVNC window, and hand it back:

```bash
poetry run python -m src.cli session accept run_<id> --operator your-name
# tick the disclosure checkbox in noVNC
poetry run python -m src.cli session resume run_<id>
```

Terminal 1 picks it up and carries on:

```
   RESUMED  handoff #1 recorded as step 16  screen changed=False
-> left_click, wait, screenshot
   "The operator has ticked the disclosure checkbox. I'll continue to the review screen."
```

With `fault set dialog` armed it will park a second time on the System Notice modal (`UNKNOWN_DIALOG`);
dismiss it with **OK** and resume again. Up to `max_handoffs` (3) times — a run that keeps needing a
human is not making progress either, and stops with `MAX_HANDOFFS_EXCEEDED`.

Afterwards, the handoff is in the artifact:

```bash
python3 -m json.tool "$RUN/run-summary.json" | grep -E 'human_steps|stop_reason'
poetry run python -c "
from src.domain.trace import RunTrace
t = RunTrace.from_yaml(open('$RUN/trace.yaml').read())
for i in t.human_steps():
    s = t.steps[i]
    print(i, s.action['reason'], 'operator =', s.action['operator'], 'policy =', s.policy)
"
```

`policy = None` on those steps is deliberate, not an omission: nobody ran the allowlist against what a
person did with their own hands, and a synthesised `allow` would be a claim that the policy engine
authorized something it never saw. The model validator enforces both directions — an automation step
must carry a decision, a human step must not.

> **One thing the human step does not capture.** It records a DOM diff — `new_text` for what appeared,
> `removed_text` for what left — so dismissing a dialog reads clearly, but ticking a checkbox shows as
> `dom_changed: false` with both lists empty, because no visible text changed either way. The
> before/after screenshots show it. Control _state_ is the gap, and closing it is the injected observer
> that belongs to the escalation layer.

> **The disclosure checkbox is an open design question, not a bug.** It is an _unclassified_ control —
> the policy engine has no rule for it, so whether the agent may accept a disclosure on a member's
> behalf is left to the model. It escalates, which is defensible. But it does so because this model is
> cautious, not because anything requires it, and a less cautious one would tick the box unchallenged.
> The fix is a rule, not a better prompt.

One escalation a handoff **cannot** clear: `IRREVERSIBLE_REQUIRES_APPROVAL`. Typing `session resume`
means "I have finished looking at the screen", not "I authorize this commit" — that needs an
`ApprovalToken` bound to the digest of the exact action. So a second reach for **Open Account** ends the
run rather than parking it.

If the model does reach the review screen and calls `goal_complete`, the controller does **not** take
its word for it: it re-observes and checks that `Review New Account`, the account type and the amount
are all on screen. A claim that disagrees with the screen returns `CHECKPOINT_FAILED` with both the
expected and observed values, because a run that reports success on the wrong screen produces an
artifact that replays garbage.

## 11. Run the tests

```bash
poetry run pytest -v
```

252 tests, no network, no Docker, no API key:

| File                              | Tests | Pins                                                                                  |
| --------------------------------- | ----- | ------------------------------------------------------------------------------------- |
| `test_domain_models.py`           | 35    | The action union rejects disabled members; the trace round-trips                      |
| `test_discovery_fake_provider.py` | 37    | Request/result shapes; the five API fields that must stay absent                      |
| `test_policy.py`                  | 26    | The irreversible gate; refused actions never reach the adapter                        |
| `test_redaction.py`               | 25    | Nothing sensitive serializes; validated against captured live payloads                |
| `test_ownership.py`               | 23    | Stale versions refused; a parked loop cannot act; a resume releases the adapter       |
| `test_evidence.py`                | 20    | The leak gate; a killed run still parses                                              |
| `test_scaling.py`                 | 20    | Coordinate conversion; clamp-vs-refuse behaviour                                      |
| `test_controller.py`              | 35    | Every stopping rule by name; a wrong-screen `goal_complete` fails; handoffs resume    |
| `test_recorder.py`                | 19    | The trace refuses to be written incomplete; `match_count=2` survives to disk          |
| `test_cli.py`                     | 12    | A parked run is found whatever its folder is called; a capture folder is never merged |

---

# Capturing a real run

Everything in the walkthrough is a **test run**: throwaway, bounded low, aimed at answering "does this
work?". A **capture run** is different in kind — it produces the artifact that gets committed, reviewed
and eventually canonicalized into a replayable capability. It is the manual part of Step 14.

The difference is not the command. It is the preparation, the bounds, where it writes, and what you
check afterwards.

|                                  | Test run                                           | Capture run                                 |
| -------------------------------- | -------------------------------------------------- | ------------------------------------------- |
| Provider                         | `--provider fake`, or `anthropic` with tiny bounds | `--provider anthropic`                      |
| Cost                             | $0, or a few cents                                 | ~$0.30–1.10 depending on handoffs           |
| Starting state                   | whatever the last run left                         | deliberately cold and known                 |
| Fault profile                    | whatever is armed                                  | set explicitly, so the trace records it     |
| Writes to                        | `evidence/run_<timestamp>/` — **gitignored**       | `evidence/<name>/` — **committed**          |
| Bounds                           | `--max-steps 8` to prove the loop bounds itself    | generous enough to actually finish          |
| A stop like `MAX_STEPS_EXCEEDED` | a pass — the loop bounded itself                   | a failed capture; raise the bound and rerun |
| Afterwards                       | glance at the outcome line                         | verify four criteria, then commit           |

## Before you spend anything

The point of this sequence is that each step rules out a class of failure that would otherwise waste a
paid run.

```bash
# 1. Cold stack. The sandbox is stateful; a container that has been up for days has
#    a browser sitting on some page from a previous experiment.
poetry run python -m src.cli sandbox down
poetry run python -m src.cli sandbox up

# 2. The hands work.                                       (~10s, free)
poetry run python -m src.cli drive

# 3. The whole loop works, against this exact sandbox.     (~40s, free)
poetry run python -m src.cli discover --provider fake --fault default

# 4. The API shape is accepted and you have credit.        (~$0.05)
set -a && source .env && set +a
poetry run python -m src.cli smoke --round-trip
```

If step 3 does not end `CHECKPOINT_VERIFIED`, stop. Something about the sandbox or the simulator has
moved, and a paid run will only tell you the same thing more slowly and less clearly.

## The capture run

```bash
poetry run python -m src.cli fault set default
set -a && source .env && set +a

poetry run python -m src.cli discover \
  --goal "Find member 12345 and prepare a savings sub-account, opening amount 25.00; stop at review" \
  --target http://bank-sim:8001/ \
  --fault overlay \
  --member-id 12345 --account-type savings --opening-amount 25.00 \
  --max-steps 30 --max-usd 2.00 \
  --evidence-dir evidence/discovery-success
```

`--evidence-dir` is what makes it a keeper: `evidence/run_*/` is gitignored, and a named folder is not.
Choose the name for what the run _demonstrates_ — `discovery-success`, `handoff-unknown-dialog`,
`business-outcome-not-found` — not for when it happened.

**If that folder already holds a run, the command refuses before doing anything:**

```
evidence/discovery-success already contains 44 file(s) from an earlier run.
Pass --overwrite to replace it, or choose another --evidence-dir.
  Nothing was run and nothing was written — this is checked first, on
  purpose, so a capture never costs money before failing.
```

Add `--overwrite` to replace it, or rename the previous attempt if it is worth keeping. Merging is not
offered: `events.redacted.jsonl` would be appended to, the previous run's step screenshots would
survive as orphans, and `run-summary.json` would list them as its own — a folder that looks complete
and is not.

**Watch it in noVNC while it runs.** It will very likely park at the disclosure checkbox and wait for
you. That is not a failure; it is the mechanism working, and a capture that includes a real handoff is
more interesting than one that does not. It prints exactly what to run:

```
   ESCALATED  MODEL_REQUESTED  intervention=int_99b529ac
   take over  : http://localhost:6080/vnc.html?autoconnect=true&resize=scale
   then run   : poetry run python -m src.cli session accept run_20260925_003244_4b61 --operator you
   and then   : poetry run python -m src.cli session resume run_20260925_003244_4b61
   evidence   : evidence/discovery-success
   waiting for a human (Ctrl-C to abandon)...
```

Paste those into the second terminal, fixing the screen by hand in noVNC between the two.

> **The run id is not the folder name**, and it does not need to be. `session accept` takes the run id
> and finds the folder by reading the `run_id` recorded inside each `intervention.json` — so
> `--evidence-dir evidence/discovery-success` and `session accept run_2026…` work together. If you
> mistype it, the error lists every run you _can_ take. You can also pass the folder path directly.

## Rehearsing the handoff for free

The control transfer is the fiddliest part to do live, and there is no reason to practise it on a paid
run. This parks and resumes against the real sandbox with a scripted provider:

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

Terminal 1 records the handoff as a human step and carries on to the review screen. Costs nothing, and
exercises the same code path a capture does.

## Verifying the capture

Four criteria, all mechanical:

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
#    'we do not know what this cost' is not the same as 'it was cheap'.
spend = t.budget.usd_estimate
print('cost           :', '~\$%.4f' % spend if spend is not None else 'NOT RECORDED')
print('bounds         :', t.budget.max_steps, 'steps,', len(t.steps), 'used')
ok &= spend is not None and spend < 2.0

# 4. Provenance: which model, which fault profile, which target.
print('provider       :', t.provider.name, t.provider.model)
print('fault_profile  :', t.fault_profile or 'NOT RECORDED')
print('human steps    :', t.human_steps() or 'none')
ok &= t.provider.name == 'anthropic' and t.fault_profile is not None

print()
print('CAPTURE OK' if ok else 'NOT A KEEPER — see above')
"

# 5. And nothing sensitive is about to be committed.
grep -rl 12345 "$RUN" --include='*.yaml' --include='*.json' --include='*.jsonl' \
  || echo 'clean: no raw member id in any text artifact'
```

Screenshots are **not** redacted, by design — a blurred screenshot cannot corroborate the log beside
it, which is the whole reason a reviewer opens one. The simulator holds synthetic data, so this is safe
here; it is a decision to revisit before pointing this at anything real.

A complete capture is about **2 MB** (44 screenshots for a 22-step run), which is fine to commit.

## What to record alongside it

The plan asks for this, and it is the part that is genuinely hard to reconstruct later: **what actually
went wrong, and how many steps it took.** Write it down while it is fresh — which controls the model
misread, where it hesitated, what you had to do by hand, and whether you changed the prompt between
attempts. A capture folder with no narrative is evidence without a witness.

Resist fixing a prompt problem by changing the loop. If the model picks the wrong control, that is a
prompt or a simulator-affordance issue; the loop's job is only to bound it, record it, and refuse to
call it a success.

---

## Ports

| Port | Bound to    | Published by | Service                     |
| ---- | ----------- | ------------ | --------------------------- |
| 8001 | `127.0.0.1` | `bank-sim`   | Credit Union Ops Simulator  |
| 6080 | `127.0.0.1` | `relay`      | noVNC — watch and take over |
| 8900 | `127.0.0.1` | `relay`      | Sandbox surface agent       |

Every port is bound to loopback, so nothing is reachable from the local network. The sandbox's own VNC
server (5900) is bound to container-loopback and never published — noVNC on 6080 is the only way in.

## The simulator

Member search → member detail → open sub-account → review. Seeded members include `12345`
(John Martinez, two accounts) and `23456`; `88888` is unseeded and exercises the not-found path.

It is deliberately hostile: the workflow is in an iframe, element ids are regenerated every request,
labels carry no `for=` attribute, two buttons share the label "Back", and one control is icon-only
with no accessible name at all.

```bash
open http://localhost:8001/       # the portal, outside the sandbox
```

## Network topology and why it matters

- **`sandbox_net`** (`internal: true`) — agent traffic, no egress. The containment boundary, enforced
  by Docker rather than by asking the model nicely.
- **`host_net`** — a normal bridge, so published ports work from the host.

`bank-sim` and `relay` join both; the **sandbox joins only `sandbox_net`**.

```bash
docker compose exec sandbox curl -m 5 https://example.com   # fails — no egress
docker compose exec sandbox curl -s http://bank-sim:8001/   # works — the target
```

Chromium runs as non-root (uid 10001) **and** with `--no-sandbox`. These are not alternatives:
Docker's default seccomp profile blocks the unprivileged user namespaces Chromium's own sandbox
requires, so it cannot start regardless of user, and the settings that would permit it
(`seccomp=unconfined`, `SYS_ADMIN`) would weaken the container far more than disabling Chromium's
internal sandbox. Containment here is the non-root user, the no-egress network, and the policy engine.

### Fault injection is an operator capability

```bash
poetry run python -m src.cli fault set overlay    # default | overlay | dialog | session | tenant_b
curl -s localhost:8001/dev/fault-profile          # what is armed
curl -s localhost:8001/dev/audit-log              # servicing actions during a run
```

| Profile    | Effect                                  | Demonstrates                                  |
| ---------- | --------------------------------------- | --------------------------------------------- |
| `default`  | none                                    | Happy-path discovery and replay               |
| `overlay`  | 1200 ms loading overlay                 | Bounded wait/retry on a recoverable condition |
| `dialog`   | unexpected modal after review loads     | Escalation to a human (walkthrough step 7)    |
| `session`  | session-expiry warning banner           | Reauthentication escalation                   |
| `tenant_b` | different theme, "Continue" → "Proceed" | Cross-tenant replay via locator fallback      |

Fault profiles are switched by the operator, never by the thing under test. Be precise about what
enforces that, because the network alone does not:

- `/dev/*` is **not** linked from any page, so it is unreachable by clicking.
- Chromium runs in `--app=` mode with no address bar, and URL entry (`ctrl+l`) is not in the agent's
  action vocabulary.
- The policy engine denies the `/dev/` route prefix on every observation, before every action.

What is **not** true: that `sandbox_net` blocks those routes. `bank-sim` is a peer on that network and
serves `/dev/*` on the same port, so `http://bank-sim:8001/dev/fault-profile` answers from inside the
sandbox — verified, not assumed. Containment of `/dev/` rests on the action vocabulary and the policy
engine; the network's job is blocking **egress**, which it does.

## Safety model, in one place

| Control                | Where it lives            | What it actually guarantees                                                                                                              |
| ---------------------- | ------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| Action vocabulary      | `src/domain/actions.py`   | Disabled members have no model at all, so they fail parsing and can never execute                                                        |
| Origin/route allowlist | `src/policy/engine.py`    | Every frame URL is checked, not just the main document — the workflow lives in an iframe                                                 |
| Typed-input guard      | `src/policy/engine.py`    | Only a declared input's value can be typed; a value the run never declared cannot be entered                                             |
| Irreversible gate      | `src/policy/engine.py`    | The commit button is `escalate`, not `deny` — a human with a digest-bound approval token can authorize that exact click and nothing else |
| Redaction              | `src/policy/redaction.py` | Applied before serialization, then re-checked at the writer, which refuses to write on a leak                                            |
| Ownership              | `src/sessions/`           | Compare-and-set on a control version; a parked loop cannot act and a stale writer cannot win                                             |
| No egress              | `compose.yaml`            | The browser cannot reach the internet                                                                                                    |

The system prompt also tells the model that on-screen text is data, never instruction. **That is a
hint, not a control** — a model that ignores it changes nothing, because the policy engine never reads
the page and the action union never widens. The guarantees above are the ones that hold.

## What is not built yet

- **Step 14 — the committed evidence folders** (`evidence/discovery-success/` and friends). Everything
  needed to produce them exists; it is a manual exercise, described in
  [Capturing a real run](#capturing-a-real-run).
- **An unclassified control.** The sub-account form's disclosure checkbox has no policy rule, so
  whether the agent may accept a disclosure on a member's behalf is left to the model's judgement. In
  a live run it escalated rather than ticking the box — a defensible default, but it happened because
  this model was cautious, not because the system required it. That needs an explicit decision.
- **A human step cannot see control state.** The handoff record is a text diff, so a dismissed dialog
  reads clearly and a ticked checkbox reads as no change at all. The screenshots cover it; the real fix
  is the injected observer that belongs to the escalation layer.
- **Beyond this layer** — the canonicalizer that turns a trace into a capability artifact, and the
  replay engine that executes one without a model.
