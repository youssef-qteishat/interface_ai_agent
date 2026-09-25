# Discovery Loop / Driver — Implementation Plan

Companion to `IMPLEMENTATION.md` (v1 system plan), `interface-ai-plan-v2.md` (cross-surface update),
and `credit_union_sim_design_doc.md` (the target application, already built under `apps/cred_union_sim/`).

This document covers **one layer only**: the discovery loop and the OS-level computer-use driver that
executes its actions. It is written to be worked through in order, by hand or by delegating individual
steps to Claude Code. Start with §0 (Docker Desktop setup); the implementation steps in §8 assume a
working `docker compose`.

---

## 0. Prerequisites: Docker Desktop setup

Everything in §8 from Step 1 onward assumes a working `docker compose`. Do this section first — it
runs **before** Step 0 of §8 ("Lock decisions"), and its sub-steps are numbered 0.1–0.7 to keep them
distinct from the implementation steps.

### 0.1 Where this machine already stands

Verified on this machine on 2026-09-21:

| Item               | State                                             | Action                                         |
| ------------------ | ------------------------------------------------- | ---------------------------------------------- |
| Docker Desktop app | Installed at `/Applications/Docker.app`           | Nothing to install                             |
| Docker CLI         | `29.7.2`                                          | Fine                                           |
| Compose plugin     | `v5.3.1` (`docker compose`, not `docker-compose`) | Use the `docker compose` spelling everywhere   |
| Active context     | `desktop-linux`                                   | Correct; leave it alone                        |
| Daemon             | **Not running**                                   | §0.2                                           |
| Host architecture  | `arm64` (Apple Silicon)                           | §0.4 — build native, do not force amd64        |
| Host RAM           | 8 GB total                                        | §0.3 — the binding constraint for this project |

So this is a start-and-configure job, not an install job. If you move to another machine, install
Docker Desktop for Mac (Apple Silicon build) from docker.com first, then continue here.

### 0.2 Start the daemon and confirm the CLI can reach it

**Owner:** manual · **Time:** 5 min

- [x] Launch Docker Desktop (`open -a Docker`) and wait for the whale icon to stop animating.
- [ ] Optional but recommended for a multi-day project: Settings → General → **Start Docker Desktop
      when you sign in**, so a reboot mid-project doesn't look like a broken sandbox.
- [ ] Confirm the daemon answers:

  ```bash
  docker info --format '{{.ServerVersion}} {{.Architecture}} {{.NCPU}}cpu {{.MemTotal}}'
  docker run --rm hello-world
  ```

**Verification:** `docker info` prints a server version and a non-zero CPU/memory line. While the
daemon is down it fails with `Cannot connect to the Docker daemon at
unix:///Users/<you>/.docker/run/docker.sock` — that error means "Docker Desktop isn't running", never
"the Compose file is wrong". Recognizing it on sight saves an hour later.

### 0.3 Allocate VM resources (the one setting that matters on 8 GB)

**Owner:** manual · **Time:** 10 min including the VM restart

Docker Desktop runs a Linux VM, and the containers only see what you give that VM. This project runs
Chromium inside it, which is the heaviest thing here.

- [x] Settings → **Resources**:

  | Setting         | Value on this machine                         | Why                                                                                                                  |
  | --------------- | --------------------------------------------- | -------------------------------------------------------------------------------------------------------------------- |
  | CPUs            | 4                                             | Chromium rendering plus `scrot` screenshots; below 2 the loop feels broken rather than slow                          |
  | Memory          | **4 GB**                                      | Chromium + Xvfb settle around 1–1.5 GB, the sim ~150 MB; 4 GB leaves headroom without starving macOS on an 8 GB host |
  | Swap            | 1–2 GB                                        | Cheap insurance against an OOM kill mid-run                                                                          |
  | Disk image size | ≥ 32 GB                                       | Two images plus build layers; `debian:bookworm-slim` + Chromium is ~1 GB                                             |
  | Virtualization  | Apple Virtualization framework + **VirtioFS** | Default and fastest; only matters if you bind-mount (§0.5)                                                           |

- [x] Apply & Restart, then re-run the `docker info` line from §0.2 and check the memory figure changed.
- [x] Do **not** enable Kubernetes. Nothing here uses it and it eats ~1 GB.

**Verification:** `docker info` reports the memory you set. With 4 GB to the VM, expect to keep Chrome
on the host modest while a run is in flight — an 8 GB machine running Docker Desktop, a browser, and
an editor is genuinely tight, and an OOM-killed container exits with code **137**, which is worth
memorizing now (§10).

### 0.4 Apple Silicon: stay on arm64

**Owner:** manual (a decision, not a task) · **Time:** 5 min to read

- [x] Build both images **natively for arm64**. Do not put `platform: linux/amd64` in `compose.yaml`.
- [x] `debian:bookworm-slim` has an arm64 `chromium` package, so nothing in §8 Step 2 needs emulation.
- [x] Emulating x86 Chromium under Rosetta/QEMU is the trap here: it is slow enough to change the
      loop's timing characteristics and it crashes in ways that look like driver bugs. If you ever see
      a suggestion to add `--platform linux/amd64` to fix a build, treat that as a signal the package
      name is wrong, not the architecture.
- [x] Record this in your decision notes: the report should say the sandbox is arch-native and why.

**Verification:**

```bash
docker run --rm debian:bookworm-slim dpkg --print-architecture   # → arm64
docker run --rm debian:bookworm-slim sh -c \
  'apt-get update -qq && apt-cache policy chromium | head -3'     # chromium is available
```

### 0.5 File sharing and the development loop

**Owner:** manual · **Time:** 5 min

The Compose files in §8 **copy** source into the images, so no file sharing is strictly required. But
rebuilding the sandbox image after every edit to `surface_agent.py` is a bad inner loop.

- [x] Confirm the repo path is inside an allowed shared path: Settings → Resources → File sharing
      defaults to `$HOME`, and this repo is under `~/Documents/`, so it already qualifies.
- [ ] During Steps 3–5, bind-mount the agent for fast iteration and drop the mount before capturing
      evidence:

  ```yaml
  # sandbox service, development only
  volumes:
    - ./sandbox:/opt/sandbox:ro
  ```

- [x] Keep `evidence/` on the **host** and never inside a container. Evidence is a committed
      deliverable; a container filesystem is disposable.

**Verification:** edit a string in `sandbox/surface_agent.py`, `docker compose restart sandbox`, and
see the change in `curl 127.0.0.1:8900/health` without a rebuild.

### 0.6 Preflight the four Docker behaviours this layer actually depends on

**Owner:** manual · **Time:** 15 min

Check these now, in isolation. Each one, if broken, produces a confusing failure several steps later.

- [x] **Port 8001 is free on the host.** Earlier work ran the sim natively with
      `uvicorn --port 8001`; Compose will publish the same port and fail with `address already in
  use`. Stop the native process first:

  ```bash
  lsof -ti:8001 | xargs -r kill
  lsof -ti:6080 ; lsof -ti:8900      # should print nothing
  ```

- [x] **`shm_size` works**, since Chromium dies on the 64 MB default (§10):

  ```bash
  docker run --rm --shm-size=1g debian:bookworm-slim df -h /dev/shm   # → 1.0G
  ```

- [x] **An `internal: true` network really has no egress** — this is load-bearing for the safety
      argument, so verify the mechanism before you rely on it:

  ```bash
  docker network create --internal preflight_net
  docker run --rm --network preflight_net debian:bookworm-slim \
    sh -c 'timeout 5 getent hosts example.com || echo "no egress — correct"'
  docker network rm preflight_net
  ```

- [ ] **Published ports bind to loopback only.** Every port in §8 is written
      `127.0.0.1:<port>:<port>` rather than `<port>:<port>`, so the sandbox and the surface agent are
      not exposed on your LAN. Confirm you can reach a published port and that the bind address is
      `127.0.0.1` in `docker ps` output.

**Verification:** all four checks pass. Note the results in your decision notes — the egress check in
particular is worth one line in `REPORT.md`, because "the browser had no internet access" is a claim a
reviewer may well probe.

### 0.7 Docker commands you will use constantly

Keep these to hand; they replace most guesswork during Steps 1–14.

```bash
docker compose up -d --build          # start everything, rebuilding changed images
docker compose ps                     # what is running, and on which ports
docker compose logs -f sandbox        # entrypoint/Chromium/agent output — first stop for any failure
docker compose exec sandbox bash      # shell inside the desktop container
docker compose exec sandbox curl -s http://bank-sim:8001/ | head   # service DNS actually resolves
docker compose restart sandbox        # after a bind-mounted code edit
docker compose down -v                # stop and remove volumes (the sim's DB is in-memory anyway)
docker stats --no-stream              # live memory use — watch this on an 8 GB host
docker system prune -f                # reclaim build cache when the disk image fills
```

**Exit criterion for §0:** `docker compose version` and `docker info` both succeed, the VM has 4 GB,
the arm64 and `shm_size` checks pass, the internal-network check shows no egress, and ports 8001,
6080, and 8900 are free on the host. Only then start §8.

---

## 1. Scope of this layer

**In scope**

- A sandboxed Linux desktop (Xvfb + Chromium + VNC) that the driver reaches into.
- An OS-level driver: screenshot in, mouse/keyboard events out, no DOM knowledge required.
- A read-only semantic probe alongside the driver, so a click coordinate can be turned into a DOM
  element later (this is what makes canonicalization possible at all — see §7).
- The model loop: observe → propose one batch of typed actions → validate → execute → record → repeat.
- Stopping rules, loop detection, budgets, cancellation.
- A recorder that writes a **run trace** (not a capability artifact) plus evidence.
- CLI entry point: `discover`.

**Out of scope (deliberately, with the seam preserved)**

| Deferred                               | Why it is safe to defer                   | What this layer must leave behind                                                              |
| -------------------------------------- | ----------------------------------------- | ---------------------------------------------------------------------------------------------- |
| Canonicalizer                          | Needs a real trace to canonicalize        | Trace must carry locator candidates, frame path, uniqueness counts, pre/post observations (§7) |
| Replay engine / locator resolver       | Consumes the artifact, not the loop       | Nothing — replay never imports discovery code                                                  |
| Operator page / intervention UI        | Only needed once a run actually escalates | Ownership state machine + pause barrier implemented now (Step 8), UI later                     |
| Artifact schema (`domain/artifact.py`) | Written with the canonicalizer            | Trace field names chosen to map onto artifact fields 1:1                                       |
| Desktop mock second surface            | Cheap add-on once the loop works          | Sandbox image ships `python3-tk` so no image rebuild is needed later (Step 3)                  |

**Non-goal:** a UI for managing runs, artifacts, or capabilities. The interface to this layer is the
CLI. The only UI the whole project needs is the small operator handoff page, which belongs to the
escalation layer, not this one.

---

## 2. Where this layer sits

Three processes, three responsibilities. The container is not a peer of the agent loop — it is the
hands and eyes the loop reaches into.

```text
macOS host
│
├── Python orchestrator  (this layer — the brain)
│     src/cli.py discover
│     src/discovery/{controller,model_provider,recorder}.py
│     src/surfaces/x11_computer.py      ── HTTP ──┐
│     src/policy, src/sessions, src/evidence      │
│     calls the Anthropic Messages API            │
│                                                 │
└── Docker Compose                                │
      ├── relay        (socat)  ◄──────────────────┘  127.0.0.1:8900 surface agent
      │     on sandbox_net + host_net                 127.0.0.1:6080 noVNC (human window)
      │     forwards INBOUND only, so the sandbox keeps its no-egress property
      │     exists because Docker publishes NO ports for an internal-only container
      │          │ 6080 / 8900 over sandbox_net
      │          ▼
      ├── sandbox      (hands + eyes)   internal-only: no published ports, no egress
      │     Xvfb :99 @ 1280x800, openbox
      │     Chromium --kiosk --app=http://bank-sim:8001/  (non-root + --no-sandbox)
      │     x11vnc (loopback 5900) + noVNC, xdotool, scrot
      │     surface_agent.py  (screenshot / act / probe)
      │     Chromium CDP on localhost:9222 (container-internal, read-only)
      │
      └── bank-sim     (target app — already built)
            uvicorn apps.cred_union_sim.server:app --host 0.0.0.0 --port 8001
            reachable from sandbox as http://bank-sim:8001
            published to host on 127.0.0.1:8001 for /dev/* fault switching only
```

Why Compose rather than `host.docker.internal`: the artifact schema in `IMPLEMENTATION.md` already
writes `allowed_origins: ["http://bank-sim:8001"]`. Making that hostname real means the origin
allowlist, the evidence, and the artifact all agree, and it lets the sandbox sit on an `internal: true`
network with **no internet access** — which is most of the prompt-injection and exfiltration story for
free.

---

## 3. Decisions to lock before writing code (Step 0)

| Decision                   | Choice                                                                                                                        | Rationale                                                                                                                                                |
| -------------------------- | ----------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Discovery surface          | Linux container, X11                                                                                                          | `xdotool` + Xvfb is the reference computer-use path; macOS-native driving needs Accessibility permissions and can't be containerized                     |
| Host → container transport | Small HTTP **surface agent** inside the container                                                                             | Clean seam matching `SurfaceAdapter`; ~5× faster than `docker exec` per action and it is the same shape a future desktop adapter would expose            |
| Display geometry           | **1280x800**, no scaling                                                                                                      | Under both API image limits (1568px long edge, ~1.15 MP), so the scale factor is 1.0 — but the scaling code path is still implemented and exercised (§4) |
| Browser chrome             | `chromium --app=<url>` (no address bar, no tabs)                                                                              | Removes navigation affordances the model must not use; origin control moves into the network layer, not the prompt                                       |
| Model                      | `claude-opus-5`                                                                                                               | Current default; adaptive thinking on by default                                                                                                         |
| Tool                       | `computer_toolset_20260801` toolset, `zoom` enabled                                                                           | Current production computer-use surface; no beta header                                                                                                  |
| Action vocabulary          | Enable `screenshot, zoom, left_click, double_click, type, key, scroll, wait, cursor_position`; disable the rest via `configs` | Smaller vocabulary = fewer invalid actions and a simpler canonicalizer; drag/middle-click/hold_key are not needed by this workflow                       |
| Provider boundary          | `ModelProvider` protocol with `AnthropicComputerUseProvider` + `FakeProvider`                                                 | Tests and CI must run with zero API calls                                                                                                                |
| Terminal declarations      | Model ends a run with one of `goal_complete`, `business_outcome`, `request_human`, `cannot_proceed`                           | Expressed as four **custom tools** alongside the computer toolset, so termination is schema-valid rather than parsed out of prose                        |

---

## 4. API facts that supersede v1 and v2

Both earlier plans predate the current computer-use surface. Carry these corrections in:

- The toolset takes **no display dimensions**. `display_width_px`, `display_height_px`,
  `display_number`, `name`, and `enable_zoom` are **rejected** with `invalid_request_error`. The model
  infers the coordinate space from the pixel size of the screenshots you return.
- Every `tool_result` answering a computer action must carry `"toolset_name": "computer"`, matching the
  `tool_use` block. A result that omits it is rejected.
- Coordinates are always in **full-screenshot pixel space**, origin top-left — including after a `zoom`.
  Zoom output never introduces a second coordinate space.
- If a batch of actions doesn't end in `screenshot`, attach a screenshot as an extra `image` block on
  the last result rather than waiting for the model to ask — saves a round trip per step.
- Put instruction text **before** the image in each user turn; it measurably improves click accuracy.
- One assistant turn may contain several `tool_use` blocks. Execute them in order, and on the first
  failure answer the remainder with `is_error: true` and `"Not executed: an earlier computer action in
this turn failed."` Return **all** results in a single user message.
- Screenshot history is the dominant context cost. Use context editing
  (`client.beta.messages.*`, beta `context-management-2025-06-27`,
  `context_management={"edits": [{"type": "clear_tool_uses_20250919"}]}`) to drop stale screenshots.
- Assistant prefill is unavailable on Opus 5. Shape output with tool schemas, not prefill.
- Mid-conversation `{"role": "system", ...}` messages are supported on Opus 5 and are the right channel
  for operator notes after a human handoff ("a human acted; re-observe before continuing") — they carry
  operator authority and don't invalidate the cached prefix. **But their position is constrained**, and
  the constraint is not obvious: a content-carrying system message must *precede an assistant message
  or end the array*. Appending one straight after the tool results of a handed-off turn puts it between
  two user messages and returns

  > `messages.15: role 'system' must precede an 'assistant' message or end the array; the
  > directive-only form (content: [] with output_config) is accepted at any position`

  Discovered by a live handoff in Step 12, which is exactly the expensive place to discover it. The
  note is therefore *queued* and appended after the next user turn, so it ends the array for that
  request and precedes an assistant turn for every request after. Consecutive `user` messages are
  fine — the tool-result turn and the next instruction turn are already two in a row.

---

## 5. Layers this driver touches

| Layer                                                                                                      | What discovery needs from it                                         | Work required now                                                                                                                                                              |
| ---------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `apps/cred_union_sim` (target)                                                                             | Reachable at `http://bank-sim:8001`, faults switchable from the host | **Yes, small.** Dockerfile + Compose service. No application code changes; `StaticFiles` and `Jinja2Templates` use CWD-relative paths, so `WORKDIR /app` is mandatory (Step 1) |
| Sandbox desktop image                                                                                      | A real X display with Chromium on it                                 | **Yes, new.** `sandbox/` — the single biggest chunk of setup (Steps 2–3)                                                                                                       |
| `src/surfaces/base.py`                                                                                     | The adapter protocol                                                 | **Yes, partial.** Define `observe`, `act`, `capture_evidence`, `probe`, `pause`, `resume`. Leave `resolve_target` declared but unimplemented — it belongs to replay            |
| `src/domain/actions.py`                                                                                    | Typed action union, one member per enabled tool                      | **Yes, now.** Pydantic v2 discriminated union; the model's raw `tool_use` input is parsed into it before anything executes                                                     |
| `src/domain/results.py`                                                                                    | Tagged run result                                                    | **Yes, minimal.** All four variants, even though only `success`/`cannot_proceed` fire early on                                                                                 |
| `src/domain/trace.py` (new file, not in v1 layout)                                                         | The recorder's output type                                           | **Yes, now.** This is the contract the canonicalizer will consume — freeze it deliberately (§7)                                                                                |
| `src/policy/engine.py`                                                                                     | Authorize every action before execution                              | **Yes, v1.** Action allowlist, risk class, origin/frame check against the probe's URLs, hard block on `/dev/*` routes and on the irreversible `Open Account` control           |
| `src/policy/redaction.py`                                                                                  | Redact before serialization                                          | **Yes, v1.** Mask member IDs and typed values in events. Screenshots are kept raw but the sim holds only synthetic data — state that explicitly in the report                  |
| `src/sessions/{manager,ownership}.py`                                                                      | Owner check + pause barrier before each action                       | **Yes, minimal.** The barrier is ~40 lines now and expensive to retrofit later; no UI yet                                                                                      |
| `src/evidence/writer.py`                                                                                   | JSONL events, screenshots, run summary                               | **Yes, now.** Discovery is where evidence conventions get set                                                                                                                  |
| `src/discovery/*`                                                                                          | The loop itself                                                      | **Yes — this is the layer**                                                                                                                                                    |
| `src/cli.py`                                                                                               | `discover`, plus `sandbox up/down` and `fault set` conveniences      | **Yes, thin**                                                                                                                                                                  |
| `src/replay/*`, `src/discovery/canonicalizer.py`, `src/operator/*`, `src/api.py`, `src/domain/artifact.py` | —                                                                    | **No.** Do not create stubs that invite scope creep; the trace contract in §7 is the handoff                                                                                   |

---

## 6. File layout added by this layer

```text
compose.yaml
apps/cred_union_sim/Dockerfile
sandbox/
  Dockerfile
  entrypoint.sh
  surface_agent.py          # runs INSIDE the container
  probe.js                  # fixed CDP expression, no model-supplied code
src/
  cli.py
  domain/
    actions.py
    results.py
    trace.py
  surfaces/
    base.py
    x11_computer.py         # host-side client of the surface agent
  discovery/
    controller.py
    model_provider.py
    prompts.py
    recorder.py
  policy/
    engine.py
    redaction.py
  sessions/
    manager.py
    ownership.py
  evidence/
    writer.py
tests/
  test_scaling.py
  test_policy.py
  test_loop_detection.py
  test_discovery_fake_provider.py
```

---

## 7. The one contract to get right: the run trace

The driver is pixel-based; the artifact must be semantic. The bridge is built **at act time**, not
afterwards — the DOM under a coordinate is only knowable while the page is in that state. Every click
and type is recorded together with the probe's answer for that exact point.

Lock this shape in Step 4 and don't change it casually — the canonicalizer, the replay locator
resolver, and the evidence tree all read it.

```yaml
run_id: "run_20260917_141230_a3f1"
goal: "Find member 12345 and prepare a savings sub-account, opening amount 25.00; stop at review"
surface_kind: "web" # selects the replay adapter later
target: "http://bank-sim:8001/"
display: { width: 1280, height: 800, scale_sent_to_model: 1.0 }
provider:
  {
    name: "anthropic",
    model: "claude-opus-5",
    tool: "computer_toolset_20260801",
  }
fault_profile: "default"
steps:
  - index: 7
    actor: "automation" # "human" after a handoff
    action: { kind: "left_click", coordinate: [914, 706] }
    policy: { decision: "allow", risk: "reversible", rule: "action_allowlist" }
    observation_before:
      main_frame_url: "http://bank-sim:8001/"
      frames:
        - {
            path: ["servicing-frame"],
            url: "http://bank-sim:8001/servicing/accounts/open?member_id=12345",
          }
      dialogs: []
      visible_text: "…flattened text of every frame, what the checkpoint reads…"
      screenshot: "evidence/run_.../steps/007-before.png"
      observation_hash: "sha256:9c1f..." # screenshot digest — pixels changed
      dom_hash: "sha256:5ab2..." # visible-text digest — content changed
    probe: # read-only, captured at the click point
      frame_path: ["servicing-frame"]
      tag: "button"
      hit_tag: "button" # what the coordinate literally hit...
      retargeted_from: null # ...and the child it was retargeted from, if any
      role: "button"
      role_source: "ax_tree" # or "retargeted_element"
      accessible_name: "Continue"
      accessible_name_source: "visible_text" # "placeholder" means DO NOT trust it
      visible_text: "Continue"
      nearby_label: null
      dom_id: null
      dom_id_stability: null # "generated" explains why no id locator was offered
      enclosing_region: "form#open-account-form"
      candidates: # ordered, each with its match count (null = not counted, NOT zero)
        - { kind: "role", role: "button", name: "Continue", match_count: 2 }
        - {
            kind: "contextual_text",
            anchor: "Opening Amount",
            relative: "button",
            match_count: 1,
          }
        - {
            kind: "css",
            selector: "form > div.actions > button.action-button",
            match_count: 1,
            stability: "unverified",
          }
      element_fingerprint: "sha256:1b7e..."
    observation_after:
      main_frame_url: "http://bank-sim:8001/"
      frames:
        - {
            path: ["servicing-frame"],
            url: "http://bank-sim:8001/servicing/accounts/open?member_id=12345",
          }
      dom_changed: true
      new_text: ["Review New Account", "Opening Amount", "$25.00"]
      screenshot: "evidence/run_.../steps/007-after.png"
    timing: { dispatched_ms: 31, settled_ms: 412 }
    model_reason: "Submit the sub-account form to reach the review screen"
outcome:
  { status: "success", terminal_declaration: "goal_complete", steps_used: 11 }
budget:
  {
    max_steps: 40,
    wall_clock_s: 300,
    input_tokens: 184203,
    output_tokens: 6120,
    usd_estimate: 1.07,
  }
```

Three rules that make this trace worth having:

1. **`match_count` is recorded during the run, not guessed later.** A candidate that matched twice is
   demoted by the canonicalizer instead of silently becoming a flaky locator.

   > **Corrected in Step 12.** The sketch above uses `Continue` as the duplicated control. It is not
   > one — `Continue` probes as `match_count: 1`. The control that is actually duplicated is Member
   > Detail's pair of identical `Back` buttons, where `role`, `text` and `css` all report
   > `match_count: 2` and `is_ambiguous` is true. Worse, no trace on disk carried an ambiguous
   > candidate at all until Step 12, because `drive` probed `Back` outside the recorded-step path and
   > only printed it to the console — so this rule, the headline argument for recording during the run,
   > was evidenced by nothing. `tests/fixtures/live_probe_ambiguous.json` is the captured payload.
2. **Coordinates never become primary locators.** They stay in `action` as evidence; the artifact's
   target comes from `probe.candidates`.
3. **The probe is optional by contract.** On a surface with no probe (the Tkinter mock), steps carry
   `probe: null` and the trace is still valid — it just can't be canonicalized into a web capability.
   That is precisely the point the cross-surface argument needs. Step 4 enforces this as a type: a
   null probe requires a sibling `probe_unavailable` reason, and the two are mutually exclusive.
4. **`match_count: null` is not `0`.** Null means nobody counted; zero means nothing matched. Only the
   second is a reason to reject a locator, and conflating them would let the canonicalizer silently
   discard good candidates.

**Amended in Step 4** (per §12.5) to match what the surface agent actually emits: the probe fields
`hit_tag`, `retargeted_from`, `role_source`, `accessible_name_source`, `dom_id`/`dom_id_stability`,
plus `visible_text` and `dom_hash` on observations. The first version of `Observation` was modelled on
this sketch and was missing `visible_text` — caught only by validating a live payload, which is why
Step 4's verification ends with exactly that check.

**Amended in Step 12**, to make room for the human as a recorded actor:

- **`RecordedStep.policy` is now optional, and required exactly where it means something.** A model
  validator demands it for `actor: "automation"` and *forbids* it for `actor: "human"`. Nobody ran the
  allowlist against what an operator did with their own hands, and a synthesised `allow` would let the
  recorder's assertion "every step has a policy decision" be satisfied by a lie.
- **`RecordedStep.action` may hold a `HumanIntervention`**, which is deliberately outside the agent
  action union — no tool schema, rejected by `parse_action`. The model cannot propose one by
  construction rather than by convention. It carries the intervention id, the reason, the operator and
  the context; it does **not** claim to know what the human clicked, because nobody watched them.
- **`Observation.removed_text`** joins `new_text`. A live handoff dismissed a modal and the step
  recorded `dom_changed: true` with an empty `new_text`, describing the only thing that happened as
  nothing at all: the event was entirely a disappearance.

---

## 8. Implementation steps

Each step is independently verifiable. Times assume Claude Code does the typing and you do the review.

---

### Step 0 — Lock decisions and freeze the action vocabulary

**Owner:** manual · **Time:** 30 min

- [x] Read §3 and §4, and record any disagreement as a decision note (it feeds `REPORT.md`).
- [x] Write the enabled/disabled member list into a single constant you will reuse in Step 10.
- [ ] Decide the goal string for the canonical demo run; keep it identical everywhere it appears.
- [x] Add `anthropic`, `httpx`, `pydantic>=2`, `pyyaml`, `typer` (or `click`) to `pyproject.toml`.
- [x] Add `.env.example` with `ANTHROPIC_API_KEY=`, `SANDBOX_AGENT_URL=http://127.0.0.1:8900`,
      `DISCOVERY_MAX_STEPS=40`, `DISCOVERY_WALL_CLOCK_S=300`.

**Verification:** `poetry install` succeeds; `poetry run python -c "import anthropic, pydantic"` works.

---

### Step 1 — Containerize the target app and start `compose.yaml`

**Owner:** Claude Code · **Time:** 45 min · **Touches:** target app layer (no code changes)

- [x] `apps/cred_union_sim/Dockerfile`: `python:3.12-slim`, `WORKDIR /app`, install project deps, copy
      `apps/`, run `uvicorn apps.cred_union_sim.server:app --host 0.0.0.0 --port 8001`.
      `WORKDIR /app` is not optional — `server.py` mounts `apps/cred_union_sim/static` and templates by
      CWD-relative path. **Built as two stages** (poetry in a builder, only the `.venv` copied into the
      runtime image) and runs as non-root uid 10001. A `.dockerignore` keeps `.git`, `evidence/`, and
      the plan docs out of the build context.
- [x] `compose.yaml` with one service so far (plus a `python`-based healthcheck, so Step 2 can use
      `depends_on: {bank-sim: {condition: service_healthy}}`):

  ```yaml
  services:
    bank-sim:
      build: { context: ., dockerfile: apps/cred_union_sim/Dockerfile }
      networks: [sandbox_net, host_net]
      ports: ["127.0.0.1:8001:8001"] # host access for /dev/* only
      environment:
        CRED_UNION_SIM_DB: ":memory:"
  networks:
    sandbox_net: { internal: true } # no internet for anything on this network
    host_net: {}
  ```

- [x] Note in the README how `/dev/fault-profile/*` is protected. **Corrected during implementation:**
      the original wording here claimed those routes are reachable "only from the host, never from the
      sandbox network". That is false, and was verified false — `bank-sim` is a peer on `sandbox_net`
      and serves `/dev/*` on the same port, so `http://bank-sim:8001/dev/fault-profile` answers from
      inside the sandbox. `internal: true` only cuts the route to the **host's published port** and to
      the internet. Containment of `/dev/` therefore rests on three code-level controls, not topology:
      no link from any page, no URL-entry affordance (`--app=` mode, and `ctrl+l` outside the action
      vocabulary), and the policy engine's `/dev/` route denial (Step 6). The network's job is blocking
      egress — which it does. Do not claim the stronger version in `REPORT.md`.

**Verification:** `docker compose up bank-sim`, then
`curl -s localhost:8001/servicing/members/search | head` returns the search page, and
`curl -X POST localhost:8001/dev/fault-profile/overlay` returns `{"status":"ok",...}`.

---

### Step 2 — Sandbox desktop image

**Owner:** Claude Code · **Time:** 2–3 h (expect image-build iteration; this is the step most likely to overrun)

- [x] `sandbox/Dockerfile` on `debian:bookworm-slim` with: `xvfb`, `x11vnc`, `novnc`,
      `websockify`, `chromium`, `xdotool`, `scrot`, `python3`, `python3-venv`,
      `python3-tk` (free now; avoids a rebuild when the desktop mock arrives), `fonts-dejavu`.
      **Deviations:** `openbox` added (a WM is what makes kiosk geometry and keyboard focus
      deterministic — without one, X falls back to PointerRoot focus and typed keys follow the
      pointer); `x11-utils`, `procps`, `curl`, `ca-certificates`, `socat` added (readiness gates,
      `pgrep` for the healthcheck, and the relay below); `imagemagick` **dropped** in favour of
      Pillow, which the `zoom` member needs for programmatic crop/resize anyway — saves ~100 MB.
      Also builds `/opt/agent-venv` with `fastapi`, `uvicorn`, `pillow`, `websockets` so Step 3 is
      code-only (bookworm enforces PEP 668, so a venv is required regardless). Image: 1.21 GB.
- [x] `sandbox/entrypoint.sh`: as sketched, plus `openbox --sm-disable`, `--kiosk`, and
      `x11vnc -localhost` (5900 is never published; noVNC is the only way in). Readiness is **polled,
      not slept for** — `xdpyinfo` gates Xvfb, and `/json/version` gates Chromium's CDP. Ends with
      `wait -n`, so the first supervised process to die takes the container down and the failure is
      visible in `docker compose ps` rather than leaving a healthy-looking box with a dead browser.
      `--log-level=3` keeps the log readable (no system D-Bus in the container ⇒ endless dbus/upower
      ERRORs); drop it temporarily when debugging Chromium itself.
- [x] Add the `sandbox` service to `compose.yaml` — `networks: [sandbox_net]` only, `shm_size: 1gb`,
      `init: true`, `TARGET_URL=http://bank-sim:8001/`, `depends_on: {bank-sim: service_healthy}`.
      **Topology correction, verified experimentally (Step 2.0):** a container attached *only* to an
      `internal: true` network **cannot publish ports** — Docker silently creates no host binding at
      all (`docker ps` shows a bare `8899/tcp`). So the published ports moved to a third service,
      `relay`, which sits on both networks and `socat`-forwards 6080/8900 inbound to the sandbox. It
      reuses the sandbox image (already has `socat`), so there is no extra build or pull. The sandbox
      stays internal-only and its no-egress property is intact — re-verified after the change.
- [x] Chromium runs as **non-root uid 10001 _and_ with `--no-sandbox`** — not "either/or" as written
      here. Docker's default seccomp profile blocks the unprivileged user namespaces Chromium's zygote
      sandbox needs, so it cannot initialize whatever user it runs as; the fixes that would let it
      (`seccomp=unconfined`, `cap_add: SYS_ADMIN`) weaken the container boundary far more than
      disabling Chromium's inner one. Containment = non-root + no egress + the policy engine.

**Verification (all passed):** noVNC at `http://localhost:6080/vnc.html` shows the servicing portal,
iframe and all, at exactly 1280x800 with no chrome — confirmed by a captured screenshot, not just by
eye. `xdotool getdisplaygeometry` → `1280 800`; `scrot` → a 1280x800 PNG; CDP answers and
`Page.getFrameTree` returns `(main) → servicing-frame`, exactly the `frame_path` §7's trace expects;
`https://example.com` fails from the sandbox while `http://bank-sim:8001/` succeeds; port 5900 is not
published; `uid=10001`; `dpkg --print-architecture` → `arm64`; the Step 3 runtime imports; killing
Chromium exits the container with the intended log line. Memory: sandbox ~236 MB, relay ~2 MB,
bank-sim ~45 MB of the 4 GB VM.

---

### Step 3 — Surface agent inside the container

**Owner:** Claude Code · **Time:** 2–3 h

`sandbox/surface_agent.py` — FastAPI, bound to `0.0.0.0:8900` inside the container (published only to
`127.0.0.1` on the host). Four endpoints, deliberately dumb: it executes primitives and reports state.
**No policy, no model, no loop logic lives here.**

- [x] `GET /health` → display size, Chromium alive, CDP reachable. The compose healthcheck now calls
      this, so a green container means the agent can actually see, act and probe.
- [x] `GET /screenshot` → `{width, height, sha256, captured_at_ms, png_base64}`. The **sha256 is
      computed here**, giving one canonical value for §7's `observation_hash` and Step 11's
      no-progress rule. Added `GET /screenshot.png` (raw bytes) purely so a human can open the
      agent's view in a browser, and `POST /zoom` (Pillow crop, coordinates stay in full-screenshot
      space per §4).
- [x] `POST /act` → one primitive, mapped to `xdotool`:

  | Action                        | Command                                                        |
  | ----------------------------- | -------------------------------------------------------------- |
  | `left_click` / `double_click` | `xdotool mousemove <x> <y> click [--repeat 2] 1`               |
  | `type`                        | `xdotool type --delay 12 -- "<text>"`                          |
  | `key`                         | `xdotool key <combo>` (`Return`, `ctrl+a`, …)                  |
  | `scroll`                      | `xdotool click 4/5/6/7 --repeat <n>` at an optional coordinate |
  | `cursor_position`             | `xdotool getmouselocation`                                     |
  | `wait`                        | server-side sleep, capped                                      |

  Returns `{"ok": true, "settled_ms": N, "detail": {...}}` after a bounded settle delay, or a
  structured error. Every call is `create_subprocess_exec` with an argv list — never a shell — and
  typed text is passed after `--` as its own argument. Validation is **syntactic only** (bounds,
  caps, keysym syntax); notably `ctrl+l` is *not* rejected here, because that is policy and belongs
  to Step 6 on the host (§12.2).

- [x] `POST /probe` → **read-only** DOM/AX lookup at `{x, y}` over CDP on `localhost:9222`:
      frame tree with URLs, then `document.elementFromPoint` inside the frame containing the point,
      returning tag, computed role, accessible name, visible text, nearest `<label>`, enclosing form or
      region, and a `match_count` for each candidate locator via `querySelectorAll` /
      text search. Shipped as a fixed `sandbox/probe.js`, applied to an element handle via
      `Runtime.callFunctionOn`; the wire carries two integers and never JavaScript.

      **Implementation notes.** Hit-testing uses `DOM.getNodeForLocation`, which descends into
      iframes natively — that removes §10's "probe returns the iframe element" failure mode entirely
      rather than mitigating it. Four behaviours were added after seeing real output:

      1. **Layout-table guard.** The sim uses tables for page layout as well as forms, so the naive
         "read the preceding cell" rule returned an entire panel's text as a button's label. Labels
         are now length-bounded and cells containing their own controls are skipped.
      2. **Generated ids are never candidates.** `inp_0a8b8b80` is recorded as
         `dom_id_stability: "generated"` — evidence of *why* no id locator was offered.
      3. **Placeholder-derived names are demoted.** Chromium computes the Opening Amount field's
         accessible name as `$0.00` (its placeholder). That looks authoritative and is useless, so
         it is flagged `name_source: "placeholder"` and ranked below the structural candidates.
      4. **Retargeting to the interactive ancestor.** A click on the icon button lands on its
         `<img>`; the probe describes the `<button>` and records `retargeted_from: "img"`.
- [x] `POST /probe/observe` → frame tree with URLs, dialogs, overlays, banners, headings, per-frame
      control inventory, and a `dom_hash` — the text-level companion to the screenshot hash, which is
      what makes `dom_changed` decidable without diffing images.

**Verification (all passed).** Automated, from the host:

```bash
curl -s 127.0.0.1:8900/health | python3 -m json.tool
curl -s -X POST 127.0.0.1:8900/probe -H 'content-type: application/json' -d '{"x":550,"y":96}'
```

Confirmed: `/health` reports 1280x800 + Chromium + CDP; the screenshot sha256 changed after a click
and the flow search → detail → form ran entirely through `/act`; probes inside the iframe return
`frame_path: ["servicing-frame"]`; the Member ID input returns **no** accessible name but
`nearby_label: "Member ID"` plus `input[name="member_id"]`; both Member Detail "Back" buttons report
`match_count: 2`; out-of-bounds coordinates, unknown kinds, malformed key combos and over-cap waits
all return 422.

### Manual verification — Terminal

```bash
docker compose up -d && docker compose ps        # sandbox healthy = /health answering

# 1. What the agent can see, in your own browser:
open http://localhost:8900/screenshot.png

# 2. Every endpoint, clickable, no curl needed:
open http://localhost:8900/docs                  # FastAPI Swagger UI

# 3. Health detail
curl -s 127.0.0.1:8900/health | python3 -m json.tool

# 4. Drive the app yourself — watch it happen live in noVNC (localhost:6080/vnc.html)
A() { curl -s -X POST 127.0.0.1:8900/act -H 'content-type: application/json' -d "$1"; echo; }
A '{"kind":"left_click","coordinate":[550,96]}'
A '{"kind":"type","text":"12345"}'
A '{"kind":"key","text":"Return","settle_ms":900}'
A '{"kind":"left_click","coordinate":[360,148],"settle_ms":1200}'   # open the result row

# 5. What is on screen, semantically
curl -s -X POST 127.0.0.1:8900/probe/observe | python3 -m json.tool | head -40

# 6. Probe a control
curl -s -X POST 127.0.0.1:8900/probe -H 'content-type: application/json' \
  -d '{"x":233,"y":239}' | python3 -m json.tool
```

**Finding coordinates without guessing:** move the pointer over any control in the noVNC window, then

```bash
curl -s -X POST 127.0.0.1:8900/act -H 'content-type: application/json' \
  -d '{"kind":"cursor_position"}'
```

which reports those exact coordinates — feed them straight to `/probe`.

**What good output looks like:** a probe of the Member ID input shows an empty `accessible_name`, a
`nearby_label` of "Member ID", and candidates led by `contextual_text` and `input[name="member_id"]`.
A probe of either "Back" on Member Detail shows `match_count: 2` — the ambiguity signal. A probe of
the icon-only button shows `retargeted_from: "img"` and only a structural candidate.

### Manual verification — Docker Desktop

1. **Containers** → `interface-ai` → `sandbox` should be green *Running (healthy)*; green now means
   `/health` is answering, not merely that the process started.
2. **Logs** tab → one uvicorn access line per request (`"POST /probe HTTP/1.1" 200 OK`). Run a curl
   and watch it appear; this is the fastest way to tell "the agent never got my request" from "the
   agent failed".
3. **Ports** column → click `8900` to open the API in a browser (append `/docs` for Swagger).
4. **Exec** tab → shell inside the container:
   `curl -s 127.0.0.1:8900/health`, `xdotool getmouselocation`, `scrot -o /tmp/x.png`.
5. **Files** tab → browse to `/opt/sandbox/` and confirm `surface_agent.py` and `probe.js` are the
   bind-mounted development copies.
6. Edit `sandbox/probe.js` on the host, hit **Restart** on the container, and re-probe — the change is
   live with no rebuild (this is §0.5's inner loop; drop the bind mount before Step 14 evidence).

### Findings that affect later steps

1. ~~**The `dialog` fault profile is inert.**~~ **FIXED in Step 8.** `.modal-overlay` existed in both
   stylesheets but no template rendered it, so `fault set dialog` had always been a no-op — which
   made Step 8's own manual test un-runnable and §10's unknown-modal path untestable. Added the modal
   block the design doc §7.4 specifies to `_review_panel.html` (the second deliberate §12.10
   exception). Verified live: the modal dims the page, `/probe/observe` reports exactly one dialog,
   and the loop escalates on it.
2. ~~**`static/icons/refresh.png` renders unconstrained at ~515x515.**~~ **FIXED.** The source PNG is
   512x512 intrinsic and nothing constrained it, so the icon-only button pushed the whole review panel
   **below the fold** — after Continue, the screen showed a giant arrow while `Review New Account`, the
   details, and the irreversible button sat off-screen. That would have failed Step 11's checkpoint on
   a screenshot even when the workflow succeeded, and put a decorative arrow in Step 14's evidence.
   Fixed by adding `width="16" height="16"` to the `<img>` in `open_subaccount.html` (the design doc
   specified 16x16 at line 1241; the file was failing its own spec). Attributes rather than CSS: no
   rule sets image dimensions, and attributes also reserve the box before load so the layout cannot
   shift under a running automation. `alt=""`/`title=""` were left untouched — **verified afterwards
   that the control is still nameless** (`accessible_name: None`, only a structural candidate,
   `retargeted_from: "img"`), now at 22x25 px. Sizing it was presentational; adding alt text would have
   silently deleted the coordinate-discovery test case. This is the one deliberate exception to
   §12.10's "Dockerfile only" rule.
3. **Stale X lock (fixed in Step 2's entrypoint).** After the container was killed hard, a leftover
   `/tmp/.X99-lock` made Xvfb refuse to start ever again with "Server is already active for display
   99". The entrypoint now removes the lock when no X server answers on that display. Worth knowing
   because the symptom looks like image corruption rather than a stale file.

---

### Step 4 — Domain models

**Owner:** Claude Code · **Time:** 1.5 h

- [x] `src/domain/actions.py`: Pydantic v2 discriminated union on `kind`, one model per enabled member
      plus the four terminal declarations, and `terminal_tool_schemas()` generating the custom-tool
      definitions from those same models — so the contract the model is shown and the contract the
      parser enforces cannot drift.
      **Decision: the eight disabled members have no model at all.** A `left_click_drag` fails
      validation, is never executed, and counts toward `INVALID_ACTIONS_EXCEEDED`. Step 6's allowlist
      re-checks the parsed kind, giving two independent gates rather than one. `extra="forbid"`
      everywhere, so a malformed action is caught whole rather than half-applied. Field names mirror
      **Anthropic's** member schemas (modifiers are `text` on clicks), because this union parses
      `tool_use.input`; translating to the agent's wire format is Step 5's job.
      `wait.duration` allows the API ceiling of 300 s — the adapter clamps to the agent's 10 s cap and
      records that it clamped, rather than burning the invalid-action budget on a legal request.
- [x] `src/domain/results.py`: `Success | BusinessOutcomeResult | Failure | Escalated` on `status`,
      every variant carrying `run_id` and a `StopReason`. Codes are `StrEnum`s (`FailureCode`,
      `BusinessOutcomeCode`, `EscalationReason`) so Step 11 cannot invent a spelling the caller has
      never heard of. `Success.checkpoint_verified` defaults to **False**: the model saying "done" is
      not verification, and that has to be set deliberately by observed state.
- [x] `src/domain/trace.py`: §7 as types — `RunTrace`, `RecordedStep`, `Observation`, `FrameInfo`,
      `ProbeResult`, six `LocatorCandidate` variants, `PolicyDecision`, `Display`, `ProviderInfo`,
      `Budget`, with `to_yaml`/`from_yaml` that preserve declaration order (a trace is read by a human
      reviewer; alphabetised keys scatter each step's story).
      `LocatorCandidate` is a **strict** discriminated union: a seventh kind in `probe.js` requires a
      matching model here, which is the right friction for something the canonicalizer matches
      exhaustively.

**Verification (all passed).** `poetry run pytest` — 35 tests, no network, no Docker, no API key.
Two findings the tests forced out:

- `Coordinate` was declared but never constrained, so `[-5, 10]` parsed happily. Now `ge=0`; the upper
  bound stays with the surface agent, which is the only component that knows the display size.
- **`Observation` was missing `visible_text`.** It was modelled on §7's sketch, while the agent had
  been returning that field all along — invisible to any hand-written fixture. Caught by validating a
  live payload, and §7 has been amended.

Also required `package-mode = false` in `pyproject.toml`: this repo is an application with no
importable root module, which is the same fact the Step 1 Dockerfile encoded as `--no-root`.

### Manual verification — Terminal

Nothing to click in this step, so the checks are REPL-shaped. The last one is the one that matters.

```bash
poetry run pytest -v                       # 35 passed, offline

# 1. Watch the first safety gate reject what it should
poetry run python -c "
from src.domain.actions import parse_action
for bad in [{'kind':'left_click_drag','start_coordinate':[1,1],'coordinate':[2,2]},
            {'kind':'left_click','coordinate':[-5,10]},
            {'kind':'type','text':'hi','unexpected':1},
            {'kind':'teleport'}]:
    try: parse_action(bad); print('ACCEPTED (wrong):', bad)
    except Exception as e: print('rejected:', bad['kind'], '->', type(e).__name__)
"

# 2. The exact JSON Schema Step 10 will send to the API
poetry run python -c "
import json; from src.domain.actions import terminal_tool_schemas
print(json.dumps(terminal_tool_schemas(), indent=2))" | head -40

# 3. Round-trip the example trace and read it back
poetry run python -c "
from src.domain.trace import RunTrace
t = RunTrace.from_yaml(open('tests/fixtures/example_trace.yaml').read())
print(t.run_id, '| steps:', len(t.steps), '| outcome:', t.outcome.status)
print('steps missing a probe or a reason:', t.steps_missing_probe())
print(t.to_yaml()[:300])"

# 4. THE REAL CHECK — validate a LIVE probe against the models
docker compose up -d
curl -s -X POST 127.0.0.1:8900/probe -H 'content-type: application/json' \
  -d '{"x":550,"y":96}' > /tmp/probe.json
poetry run python -c "
from src.domain.trace import ProbeResult
p = ProbeResult.model_validate_json(open('/tmp/probe.json').read())
print('validated:', p.tag, p.role, '| name:', p.accessible_name, '| label:', p.nearby_label)
print('candidates:', [(c.kind, c.match_count) for c in p.candidates])
print('best:', p.best_candidate.kind if p.best_candidate else None, '| ambiguous:', p.is_ambiguous)"
```

Check 4 is what separates "the models match the plan" from "the models match the software". Its
payloads are now committed as `tests/fixtures/live_*.json` so the check runs offline forever — a
hand-written fixture agrees with whatever you imagined; a captured one does not.

**What good output looks like:** the Member ID input validates with `accessible_name: None`,
`nearby_label: "Member ID"`, `dom_id_stability: "generated"`, and a `contextual_text` candidate at
`match_count: 1`. The Search button validates with `role: button`, `accessible_name: "Search"`,
`accessible_name_source: "visible_text"`.

### Manual verification — Docker Desktop

This step adds no services, so Docker Desktop's only role is supplying a live agent for check 4:
confirm `sandbox` is green *Running (healthy)*, then use the **Exec** tab to run the probe from inside
the container if the relay is ever in doubt:

```bash
curl -s -X POST 127.0.0.1:8900/probe -H 'content-type: application/json' -d '{"x":550,"y":96}'
```

Same JSON, one hop shorter — if this works and the host call does not, the problem is the relay, not
the agent.

---

### Step 5 — Surface adapter (host side) and a model-free drive

**Owner:** Claude Code · **Time:** 2 h

- [x] `src/surfaces/base.py`: async `SurfaceAdapter` protocol plus a typed error hierarchy, because
      the controller must tell these apart: `SurfaceUnavailable` (the sandbox is gone → stop the run),
      `ActionRejected` (well-formed but wrong for this screen → let the model retry),
      `UnsupportedAction` (**the driver cannot express it at all** → no amount of re-aiming helps),
      and `ProbeFailed`. `resolve_target` is declared and raises `NotImplementedError` — the replay
      seam, marked without letting replay logic leak in.
- [x] `src/surfaces/x11_computer.py`: `httpx.AsyncClient` against `SANDBOX_AGENT_URL`, returning Step 4
      `Observation` / `ProbeResult` objects rather than dicts. **Scaling is three pure functions**
      (`compute_scale`, `to_model`, `to_display`) with the limits as *parameters* — conservative
      defaults (1568 px / 1.15 MP), with Opus 5's larger ceiling a passed argument rather than an edit.
      At 1280x800 the factor is exactly 1.0 and the path still runs, so it cannot rot unnoticed.
- [x] **Capability-gap policy: clamp bounds, refuse semantics.** The domain models mirror Anthropic's
      schema; the agent has tighter limits. Where a smaller number preserves intent (`wait` 300s→10s,
      `scroll` 50→20) the adapter clamps and records `clamped: {requested, applied}`, so the trace
      never claims an action ran as asked when something else did. Where clamping would change meaning
      it refuses: truncating typed text would enter **wrong data** into a banking form, and dropping a
      `ctrl` modifier yields a plain click that looks like it worked.
- [x] `src/cli.py`: `drive`, plus `sandbox-status` and `fault set` (pulled forward from Step 13 because
      `drive` wanted them). `drive` prints the noVNC URL, resets to a known screen, probes the Member ID
      field **before** navigating away, acts, then probes an ambiguous control.

**Verification (all passed).** `poetry run pytest` — 55 tests, offline. Live: `sandbox-status` reports
1280x800 / scale 1.0 / Chrome alive, and `drive` exits 0 having driven search → result row → Member
Detail, leaving `evidence/drive/01-before.png` and `02-after.png` (confirmed by eye: Martinez, J. with
both Back buttons). `drive` asserts in-band that the screenshot hash **changed** and that `12345` is on
screen, so a run where the clicks go nowhere fails loudly instead of printing a happy log.

Three things the run surfaced:

1. **The sandbox is stateful.** Chromium keeps whatever page the previous run left behind, so the
   second `drive` started on Member Detail and its clicks went nowhere — caught only because of the
   in-band assertion. `drive` now resets by clicking the app's own **Members** nav link (no URL entry,
   which the agent deliberately cannot do) and verifies it landed on Member Search before acting.
   A smoke test that only passes on a fresh container is not a smoke test.
2. **Probe placement matters.** The first version probed the member field's coordinate *after*
   navigating, landing on a `<fieldset>` and demonstrating nothing. It now probes while the field is on
   screen — showing `accessible_name: None`, `nearby_label: "Member ID"`, `dom_id: inp_266d92b0
   (generated)` — then probes a "Back" button, where every candidate reports `match_count: 2`.
3. `observe(label=...)` now names its own screenshot, replacing a separate `capture_evidence` call that
   cost an extra round trip and wrote the same frame to disk twice under different names.

### Opening the noVNC window — Terminal

```bash
docker compose ps                              # relay must be Up: IT publishes 6080, not sandbox
curl -sI localhost:6080/vnc.html | head -1     # expect HTTP/1.1 200 OK before opening anything

open "http://localhost:6080/vnc.html?autoconnect=true&resize=scale"
```

The query string earns its place: `autoconnect=true` skips noVNC's Connect button, and `resize=scale`
fits 1280x800 into a smaller window **without touching the remote display**. Never use
`resize=remote` — that resizes the X display itself, which would silently invalidate every coordinate
in the trace and every hardcoded coordinate in `drive`.

`sandbox-status` and `drive` both print this URL, so in practice you can copy it from their output.

Side-by-side is the intended way to watch: terminal on one half, noVNC on the other, then run
`poetry run python -m src.cli drive` and watch the cursor move.

### Opening the noVNC window — Docker Desktop

1. **Containers** → `interface-ai` → the **`relay`** row (not `sandbox`).
2. Click **6080:6080** in the **Port(s)** column, or hover the row → ⋮ → *Open with browser*.
3. Docker opens `http://localhost:6080/` — **append `/vnc.html`**, since Docker cannot know the path.
   For the no-click version paste the full URL with the query string above.
4. The `sandbox` row shows no clickable ports at all. That is correct: it is on the internal-only
   network and publishes nothing (Step 2.0). If noVNC will not load, check `relay` before suspecting
   the desktop.

---

### Step 6 — Policy engine v1

**Owner:** Claude Code · **Time:** 1.5 h

Enforced in code, outside the prompt, on **every** action in both modes.

**Structural correction — policy runs in two phases.** This step's ordering as originally written could
never work: risk is classified *by target* ("a click whose probe reports accessible name `Open
Account`"), but Step 11 ran policy **before** the probe, so the irreversible rule could never fire — an
engine that looks right and enforces nothing. `src/policy/engine.py` therefore has two entry points,
and Step 11's order below is amended to match:

```
validate schema → check_action(action, observation) → probe() → check_target(action, probe) → execute
```

Not a workaround: some refusals are knowable from the URL alone, others only from the element under the
cursor. Both phases return a `PolicyDecision` and both are recorded.

- [x] Action allowlist: reject any `kind` not in the Step 0 vocabulary, before it reaches the adapter.
      Deliberately redundant with Step 4's schema — two independent gates, so a schema gap and a policy
      misconfiguration must *both* fail for an out-of-vocabulary action to run.
- [x] Origin/frame allowlist: **every** frame URL is checked, not just the main document. The workflow
      lives in an iframe, so checking only the top frame would check the one URL that never changes
      during a run. `/dev/` is denied outright — and per Step 1, the network does *not* block those
      routes, so this rule is the control rather than a second belt.
- [x] Risk classes per action and per target. The irreversible control is matched on **two independent
      signals** — accessible name `Open Account` *or* class `danger-button` — because either alone is
      brittle: the tenant_b theme renames the label, and a CSS refactor renames the class.
      **Decision: `decision="escalate"`, not `"deny"`**, with `code=IRREVERSIBLE_REQUIRES_APPROVAL`.
      `deny` stays reserved for what nothing can authorize (`/dev/`, out-of-vocabulary actions), so the
      controller can tell "a human could allow this" from "never" without parsing code strings. Step 4
      had already filed that code under `EscalationReason` rather than `FailureCode`.
- [x] Typed input guard: `type` text must **exactly** equal a declared input value. This is the
      project's crispest prompt-injection story — a page that says "type your API key here" cannot
      execute, because the key was never a declared input. The refusal detail deliberately does **not**
      echo the rejected text, since that text may be precisely the secret being fished for.
- [x] Approval tokens are bound to `sha256(action)`, plus run and expiry. Approving one click therefore
      authorizes that click and nothing else — otherwise "approval" would just mean the engine is off
      for a while.
- [x] Every decision is returned as a `PolicyDecision` and lands in the trace, allow or deny.

**Verification (all passed).** `tests/test_policy.py` — 26 tests, offline, including: a refused action
**never reaches a stubbed adapter** (proving the gate sits before dispatch, not as a label attached
after); `/dev/` denied when it appears only in a *child* frame; a valid token allowing the exact click;
the same token refused for a different click; an expired token and a token from another run both
refused; and no decision detail echoing a rejected secret. Full suite: **81 passed**.

Then the check that matters — driven live to the review screen and fed the **real** probe:

```text
=== checkpoint text visible WITHOUT scrolling ===
  'Review New Account' present: True | 'Savings': True | '$25.00': True
=== Open Account @ (246, 447) ===
  probe : <button> name='Open Account' classes=['danger-button']
  phase2: escalate risk=irreversible code=IRREVERSIBLE_REQUIRES_APPROVAL
=== Edit @ (207, 422) ===            <- the button right beside it
  phase2: allow    risk=reversible
=== Open Account WITH approval token ===  allow via approval_token
=== same token, different action ===      escalate
```

The `Edit` line matters as much as the `Open Account` one: it shows the rule discriminates rather than
blanket-refusing everything on the review screen.

(The first run of this check needed a `Scroll` to reach the button, because the oversized refresh icon
pushed the review panel off-screen. That is now fixed — see finding 2 — and the coordinates above are
the post-fix ones.)

### Manual verification

```bash
poetry run pytest tests/test_policy.py -v

# Live: drive to review, probe the button, ask the engine.
docker compose up -d
# search 12345 -> detail -> Open Sub-Account -> amount 25.00 -> disclosure -> Continue
# then probe ~(246, 447) for Open Account and ~(207, 422) for Edit — no scroll needed.
```

---

### Step 7 — Redaction v1

**Owner:** Claude Code · **Time:** 45 min

- [x] `redact_event(event) -> event` applied **before** serialization, not as a cleanup pass. The
      redactor walks structures recursively rather than field-by-field, because on the review screen
      the member id appears in the URL, a heading, the panel text and the action payload
      *simultaneously* — a field list would miss most of them and would need updating every time the
      trace grows a field.
- [x] **Key decision: a declared input redacts to its own placeholder, not to a mask.** Masking
      `12345` → `1***5` would have quietly broken the canonicalizer, whose entire job is to find
      literal input values in `trace.yaml` and replace them with `${inputs.*}`. Redacting to
      `${inputs.member_id}` loses nothing, and the trace comes out safe *and* already half
      canonicalized:

      ```
      12345        -> ${inputs.member_id}     # declared input
      23456        -> 2***6                   # another member from search results
      Bearer eyJ…  -> ***REDACTED***          # never an input, never readable
      $5420.50     -> $5420.50                # undeclared amounts stay readable
      ```

      Rule order is load-bearing: declared values run **first** (longest value first, so a shorter
      value cannot chew a hole inside a longer one), then secrets, then the generic `\b\d{5}\b`
      member-id shape. Reversing the first two would mask `12345` before the placeholder rule could
      claim it.
- [x] Amounts: an amount the run **declared** is claimed by the placeholder rule
      (`$25.00` → `$${inputs.opening_amount}`), which is what the artifact needs — otherwise the
      capability would hardcode 25.00 rather than parameterize it. That is not a loss of readability
      the way a mask is: a reader sees a named parameter. Amounts the run did *not* declare (account
      balances) stay readable by default, with `mask_amounts=True` for a deployment that disagrees.
- [x] Idempotent, and hashes untouched: `observation_hash`/`dom_hash` were computed from the real
      bytes, which is what makes them useful for change detection, and a digest is not a disclosure.
- [x] Screenshots are stored unmodified. Justification for the report: **the PNG beside the redacted
      log shows `12345` in plain pixels, and that is deliberate.** Masking it would be worse — the
      screenshot is how a reviewer confirms the run did what the log claims, and a blurred one proves
      nothing. The simulator holds only synthetic data, so there is nothing to protect. The hook for a
      deployment that does need it: `X11ComputerAdapter._write_png` is the single choke point every
      screenshot passes through, and `ProbeResult.rect` already carries the pixel box of each field —
      so region masking is a Pillow rectangle draw there, driven by probe data the trace already
      collects.

**Known limitation (deliberate).** Matching is case-sensitive, so a declared `account_type: "savings"`
is *not* substituted where the page renders `Savings`. Left as-is on purpose: case-insensitive
replacement of a common word would corrupt unrelated prose (`"Savings Account Options"` →
`"${inputs.account_type} Account Options"`), and `account_type` is not sensitive. The canonicalizer
should parameterize it from the **action payload** — where the select's value is exactly `savings` —
rather than by string-matching rendered display text.

**Verification (all passed).** `tests/test_redaction.py` — 25 tests, offline; full suite **106
passed**. The criterion the step names is checked against a payload captured from the real agent, not
a hand-written string. Then live, against an actual observation of the review screen:

```text
BEFORE  url : .../accounts/open?member_id=12345
        text: Review New Account Member: 12345 … Opening Amount: $25.00
AFTER   url : .../accounts/open?member_id=${inputs.member_id}
        text: Review New Account Member: ${inputs.member_id} … Opening Amount: $${inputs.opening_amount}

raw 12345 present after redaction : False
leak detector (names only)        : []
dom_hash unchanged                : True
```

The leak detector returns input **names**, never values — a detector that prints the secret it found
would be a poor one.

---

### Step 8 — Session ownership and the pause barrier

**Owner:** Claude Code · **Time:** 1.5 h

Small now, very expensive later.

- [x] `src/sessions/ownership.py`: the state machine, with compare-and-set on `control_version`.
      `ControlState` is **immutable** — a transition returns a new state, so a stale reference simply
      carries an old version that the next compare-and-set rejects. A test pins the consequence
      explicitly: `transition()` is a *pure function*, so two calls from the same value both succeed.
      That is correct, and it means mutual exclusion cannot live in the value — it lives in the
      manager, which holds the single current state. Worth stating so nobody later "fixes" the value
      type into a lock.
- [x] `src/sessions/manager.py`: the barrier plus a **file-based cross-process handshake**. The loop
      runs in one process and the operator types in another, so they meet at
      `evidence/<run_id>/intervention.json`, written atomically (write-then-rename) and polled while
      parked. That file *is* the audit trail the brief asks for, and unlike a shared object it
      outlives the process. Reloads only ever move forward, so a stale reader cannot roll control back.
- [x] **Two independent guards, on purpose.** `await manager.barrier()` makes a well-behaved loop
      *wait*; `manager.assert_automation_owns()` makes a stray call *fail* with `NotControlOwner`.
      Either alone would be a single point of failure in the one mechanism that must not have one.
      Beneath both, the adapter's own `pause()` (Step 5) refuses to act — three layers, and the test
      asserts a stubbed adapter records **zero** calls while a human holds the screen.
- [x] Escalation order matters: clear the gate and `adapter.pause()` **before** announcing the
      intervention, so there is no window where the operator has been told to take over while the
      automation can still click.
- [x] Resume path: fresh observation forced, plus a mid-conversation `{"role": "system"}` note. A
      system message rather than a user turn because it carries operator authority and does not
      invalidate the cached prefix on Opus 5; its content describes *state* ("re-observe before
      acting") rather than dictating a conclusion.
- [x] **Waiting is intentional; hanging is a bug.** The barrier blocks indefinitely for a human — any
      fixed window would be hostile to a reviewer actually reading the intervention — but accepts a
      deadline, so an unattended run ends as `escalated` with evidence flushed rather than pinning a
      container.
- [x] `src/cli.py`: `session status|accept|resume|complete|cancel` and `handoff-demo`, the model-free
      escalation walkthrough.
- [x] **Simulator fix (second deliberate §12.10 exception).** `_review_panel.html` received `fault`
      but rendered nothing for `fault.unexpected_dialog`, so `fault set dialog` had always been a
      no-op. Added the modal the design doc §7.4 specifies; the `.modal-overlay` CSS already existed.
      Without it the plan's own manual test was un-runnable and Step 11's unknown-modal path
      untestable.

**Verification (all passed).** `tests/test_ownership.py` — 21 tests, offline; full suite **127
passed**. Then the whole cycle live, against the real sandbox:

```text
window 1  ESCALATED intervention=int_1453fcbb reason=UNKNOWN_DIALOG   ...parked
window 2  session status  -> owner=HUMAN_PENDING v1
          session accept  -> owner=HUMAN v2      (window 1 STAYS parked)
          resume --control-version 1 -> refused: version 1 is stale; current is 2
noVNC     human dismisses the modal by hand in the same live session
window 2  session resume  -> owner=AUTOMATION v3
window 1  unblocks: dialogs [] · changed True · re-observed

audit: v0->v1 AUTOMATION->HUMAN_PENDING · v1->v2 ->HUMAN by youssef · v2->v3 ->AUTOMATION
```

Two defects the live run exposed, both fixed:

1. **The intervention context was erased by the operator's own transition.** `accept` rewrote
   `intervention.json` without the `step_index`/`context` captured at escalation, so the moment an
   operator took the screen, the explanation of *why they were called* vanished. Now held on the
   manager and rewritten every time. Unit tests missed it because they only ever called `escalate()`.
2. **One modal was reported as two dialogs** — the observe selector matched `.modal-overlay` and its
   nested `.modal-box`. Now only the outermost match is reported, so "is there a dialog?" gives an
   honest count.

### Manual verification — Terminal (two windows + noVNC)

```bash
# ── window 1 ────────────────────────────────────────────────────────────────
docker compose up -d
poetry run python -m src.cli fault set dialog      # arm the unknown dialog
poetry run python -m src.cli handoff-demo          # drives, hits the modal, PARKS

#   ESCALATED  intervention=int_…  reason=UNKNOWN_DIALOG
#   take over  : http://localhost:6080/vnc.html?autoconnect=true&resize=scale
#   ...and it sits there. Automation has stopped.

# ── window 2 ────────────────────────────────────────────────────────────────
poetry run python -m src.cli session status handoff-demo
poetry run python -m src.cli session accept handoff-demo --operator you
#   owner=HUMAN v2 — and window 1 is STILL parked. Accepting is not resuming;
#   the interval between them is the entire point.

# ── noVNC ───────────────────────────────────────────────────────────────────
# Click OK on the modal yourself. This is the same live session the agent was
# driving, not a copy — which is why the page you fix is the page it sees next.

# ── window 2 ────────────────────────────────────────────────────────────────
poetry run python -m src.cli session resume handoff-demo
#   window 1 unblocks, re-observes, reports dialogs [] and changed True
```

**Two things that should fail**, because a control mechanism is only proved by what it refuses:

```bash
poetry run python -m src.cli session resume handoff-demo --control-version 1
#   refused: control version 1 is stale; current version is 2

poetry run python -m src.cli session accept handoff-demo   # after completing
#   refused: HUMAN -> ... is not a legal control transition
```

**The honest boundary, worth knowing before a reviewer finds it.** While parked, this still works:

```bash
curl -s -X POST 127.0.0.1:8900/act -H 'content-type: application/json' \
  -d '{"kind":"left_click","coordinate":[640,400]}'     # succeeds
```

The surface agent is a deliberately dumb executor; ownership is enforced in the orchestrator above it.
Anything holding the agent's URL bypasses the barrier — which is exactly how the *human* acts during a
handoff. Moving enforcement into the agent would mean the human could not take over either.

### Manual verification — Docker Desktop

1. **Containers → sandbox → Logs** while parked: no new `POST /act` lines from the loop. The
   automation really has stopped rather than looping quietly.
2. Your noVNC clicks appear as X activity but **not** as `/act` requests — the human drives the
   browser directly, not through the agent. That contrast is the clearest proof it is one live session.
3. **relay → 6080** is the takeover window; `sandbox` publishes nothing (Step 2.0).
4. After the run, `evidence/handoff-demo/` holds `01-before-handoff.png`, `02-after-handoff.png` and
   `intervention.json` — before/after the human, plus who held the screen when.

---

### Step 9 — Evidence writer

**Owner:** Claude Code · **Time:** 1 h

- [x] `evidence/run_YYYYmmdd_HHMMSS_xxxx/` with `events.redacted.jsonl`, `steps/NNN-{before,after}.png`,
      `trace.yaml`, `run-summary.json`, `final.png` (plus `intervention.json` when Step 8 fires).
      The `run_` prefix is load-bearing: `.gitignore` carries `evidence/run_*/`, so scratch runs stay
      out of the repo while curated folders remain committable. A test pins the format.
- [x] **The writer is now the only component that writes evidence.** The adapter's `evidence_dir` and
      `_write_png` are gone; `observe()` leaves bytes on `last_screenshot_png` and the writer persists
      them. That exclusivity is what makes "everything on disk passed the redaction gate" a true
      statement rather than an intention.
- [x] **Leak gate: refuse to write.** Every record is redacted, serialized, then re-checked with
      `Redactor.contains_unredacted`; a hit raises `EvidenceLeak` and writes nothing. The error names
      the *input*, never the value. This immediately earned its place — see below.
- [x] Flush per event; `trace.yaml` rewritten atomically (write-then-rename) after every step, so a
      reader never sees a half-written file and a dead run still has a complete-as-of-last-step trace.
- [x] Correlation: `step_id = run_id#007` plus a monotonic `seq` on every line. Without it a
      forty-line JSONL is forty unrelated lines; with it an action, its policy decisions, its probe and
      its two screenshots are one story.
- [x] `--out` override so Step 14 writes `evidence/discovery-success/` directly rather than producing
      a `run_*` folder and copying it by hand.
- [x] `drive` now records through the writer using the **full Step 11 path** — observe → check_action →
      probe → check_target → act → observe → record. The evidence pipeline is therefore exercised end
      to end before a single token is spent on it.

**The gate caught a bug in its own writer.** `event()` redacted before writing but `write_trace()` only
*checked* — so the first real trace write raised `EvidenceLeak` on the run's own goal string. Fixed by
redacting the trace and re-validating the redacted copy as a `RunTrace`, which also confirms redaction
has not broken the schema the canonicalizer reads. A check that never fires proves nothing; this one
fired on day one.

**Verification (all passed).** `tests/test_evidence.py` — 20 tests, offline; full suite **147 passed**.
Live, a real `drive` run:

```text
evidence/run_20260923_205936_dbf1/
  events.redacted.jsonl  trace.yaml  run-summary.json  final.png
  steps/000-before.png … 004-after.png        (5 steps, 10 screenshots)

run-summary : status success · checkpoint_verified True · 5 steps · 6 events
grep 12345  : 0 hits in events.jsonl, trace.yaml, run-summary.json
replaced by : ${inputs.member_id}
every step  : has a policy decision; steps_missing_probe() == []
```

**The named criterion, done properly.** `SIGINT` was not enough — a `drive` takes ~4.4 s and kept
finishing first, which would have made this a test of tidiness rather than crash safety. A `SIGKILL`
at 2.2 s produced a genuinely incomplete folder (no `run-summary.json`, `finish()` never ran):

```text
trace.yaml parses   : yes — 1 step captured before the kill
outcome             : None (never written: the run died)
events parse        : yes — 2 lines, all valid JSON, seq [1, 2]
screenshots on disk : 000-before.png, 000-after.png
no half-written .tmp: True
leak-free           : True
```

### Manual verification — Terminal

```bash
poetry run pytest tests/test_evidence.py -v

docker compose up -d
poetry run python -m src.cli drive
RUN=$(ls -td evidence/run_*/ | head -1)

# 1. Read it as a reviewer would
python3 -m json.tool "$RUN/run-summary.json"
head -3 "$RUN/events.redacted.jsonl" | python3 -m json.tool
open "$RUN/steps/000-before.png" "$RUN/steps/000-after.png"   # the change step 0 claims

# 2. The leak check, proven rather than asserted
grep -c 12345 "$RUN"/{events.redacted.jsonl,trace.yaml,run-summary.json}   # 0, 0, 0
grep -o 'inputs\.[a-z_]*' "$RUN/trace.yaml" | sort -u                       # what replaced it

# 3. Crash safety — SIGKILL, because SIGINT lets a 4s run finish
poetry run python -m src.cli drive & PID=$!; sleep 2; kill -9 $PID
RUN=$(ls -td evidence/run_*/ | head -1)
test -f "$RUN/run-summary.json" && echo "it finished — kill earlier" || echo "incomplete, as intended"
poetry run python -c "
from src.domain.trace import RunTrace
print('steps that survived:', len(RunTrace.from_yaml(open('$RUN/trace.yaml').read()).steps))"
ls "$RUN"/*.tmp 2>/dev/null && echo "HALF-WRITTEN FILE" || echo "no partial writes"
```

### Manual verification — Docker Desktop

1. **sandbox → Logs** during a `drive`: one `GET /screenshot` per PNG under `steps/`. If those counts
   disagree, the adapter and the writer have drifted.
2. **sandbox → Exec** → `ls /tmp/agent-shot-*.png`: the container's scratch copies, overwritten every
   capture. These are **not** evidence — the durable copies are on the host, which is the §0.5 rule
   made visible.
3. `docker compose down -v` and confirm `evidence/` on the host is untouched: evidence never lives in
   a container filesystem.

### What "good" looks like

Open `run-summary.json`, see `status: success` and a step count; open `steps/000-before.png` and
`000-after.png` and see the change the trace's step 0 claims; `grep 12345` the whole folder and get
nothing — all without running any code.

---

### Step 10 — Model provider

**Owner:** Claude Code · **Time:** 2.5 h

- [x] `ModelProvider` protocol: `propose(observation, screenshot_png) -> ProposedBatch`,
      `record_results(outcomes)`, plus usage accounting.
      **Decision: the provider owns the transcript.** Tool results must reference the `tool_use` ids
      the provider generated, so that bookkeeping belongs here; the controller passes an observation
      and gets typed actions back, never seeing a `tool_use` id, a `toolset_name`, or a
      `cache_control` block. That is what makes `FakeProvider` a genuine drop-in rather than a mock
      that has to fake Anthropic message shapes.
- [x] `AnthropicComputerUseProvider` built exactly as specified. Tests assert the *absence* of the
      five fields the current API rejects (`display_width_px`, `display_height_px`, `display_number`,
      `name`, `enable_zoom`) — they appear in older references, so an absence test is the only thing
      that keeps them out.
- [x] Results: one `tool_result` per `tool_use`, **all in a single user message**, `toolset_name`
      present for computer members and **absent** for the four terminal declarations (they are ordinary
      custom tools; tagging them is rejected). Images for `screenshot`/`zoom`, text otherwise,
      `is_error` + the not-executed sentinel for actions skipped after a failure, and a trailing
      screenshot appended when the batch did not end with one.
- [x] Invalid actions become `InvalidAction` records rather than exceptions: answered with
      `is_error: true` **and the reason**, never executed, counted toward Step 11's limit. Being told
      what was wrong is how the model recovers — an unanswered `tool_use` is also a protocol error.
- [x] `src/discovery/prompts.py`: built from `ENABLED_MEMBERS` itself, so the vocabulary shown and the
      vocabulary enforced cannot drift (a test asserts no disabled member appears). Carries the goal,
      the stop-at-review rule, "end each batch with a screenshot", and the injection notice.
      **Stated honestly in the docstring: the injection paragraph is a hint, not a control.** A model
      that ignores it changes nothing, because the policy engine never reads the page and the action
      union never widens. Worth saying plainly in `REPORT.md` rather than presenting English as a
      security boundary.
- [x] `FakeProvider` replays a YAML script (`tests/fixtures/scripts/`), including an `invalid:` key to
      exercise the refusal path. Zero network.

**Verification.** `tests/test_discovery_fake_provider.py` — 31 tests, offline; full suite **178
passed**. `--dry-run` prints the exact request shape without sending it.

**The real smoke run PASSED — request and result shapes both validated live.**

The first attempt returned `400 … credit balance is too low`, which says nothing about the request
shape because billing is checked *before* validation. After credit was added:

```text
$ python -m src.cli smoke --round-trip
what it asked for (NOT executed)
  {'kind': 'left_click', 'coordinate': [549, 96]}     <- drive() uses (550, 96)
  {'kind': 'type', 'text': '12345'}
  {'kind': 'left_click', 'coordinate': [1070, 96]}    <- drive() uses (1070, 96)
  {'kind': 'wait', 'duration': 2.0}
  {'kind': 'screenshot'}                              <- ended the batch as instructed
send results back
  5 result blocks, 5 carrying toolset_name  ->  accepted
cost: in 8,908  out 345  ~$0.057 (2 turns) · cache 6,586 read (43% of billed input)
```

Three things this proved that offline tests could not:

1. **The request shape is accepted** — toolset type, `configs`, betas, `output_config`, cache_control.
2. **The result shape is accepted** — `--round-trip` sends the `tool_result` blocks back, which is the
   other place §4 warns a 400 comes from. Without that second call, the riskiest half stays unproven.
3. **The coordinate space is right.** The model chose (549, 96) and (1070, 96) purely from the
   screenshot — within one pixel of coordinates measured by hand for `drive`. That is independent
   confirmation that the screenshot pipeline and scale factor are correct end to end.

**A cost-accounting bug the live run exposed.** The API reports cache reads in their **own** bucket —
`input_tokens` already excludes them — so an estimate of input + output alone billed 6,586 cached
tokens at **zero**. That understates the run, which is the dangerous direction for a rule meant to
stop a runaway. `Usage` now prices all four buckets (input, cache read at 0.1x, cache write at 1.25x,
output). The estimate moved $0.0531 → $0.0564 for the same two turns.

**Useful for Step 11/14 budgeting:** ~$0.03/turn at this context size, and input grew 2,968 → 8,908
across two turns, so context editing is doing real work. Caching is confirmed active at a 43% hit rate.

The CLI now distinguishes these cases rather than printing a traceback: out-of-credit, auth,
`BadRequestError` (which prints the §4 checklist of likely causes), and transient errors each get
their own actionable message.

### Manual verification — Terminal

```bash
poetry run pytest tests/test_discovery_fake_provider.py -v

# The exact request, without sending it — tools, betas, effort, system prompt:
poetry run python -m src.cli smoke --dry-run

# The one real call (needs credit). Nothing is executed either way:
set -a && source .env && set +a
poetry run python -m src.cli smoke
```

Expected once credit is available: a `tool_use` naming an enabled member, token counts, and a cost
estimate under a cent. **A `BadRequestError` here is the useful outcome, not a failure** — it means the
shape is wrong while exactly one action is in flight, and the printed checklist maps straight to §4.

---

### Step 11 — Discovery controller (the loop)

**Owner:** Claude Code · **Time:** 3 h

Order inside one iteration — keep it exactly this:

1. `await barrier.wait_if_paused()`; assert control owner is `AUTOMATION`.
2. `observe()` → frame tree, dialogs, screenshot; hash the observation.
3. Check global exception states (unknown modal, session warning) → escalate or recover per policy.
4. Ask the provider for the next batch.
5. For each action: validate schema → `policy.check_action(action, observation)` → `probe()` at the
   target point (clicks and types only) → `policy.check_target(action, probe)` → execute →
   `probe/observe()` after → record. **Both** policy phases are recorded; see Step 6 for why the
   target check cannot happen before the probe.
6. Append results; loop.

Stopping rules — implement all of them, and record which one fired:

- Terminal declaration from the model (the normal path).
- Checkpoint verified independently of the model's claim: the review panel is confirmed by
  `observation_after.new_text` containing `Review New Account` **and** the requested account type and
  amount. The model saying "done" is not sufficient.
- `max_steps` (default 40), wall-clock (default 300 s), invalid-action count (3), repeated
  observation hash (3 in a row with no DOM change), repeated failed target (2), token/USD budget.
- `asyncio` cancellation on SIGINT that still writes evidence.

- [x] Emit a `results.py` variant at the end, never a bare exception.

**Done.** `src/discovery/controller.py` — `DiscoveryController.run()` (the loop) and
`execute_action()` (the per-action path), plus a `Budget` dataclass and a `Checkpoint` built from the
run's declared inputs. `src/sessions/__init__.py` added (it had been working only via namespace
packages). A `discover` command drives it; `drive` was refactored onto `execute_action` so the
per-action path — where the two policy phases and the probe live — exists in exactly one place.

**Every ending has its own code**, so a reader never has to guess which limit fired:

| Ending | Result | `stop_reason` |
|---|---|---|
| `goal_complete`, checkpoint agrees | `Success(checkpoint_verified=True)` | `CHECKPOINT_VERIFIED` |
| `goal_complete`, checkpoint disagrees | `Failure(CHECKPOINT_FAILED)` + expected/observed | `TERMINAL_DECLARATION` |
| `business_outcome` | `BusinessOutcomeResult` — an answer, not a crash | `TERMINAL_DECLARATION` |
| `request_human` / 2nd irreversible attempt / unknown dialog | `Escalated` | `ESCALATION` |
| step, clock and wallet limits | `MAX_STEPS_EXCEEDED` · `WALL_CLOCK_EXCEEDED` · `BUDGET_EXCEEDED` | `MAX_STEPS` · `WALL_CLOCK` · `BUDGET` |
| 3 identical observations, no DOM change | `Failure(NO_PROGRESS)` | `NO_PROGRESS` |
| 3 invalid actions | `Failure(INVALID_ACTIONS_EXCEEDED)` | `INVALID_ACTIONS` |
| SIGINT / cancellation | `Failure(CANCELLED)` — **evidence still written** | `CANCELLED` |

**Decisions taken, with the reasons:**

- **A policy `escalate` refuses, explains, and escalates only on repeat.** The click never executes
  either way. But telling the model merely that something "failed" invites it to retry the same thing,
  so the refusal says *why* — "this control is irreversible and requires human approval; do not retry"
  — and a first reach for the commit button usually becomes a `goal_complete` instead. A **second**
  attempt is a loop, not a slip, and parks for a human.
- **The checkpoint matches case-insensitively.** The run declares `account_type: "savings"` and
  `opening_amount: "25.00"`; the page renders `Savings` and `$25.00`. A case-sensitive checkpoint would
  reject a screen that is in fact correct — the worst kind, since it converts a good run into a
  reported failure. Verified against text captured from the running sim.

**Three bugs the verification found** (the reason it is worth doing rather than declaring):

1. **The no-progress rule sampled the screen twice per iteration** — once at the top of the loop and
   once after the batch — so "3 identical observations" fired after one and a half steps. Progress is
   now sampled once per iteration, at the top only.
2. **Every action in a batch was handed the turn's opening observation as its `observation_before`.**
   A step's before-observation is its *precondition*: this claimed the click on Continue happened on a
   form with the amount still empty. Only the first action of a batch may reuse the loop's
   observation; each later one looks again.
3. **`Observation.new_text` was declared in the trace contract and never populated by anything.** The
   controller is where before and after meet, so it fills it now — diffed by word rather than by line,
   because the agent flattens each frame to one line and a line diff would only ever report "the frame
   changed". `dom_changed` says something happened; `new_text` says what.

**Verification:** offline fixtures for every stopping rule, then bounded live runs.

#### 1. Offline — no network, no Docker

```bash
poetry run pytest tests/test_controller.py -v
poetry run pytest -q                        # 202 passed
```

`tests/test_controller.py` (21 tests) drives a `FakeSurface` of canned screens with the `FakeProvider`
of scripted actions. The catalogue, and what each one is actually protecting:

| Test | Protects |
|---|---|
| happy path on a review screen | `success`, `checkpoint_verified=True`, `CHECKPOINT_VERIFIED` |
| `goal_complete` on the **wrong** screen | `CHECKPOINT_FAILED` with expected vs observed — the model's claim loses to the screen |
| case/format mismatch (`savings`→`Savings`, `25.00`→`$25.00`) | a correct screen is not rejected |
| `business_outcome` | `MEMBER_NOT_FOUND` is an answer, **not** a failure |
| Open Account clicked once | refused, `surface.acted == []`, run continues, decision in the trace |
| Open Account clicked twice | `escalated: IRREVERSIBLE_REQUIRES_APPROVAL` |
| the refusal text | contains "irreversible" and "do not retry" |
| a failed action mid-batch | later actions marked `skipped`, never executed, model told so |
| script longer than `max_steps` | `MAX_STEPS_EXCEEDED` |
| unchanging screen | `NO_PROGRESS` |
| three unlisted actions | `INVALID_ACTIONS_EXCEEDED` |
| `max_usd` of $0.001 | `BUDGET_EXCEEDED` |
| negative wall clock | `WALL_CLOCK_EXCEEDED` |
| `task.cancel()` mid-run | `CANCELLED` **and** a parseable evidence folder |
| unknown dialog on screen | `UNKNOWN_DIALOG`, and `provider.turn == 0` — the model is never consulted |
| `request_human` | `MODEL_REQUESTED` |
| a loading overlay | exactly one bounded `wait`, never a retry loop |
| every run | every step carries a policy decision; `steps_missing_probe() == []` |
| every run | `trace.yaml` / `events.redacted.jsonl` / `run-summary.json` all exist and contain no raw member id |
| batched actions | each step records its own precondition |
| any step | `new_text` names what appeared, raw in memory and redacted on disk |

The limit tests deliberately use a `ProgressingSurface` whose screen changes on every look. Against a
static screen the no-progress rule fires first, so a test named for `MAX_STEPS_EXCEEDED` would silently
have been testing `NO_PROGRESS` instead.

#### 2. Live — the model-free path first (free)

`drive` runs through the same `execute_action`, so it answers "did the refactor break the hands?"
without spending a token:

```bash
docker compose up -d
poetry run python -m src.cli sandbox-status
poetry run python -m src.cli fault set default
poetry run python -m src.cli drive
```

Expect `screen changed: True   member 12345 on screen: True`, and note that `settled in ...ms` now
reports real numbers (481/370/374 ms) where it previously printed `None` — the controller times the
dispatch and the settle, which the hand-written path never did.

#### 3. Live — a bounded real run

```bash
set -a && source .env && set +a
poetry run python -m src.cli discover --max-steps 8       # ~$0.10, 2 turns
```

Watch it in noVNC. Then read what it left behind:

```bash
RUN=$(ls -td evidence/run_*/ | head -1)
python3 -m json.tool "$RUN/run-summary.json" | head -20

poetry run python -c "
from src.domain.trace import RunTrace
t = RunTrace.from_yaml(open('$RUN/trace.yaml').read())
print('steps          :', len(t.steps))
print('outcome        :', t.outcome.status, t.outcome.stop_reason)
print('policy denials :', [s.policy.code for s in t.steps if s.policy.decision != 'allow'])
print('missing probes :', t.steps_missing_probe())
for s in t.steps:
    print(f'  {s.index:>2} {s.action[\"kind\"]:<11} {s.policy.decision:<7}'
          f' probe={(s.probe.accessible_name if s.probe else None)!r}')
"

# Nothing raw on disk. grep exits 1 on no match, so the || is load-bearing:
grep -l 12345 "$RUN"/*.jsonl "$RUN"/*.yaml "$RUN"/*.json 2>/dev/null \
  || echo "clean: nothing raw on disk"

open "$RUN/steps/000-before.png" "$RUN/final.png"
```

**Any named stop reason is a pass.** `MAX_STEPS_EXCEEDED` at 8 steps means the loop bounded itself
correctly. A *failure* would be an unhandled exception, a step with no policy decision, a raw member id
on disk, or a success claimed on the wrong screen.

**What the live runs actually did.**

An 8-step run reached the member record and stopped itself: `MAX_STEPS_EXCEEDED`, 8 steps, 2 turns,
~$0.10, no policy refusals, no missing probes, nothing raw on disk. The model batched 5 and then 3
actions per turn, which is why 8 steps is only 2 paid calls.

A 20-step run got further and ended `ESCALATED / MODEL_REQUESTED` at step 16 (5 turns, ~$0.46). It
found the member, opened the sub-account form, typed the amount, clicked Continue, and the form
answered *"You must accept the account disclosure to continue."* The model then asked for a human
rather than ticking the disclosure checkbox itself.

Note the probes recorded along the way: `'Open Sub-Account'`, then an amount field with
`accessible_name='$0.00'` and `nearby_label='Opening Amount'`, then `accessible_name='Continue'` with
`nearby_label='Back'` — the ladder degrading exactly as Step 6 designed it to.

> **Open question for Step 13, not a controller bug.** The disclosure checkbox is an **unclassified
> control**: the policy engine has no rule for it, so whether the agent may accept a disclosure on a
> member's behalf is currently left to the model's judgement. It escalated, which is a defensible
> default — but it did so because this model happened to be cautious, not because anything in the
> system required it. A less cautious model would have ticked the box and nothing would have objected.
> This needs an explicit decision: either a policy rule classifying consent controls as
> human-only, or a prompt line stating that ticking them is in scope.

---

### Step 12 — Recorder

**Owner:** Claude Code · **Time:** 1.5 h

- [x] Assemble `RecordedStep` objects from what the controller already has; the recorder does **no**
      interpretation and **no** parameter substitution — that is the canonicalizer's job in the next
      layer.
- [x] Persist `trace.yaml` incrementally, not only at the end.
- [x] Record `actor: "human"` steps for anything observed after a handoff (a DOM-diff entry is enough
      at this stage; the injected observer described in `IMPLEMENTATION.md` belongs to the escalation
      layer).
- [x] Assert before writing: every step has a policy decision; every click/type step has a probe or an
      explicit `probe_unavailable` reason.

**Done.** The recorder's shape was mostly already right after Step 11 — incremental, atomic, redacted.
What this step actually fixed was the gap between what §7 *promises* a reviewer and what was being
filled in.

**Three fields the contract declared and nothing populated:**

1. **`model_reason`.** In the §7 sketch, in `RecordedStep`, and written by nobody — while the controller
   held `batch.reason` at the moment it built each step. A step that records *what* was clicked but not
   *why* is the half a reviewer actually reads. Now threaded through `execute_action`, including for
   `drive`'s hardcoded walkthrough.
2. **`actor: "human"`.** See below — it could not be set, because escalation was a dead end.
3. **The ambiguity signal.** Covered in §7's correction note: `match_count > 1` appeared in no artifact
   at all, only on a console.

**Handoffs became recoverable.** Previously `_escalate()` returned an `Escalated` result and the loop
ended, so there was nothing "after a handoff" to record and a run that needed two seconds of human help
could never finish. Now a recoverable escalation parks at the barrier; when control comes back the loop
records one `actor: "human"` step and continues.

| Aspect | Choice, and why |
|---|---|
| Which escalations recover | `UNKNOWN_DIALOG`, `MODEL_REQUESTED`, `SESSION_EXPIRED`, `POLICY_ESCALATION` |
| Which does not | **`IRREVERSIBLE_REQUIRES_APPROVAL`.** Typing `session resume` means "I have finished looking at the screen", not "I authorize this commit". Conflating them would let a human approve an irreversible action by accident while `ApprovalToken` — bound to `sha256(action)`, expiring, scoped to one action — went unused |
| The human step's `policy` | **`null`.** Nobody ran the allowlist against what a person did with their own hands. A synthesised `allow` would make the assertion "every step has a policy decision" satisfiable by lying, so the model validator now requires policy for `automation` and *forbids* it for `human` |
| The human step's `action` | A `HumanIntervention` model deliberately **outside** the agent union — no tool schema, rejected by `parse_action`. Unproposable by construction, not by convention |
| What it claims to know | Only the DOM diff. We did not watch the operator work, and inventing a coordinate for a step nobody observed would put a fiction into the artifact |
| Bound | `Budget.max_handoffs = 3` → `MAX_HANDOFFS_EXCEEDED`. A run that keeps needing a human is not making progress either |

**The assertion gate.** `steps_missing_probe()` existed and was *printed*; nothing refused to write.
`assert_recordable()` now runs at the top of `write_trace()` — the one chokepoint every byte of trace
already passes, beside the `EvidenceLeak` gate it mirrors — checking that automation steps carry a
policy decision, coordinate steps carry a probe or a stated reason, and indices are unique and
ascending. It runs on every incremental write, so a violation surfaces at the step that caused it. It
raises rather than becoming a `Failure`: this is a recorder bug, and write-then-rename means the trace
on disk keeps its last valid version.

**Four bugs found by verifying rather than declaring:**

1. **`barrier()` could hang forever.** `complete()` does not set the gate and `automation_may_act` is
   false for `COMPLETED`, so a human finishing the run by hand left the parked loop polling until its
   deadline — then reporting `InterventionTimeout`, blaming an absent human for a decision one actually
   made. `barrier()` now raises `RunEndedByHuman` on a terminal owner → `HUMAN_ENDED_RUN`.
2. **The timeout handler mislabelled every reason.** It hardcoded `MODEL_REQUESTED` regardless of what
   the run had escalated for.
3. **A live handoff earned a 400.** `role 'system' must precede an 'assistant' message or end the
   array` — `add_system_note` appended the resume note between two user messages. It is now *queued*
   and placed by `propose()` after the user turn, so it ends the array for that request and precedes an
   assistant turn for every request after. Confirmed against the live API. See §4.
4. **A crash left no `run-summary.json`.** That 400 propagated out of `run()` unhandled, so `result`
   stayed `None` and the summary was never written — the file naming the outcome was missing from
   exactly the run that needed explaining, breaking the writer's central promise. `run()` now records
   an unexpected exception as `PROVIDER_ERROR` **and re-raises**, so the folder is complete and the
   traceback still reaches the caller.

5. **The adapter stayed paused after a resume — the worst of the five.** `escalate()` pauses the
   adapter in the loop's process; `resume()` runs in the *operator's* process, where `adapter` is None
   because the CLI loads control state from disk and holds no surface. Nothing un-paused the loop's
   adapter. So the barrier opened, the loop carried on, and every single action came back
   `ACTION_REJECTED: surface is paused; automation does not hold control` until the no-progress rule
   stopped the run. Step 8's second guard was doing exactly its job; the release path simply did not
   exist. `barrier()` now un-pauses the adapter when the reload shows automation may act — the
   in-process half of a cross-process handshake.

6. **The DOM diff only reported additions.** The second live handoff dismissed a modal and the step
   recorded `dom_changed: true` with an empty `new_text` — the artifact described the one thing that
   happened as nothing at all, because a dismissal is entirely a *disappearance*. `Observation` gained
   `removed_text` and the diff now runs both ways.

**One honest limitation that remains.** The diff is still blind to control *state*: when the operator
ticked the disclosure checkbox, the human step recorded `dom_changed: false` with both text lists
empty, because no visible text changed either way. The before/after screenshots show it. Dialogs and
navigation diff well; a checkbox does not, and closing that gap is the injected observer the plan
assigns to the escalation layer.

**`model_reason` is present when the model spoke and absent when it did not.** In the successful run
below, 9 of 22 steps carry no reason, because those turns returned only `tool_use` blocks with no text.
That is the correct behaviour: synthesising a plausible reason is exactly the interpretation the
recorder is forbidden to do.

**Verification**

#### 1. Offline

```bash
poetry run pytest tests/test_recorder.py tests/test_controller.py -v
poetry run pytest -q        # 236 passed
```

`tests/test_recorder.py` (19 tests) is about the artifact; the additions to `test_controller.py` and
`test_ownership.py` are about the handoff.

| Test | Protects |
|---|---|
| the captured fixture really is ambiguous | guards the fixture itself — if the sim's markup changes, the tests below would pass while proving nothing |
| `match_count=2` through redaction + YAML | §7's headline rule, end to end to disk |
| `match_count: null` not rewritten as `0` | §7's fourth rule: nobody counted ≠ nothing matched |
| a step built with no policy decision | rejected at construction |
| a step that *lost* one later | rejected at write time — the only way one reaches the writer |
| a click with neither probe nor reason | rejected; a stated reason alone is accepted (the cross-surface argument, as a test) |
| duplicate / out-of-order indices | rejected — two steps at index 3 overwrite each other's screenshots |
| a refused write | leaves the previous valid `trace.yaml` untouched |
| the gate | reports every problem at once, not one per run |
| a human step | `policy is None`; and a human step *with* a policy is refused |
| `human_intervention` | not parseable as an agent action, not in the tool schemas |
| a human step's event line | carries `policy: null` in the JSONL a reviewer greps |
| `run-summary.json` | counts `human_steps` |
| §7 field-by-field | every field the canonicalizer reads survives the round trip |
| `example_trace.yaml` | still validates — the contract as a file |
| a recoverable escalation | parks, resumes, records ONE human step, continues to success |
| the resume note | reaches the provider once per handoff, and is placed last in the array |
| `IRREVERSIBLE_REQUIRES_APPROVAL` | still terminal; `handoffs == 0` and the click never executes |
| three handoffs | `MAX_HANDOFFS_EXCEEDED` |
| a human ending the run while parked | `HUMAN_ENDED_RUN`, not a timeout |
| an unattended park | reports the reason that parked it |
| an unexpected crash | still writes a parseable `run-summary.json`, and still raises |
| a resume across processes | the loop's adapter is un-paused, not just the gate |
| a dismissed dialog | recorded in `removed_text` — the diff runs both ways |

#### 2. Live, free — the ambiguity signal in a real artifact

```bash
docker compose up -d
poetry run python -m src.cli fault set default
poetry run python -m src.cli drive
```

`drive` now *records* the Back-button probe instead of printing it:

```
6. record an ambiguous control (two 'Back' buttons)
   <button> role=button name='Back'
     - role             match_count=2  <- ambiguous
     - text             match_count=2  <- ambiguous
     - css              match_count=2  <- ambiguous
   is_ambiguous    : True
   recorded as     : step 5 in trace.yaml
```

Then read it back out of the artifact:

```bash
RUN=$(ls -td evidence/run_*/ | head -1)
poetry run python -c "
from src.domain.trace import RunTrace
t = RunTrace.from_yaml(open('$RUN/trace.yaml').read())
for s in t.steps:
    print(f'  {s.index}  {s.action[\"kind\"]:<11} {s.model_reason!r}')
print('human steps    :', t.human_steps())
print('missing policy :', t.steps_missing_policy())
print('missing probes :', t.steps_missing_probe())
for s in t.steps:
    for c in (s.probe.candidates if s.probe else []):
        if (c.match_count or 0) > 1:
            print(f'  AMBIGUOUS step {s.index}: {c.kind} matches {c.match_count}')
"
```

Every step should carry a reason, both `missing_*` lists should be empty, and at least one `AMBIGUOUS`
line should appear.

#### 3. Live, with the model — a human rescuing a run

Two terminals, both on the host. `fault set dialog` arms the unexpected modal on the review screen.

```bash
# terminal 1
poetry run python -m src.cli fault set dialog
set -a && source .env && set +a
poetry run python -m src.cli discover --max-steps 30
```

It parks, prints what to do, and **waits** rather than exiting:

```
-> request_human
   "The form will not continue without ticking "I have reviewed the account disclosure..."
   ESCALATED  MODEL_REQUESTED  intervention=int_d8e70cc1
   take over  : http://localhost:6080/vnc.html?autoconnect=true&resize=scale
   then run   : python -m src.cli session accept run_20260924_221831_c389
   and then   : python -m src.cli session resume run_20260924_221831_c389
   waiting for a human (Ctrl-C to abandon)...
```

In terminal 2: take the screen, fix it by hand in noVNC (tick the disclosure box; later, dismiss the
System Notice with **OK**), hand it back.

```bash
poetry run python -m src.cli session accept run_<id> --operator your-name
poetry run python -m src.cli session resume run_<id>
```

Terminal 1 then prints `RESUMED  handoff #1 recorded as step 16` and carries on. Afterwards:

```bash
python3 -m json.tool "$RUN/run-summary.json" | grep -E 'human_steps|stop_reason'
poetry run python -c "
from src.domain.trace import RunTrace
t = RunTrace.from_yaml(open('$RUN/trace.yaml').read())
for i in t.human_steps():
    s = t.steps[i]
    print(i, s.action['reason'], 'operator=', s.action['operator'], 'policy=', s.policy)
"
```

**A pass is:** one `actor: "human"` step per handoff, `policy: null` on each, the run continuing
afterwards, and any named stop reason. **A failure is:** the loop acting while a human holds the screen,
a human step with a fabricated policy decision, a `RecorderAssertionError` reaching the user, or a
trace that does not validate.

**What the live runs actually did.** Three, and the first two each paid for themselves by finding a bug
the offline tests could not have: the 400 on the resume note, then the adapter that stayed paused.

The third completed the workflow end to end with **two** handoffs:

```
outcome
   SUCCESS  stop_reason=CHECKPOINT_VERIFIED
   checkpoint_verified : True
   steps               : 22
   cost                : ~$1.0514 over 8 turn(s)  (cache 12% of billed input)
   policy refusals     : none
   human interventions : 2 at steps [16, 20]
   steps missing probe : none
```

The sequence: the model searched, opened the member, filled the sub-account form, and asked for a human
at the disclosure checkbox (`MODEL_REQUESTED`, step 16). The operator ticked it and resumed; the model
clicked Continue and met the armed System Notice modal, which the loop parked on before consulting it
(`UNKNOWN_DIALOG`, step 20). The operator dismissed it and resumed; the model zoomed to read the review
panel and declared completion, which the checkpoint then **verified against the screen** rather than
taking on trust.

Both human steps carry `policy: null` and a stated `probe_unavailable`. Step 20 captured the dialog's
text as its context; step 16 recorded `dom_changed: false`, which is the honest answer for a checkbox.
`missing policy`, `missing probes` and the raw-value grep all came back empty.

---

### Step 13 — CLI wiring

**Owner:** Claude Code · **Time:** 1 h

- [x] `discover --goal ... --target ... [--fault default] [--max-steps 40] [--provider anthropic|fake]
  [--evidence-dir ...]`.
- [x] `sandbox up|down|status`, `fault set <profile>` (host-side call to `/dev/fault-profile/...`).
- [x] Print the noVNC URL on start, and on escalation print it again with the intervention id.
- [x] README demo path, exact commands.

**Done.** `discover` now takes `--goal`, `--target`, `--provider anthropic|fake`, `--script`,
`--fault`, `--evidence-dir`, `--max-handoffs`, and the bounds from Step 11. `sandbox up|down|status`
wraps compose (`up` passes `--wait`, so it blocks until the sandbox is actually usable rather than
merely started), and `fault show` reports what is armed. The noVNC URL prints on start and again at the
moment of a park, with the `session accept` / `session resume` lines — printed when they are actionable
rather than after the fact.

**`--provider fake` is the piece worth having.** It runs the *identical* loop against the *identical*
sandbox with a scripted action list instead of a model: same policy engine, same probe, same evidence
writer, same checkpoint. Everything below the provider cannot tell the difference. So the whole path
can be rehearsed for nothing and with no API key before a paid run, and any failure it finds is a real
failure rather than a mock's.

`tests/fixtures/scripts/full_workflow.yaml` drives search → detail → sub-account form → review and
verifies the checkpoint. It ticks the disclosure checkbox, which the real model declines to do on its
own — that difference is the subject of the handoff walkthrough, not a discrepancy to paper over.

**Provenance in the trace.** A run now records `target`, `provider.name`/`model`, and the armed
`fault_profile` (read back from the simulator, so it reflects what was actually set rather than what
was asked for). `budget` carries the limits and the spend.

**Two bugs found by running it:**

1. **`sandbox status` reported `fault: unreachable` on a healthy stack.** `GET /dev/fault-profile`
   returns the profile object, whose key is `name`; only the **POST** returns `active_profile`. The
   reader looked for the latter.
2. **`trace.budget` reached disk empty on every run ever recorded.** The CLI assigned it after
   `controller.run()` returned — but `writer.finish()` serializes the trace *inside* that call, so the
   assignment was always too late. The controller now writes the limits at construction (a run that
   dies should still say what it was allowed to do) and the spend just before the folder closes. This
   is the field Step 14's "within budget" check reads, so it was silently unverifiable.

**Verification**

```bash
poetry run python -m src.cli sandbox down
poetry run python -m src.cli sandbox up        # compose up -d --wait
poetry run python -m src.cli sandbox status    # incl. the armed fault profile
poetry run python -m src.cli discover --provider fake --fault default
```

From a cold stack, that reaches the review screen and verifies the checkpoint:

```
   provider : fake (tests/fixtures/scripts/full_workflow.yaml)
   fault    : default
   bounds   : 20 steps, $2.00, 600s, 3 handoffs
   ...
outcome
   SUCCESS  stop_reason=CHECKPOINT_VERIFIED
   checkpoint_verified : True
   steps               : 18
   cost                : $0.00 — scripted, no model was called
   policy refusals     : none
   human interventions : 0
   steps missing probe : none
```

And the artifact carries its provenance:

```
provider      : fake | tests/fixtures/scripts/full_workflow.yaml
fault_profile : default
budget        : max_steps 20, wall_clock_s 600.0, input_tokens 9000, usd_estimate 0.057
outputs       : {'account_type': 'Savings', 'opening_amount': '${inputs.opening_amount}',
                 'submitted': 'no'}
```

(The token counts on a `fake` run are synthetic — `provider.name` in the same trace says which it was.)

#### Found by the first real capture attempt

The first Step 14 capture parked for a human and **the handoff could not be completed**. The run was
abandoned with Ctrl-C after ~$0.46. Three defects, none of which any test or rehearsal could have hit,
because each needed two features used *together* that had only ever been used apart.

1. **`session accept` could not find a run whose folder is not named after it.** The lookup hardcoded
   `evidence/<run_id>/intervention.json`, which holds right up until someone passes `--evidence-dir` —
   and the capture procedure passes it on every run. The file was sitting in
   `evidence/discovery-success/`, with the run id recorded *inside* it. Steps 12 and 13 each worked;
   their combination had never been exercised, and the README asserted it did.

   Now resolved rather than assumed: a path is used directly, then `evidence/<ref>/`, then a scan of
   every `intervention.json` for a matching `run_id` field. A miss lists the runs that *can* be taken.
   The old message named `handoff-demo` regardless of what was running.

2. **The printed instructions were not runnable.** They said `python -m src.cli …`; the project runs
   through Poetry and the operator got `zsh: command not found: python` with the run parked and the
   clock going. All such output now goes through one `CLI` constant, and the park block also prints the
   evidence dir — so a folder/run-id mismatch is visible where it happens rather than in the other
   terminal.

3. **Re-running a capture into the same folder would have merged two runs silently.**
   `events.redacted.jsonl` opens in append mode, so both runs' events land in one file with sequence
   numbers restarting partway; the previous run's step screenshots survive as orphans; and
   `run-summary.json` builds its artifact list by walking the directory, so it lists them as its own. A
   folder that looks complete and is not — the worst failure mode for an artifact whose entire purpose
   is to be trusted. `EvidenceWriter` now refuses a non-empty **named** directory unless `overwrite=True`,
   checked before anything is written so a capture never costs money before failing. Auto-generated
   `run_*` folders are unique by construction and unaffected.

And one thing that made it hard to diagnose: after the Ctrl-C, `intervention.json` still read
`HUMAN_PENDING`, so `session status` reported a parked run that nothing was waiting on. The controller
now appends a closing transition when a run ends while a human still holds or is owed the screen. The
audit trail keeps the park; the file just stops describing a state that is no longer true.

**`tests/fixtures/scripts/handoff.yaml`** exists so this is never again first discovered during a paid
run: it drives to the disclosure checkbox, calls `request_human`, and finishes after the resume. With
`--provider fake` it exercises a complete control transfer, through a custom `--evidence-dir`, against
the real sandbox, for nothing:

```bash
poetry run python -m src.cli discover --provider fake \
  --script tests/fixtures/scripts/handoff.yaml \
  --evidence-dir evidence/handoff-fake --overwrite --max-steps 30
# then, in another terminal, the two commands it prints
```

Verified end to end: `SUCCESS stop_reason=CHECKPOINT_VERIFIED`, 21 steps, 1 human intervention at step
17 carrying `policy: null`, and after the `--overwrite` exactly 42 screenshots, one `run_id` in the
events file and an unbroken `seq` 1→23 — no trace of the previous run.

---

### Step 14 — First real discovery run and evidence capture

**Owner:** manual · **Time:** 1–2 h including reruns

- [ ] `fault set default`, then a real run against member 12345.
- [ ] Watch it in noVNC. Expect to iterate on the system prompt here, not on the driver — resist
      changing the loop to fix a prompt problem.
- [ ] Save the run into `evidence/discovery-success/` (`capability.yaml` lands here later, from the
      canonicalizer).
- [ ] Record what actually went wrong and how many steps it took; this is report material.

**Verification:** the review screen is reached, the checkpoint verifies independently, the trace has a
probe on every click, and the run cost is within budget.

---

### Step 15 — Tests

**Owner:** Claude Code · **Time:** 1.5 h

- [ ] `test_scaling.py`: scale factor 1.0 at 1280x800; a 2560x1600 display scales to within both
      limits; round-tripping a coordinate through scale-down/scale-up lands within one pixel.
- [ ] `test_policy.py`: the allow/deny matrix from Step 6.
- [ ] `test_loop_detection.py`: each stopping rule fires with its own code.
- [ ] `test_discovery_fake_provider.py`: the full loop, no network, evidence written, trace valid.

**Verification:** `poetry run pytest` green with no `ANTHROPIC_API_KEY` set.

---

### Step 16 (optional, after the core is solid) — Second surface

**Owner:** Claude Code + manual · **Time:** 2 h

- [ ] `apps/desktop_mock/form.py`: Tkinter window — Member ID field, Account Type dropdown, Look Up
      button, result panel with a mocked balance.
- [ ] Run it on the same `:99` display in the sandbox container; no new image needed if Step 2 installed
      `python3-tk`.
- [ ] Run `discover` against it with `--surface-kind desktop`. The **only** code difference must be
      that `probe` returns `probe_unavailable: "no_accessibility_backend"` — if anything else in the
      loop needs changing, the abstraction has a leak worth reporting honestly.
- [ ] Save to `evidence/discovery-desktop/`. Do not build a desktop replay engine.

**Verification:** the same binary, the same loop, a second evidence folder, and one paragraph in
`REPORT.md` about what did and did not transfer.

---

## 9. Acceptance criteria for this layer

| Criterion                                                                      | How to verify                                                    |
| ------------------------------------------------------------------------------ | ---------------------------------------------------------------- |
| Sandbox shows the sim at 1280x800 with no browser chrome                       | Open noVNC                                                       |
| A human can click in the same session the agent drives                         | Click in noVNC mid-run                                           |
| The driver executes all enabled actions                                        | `cli.py drive`                                                   |
| Model coordinates land on the right elements                                   | Probe output matches the intended control                        |
| Scaling is implemented even though it is identity here                         | `test_scaling.py`                                                |
| No action executes without a policy decision                                   | Recorder assertion + trace inspection                            |
| The irreversible button cannot be pressed                                      | Policy test + a real run where the model tries                   |
| `/dev/*` is unreachable **to the agent** (not to the network)                   | Policy test: a `/dev/` frame URL denies with `ROUTE_NOT_ALLOWED`. Note `curl` from inside the sandbox *does* reach it — see Step 1 |
| The sandbox has no internet                                                    | `docker compose exec sandbox curl https://example.com` fails     |
| Every click/type step carries a probe with ordered candidates and match counts | Trace inspection                                                 |
| Duplicated **`Back`** is recorded as `match_count: 2` (see Step 3 — the built sim has two Back buttons on Member Detail, not two Continues) | Trace inspection; verified at the probe level in Step 3 |
| Completion is verified from observed state, not the model's claim              | Wrong-screen `goal_complete` fixture returns `CHECKPOINT_FAILED` |
| Every stopping rule fires with a distinct code                                 | `test_loop_detection.py`                                         |
| A cancelled run still leaves valid evidence                                    | Ctrl-C mid-run                                                   |
| Full test suite runs with no API key                                           | `pytest` with the key unset                                      |
| One real run reached review and is committed as evidence                       | `evidence/discovery-success/`                                    |

---

## 10. Failure modes to expect

| Symptom                                                                              | Cause                                                    | Response                                                                                                                                                               |
| ------------------------------------------------------------------------------------ | -------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `Cannot connect to the Docker daemon at unix:///Users/<you>/.docker/run/docker.sock` | Docker Desktop is not running                            | `open -a Docker`, wait for the whale to settle (§0.2). This is never a Compose-file problem                                                                            |
| A container exits with code **137**                                                  | The VM ran out of memory and the kernel OOM-killed it    | Raise VM memory or close host apps; `docker stats` while a run is in flight (§0.3)                                                                                     |
| `bind: address already in use` on 8001                                               | The sim is still running natively from earlier work      | `lsof -ti:8001 \| xargs -r kill` (§0.6)                                                                                                                                |
| Chromium won't start or dies instantly                                               | Default 64 MB `/dev/shm`, or root without `--no-sandbox` | `shm_size: 1gb`, non-root user                                                                                                                                         |
| Clicks land slightly off                                                             | Window position drift, or scaling applied twice          | Pin `--window-position=0,0`; keep scaling in one function with a test                                                                                                  |
| Probe returns the iframe element, not the control                                    | Not descending into the frame                            | Walk the CDP frame tree and evaluate inside the containing frame; assert `frame_path` is non-empty for in-frame controls                                               |
| Model clicks before an HTMX swap lands                                               | No settle wait                                           | Settle delay in `/act` plus a DOM-change check in `observation_after`; escalate to a bounded wait, never a fixed long sleep                                            |
| Context grows until requests slow or fail                                            | Screenshot accumulation                                  | Context editing from Step 10; assert a cap on retained images                                                                                                          |
| Model tries to navigate by URL                                                       | No address bar, but it may try `ctrl+l`                  | `key` combos are allowlisted; `ctrl+l` is not in the vocabulary                                                                                                        |
| Page text tries to redirect the agent                                                | Prompt injection                                         | Goal and policy live in the system prompt and in code; the system prompt states on-screen text is data; policy blocks the action regardless of what the model was told |
| A run burns budget with no progress                                                  | Loop                                                     | Repeated-observation-hash and no-progress limits from Step 11                                                                                                          |
| `invalid_request_error` on the tool definition                                       | Carrying `display_width_px` etc. from the older plans    | §4                                                                                                                                                                     |
| `tool_result` rejected                                                               | Missing `toolset_name: "computer"`                       | §4                                                                                                                                                                     |

---

## 11. Budget

Rough per-run arithmetic at 1280x800: about 1.3–1.5 k tokens per screenshot, a dozen or so retained
images under context editing, ~10–25 steps per run. On Opus 5 ($5/MTok in, $25/MTok out) that puts a
successful discovery run in the low single-digit dollars, less with prompt caching on the tool block and
system prompt. Instrument it rather than trusting the estimate: accumulate `response.usage` per turn
into `budget` in the trace, and give the loop a hard USD ceiling that stops the run. Expect the first
working run to cost several times a steady-state one because of prompt iteration — do that iteration
with short `--max-steps` values.

---

## 12. Notes for Claude Code

1. Working directory is the repo root; the sim lives at `apps/cred_union_sim/` (not `apps/bank_sim/`
   as the older docs' file trees say). The Compose **service** name is `bank-sim` because the artifact
   schema's `allowed_origins` already uses that hostname — keep that spelling.
2. `surface_agent.py` runs **inside** the container and must not import anything from `src/`. It has no
   policy, no model, and no loop logic. Everything in `src/` runs on the host.
3. The probe is read-only and its JS is a fixed file. Never add an endpoint that evaluates a JS string
   supplied by a caller.
4. Do not create `src/replay/`, `src/discovery/canonicalizer.py`, `src/operator/`, or
   `src/domain/artifact.py` in this layer, even as stubs.
5. The trace in §7 is a contract. If a step needs a field that isn't there, add it to §7 with a reason
   in the same commit.
6. Use `asyncio` throughout the controller — the pause barrier and cancellation depend on it.
7. No `time.sleep` for page settling in host code; waits are bounded, condition-based, and recorded.
8. Tests never hit the network. `FakeProvider` fixtures live in `tests/fixtures/*.yaml`.
9. Synthetic data only; redaction happens before serialization.
10. The sim is the application under test and stays unaware of this layer. The only permitted change to
    `apps/cred_union_sim/` in this layer is the Dockerfile.

---

## 13. Handoff to the next layer

When this layer is done you have: a sandbox you can watch and take over, a driver that executes typed
actions under policy, a loop that stops for the right reasons, and a run trace that records — for every
click — the element that was actually hit, its ordered locator candidates, and how many things each
candidate matched.

That last item is the whole reason the canonicalizer can exist. Build it next, against
`evidence/discovery-success/trace.yaml`, and do not let it read anything from the provider transcript.
