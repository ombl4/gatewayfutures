# gatewayfutures: simulated calls for voice agents

Runs simulated phone calls against a LiveKit support agent, records each call (audio, both transcripts, tool calls, timings), scores it on what actually happened, and shows the results in a web UI and a static report that a non-technical reader can follow from a one-minute summary down to the exact second a call went wrong.

- **Sample report** from real calls (11 sessions × 2, two failures caught): published at **https://ombl4.github.io/gatewayfutures/** (also `docs/sample-report/index.html`, which opens from disk).
- **Design note**: [docs/design-note.md](docs/design-note.md) (choices, next week, second provider).
- **PRD** and **spec**: [docs/PRD.md](docs/PRD.md), [docs/spec.md](docs/spec.md); progress in [docs/TASKS.md](docs/TASKS.md).

## How it works

```
sessions/*.yaml ──► simulated caller ──(LiveKit room, real audio)──► support agent "Ava" ──► mock order system
                      │ records what it said and heard                 │ transcripts, tool calls, latency   │ request log + final state
                      └────────────────────────── one call record per attempt ───────────────────────────────┘
                                                              │
                                                  scoring (deterministic checks) ──► run summary ──► UI / static report
```

| Piece | Where | What it is |
| --- | --- | --- |
| Support agent | `gf/agent/` | A real LiveKit Agents worker: Deepgram Nova-3 → OpenAI → Deepgram Aura-2 (voice Luna), Silero VAD, LiveKit turn detector and noise cancellation, four tools that call the mock backend. Configured in one file, `gf/agent/config.yaml`, shown read-only in the UI. |
| Mock order system | `gf/backend/` | FastAPI. Orders, refunds, tickets; business rules (zip check, refund once up to total, address only while processing); per-call state; fault injection; request log. The ground truth for what the agent did. |
| Simulated caller | `gf/caller/` | A second voice agent that plays the customer from a session file: persona, facts, goal, voice/accent, pace, noise at a measured SNR, phone-line filter, packet loss, patience. A hidden participant records both sides to a stereo WAV. |
| Runner | `gf/runner/` | One LiveKit room per call, concurrent calls with per-call isolation, a record folder per attempt, a manifest per run. |
| Scoring | `gf/scoring/` | Tool/outcome checks, claimed-without-acting, WER and entity checks, latency/dead-air/talk-over/barge-in from the audio, transcript quality gates, validity rules, Wilson intervals and flaky detection. |
| Reports and UI | `gf/report/`, `gf/ui/` | The same Jinja templates render the live UI (`gf ui`) and the static report (`gf report`): one workbench page (KPI cards with trends, practice-session accordion, prioritised issues, call inspector with synchronised audio, transcript, tool events and checks), a left navigation with the agent's configuration, and run control. No CDN, no JavaScript build. |

## Setup

Prerequisites: Python 3.12 with [uv](https://docs.astral.sh/uv/), Docker (for `make up`), `ffmpeg` (report audio), and keys for LiveKit Cloud, Deepgram and OpenAI.

```bash
git clone https://github.com/ombl4/gatewayfutures && cd gatewayfutures
cp .env.example .env          # fill in LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET, DEEPGRAM_API_KEY, OPENAI_API_KEY
make install                  # uv sync
make doctor                   # checks keys, LiveKit, Deepgram, OpenAI, Docker, ffmpeg
```

Use a dedicated LiveKit Cloud project for this platform. The agent registers under the name `gf-support-agent` with explicit dispatch, so it only joins rooms the runner creates.

### One command

```bash
make up                       # docker compose: order system (:8080), agent worker, UI (http://localhost:8090)
```

Then open http://localhost:8090, check the "Can a run start?" panel, and press **Start run**. The run page shows calls as they finish; each call opens to its timeline, transcript, tool calls and checks.

### Without Docker

```bash
uv run uvicorn gf.backend.app:app --port 8080     # terminal 1: order system
uv run gf agent dev                                # terminal 2: agent worker (connects out to LiveKit Cloud)
uv run gf ui                                       # terminal 3: UI on http://127.0.0.1:8090
```

### Command line

```bash
uv run gf call sessions/refund-basic.yaml          # one simulated call → runs/_single/<session>/1/
uv run gf run --all --repeat 3 --concurrency 4     # the whole session set → runs/<run_id>/
uv run gf score <run_id>                           # scores.json per call, summary.json per run
uv run gf report <run_id>                          # static pages in runs/<run_id>/report/
uv run gf report <run_id> --bundle --out docs/sample-report   # self-contained, MP3 audio
uv run gf sessions                                 # validate and list sessions (incl. sessions/generated/)
uv run gf sessions generate --count 8 --focus "refund edge cases"   # LLM-generated sessions → sessions/generated/
uv run gf sessions export-simulate --out scenarios.yaml             # same sessions for LiveKit's own simulator
lk agent simulate audio --agent-name gf-support-agent --scenarios scenarios.yaml   # then: lk agent simulate export <run-id> > lk.json
uv run gf import-simulate lk.json --run-id lk-1    # LiveKit's results as a run, with its verdict next to our checks
uv run gf probe                                    # scripted caller, no LLM: checks the audio path
```

A full run of 11 sessions × 3 takes about 12 minutes at concurrency 4.

## Adding a practice session

A session is one YAML file in `sessions/`. It is immutable: its id is a hash of the content, so editing a file creates a new session and old results stay tied to the definition that produced them.

Three ways: **Sessions → Generate** in the UI (or `gf sessions generate`) proposes sessions from the agent description and the fixture data, validates them and writes them to `sessions/generated/` for review; **Sessions → New session** is a validated editor, prefilled from a template or from any existing session; or copy a file:

```bash
cp sessions/refund-basic.yaml sessions/refund-angry-caller.yaml
$EDITOR sessions/refund-angry-caller.yaml
uv run gf sessions          # validates every file: known tools, existing fixture, well-formed values
```

```yaml
title: Refund for a broken blender, clean line
caller:
  persona: {name: Maria Lopez, style: polite, accent: en-US}
  facts: {order_id: GW-48213, zip: "94110", amount: 89.99, item: countertop blender}   # the only numbers the caller may say
  goal: Get a full refund of $89.99 for order GW-48213, which arrived broken.
  stop_when: the agent confirms the refund was issued
  voice: aura-2-asteria-en            # Deepgram Aura-2 voice; accents: en-US, en-GB, en-AU, en-PH, ...
  pace: 1.0
  llm: {model: gpt-4.1-mini, temperature: 0.0, seed: 101}
  conditions: {noise: "cafe@15dB", phone_line: true, packet_loss: 0.02, low_quality_mic: false, interruptions: 0.4, patience_s: 20}
  limits: {max_turns: 12, max_duration_s: 150, mutual_silence_reprompt_s: 4, mutual_silence_abort_s: 8}
fixtures: orders_basic                # customers and orders the mock backend starts from (fixtures/orders_basic.yaml)
faults: [{tool: issue_refund, type: error_500, nth: 1}]   # optional: latency_ms | error_500 | timeout | reject
expected:
  outcome: refunded
  tool_calls:
    required:
      - {tool: lookup_order, args: {order_id: GW-48213, zip: "94110"}}
      - {tool: issue_refund, args: {order_id: GW-48213, amount: 89.99}}
    order: [lookup_order, issue_refund]
    forbidden: [escalate_to_human, update_shipping_address]
  final_state:
    - refunds[GW-48213].amount == 89.99
```

The customers and orders available are listed on the UI's **Order system** page. Keep every number the caller needs inside `facts`: a caller that says a number outside its facts makes the call invalid rather than counting against the agent.

## Latency, per turn

Every agent reply shows two latencies in the call inspector's **Latency** tab: what the caller heard (last caller word → first agent sound, measured on the recording) and where the agent's own clock says the time went: end-of-turn detection, transcription, LLM time to first token, TTS time to first byte, playback, with the rest shown as unaccounted (network and buffering). The run page aggregates the medians so a slow run can be attributed to one stage.

## What a call record contains

`runs/<run_id>/<session_id>/<attempt>/`: `audio.wav` (stereo: left caller, right agent) and `audio.json`, `caller.json` and `caller_events.jsonl` (what the caller said, heard and decided), `agent_events.jsonl` and `agent_session_report.json` (the agent's transcripts, replies, tool calls and per-turn metrics), `backend_log.json` and `backend_state.json`, `meta.json`, then `timeline.json` and `scores.json` after scoring.

## Hosting the UI

The UI is a stateless FastAPI service over `runs/` and `sessions/`; the compose `ui` service has a health check and restart policy. To expose it:

1. Set `GF_UI_TOKEN=<long random string>` in `.env`; pages then require a sign-in and the JSON API a `Authorization: Bearer` header.
2. Put a reverse proxy with TLS in front of port 8090 (for example Caddy: `example.com { reverse_proxy localhost:8090 }`).
3. Keep the agent worker and the order system as sibling services (`make up` does this); the UI starts runs by spawning `gf run` next to them.

The JSON API mirrors every page (`/api/runs`, `/api/runs/<id>`, `/api/runs/<id>/<session>/<attempt>`, `/api/sessions`, `/api/agent`, `/api/status`), with OpenAPI docs at `/api/docs`.

The static report has no server at all: `.github/workflows/pages.yml` publishes `docs/sample-report` to GitHub Pages on every push to `main` (Pages source: GitHub Actions). Current link: https://ombl4.github.io/gatewayfutures/

## Tests

```bash
make test        # unit tests, no keys or network (backend, schema, audio conditions, timeline, scoring, report, UI)
make test-ui     # dashboard in headless Chromium (Playwright): every button, tab, player, replay and link
make test-live   # tests that call the real LLM (agent behaviours in text mode)
make lint
```

Thresholds for every flag live in `thresholds.yaml`. Scoring is deterministic; re-scoring never re-runs calls.
