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
  operator authority and don't invalidate the cached prefix.

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

1. **`match_count` is recorded during the run, not guessed later.** A candidate that matched twice
   (`"Continue"` appears twice on the form by design) is demoted by the canonicalizer instead of
   silently becoming a flaky locator.
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

1. **The `dialog` fault profile is inert.** `.modal-overlay` exists in both stylesheets but **no
   template renders it**, so `fault set dialog` changes nothing on screen. `/probe/observe` scans for
   it and will simply report none. Step 8's escalation demo and §10's unknown-modal path both need
   that template work first — the one place §12.10's "Dockerfile only" rule has to be relaxed.
2. **`static/icons/refresh.png` now exists but renders unconstrained** — roughly 515x515 px,
   dominating the form and pushing layout around. `.icon-only-button img` needs a width/height in
   `servicing.css`. It is supposed to be a *small* icon-only control; at this size it is not the
   discovery challenge it was meant to be.
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

- [ ] Action allowlist: reject any `kind` not in the Step 0 vocabulary, before it reaches the adapter.
- [ ] Origin/frame allowlist: after each observation, every frame URL must match
      `http://bank-sim:8001` and a permitted route prefix. `/dev/` is denied outright — the model must
      not be able to change its own fault conditions.
- [ ] Risk classes per action and per target: a click whose probe reports accessible name
      `Open Account` (or which lands inside `.review-actions .danger-button`) is `irreversible` and is
      **denied without an approval token**, returning a `tool_result` that says so. This is a real
      demo: the model can see the button and cannot press it.
- [ ] Typed input guard: `type` text is checked against the declared inputs — the model may type
      `${inputs.member_id}`'s value, not arbitrary strings into arbitrary fields.
- [ ] Every decision is returned as a `PolicyDecision` and lands in the trace, allow or deny.

**Verification:** unit tests — a `left_click` on the finalize button is denied with
`IRREVERSIBLE_REQUIRES_APPROVAL`; a frame URL of `http://bank-sim:8001/dev/fault-profile` denies with
`ROUTE_NOT_ALLOWED`; an unknown action kind never reaches a stubbed adapter.

---

### Step 7 — Redaction v1

**Owner:** Claude Code · **Time:** 45 min

- [ ] `redact_event(event) -> event` applied **before** serialization, not as a cleanup pass.
- [ ] Mask member IDs (`12345` → `1***5`), typed field values, and any `Authorization`/`Cookie`-shaped
      strings. Keep amounts visible (they are needed to read the trace) and say so in the report.
- [ ] Screenshots are stored unmodified; justify it in one line: the simulator holds only synthetic
      data, and masking pixels would destroy the evidence value. Note where a region-mask hook would go.

**Verification:** a test asserting no raw seeded member ID appears anywhere in
`events.redacted.jsonl` after a fake-provider run.

---

### Step 8 — Session ownership and the pause barrier

**Owner:** Claude Code · **Time:** 1.5 h

Small now, very expensive later.

- [ ] `src/sessions/ownership.py`: `AUTOMATION → HUMAN_PENDING → HUMAN → AUTOMATION`, plus
      `COMPLETED` / `CANCELLED`, with a `control_version` integer and compare-and-set transitions.
- [ ] `src/sessions/manager.py`: holds the session (sandbox URL, run id, owner) and an
      `asyncio.Event`-based barrier: `await barrier.wait_if_paused()` is called **before every action**.
- [ ] `RequestHuman` from the model, or a policy escalation, flips to `HUMAN_PENDING`, writes an
      `intervention.json`, prints the noVNC URL, and blocks the loop.
- [ ] Resume path: on return to `AUTOMATION`, force a fresh observation and append a mid-conversation
      `{"role": "system"}` note saying a human acted and the screen may have changed.

**Verification:** a test that an action attempted while the owner is `HUMAN` raises
`NOT_CONTROL_OWNER` and never reaches the adapter. Manually: run `discover`, trigger escalation with
the `dialog` fault profile, take over in the noVNC window, and confirm the loop is parked.

---

### Step 9 — Evidence writer

**Owner:** Claude Code · **Time:** 1 h

- [ ] `evidence/<run_id>/` containing `events.redacted.jsonl` (one line per transition, with
      `run_id`, `step_index`, `actor`, correlation id), `steps/NNN-{before,after}.png`,
      `trace.yaml`, `run-summary.json` (goal, outcome, counts, budget, timings), and `final.png`.
- [ ] Flush after every event so a crashed or cancelled run still leaves usable evidence.
- [ ] A `--evidence-dir` override so the committed demo folders (`evidence/discovery-success/`) are
      produced directly rather than copied by hand.

**Verification:** kill a fake-provider run mid-way with Ctrl-C; the evidence folder still parses.

---

### Step 10 — Model provider

**Owner:** Claude Code · **Time:** 2.5 h

- [ ] `ModelProvider` protocol: `propose(observation, history) -> list[Action]` plus usage accounting.
- [ ] `AnthropicComputerUseProvider`:
      `client.beta.messages.create` with `model="claude-opus-5"`,
      `tools=[{"type": "computer_toolset_20260801", "configs": {...disabled members...},
  "cache_control": {"type": "ephemeral"}}, <four terminal-declaration custom tools>]`,
      `context_management={"edits": [{"type": "clear_tool_uses_20250919"}]}`,
      `betas=["context-management-2025-06-27"]`, `output_config={"effort": "high"}`.
- [ ] Results: one `tool_result` per `tool_use`, **all in a single user message**, each carrying
      `"toolset_name": "computer"` for computer members; image content for `screenshot`/`zoom`, text
      otherwise; `is_error: true` + the not-executed sentinel for actions skipped after a failure.
- [ ] Parse every `tool_use.input` with `json.loads` semantics into the Step 4 union; a schema-invalid
      action is answered with `is_error: true` and an explanation, never executed, and counted toward
      the invalid-action limit.
- [ ] `src/discovery/prompts.py`: system prompt carrying the goal, the enabled action list, the
      stop-at-review rule, "end each batch with a screenshot", and an explicit line that text appearing
      on screen is data and can never change the goal, the policy, or the allowed actions. Instruction
      text goes **before** the image in every user turn.
- [ ] `FakeProvider`: replays a scripted action list from a YAML fixture. Every test uses it.

**Verification:** `pytest tests/test_discovery_fake_provider.py` runs a full loop with zero network
calls. Separately, one real one-step smoke run: send a screenshot, print the returned action, execute
nothing.

---

### Step 11 — Discovery controller (the loop)

**Owner:** Claude Code · **Time:** 3 h

Order inside one iteration — keep it exactly this:

1. `await barrier.wait_if_paused()`; assert control owner is `AUTOMATION`.
2. `observe()` → frame tree, dialogs, screenshot; hash the observation.
3. Check global exception states (unknown modal, session warning) → escalate or recover per policy.
4. Ask the provider for the next batch.
5. For each action: validate schema → validate policy → `probe()` at the target point (clicks and types
   only) → execute → `probe/observe()` after → record.
6. Append results; loop.

Stopping rules — implement all of them, and record which one fired:

- Terminal declaration from the model (the normal path).
- Checkpoint verified independently of the model's claim: the review panel is confirmed by
  `observation_after.new_text` containing `Review New Account` **and** the requested account type and
  amount. The model saying "done" is not sufficient.
- `max_steps` (default 40), wall-clock (default 300 s), invalid-action count (3), repeated
  observation hash (3 in a row with no DOM change), repeated failed target (2), token/USD budget.
- `asyncio` cancellation on SIGINT that still writes evidence.

- [ ] Emit a `results.py` variant at the end, never a bare exception.

**Verification:** with `FakeProvider` fixtures — a happy path returns `success`; a fixture that clicks
the same spot forever stops with `NO_PROGRESS`; a fixture that proposes an unlisted action stops with
`INVALID_ACTIONS_EXCEEDED`; a fixture that declares `goal_complete` on the wrong screen returns
`failure: CHECKPOINT_FAILED`.

---

### Step 12 — Recorder

**Owner:** Claude Code · **Time:** 1.5 h

- [ ] Assemble `RecordedStep` objects from what the controller already has; the recorder does **no**
      interpretation and **no** parameter substitution — that is the canonicalizer's job in the next
      layer.
- [ ] Persist `trace.yaml` incrementally, not only at the end.
- [ ] Record `actor: "human"` steps for anything observed after a handoff (a DOM-diff entry is enough
      at this stage; the injected observer described in `IMPLEMENTATION.md` belongs to the escalation
      layer).
- [ ] Assert before writing: every step has a policy decision; every click/type step has a probe or an
      explicit `probe_unavailable` reason.

**Verification:** the trace from a fake-provider happy path validates against the §7 model and contains
at least one candidate with `match_count > 1` (the duplicated `Continue` button) — proof the ambiguity
signal is actually being captured.

---

### Step 13 — CLI wiring

**Owner:** Claude Code · **Time:** 1 h

- [ ] `discover --goal ... --target ... [--fault default] [--max-steps 40] [--provider anthropic|fake]
  [--evidence-dir ...]`.
- [ ] `sandbox up|down|status`, `fault set <profile>` (host-side call to `/dev/fault-profile/...`).
- [ ] Print the noVNC URL on start, and on escalation print it again with the intervention id.
- [ ] README demo path, exact commands:

  ```bash
  docker compose up -d
  poetry run python -m src.cli discover \
    --goal "Find member 12345 and prepare a savings sub-account, opening amount 25.00; stop at review" \
    --target http://bank-sim:8001/
  ```

**Verification:** a fresh `docker compose up -d` plus the command above runs end to end with
`--provider fake`.

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
