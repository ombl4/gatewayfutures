# gatewayfutures — simulated calls for voice agents

A small platform that runs simulated phone calls against a support agent built on [LiveKit Agents](https://docs.livekit.io/agents/), records each call (audio, both transcripts, every tool call, timings), scores it on what actually happened in the backend, and shows the results in a simple web UI and a static HTML report.

```
practice session (persona + goal)
        ↓
simulated caller ⇄ support agent (LiveKit)
        ↓               ↓ tool calls → mock order backend
call record: audio · transcripts · tool calls · timings → scores → report
```

## Documents

| Document | What it is |
| --- | --- |
| [docs/PRD.md](docs/PRD.md) | Product requirements: goals, decisions, architecture, scoring, UI |
| [docs/spec.md](docs/spec.md) | Implementation spec: parts, tasks, verification steps, gates, metrics catalogue |
| [docs/TASKS.md](docs/TASKS.md) | Task tracker, updated as each task is verified |

## Setup

Requirements: Python 3.12 via [uv](https://docs.astral.sh/uv/), Docker, `ffmpeg`, and the LiveKit CLI (`lk`). On macOS: `brew install uv ffmpeg livekit-cli`.

```bash
cp .env.example .env     # fill in LiveKit (dedicated project), Deepgram and OpenAI keys
make install             # creates .venv and installs everything
make doctor              # checks keys, LiveKit, Deepgram, OpenAI, Docker, ffmpeg, lk
make test                # unit tests: no keys, no network
```

The LiveKit project must be dedicated to this platform: workers register under explicit agent names (`gf-support-agent`, `gf-sim-caller`) and only join rooms the runner creates.

## Commands

| Command | Purpose |
| --- | --- |
| `make test` | Unit tests (fast, offline) |
| `make test-live` | Tests that call real APIs |
| `make smoke` | One full simulated call end to end |
| `make up` | Start every service with Docker Compose |
| `uv run gf --help` | The `gf` CLI (doctor, agent, sessions, run, score, report, ui) |

Further sections (adding a practice session, reading a report) are added as those parts land; see the tracker.
