# Computer-Use Automation System — Credit Union Ops

A computer-use agent that discovers a workflow in a hostile legacy web UI by watching pixels and
driving mouse/keyboard, records what it did as a reviewable artifact, and replays that artifact
deterministically without the model in the loop.

- `apps/cred_union_sim/` — the Credit Union Ops Simulator, the application under test.
- `discovery-loop-plan.md` — implementation plan for the discovery loop and its OS-level driver.
- `IMPLEMENTATION.md`, `interface-ai-plan-v2.md` — system design and the cross-surface update.
- `REPORT.md` — write-up.

## Prerequisites

- Docker Desktop (Apple Silicon build on an M-series Mac), daemon running.
- Python 3.12+ and Poetry 2.x for the host-side orchestrator.
- An Anthropic API key, for discovery runs only. Replay and the test suite need no key.

```bash
cp .env.example .env     # then put your real key in .env — .env is gitignored
poetry install
```

## Running the simulator

```bash
docker compose up -d bank-sim
docker compose ps                    # expect "healthy"
open http://localhost:8001/          # servicing portal (the workflow lives in an iframe)
docker compose logs -f bank-sim
docker compose down
```

The workflow is: member search → member detail → open sub-account → review. Seeded members include
`12345` (John Martinez, two accounts) and `23456`; `88888` is unseeded and exercises the
member-not-found path.

## Ports

| Port | Bound to    | Published by | Service                     | Added in |
| ---- | ----------- | ------------ | --------------------------- | -------- |
| 8001 | `127.0.0.1` | `bank-sim`   | Credit Union Ops Simulator  | Step 1   |
| 6080 | `127.0.0.1` | `relay`      | noVNC — watch and take over | Step 2   |
| 8900 | `127.0.0.1` | `relay`      | Sandbox surface agent       | Step 3   |

Every port is bound to loopback, so nothing here is reachable from the local network. The sandbox's
own VNC server (5900) is bound to container-loopback and never published — noVNC on 6080 is the only
way in.

## Watching the agent work

```bash
docker compose up -d
open http://localhost:6080/vnc.html    # the live desktop the agent drives
```

This is the same session the agent controls, not a copy — which is what makes the Step 8 human
handoff real: you take over in this window, act, and hand control back.

## Poking at the sandbox by hand

The surface agent exposes the three primitives the driver uses. Its Swagger UI makes all of them
clickable, which is the fastest way to explore:

```bash
open http://localhost:8900/docs           # every endpoint, with schemas
open http://localhost:8900/screenshot.png # exactly what the agent sees
curl -s 127.0.0.1:8900/health | python3 -m json.tool
```

`POST /probe {"x":…,"y":…}` answers the question the artifact depends on: *what element is under this
pixel, and how would you find it again?* To get coordinates without guessing, hover a control in the
noVNC window and ask `POST /act {"kind":"cursor_position"}`.

## Driving it without a model

```bash
poetry run python -m src.cli sandbox-status   # display, browser, CDP, scale factor
poetry run python -m src.cli drive            # a full walkthrough, no API key needed
poetry run python -m src.cli fault set overlay
```

`drive` searches for member 12345 and opens their detail page using hardcoded coordinates, printing
what the probe would record at each step and leaving screenshots in `evidence/drive/`. Open the noVNC
window beside it and watch the cursor move.

It exists to answer one question cheaply: **do the hands work?** When a real discovery run misbehaves
later, running `drive` separates "the model chose badly" from "the plumbing is broken" in ten seconds
and for nothing. It fails loudly — non-zero exit — if the screen does not actually change.

## Network topology and why it matters

Two Docker networks:

- **`sandbox_net`** (`internal: true`) — carries agent traffic. It has no egress, so the browser the
  agent drives cannot reach the internet. This is the containment boundary, enforced by Docker rather
  than by asking the model nicely.
- **`host_net`** — a normal bridge, so published ports work from the host.

`bank-sim` and `relay` join both; the **sandbox joins only `sandbox_net`**.

That last point is why there are three services rather than two. A container attached only to an
internal network **cannot publish ports** — Docker creates no host binding at all, silently. So
`relay` (a two-line `socat` forwarder sharing the sandbox image) sits on both networks and forwards
6080 and 8900 inbound. It never forwards outbound, so the sandbox still cannot reach the internet:

```bash
docker compose exec sandbox curl -m 5 https://example.com   # fails — no egress
docker compose exec sandbox curl -s http://bank-sim:8001/   # works — the target
```

Chromium runs as non-root (uid 10001) **and** with `--no-sandbox`. These are not alternatives:
Docker's default seccomp profile blocks the unprivileged user namespaces Chromium's own sandbox
requires, so it cannot start regardless of user, and the settings that would permit it
(`seccomp=unconfined`, `SYS_ADMIN`) would weaken the container far more than disabling Chromium's
internal sandbox. Containment here is the non-root user, the no-egress network, and the policy engine.

### Fault injection is a host-only capability

```bash
curl -s localhost:8001/dev/fault-profile                  # active profile
curl -X POST localhost:8001/dev/fault-profile/overlay     # default | overlay | dialog | session | tenant_b
curl -s localhost:8001/dev/audit-log                      # servicing actions recorded during a run
```

Fault profiles are switched by the operator from the host, never by the thing under test. Be precise
about what enforces that, because the network alone does not:

- `/dev/*` is **not** linked from any page, so it is unreachable by clicking.
- Chromium runs in `--app=` mode with no address bar, and URL entry (`ctrl+l`) is not in the agent's
  action vocabulary — so the agent has no way to navigate to an arbitrary URL.
- The policy engine denies the `/dev/` route prefix on every observation (Step 6), and every action is
  checked against it before execution.

What is **not** true: that `sandbox_net` blocks these routes. `bank-sim` is a peer on that network and
serves `/dev/*` on the same port, so `http://bank-sim:8001/dev/fault-profile` answers from inside the
sandbox — verified, not assumed. The host's published port is unreachable from `sandbox_net`
(`internal: true` cuts the route to the host), but that is a different claim. Containment of `/dev/`
rests on the action vocabulary and the policy engine; the network's job is blocking **egress**, which
it does.

| Profile    | Effect                                | Demonstrates                          |
| ---------- | ------------------------------------- | ------------------------------------- |
| `default`  | none                                  | Happy-path discovery and replay       |
| `overlay`  | 1200 ms loading overlay               | Bounded wait/retry on a recoverable condition |
| `dialog`   | unexpected modal after review loads   | Escalation to a human                 |
| `session`  | session-expiry warning banner         | Reauthentication escalation           |
| `tenant_b` | different theme, "Continue" → "Proceed" | Cross-tenant replay via locator fallback |

## Status

Steps 1–5 of `discovery-loop-plan.md` are complete: the simulator is containerized, the sandbox
desktop runs, the surface agent exposes screenshot/act/probe on port 8900, and the host-side adapter
drives it (`python -m src.cli drive`). The discovery loop itself follows in Steps 6–13.
