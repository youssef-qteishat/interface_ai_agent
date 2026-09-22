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

| Port | Bound to    | Service                       | Added in |
| ---- | ----------- | ----------------------------- | -------- |
| 8001 | `127.0.0.1` | Credit Union Ops Simulator    | Step 1   |
| 6080 | `127.0.0.1` | noVNC — watch and take over   | Step 2   |
| 8900 | `127.0.0.1` | Sandbox surface agent         | Step 3   |

Every port is bound to loopback, so nothing here is reachable from the local network.

## Network topology and why it matters

Two Docker networks:

- **`sandbox_net`** (`internal: true`) — carries agent traffic. It has no egress, so the browser the
  agent drives cannot reach the internet. This is the containment boundary, enforced by Docker rather
  than by asking the model nicely.
- **`host_net`** — a normal bridge, so `bank-sim`'s published port works from the host.

`bank-sim` joins both; the sandbox (Step 2) joins only `sandbox_net`.

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

Step 1 of `discovery-loop-plan.md` is complete: the simulator is containerized and `compose.yaml`
exists. The sandbox desktop, surface agent, discovery loop, and CLI follow in Steps 2–13.
