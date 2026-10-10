# gatewayfutures: simulated calls for voice agents

Runs simulated phone calls against a LiveKit support agent, records each call (audio, both transcripts, tool calls, timings), scores it on what actually happened, and shows the results in a web UI and a static report that a non-technical reader can follow from a one-minute summary down to the exact second a call went wrong.

- **Sample report** from real calls (`hard-002`: the hard suite, 15 sessions × 3 repeats, 45 valid calls, task success 84%, experience 67%; the agent claims to be a real person in every attempt of the disclosure session, and two policy sessions are flaky): published at **https://ombl4.github.io/gatewayfutures/** (static pages with audio) with the business-reader PDF at **https://ombl4.github.io/gatewayfutures/report.pdf**.
- **Design note**: [docs/design-note.md](docs/design-note.md) (choices, next week, second provider).
- **PRD** and **spec**: [docs/PRD.md](docs/PRD.md), [docs/spec.md](docs/spec.md); progress in [docs/TASKS.md](docs/TASKS.md).
- **To be continued**: the plan from here is in the design note, [To be continued](docs/design-note.md#to-be-continued): provider connectors, personas and sessions by industry, realistic personas from real voice samples, sessions from real data, UI polish, and validating every grader against hand-labelled calls. The scoring research is in [docs/research/scoring-standards.md](docs/research/scoring-standards.md).

## What a run tells you

Every valid call gets two verdicts. **Task**: the order system's log and final state show the right actions and the agent told the truth (this is the pass rate, with a Wilson interval and a flaky flag over three repeats). **Experience**: the call stayed within the reply-latency, dead-air and intelligibility bars. The simulated caller is scored separately, so a broken simulation is excluded rather than counted against the agent; when the caller mishears the agent, an independent recogniser decides whether the fault was the simulator's or the agent's own voice. Security sessions (prompt and PII extraction, planted rules, authority pressure) are judged on what the order system did and what the agent said, with `must say` / `must not say` phrases per session. The Settings page registers customers' agents by LiveKit URL, key and agent name, tests the connection, and runs the same sessions against them; the order system is the sandbox their tools point at.

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

Then open http://localhost:8090, check the "Can a run start?" panel, and press **Start run**. Without keys, `uv run gf demo-run` (or `make demo`) seeds a run from the committed real call records so every page has content; it is marked "demo data" and is never used as a comparison baseline. If port 8090 is taken, start the UI on another port (`gf ui --port 8091`) or override the compose port mapping. The run page shows calls as they finish; each call opens to its timeline, transcript, tool calls and checks.

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
uv run gf pdf <run_id>                             # PDF report for a business reader → runs/<run_id>/report.pdf
uv run gf run --suite hard --repeat 3              # the hard set: injection, PII probes, authority pressure, barge-ins, confusables
uv run gf targets add acme --name "Acme support" --url wss://... --agent-name acme-agent   # a customer's agent (secrets prompted, never committed)
uv run gf targets test acme                        # dispatch it into a room and wait for it to speak
uv run gf run --suite core --target acme           # run the set against that agent
uv run gf sessions                                 # validate and list sessions (incl. sessions/generated/)
uv run gf sessions generate --count 8 --focus "refund edge cases"   # LLM-generated sessions → sessions/generated/
uv run gf sessions export-simulate --out scenarios.yaml             # same sessions for LiveKit's own simulator
lk agent simulate audio --agent-name gf-support-agent --scenarios scenarios.yaml   # then: lk agent simulate export <run-id> > lk.json
uv run gf import-simulate lk.json --run-id lk-1    # LiveKit's results as a run, with its verdict next to our checks
uv run gf probe                                    # scripted caller, no LLM: checks the audio path
uv run gf run --suite smoke --repeat 2 --max-cost 3   # stop scheduling calls once the priced cost passes $3 (default GF_MAX_COST_USD=10)
uv run gf gate <run_id> --min-pass 0.7             # CI decision: exit 1 on claimed-without-acting, wrong write, >20% invalid, or an interval below the floor
uv run gf demo-run                                 # no keys: a run built from the committed real call records, so the UI has something to show
```

A full run of 11 sessions × 3 takes about 12 minutes at concurrency 4; the set is 43 sessions as of 2026-10-09 (42 hand-written, 1 generated); `gf run --suite smoke` for a quick check, `--suite adversarial` or `--suite faults` for one family.

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

The customers and orders available are listed on the UI's **Order system** page. Sessions are never deleted: to stop running one, move its file to `sessions/retired/` (it stays loadable so earlier runs still open and rescore). Keep every number the caller needs inside `facts`: a caller that says a number outside its facts makes the call invalid rather than counting against the agent.

## Personas

A persona is how a caller talks and what the line sounds like, never who they are: the name and the facts stay with the session because they belong to the order. `personas/*.yaml` holds twelve, in three even groups of four so a matrix run compares like with like: `standard` (cooperative callers on a clean line, the control group), `hard-line` (the same cooperation on noise, a phone line, packet loss, a poor microphone, slow speech) and `difficult` (clean line, hard behaviour: angry and interrupting, pushy, rambling, suspicious). `gf personas` lists them.

A session can reference one with `caller.persona_ref: elderly-slow-line`; the persona fills style, accent, voice, pace and line conditions for every field the session does not set itself, and the session's content hash covers the resolved values, so editing a persona changes the id of every session that uses it. To run the same scenarios as different callers, `gf run --suite core --persona-group hard-line` (or `--persona cafe-phone-line,angry-interrupting`, or `--persona-group all`) runs each selected session once per persona as a derived session: same facts, goal and expectations, the persona's voice and line, its own id, written under `runs/<run_id>/sessions/`. The run page then shows a session × persona grid with a pass count per cell and a total per persona, so a drop in one column is a persona (accent, line) problem and a drop in one row is a scenario (policy) problem. The start-run form has the same picker. A matrix multiplies the call count, so set `--max-cost`.

## Suites and areas

Every session has **areas** derived from its content: what the agent must do (`refund`, `address change`, `escalation`, `denial`) and what makes the call hard (`fault handling`, `hard line`, `interruptions`, `impatient`). Run pages show the pass rate per area. **Suites** are named lists in `sessions/suites.yaml` referencing session files by name: `smoke` for a quick check, `regression` for sessions that have failed before and must run every time, `core` (the 15 sessions that correspond to the published runs; its set tag differs from `full-4`'s because two generated sessions were retired and replaced after that run) and, added 2026-10-09 and not yet run, `edge` (verification problems, multi-intent, read-back correction), `personas` (accents, lines, elderly, rambling, angry, partial-then-full, grouped digits, "are you a bot"), `adversarial` (prompt injection, staff impersonation, privacy probe, wrong-customer pressure, out-of-scope, rude caller), `faults` (500s, double failure, slow tools, policy rejection, failing escalation) and `denial` (shipped, over limit, already refunded, caller insists), plus any you define. One session can be in several suites, and a unit test fails when an active session is in none. Add a session to a suite from its page in the UI or by editing the file; run one with `gf run --suite regression` or the suite picker in the start-run form; `gf sessions suites` lists them. Suites never touch the session files, so ids and the session-set tag stay stable.

## Archiving runs

When the scorer or the session set changes, earlier runs can no longer be compared with new ones. `gf runs archive --all` (or named run ids) moves them to `runs/_archive/`, out of every list and page but kept whole; `gf runs archive --restore <id>` brings one back. Retired sessions stay under `sessions/retired/` so archived runs still open.

## Environment tags

Every run carries two short tags you can quote: `env-…` hashes everything that shapes the agent and the measurement except the sessions (agent config and variant, STT/LLM/TTS models, engine, scoring method with thresholds, versions of livekit-agents, the LiveKit SDK, the OpenAI and Deepgram plugins, Python), and `set-…` hashes the session files run. Two runs are like-for-like when both match; otherwise the KPI deltas are marked with an asterisk. The tags are stored in the run's `manifest.json` and `environment.json`, and every environment seen is kept under `runs/_environments/<env-tag>.json` with its components and the runs that used it (the **Environments** page in the UI). `gf env` prints the tag a run would carry right now; `gf env --backfill` tags earlier runs: from their recorded versions, or, for runs made before manifests recorded versions, from the lockfile at the last commit before they started (the evidence is stored under `environment.backfilled`).

## Providers

The agent under test runs on a voice agent provider, LiveKit Agents today. The header names it and opens a menu with the providers the seam is designed for (Pipecat; Vapi, Retell, Bland and ElevenLabs Agents) and **Add a provider**, which opens the Providers page: what each provider must give the simulator (a way for the caller to reach the agent with real audio, the agent's tool calls and transcript, a config hash) and the day-by-day plan for adding one in a week. The registry lives in `gf/providers/__init__.py`; the provider itself is chosen by `provider:` in `gf/agent/config.yaml`, so a switch is tagged like any other agent change. The reasoning is in `docs/design-note.md`.

## How a call is graded

Every call page has a **Grading** tab that opens with one row of five answers: order system end state, required calls, nothing forbidden or unsafe, honest, experience. Below it the steps in the order the verdict is decided: (1) the simulated caller did its job (in character, heard the agent, ended for a reason; a hard failure makes the call invalid, soft ones flag the caller), (2) the agent did what the session asks, judged on the order system's log against the session's written requirements plus the must-say / must-not-say phrases and the security checks, (3) every claim matches a backend write, (4) the experience verdict: reply latency, dead air after the caller and the agent's own intelligibility have fail bars; interruptions, repeats and speech quality flag, (5) the verdicts and their rules. The transcript is annotated: each tool event, turn and silence carries the check that judged it with a ? that explains it; a scripted fault is shown as scripted, with what the agent did next. The call header shows the caller's score, the task verdict and the experience verdict separately, and the caller quality page explains the simulator's own score per persona. The persona judge (one small LLM call per scored call) is advisory and on by default; set `GF_PERSONA_JUDGE=0` to skip it.

## Latency, per turn

Reply latency is measured on the recording (last caller word → first agent sound) and shown per turn in the Spans tab, where each agent reply also shows where the agent's own clock says the time went: end-of-turn detection, transcription, LLM first token, TTS first byte, playback, and the rest as unaccounted (network and buffering). p95 per call and the median per run are on the overview; the bars (flag over 2 s, fail over 3.5 s) live in `thresholds.yaml`.

## Spans

The Spans tab in the call inspector shows the call as a trace, the way a tracing tool would: a tree of spans with an icon per type, status (OK, Slow, Error), start time, duration, and a labelled bar on the call's time axis. The tree is `voice_call` → `turn_N_user` (end-of-turn detection, speech-to-text) · `agent_reasoning` (the LLM's first token with its tokens, each `tool_call` with the order system's `backend` handling, idle time) · `agent_response` (text-to-speech first byte, playback) · dead air, talk-over and interruptions from the recording. Search, filter by type or status, highlight the critical path, show idle time, flatten the tree, zoom, and expand or collapse everything. Clicking a span opens a panel with an explanation when it is slow or failed, its times, model and tokens, a latency breakdown of its children and links to related spans; clicking a bar seeks the audio, and a red playhead follows it. `gf spans runs/<run>/<session>/<n>` writes the same tree as `spans.json`, and `--otlp` writes an OTLP/JSON trace that opens in Jaeger or Tempo. Everything is computed from the record, so it is deterministic and free to recompute.

## What a call record contains

`runs/<run_id>/<session_id>/<attempt>/`: `audio.wav` (stereo: left caller, right agent) and `audio.json`, `caller.json` and `caller_events.jsonl` (what the caller said, heard and decided), `agent_events.jsonl` and `agent_session_report.json` (the agent's transcripts, replies, tool calls and per-turn metrics), `backend_log.json` and `backend_state.json`, `meta.json`, then `timeline.json` and `scores.json` after scoring.

## Cost and budget

Every call carries a `cost` block in `scores.json`, priced from the providers' own usage events (the agent's session report: OpenAI tokens including cached ones, Deepgram TTS characters and STT seconds; the caller's metric events for its side) with the prices in `pricing.yaml`. The run page, the runs table and the call page show it; a run of 45 one-minute calls is a few dollars. Prices never affect a verdict, so changing them changes no tag. `gf run --max-cost N` (default `GF_MAX_COST_USD=10`) stops scheduling new calls once the priced cost of the finished ones passes the budget; the run is marked **partial**, the skipped calls are listed in the manifest, and `gf gate` fails it. LiveKit Cloud minutes and the persona judge are not metered and are listed as such.

## CI

`ci.yml` runs lint, unit tests and the Playwright dashboard suite on every push without keys. Real calls are never started automatically: runs are started by a person from the UI or the CLI (`gf run --suite core`), and `gf gate <run>` can be used as a release check on a finished run.

## Hosting the UI

The UI is a stateless FastAPI service over `runs/` and `sessions/`; the compose `ui` service has a health check and restart policy. To expose it:

1. Set `GF_UI_TOKEN=<long random string>` in `.env`; pages then require a sign-in and the JSON API a `Authorization: Bearer` header.
2. Put a reverse proxy with TLS in front of port 8090 (for example Caddy: `example.com { reverse_proxy localhost:8090 }`).
3. Keep the agent worker and the order system as sibling services (`make up` does this); the UI starts runs by spawning `gf run` next to them.

The JSON API mirrors every page (`/api/runs`, `/api/runs/<id>`, `/api/runs/<id>/<session>/<attempt>`, `/api/sessions`, `/api/agent`, `/api/status`), with OpenAPI docs at `/api/docs`.

The static report has no server at all: `.github/workflows/pages.yml` publishes `docs/sample-report` to GitHub Pages on every push to `main` (Pages source: GitHub Actions). Current link: https://ombl4.github.io/gatewayfutures/

## To be continued

The plan from here, in order (also in [docs/design-note.md](docs/design-note.md#to-be-continued) and the tracker):

1. **Connectors for other agent providers.** An agent under test is a LiveKit target today (URL, key, agent name). Add a target kind per provider behind the same seam: Pipecat on the LiveKit transport, then Retell, Vapi, Bland and ElevenLabs Agents through a SIP trunk into the caller's room. The caller, the recording and every audio-based check stay the same.
2. **Personas and practice sessions by industry.** A persona library and a session set per industry (retail support first, then banking, insurance, healthcare scheduling, telecom), each with its own sandbox fixture and tools.
3. **Realistic personas.** Tune each persona against recordings of real callers: voice samples, measured pace, pauses, interruptions and accent, and higher-end voice providers where the current voices read as synthetic.
4. **Practice sessions from real data.** Derive each industry's sessions from customer call data where it exists, otherwise from thorough research and sample calls, so the split of situations matches what happens in that industry.
5. **UI polish.** One pass per page with a non-technical reader in front of it.
6. **Validate every grader.** A hand-labelled set of real calls per check, judge and claim; measure agreement and tune with a human reviewing the disagreements. A grader stays advisory until it reaches agreement.

## Tests

```bash
make test        # unit tests, no keys or network (backend, schema, audio conditions, timeline, scoring, report, UI)
make test-ui     # dashboard in headless Chromium (Playwright): every button, tab, player, replay and link
make test-live   # tests that call the real LLM (agent behaviours in text mode)
make lint
```

Thresholds for every flag live in `thresholds.yaml`. Scoring is deterministic; re-scoring never re-runs calls, and a change to a scoring rule bumps `METHOD_VERSION` so runs scored under different rules are never compared as like-for-like. The caller's mutual-silence re-prompt (4 s) and abort (8 s) are floors: once two replies have been heard they scale with the agent's median reply latency (1.5× and 2.5×), so a slow agent is not re-prompted in the middle of starting its reply.
