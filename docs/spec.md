# Spec: Voice Agent Call Simulator (gatewayfutures)

## Context

The PRD (Claude Doc "Voice Agent Call Simulator — PRD") defines a platform that runs simulated phone calls against a LiveKit support agent, records each call, scores it on what actually happened, and shows layered reports. This spec breaks the PRD into parts and tasks. Each task has a verification step that must pass before the next part starts. The build order is POC first (mock backend + light agent, proven working), then the harness on top, with every layer testable on its own.

Decisions finalized on 2026-10-08 (to be written back into the PRD on approval):

| Topic | Decision |
| --- | --- |
| Repeats per session | 3 (default), configurable per run |
| Speech | Deepgram for both STT (Nova-3) and TTS (Aura-2), agent and caller |
| LLM | OpenAI only. Agent and caller on one model; judge on a different model at temperature 0 with a versioned prompt |
| Sessions | Immutable. A session's id is the hash of its content; the UI cannot edit, only create new ones |
| Reports | A static, self-contained `report.html` per run, layered so a non-technical reader can go summary → session → call → timeline |
| UX thresholds | p95 latency 2.0 s is the starting flag; all thresholds live in one config file |
| UI | Python-only: FastAPI + Jinja2, native audio + canvas lanes, no CDN. The same templates render the live UI and the static report |
| Isolation | Dedicated LiveKit Cloud project; agent names `gf-support-agent` / `gf-sim-caller`; explicit dispatch; rooms `gf-sim-<run>-<call>` |

Local toolchain: Docker Desktop running (CLI at `~/.docker/bin/docker`), `uv` 0.12 for Python 3.12, no Node needed.

## Test tiers (apply to every part)

| Tier | Command | Needs | Runs in |
| --- | --- | --- | --- |
| unit | `make test` | nothing external; fixtures only | seconds |
| live | `make test-live` | API keys (OpenAI, Deepgram, LiveKit) | minutes, marked `@pytest.mark.live` |
| e2e | `make smoke` | everything, Docker | one full simulated call |

Rule: scoring, timeline building, session validation and report rendering are pure functions over files, so they are all unit-testable with committed fixtures. Only workers and the runner need `live`.

## Repository layout

```
gatewayfutures/
  pyproject.toml           uv project, one package "gf"
  Makefile                 up, test, test-live, smoke, run, report, ui, doctor
  docker-compose.yml       backend, agent, caller, ui
  .env.example
  gf/
    cli.py                 `gf` entry point (typer)
    config.py              settings, thresholds.yaml loader
    backend/               Part 1  mock backend (FastAPI)
    agent/                 Part 2  support agent worker + config.yaml + description.md
    caller/                Part 3  simulated caller worker, brain, audio conditions
    sessions/              Part 4  schema, loader, generator
    providers/             Part 4  base.py (start_call, collect, agent_config), livekit.py
    runner/                Part 4  room orchestration, artifact collection (through providers/)
    record/                Part 4  call record schema, timeline builder (VAD, alignment)
    scoring/               Part 5  tools, claims, wer, ux, validity, stats
    report/                Part 6  report model, Jinja templates, static renderer
    ui/                    Part 6  FastAPI app serving the same templates
  sessions/                *.yaml hand-written; generated/ for LLM-generated
  fixtures/                backend seed data, noise clips, test WAVs, recorded records
  runs/                    <run_id>/... (gitignored except docs/sample-report)
  docs/                    spec.md, sample-report/, design-note.md
  tests/                   unit/, live/, e2e/
```

---

## Part 0 — Scaffold and environment

**T0.1 Project skeleton.** `uv` project (Python 3.12), `ruff`, `pytest`, `typer` CLI with `gf --help`, Makefile targets, `.env.example`, `docker-compose.yml` with empty services, `thresholds.yaml`.
Verify: `make test` passes with one placeholder test; `uv run gf --help` lists commands.

**T0.2 Environment check.** `gf doctor`: validates `.env` (LIVEKIT_URL/KEY/SECRET, DEEPGRAM_API_KEY, OPENAI_API_KEY), lists rooms via the LiveKit API, pings Deepgram and OpenAI with a minimal request, checks Docker.
Verify: `gf doctor` prints all green on this machine with the keys provided.

Gate 0: both verifications pass; committed.

---

## Part 1 — Mock backend (POC, no LiveKit)

**T1.1 Data model and seeding.** Pydantic models: Customer, Order (status: processing/shipped/delivered), Refund, Ticket. Seed from `fixtures/orders_basic.yaml`. State is held per `call_id` so concurrent calls never share data.
Verify: unit tests — seed loads, state isolated per call id.

**T1.2 Tool endpoints and business rules.** `POST /tools/lookup_order` (`order_id`, `zip`; mismatch → 404 "not found", so verification is enforced by the backend), `issue_refund`, `escalate_to_human`, `update_shipping_address`. Strict argument formats (order id `GW-\d{5}`, amounts to 2 dp); problems logged as `arg_problems`. Rules: refund only for delivered orders, amount ≤ total, once per order; address change only before shipping; escalate creates a ticket and marks the call ended. Every request requires `X-GF-Call-Id`.
Verify: unit tests for each rule (allowed and rejected cases), missing header → 400.

**T1.3 Request log and state endpoints.** Every request logged with monotonic start/end, args, response, status. `GET /calls/{id}/log`, `GET /calls/{id}/state`, `POST /calls/{id}/seed`, `DELETE /calls/{id}`.
Verify: unit tests — log order and timing fields; state before/after a refund.

**T1.4 Fault injection.** Per-call config: `{tool, type: latency_ms|error_500|timeout|reject, nth}`.
Verify: unit tests — each fault type produces the expected response and is logged with the fault tag.

Gate 1: `docker compose up backend`; `curl` a lookup and a refund; log shows both. All unit tests pass.

---

## Part 2 — Support agent (POC "light agent", a real LiveKit voice agent)

The agent is the thing under test, so it must behave like a production support line: it greets, verifies, handles interruptions and noise, covers tool latency, confirms before irreversible actions, recovers from misheard values, and ends the call cleanly. The mock backend (Part 1) is its system of record for orders, refunds and tickets.

**T2.0 LiveKit Cloud project setup.** A dedicated LiveKit Cloud project for this platform. `lk` CLI configured against it. Agent dispatch set to explicit (no automatic dispatch), so only rooms created by the runner get the agent. Document in README: project creation, keys into `.env`, `lk room list`, `lk dispatch create --agent-name gf-support-agent --room <room>`.
Verify: `gf doctor` lists rooms; `lk dispatch create` on a test room shows the agent joining in the LiveKit Cloud dashboard.

**T2.1 Agent config.** `gf/agent/config.yaml`: provider, system prompt, STT/LLM/TTS models and voice, VAD and turn-detection settings, interruption settings, tool list with JSON schemas, plus `description.md` (short plain-language description used for session generation). Loader computes a `config_hash`.
Verify: unit test — config loads, hash is stable, tools match the backend's four endpoints.

**T2.2 Agent worker.** `livekit-agents` 1.x worker named `gf-support-agent`, explicit dispatch only, runs in Docker and connects outbound to LiveKit Cloud (no inbound ports). Pipeline: Deepgram STT (nova-3, with keyterm boosting for "GW" order ids) → OpenAI LLM → Deepgram TTS (aura-2), Silero VAD, LiveKit multilingual turn detector, LiveKit Cloud noise cancellation (BVC) on the inbound track. Four `@function_tool`s that call the backend over HTTP with the call id from job metadata; `RunContext.userdata` holds call id and verification state.
Voice behaviours (each one is a test case in T2.4):
- Greets first on join ("Thanks for calling Gateway Goods…"), so the caller never speaks into silence.
- Verifies name + zip before disclosing order details (enforced by the backend's `lookup_order` zip check as well).
- Reads back amount and order id and asks for a yes before `issue_refund` or `update_shipping_address`.
- Says a short filler ("One moment while I check that") when a tool takes > 700 ms, and never claims a result before the tool returns.
- Interruptions allowed: stops speaking within the turn-detector budget when the caller barges in; resumes with context.
- On a tool error or rejection, tells the caller truthfully and offers escalation; never says the action happened.
- On an unknown order id, asks the caller to repeat or spell it digit by digit (handles STT mishears).
- After `escalate_to_human`, says the handoff line, then ends the call by disconnecting; also ends after a configurable max duration or 20 s of caller silence after a prompt.
Verify (no audio): `live` test using the LiveKit Agents testing API (`AgentSession.run(user_input=...)`) with the real LLM: "I want a refund for GW-48213, zip 94110" → `lookup_order` then `issue_refund` appear in the backend log with correct args, and the refund is only issued after the caller says yes.
Verify (audio, manual): `uv run gf agent console` — talk to it from the laptop mic, get an order looked up and refunded, interrupt it mid-sentence.
Verify (cloud): the Dockerised worker registers with the LiveKit Cloud project; `lk dispatch create` puts it in a room; the LiveKit Agents Playground can talk to it end to end.

**T2.3 Agent event capture.** The worker emits structured events to a per-call JSONL (`agent_events.jsonl`): `agent_greeting`, `user_transcript_final` (what the agent heard), `agent_speech_start/end` with the spoken text, `tool_call_start/end` (captured from the tool-execution event, not from conversation items), `metrics` (end-of-utterance delay, LLM TTFT, TTS TTFB, durations), `interrupted`, `call_end` with reason. All timestamps monotonic ms. Events are also sent on the room data channel so the caller worker collects them live.
Verify: unit test on the event schema; the T2.2 text-mode test produces an events file whose tool events match the backend log one-to-one.

**T2.4 Conversation test cases (text mode, `live`).** One test per voice behaviour above, driven through `AgentSession.run`: greeting first; refuses details before verification; refund needs confirmation; tool fault → honest message + escalation offer; unknown id → asks to repeat; escalation ends the call. These become the regression suite for the agent itself, independent of the simulator.
Verify: all pass 3 runs in a row.

Gate 2 (POC done): a human can call the agent from the Playground and from the console, complete a refund with interruption and background noise, and get escalated; the text-mode suite passes; events and backend log line up. **Stop here for review before the harness.**

---

## Part 3 — Simulated caller

**T3.1 Caller brain.** Pure function: (persona, goal, facts, transcript so far) → structured output `{say, goal_met, giving_up, repeating_myself, broke_character}` via OpenAI structured outputs. The persona prompt forbids inventing facts outside `facts`.
Verify: unit tests with cached LLM responses; one `live` test that a refund persona says a line containing its order id and sets `goal_met` after the agent confirms.

**T3.2 Scripted caller (audio plumbing).** A deterministic caller mode that plays a fixed list of lines (no LLM). Used to test audio end-to-end without non-determinism.
Verify: `live` test — scripted caller joins a room with the agent, both tracks are recorded, `audio.wav` has two channels with speech on each.

**T3.3 Caller worker.** Worker `gf-sim-caller`: joins the room from job metadata, subscribes to the agent track, runs Deepgram STT on it, speaks via Deepgram Aura (voice from persona), waits for the agent's greeting (or opens after 4 s and flags `caller_spoke_first`), ends on `goal_met` / `giving_up` / `max_duration_s` / agent escalation. Records stereo WAV (L = caller as sent, R = agent as heard). Writes `caller_transcript.json` (its own text per turn with turn start/end; word-level timings obtained by transcribing its own clean TTS audio and aligning to the known text, since Aura returns no word timestamps) and `agent_heard.json` (its STT of the agent). Collects the agent's data-channel events. Sends a clock-sync message at join.
Verify: `make smoke` — one full automated call from a session file produces `audio.wav`, both transcripts, `agent_events.jsonl`, `meta.json` with an end reason.

**T3.4 Audio conditions.** DSP on the outgoing track: noise mix at target SNR, 8 kHz band-limit + light packet loss, TTS pace, barge-in (start speaking while the agent is talking with probability p), patience budget. Accent = Deepgram Aura voice choice (US, UK, AU, IE, IN and Spanish-accented English as available in the catalogue); a per-session `tts` override hook exists so another TTS vendor can be added later for wider accent coverage.
Verify: unit tests — measured SNR within 1 dB of target; band-limit removes energy above 4 kHz; barge-in triggers at p=1 and never at p=0. A `live` call with `noise: cafe@15dB` completes.

**T3.5 Validity detection.** Rules: caller LLM error/timeout, `broke_character`, facts contradiction, no caller audio published, hang-up before first goal attempt, watchdog (10 s mutual silence).
Verify: unit tests, one fixture per invalid reason.

Gate 3: `make smoke` passes; a noisy session completes; invalid detection tests pass.

---

## Part 4 — Sessions, runner, call record

**T4.1 Session schema.** Pydantic schema as in the PRD (`persona`, `goal`, `conditions`, `fixtures`, `faults`, `expected`). `id = sha256(content)[:12]`; the loader refuses a file whose stored id does not match its content. Validation rejects unknown tools/args (checked against agent config) and missing fixtures. `gf sessions validate`.
Verify: unit tests — valid file loads, tampered file rejected, unknown tool rejected.

**T4.2 Provider interface and runner.** `gf/providers/base.py` defines `Provider` with `agent_config()`, `start_call(session, call_id) -> CallHandle`, `collect(handle) -> attempt folder`; `livekit.py` implements it (create room `gf-sim-<run>-<call>`, dispatch both workers with metadata, wait for the caller's completion signal, delete the room). `gf run --session <id>|--all --repeat 3 --concurrency 2` uses only the interface: seed backend for `call_id`, start, collect, pull backend log/state, write `runs/<run_id>/<session_id>/<n>/`. `manifest.json` per run with config hash, models, cost, durations. Retries once on infrastructure failure, then marks the attempt invalid.
Verify: unit test with a `FakeProvider` (orchestration order, folder layout, retry) — this also proves the second-provider seam. `live`: `gf run --session smoke --repeat 2` produces two complete attempt folders.

**T4.6 Provider seam (added 2026-10-08).** `gf/providers/base.py`: `Provider` protocol with `agent_config() -> (hash, description)`, `start_call(session, call_id, record_dir, attempt, variant) -> CallHandle`, `collect(handle) -> meta`; `gf/providers/livekit.py` implements it with what `runner/call.py` does today, and the runner only uses the protocol. A `FakeProvider` in tests exercises the runner's orchestration (folder layout, manifest, retry on infrastructure failure) without any network.

The seam is designed for the providers teams actually use, not only LiveKit. Every provider must meet three obligations, and the module for each says how:

| Provider | Reach the agent with real audio | Tool calls and transcripts | Config hash |
| --- | --- | --- | --- |
| LiveKit Agents (today) | caller joins the room the runner created; explicit dispatch | data channel events + mock backend log | hash of `config.yaml` + tools |
| Pipecat | Pipecat bots run on a transport; with the LiveKit or Daily transport the caller joins the same room; otherwise a WebRTC/WebSocket client | the bot's function-call frames via its observer/event hooks + the backend log | hash of the bot's pipeline definition |
| Vapi / Retell / Bland / ElevenLabs Agents | the caller dials the agent's number through a SIP trunk attached to the caller's room (the recording and timing stay caller-side), or opens the provider's web-call session where offered | call-end webhook or call API (transcript with timestamps, tool-call records) + the backend log, which is authoritative | hash of the assistant/agent definition fetched from the provider's API |

Scoring, the record schema and the UI never change: they only see a record folder. Provider-specific metrics (the agent's own latency breakdown) are optional per record. `gf/providers/vapi.py` and `gf/providers/pipecat.py` ship as documented skeletons (the mapping above as code comments and typed stubs that raise `NotImplementedError`), so the next provider is a new module, not a rewrite.
Verify: unit test of `run_batch` with `FakeProvider`; `gf run` unchanged for users; the skeleton modules import cleanly and list their obligations.

**T4.5 Session generation.** `gf sessions generate --count 12` from `description.md`, the tool list and the fixture data: asks the LLM (structured output, temperature 0, seed) for sessions spread across tools, policy edges, injected faults and hard callers (accents, noise, pace, impatience); validates each against the schema and agent config; writes to `sessions/generated/` for human review (the runner and the UI treat `sessions/` and `sessions/generated/` alike; a generated session is tagged in the UI). The UI offers the same action (Sessions → Generate) with the count and an optional focus ("refund edge cases").
Verify: `live` test generates 5 valid sessions; unit test that invalid generated output is rejected and never written.

**T4.3 Timeline builder.** Pure function over an attempt folder: VAD per channel on `audio.wav` → speech segments; align event clock to audio using the sync marker; merge agent events and backend log into `timeline.json` (one ordered list: caller_turn, agent_turn, tool_call, silence, overlap).
Verify: unit tests on synthetic WAVs with known gaps — dead air and latency exact to ±50 ms; overlap detected; tool calls land inside the right agent turn.

**T4.4 Call record schema.** `CallRecord` Pydantic model that loads an attempt folder and is the only input to scoring and reporting. `gf record show <path>` prints it.
Verify: fixture folder from the smoke run (committed under `fixtures/records/`) loads and round-trips.

Gate 4: a 3-repeat run of two sessions produces complete, loadable records; timeline tests pass.

---

## Part 5 — Scoring (pure functions over CallRecord)

**T5.1 Tool and outcome checks.** required (exact args, numeric ±0.01), order, forbidden, final_state assertions, extra calls counted. Each result carries evidence ids (tool call ids, timeline indices).
Verify: unit tests from fixture records: pass case, missing call, wrong arg, wrong order, forbidden call.

**T5.2 Claimed-without-acting.** LLM extracts claims of completed actions from agent turns (versioned prompt `claims/v1`), code matches each claim to a successful backend write within the call. Also: after an injected fault, did the agent say something false.
Verify: unit tests with cached extraction output; a labelled set of ~30 agent turns in `fixtures/labels/claims.jsonl` with an agreement test (target ≥ 0.9).

**T5.3 Speech accuracy.** Normalise (case, punctuation, numbers→words), WER per turn and per call (`jiwer`), entity check for `facts` (order id, name, zip, amount), causal link: misheard entity that appears in a tool argument.
Verify: unit tests with known WER values; entity tests; causal link fixture.

**T5.4 UX metrics.** From `timeline.json`: response latency p50/p95 (caller speech end → agent audio start), dead air (both channels silent > 3 s), talk-over count, barge-in stop time, repeats (`repeating_myself` + near-duplicate caller lines), time to resolution (start → goal-meeting backend write), would-hang-up rule. Thresholds from `thresholds.yaml`.
Verify: unit tests on synthetic timelines with known answers.

**T5.5 Statistics and aggregation.** Per session: k/N pass, Wilson 95% interval, pass^k, flaky flag (neither all pass nor all fail), invalid rate. Per run: overall pass rate, top failure reasons, latency distribution. Comparison to previous run only when `config_hash` matches.
Verify: unit tests against hand-computed Wilson values; flaky/invalid classification.

**T5.7 Detector self-test (added 2026-10-08).** Anyone who doubts a check can make the agent misbehave on purpose and watch the check fire. The worker accepts an `agent_variant` per call (job metadata). Variant `dishonest` removes the write tools and instructs the agent to tell the caller the refund or address change was done anyway; its config hash is stamped `<hash>+dishonest`, so such calls never compare with real runs. `gf run --variant dishonest --sessions <file>` (and `gf check-detector`, which runs, scores and prints the verdict) produce a run whose manifest carries `kind: detector_check` and the expected failing check; the run page shows "detector caught it" when every call fails on `claims.claimed_without_acting`, else "detector MISSED" in red. The UI offers the same from the Scoring page and from any run page ("Prove the honesty check": pick a session, start). Detector-check runs are listed separately and never used as a "previous comparable run". Claim patterns are broadened ("I've refunded you", "the refund is on its way", "I've gone ahead and refunded", "your address is now ...").
Verify: unit tests for the variant (tools removed, instructions appended, hash suffix) and for the verdict logic on a fixture run; a real detector-check call fails on the honesty check and the run page shows "caught".

**T5.8 Simulator hearing check (added 2026-10-08).** The caller hears the agent through its own speech recognition, and a mishearing there ("Austin" for "Boston") can derail a call through no fault of the agent. Per call: word error rate of what the agent said (its own text) vs what the caller heard (its STT), aligned turn by turn; entity check on the agent's spoken order ids, zips, amounts and addresses (digits normalised). A misheard entity that the caller then acted on (said or disputed within its next two turns) marks the call **invalid: simulator misheard the agent**, with the turn pair as evidence; a high caller-side WER without an acted-on entity is a soft flag "simulator hearing". Shown on the call page as "heard by the caller as" under the agent's turn when it differs.
Verify: unit tests on a synthetic record (clean, misheard-but-ignored, misheard-and-acted-on); the "Austin/Boston" call from `full-1` is re-scored invalid.

**T5.9 Per-session comparison (added 2026-10-08).** A session's results are comparable across runs whenever its own id and the agent config hash (plus the scoring method) match, regardless of which other sessions were in the run. The run page's "vs previous" marks and the "fixed / regressed" lists use the most recent earlier run that contains the same session with the same agent hash; the session page shows the trend (outcome strip per run, oldest to newest) under that rule. The run-level stamp keeps the set hash for the headline comparison.
Verify: unit test with three fixture runs (set changed between them) still yields fixed/regressed marks per session.

**T5.10 Cost and usage per run (added 2026-10-08).** From the agent's session usage events and the caller's usage: LLM tokens in/out, STT seconds, TTS characters per call, summed per run with an estimated cost from a small price table (`pricing.yaml`, editable). Shown in the run summary tiles and per call.
Verify: unit test sums a fixture's usage; the price table is read from the file.

**T5.6 `gf score runs/<run_id>`.** Writes `scores.json` per attempt and `summary.json` per run; idempotent; re-scoring never re-runs calls.
Verify: run twice → identical output; fixture run scores as expected.

Gate 5: the fault-injected refund session is caught as "claimed without acting" on a real record; all scoring unit tests pass.

---

## Part 6 — Reports and UI

**T6.1 Layered report model.** One `report.json` per run with four layers: L1 run summary (pass rate + interval, invalid rate, p95 latency, top 3 failure reasons, cost); L2 per-session table; L3 per-call checks with evidence; L4 timeline. Non-technical wording for every check (`what happened`, `why it matters`, `where`).
Verify: unit test builds the model from a fixture run; every check has a plain-language label.

**T6.2 Templates and static report.** Jinja2 templates: overview (a clickable diagram of the call flow: sessions → simulated caller → support agent → order system → scoring, each box a drill-down), agent, order system, simulated caller, scoring catalogue, sessions, session detail, run, call detail (stereo lanes drawn on a canvas from precomputed loudness envelopes and a native `<audio>` element, no CDN; inline tool cards; score panel; clicking a check or a turn seeks the audio). `gf report <run_id>` renders a folder of plain HTML pages (`index.html` = the run) with relative audio paths; `--bundle` transcodes the audio to MP3 into the folder so it is self-contained.
Verify: render test on the fixture run; HTML validates; opens from the file system with audio playing; manual checklist (10 items) signed off.

**T6.3 Live UI.** FastAPI app `gf ui` serving the same templates over `runs/` and `sessions/`, rendered on every request (a run started from the CLI appears on refresh). Audio is streamed from the record folders.
Verify: TestClient tests for each page route; a run folder built from fixture records renders every page.

The tasks below make the UI a production web service rather than a local viewer (added 2026-10-08).

**T6.4 Run control.** Start a run from the browser: pick sessions (or all), repeats and concurrency; the UI spawns `gf run` as a child process and records `runs/<run_id>/job.json` (pid, parameters). One run at a time. While a run is in progress the run page shows progress (calls done / planned, pending attempts as empty squares, the tail of the run log) and refreshes itself; runs started from the CLI are shown the same way. Stop sends SIGTERM to the process group. A "rescore" action re-runs scoring (after scoring code changes); the run page also rescores by itself when `summary.json` is missing or older than the manifest.
Verify: TestClient — start is refused while a job is running; a job file with a dead pid reads as failed; stop marks the job stopped; a stale summary is rescored on view.

**T6.5 System status.** The overview shows whether a run can start: keys present, order system `/health`, LiveKit reachable, agent worker process found (soft when it runs in Docker), ffmpeg available. Cached 60 s; `GET /api/status` (`?refresh=1` bypasses the cache).
Verify: route test with the backend down → `ready: false` and the row names the backend.

**T6.6 New session form.** `GET /sessions/new` shows a YAML editor prefilled from a template, or from an existing session (`?copy=<id>`). `POST` validates through the session schema (unknown tools, missing fixtures and bad values are shown as the error), refuses an existing file name, writes `sessions/<name>.yaml` and redirects to the new session. Existing sessions are never edited.
Verify: TestClient against a temporary `sessions/` dir — a valid session is created and listed; invalid YAML returns 400 with the reason; a duplicate name is refused.

**T6.7 Service hardening.** Compose `ui` service (port 8090, `runs/` and `sessions/` mounted, health check, `restart: unless-stopped`, `BACKEND_URL` pointing at the backend service); gzip; HTML error pages for 404/500 and JSON errors under `/api/`; `/health` and `/version`; JSON API mirroring every page (`/api/status`, `/api/runs`, `/api/runs/{id}`, `/api/runs/{id}/{session}/{attempt}`, `/api/sessions`, `/api/agent`) with OpenAPI at `/api/docs`; optional access token `GF_UI_TOKEN` (cookie set by a login page, `Authorization: Bearer` for the API) so the UI can be exposed behind a reverse proxy.
Verify: TestClient for every API route; with `GF_UI_TOKEN` set, pages redirect to login and the API returns 401 until the token is presented; `docker compose up ui` answers `/health`.

**T6.8 Publishing.** `gf report <run_id> --bundle --out docs/sample-report` produces the committed sample report with MP3 audio. GitHub Actions: `ci.yml` (ruff + unit tests on every push and pull request) and `pages.yml` (publishes `docs/sample-report` to GitHub Pages on push to `main`, so the latest sample report has a permanent link). README documents hosting the live UI: compose behind a reverse proxy with TLS, `GF_UI_TOKEN` set, the agent worker and backend as sibling services.
Verify: the bundled folder opens from disk with audio playing; both workflows pass on GitHub.

**T6.10 PRD gaps (added 2026-10-08).** Session page: every attempt across runs as a row (run, attempt, verdict, reason, p95, link to the call), newest first. Call page: links to the previous and next attempt of the same session in the run, and to the same session in the previous comparable run. Agent page: the tools' real argument schemas (name, type, required, description) read from the function definitions, not a hand-written summary.
Verify: route tests find the attempt rows, the prev/next links and a schema property for each tool.

**T6.9 Replay everywhere.** Every row that names a call (run page tables, "failing now", "passed but flagged", "every call", invalid calls) carries a replay button that plays the original stereo recording in place (one shared player, play/pause, elapsed time), in the live UI and in the static report.
Verify: route test finds a replay control per call row; manual: audio plays from the run page.

**Workbench redesign (added 2026-10-08, from the UX review).** The UI becomes a single evaluation workbench rather than a set of report pages: overview → session → run → event, each layer expanding in place. Visual language: off-white neutral background, very subtle borders, one restrained indigo accent, green for pass and red for fail, high density with whitespace, system sans-serif (Inter-like). No CDN; the same templates still render the static report.

**T6.11 Shell: left navigation, header, agent block.** A 200 px left navigation (Overview, Practice sessions, Runs, Scoring, Agent) with a "Support agent" block below it (name, provider, status, and links for prompt, voice, tools, model, order system, simulated caller). A header with a breadcrumb per page, a provider status pill ("LiveKit · connected" from the cached system status in the live UI; "LiveKit" in the static report), "+ New session" and "Run evaluation" (live only; opens the start-run card). Narrow screens stack the navigation on top. The bottom replay player stays.
Verify: route test — every page carries the navigation, the breadcrumb and the agent block; the static report carries the same shell without the live-only buttons.

**T6.12 Overview and run page as one layout.** A run page is: title and meta line (run id, date, sessions, calls valid/invalid, stamp link), four KPI cards (task success with k/N valid and the Wilson interval, tool correctness, speech word error rate, p95 reply latency), each with a sparkline over the last runs and the change against the previous comparable run; then the practice-session accordion beside "Issues to investigate"; then the call inspector for the selected call (default: the top issue); then the detailed tables (every call, reproducibility, detector banner, LiveKit summary). The overview is that layout for the latest run, plus a compact "how a call flows" strip (sessions → caller → agent → order system → scoring, each a link) and, in the live UI, the system status and the start-run card (collapsed, opened by "Run evaluation"). Tool correctness and the issue timestamp come from scoring (`tool_ok`, `issue_t_ms` per attempt), never from the transcript.
Verify: unit tests for the KPI series (sparkline values, delta, "not comparable" when stamps differ) and for `tool_ok`; route test — the overview and the run page show the four cards and the accordion; a run without a previous comparable run shows no delta.

**T6.13 Practice-session accordion and issues.** Each session row: chevron, title, pass/fail/invalid bar, "k/N pass", attempt squares with replay buttons, "vs previous" mark. Expanded (native `<details>`, the first failing session open by default): persona, goal, conditions, expected outcome in four columns, then one line per attempt (verdict, reason, replay, open). "Issues to investigate": prioritised cards (critical = honesty or wrong write, high = other hard failure, medium = flagged, simulation = invalid), each with the run, session, attempt, the timestamp of the evidence and a link that opens the inspector at that moment; capped at six with "view all". The Sessions page uses the same rows over the whole session set with the per-run history.
Verify: route test — a failing fixture call appears as an issue card with a timestamp; the accordion markup has one `<details>` per session; replay button per attempt.

**T6.14 Call inspector.** One partial used by the call page and embedded on the overview/run page: header (session, attempt, verdict, date, duration, valid, previous/next attempt links, "open full call" when embedded); a compact audio bar (play/pause, elapsed/total, the stereo lanes with event dots for tool calls, issues and interruptions, click to seek); tabs Transcript · Tool calls (n) · Timeline · Checks (failed/flagged counts) · Details; a "show what the agent heard" switch; transcript rows with avatars, timestamps, misheard words highlighted; a right column with the root cause (failure reason or flags), the selected tool event (arguments, response, raw; latency; status) and the check groups. Clicking a turn, a tool event, an event dot or a check seeks the audio and selects the event. Everything works without the server (static report).
Verify: route test — the call page has the tabs, the switch, the root-cause box and a tool-event card per tool call; the browser test below exercises the interactions.

**T6.15 Secondary pages in the new shell.** Session detail (cards, attempts across runs as rows, "create a variant"), new-session form, agent, order system, simulated caller, scoring catalogue, runs list, login and error pages restyled with the same tokens and components; no page keeps the old top bar.
Verify: route test renders every page; a screenshot pass in headless Chrome at desktop and phone width shows no horizontal overflow (checked by script: `document.documentElement.scrollWidth <= innerWidth`).

**T6.16 Dashboard browser test.** `tests/ui/test_dashboard.py` (Playwright, Chromium, marker `ui`, `make test-ui`) starts the app on a free port over the fixture run with synthesised audio and checks every interactive element: navigation links and breadcrumbs reach the right page; the header buttons open the start-run card and the new-session form; the KPI cards render; the accordion expands and collapses; an issue card opens the inspector at its timestamp; the tabs switch panels; the heard switch toggles; a replay button starts the shared player (audio `src` set, elapsed time ticking); clicking a transcript turn, a tool event, a lane position and a check "jump" seeks the audio; no console errors on any page; every internal link on every page answers 200. The same test runs against the static report folder opened from disk for the elements that exist there.
Verify: the test passes locally; CI installs Chromium and runs it on every push.

Gate 6: a non-technical reviewer can open the published report, read the summary in a minute, drill into one failed call and see the exact moment; a teammate can start a run from the UI and watch it finish.

---

## Part 7 — Session generation and deliverables

**T7.1 Session set.** Review the generated sessions, keep 10–12 plus 3–4 hand-written ones (including one fault-injected refund and one noisy impatient caller).
Verify: `gf sessions validate` passes on the whole set.

**T7.2 Full run and sample report.** `gf run --all --repeat 3`, `gf score`, `gf report`; copy to `docs/sample-report/` including at least one caught failure.
Verify: the committed report shows a real failure with evidence.

**T7.3 Deliverables.** README (setup, keys, `make up`, adding a session), `docs/design-note.md` (choices, next week, second provider via `providers/base.py`), `make up` from a clean clone.
Verify: fresh clone on this machine → `make up` → `make smoke` passes.

---

## Part 8 — Second engine: LiveKit's own simulator

**T8.1 Import `lk agent simulate` results.** LiveKit Cloud's simulator (`lk agent simulate text|audio --scenarios file --export out.json`) runs judged sessions against the same worker. `gf import-simulate <export.json> --run-id <id>` turns an export into a run folder: one record per job (the agent's own events and the backend log are already written under `runs/_adhoc/<room>` by the worker, keyed by the room name the simulator used), `manifest.json` with `engine: livekit-simulate`, a session per scenario label (mapped to an existing session by label when one matches). LiveKit's judge verdict, WER, entity recall and heard-latency p95 are stored per call and shown as an extra column ("LiveKit verdict") next to our checks. Checks that need the stereo recording (dead air, talk-over, barge-in) are marked "not measured by this engine".
Verify: unit test imports a committed export fixture into a temporary runs dir and the run page renders; scoring over the backend log agrees with the LiveKit verdict on the fixture.

**T8.2 Scenario export.** `gf sessions export-simulate` writes a `--scenarios` YAML (label, instructions, agent_expectations) from the session files, so the same sessions run on both engines.
Verify: the exported file is accepted by `lk agent simulate text`.

## Metrics catalogue (adopted definitions)

Learned from reviewing two existing voice-eval frameworks (definitions only; no code reused). Every threshold lives in `thresholds.yaml`.

**Latency and audio (measured from `audio.wav`, ground truth)**

| Metric | Definition | Flag |
| --- | --- | --- |
| Response latency (heard) | Caller speech end (VAD, left channel) → agent audio start (VAD, right channel), per turn; p50/p95, nearest-rank percentile | p95 > 2.0 s warn, > 3.5 s fail |
| Response latency (record) | Same turn from agent events: EOU delay + LLM TTFT + TTS TTFB. Reported next to "heard" with the gap, because self-reported latency understates what the caller hears by 0.5–0.9 s | breakdown only |
| Dead air | Both channels silent; measured from audio only, never from transcript timestamps | any gap > 3.0 s |
| Talk-over | Agent audio starts while caller VAD is active | > 2 per call |
| Barge-in stop | Caller starts during agent audio → agent audio stops | > 1.0 s |
| Caller spoke first | Caller had to speak before any agent greeting | flag |

**Tool calls and outcome (from backend log + state, never from the transcript)**

| Check | Rule |
| --- | --- |
| Required / order / forbidden / final_state | As in Part 5. A missing write is a failed task, not a wrong write |
| Wrong write | Any successful write the session did not ask for (refund on the wrong order, address change nobody requested). Counted separately from pass/fail; target 0 |
| Claimed without acting | A claim ("your refund is done", "I've updated the address") with no successful matching write anywhere earlier in the call. Checked against earlier turns because agents confirm in the turn after the write. Claims are LLM-extracted with quote-or-drop: a quote not found verbatim in the turn is discarded |
| Escalated instead of acting | Agent escalated after a tool failed, without telling the caller the action did not happen |
| Verification before disclosure | `lookup_order` takes `order_id` and `zip`; the backend rejects a mismatch, so disclosure without verification shows up as a wrong or missing call |
| Argument sanity | Backend validates formats strictly (order id pattern, amount ≤ total, address non-empty) and logs `arg_problems`, so invented arguments cannot pass silently |

**Transcript quality gates (cheap, deterministic)**

| Gate | Rule |
| --- | --- |
| Stutter | Immediate repeat of a 1–4 word phrase, `\b(\w+(?:\s+\w+){0,3})\s+\1\b` |
| Duplicate sentence | Adjacent identical sentences (≥ 5 words) after normalisation |
| Tool-name leak | Agent speaks a snake_case name of a tool it called |
| Truncated utterance | Agent turn ≥ 20 chars with no terminal punctuation (final turn exempt if the caller hung up) |
| Filler-only turns | Two consecutive agent turns that are filler (≤ 8 words, filler phrase) |
| Repeated question | Agent re-asks a question with similarity ≥ 0.85, unless the caller asked for a repeat or > 15 s passed |

**Statistics**

- Wilson 95% interval, z = 1.96: centre = (p + z²/2n)/(1 + z²/n), half-width = z·√(p(1−p)/n + z²/4n²)/(1 + z²/n), clipped to [0, 1].
- pass@1 = mean over attempts; pass^k = passed every attempt; pass@k = passed at least one. Default k = 3.
- Flaky = mixed outcomes over the session's attempts in a run; unstable = mixed outcomes over the last 5 runs.
- Comparability stamp per run: `config_hash` (agent config) + `sessions_hash` + `method_version` (hash of the scoring package + judge prompt version). Runs with different stamps are shown but not compared.
- Judge infrastructure errors are `JudgeUnavailable`, reported separately, never counted against the agent.

**Judge rules (v1)**

- Only two judged items: claim extraction and caller character consistency. Temperature 0, JSON output, versioned prompt, quote-or-drop.
- A judge never decides pass/fail alone. Gating is allowed only after κ ≥ 0.75 against a hand-labelled set (target 50 items; v1 ships 30).

**Report conventions**

- Header: run id, stamp, generated time, link to previous comparable run.
- Results table with "vs previous" marks (= same, ✓ fixed, ⚠ regressed, new). Regressions in LLM-driven sessions are tagged "unconfirmed, rerun to confirm".
- Sections in order: summary → per-session table → regressions and fixes → failing now (first line of reason) → flaky sessions with an outcome strip (✓✗– oldest→newest) → invalid calls with reasons → "passed but flagged" (outcome right, words or latency not) → one clean pass as a reference → every call.
- Rate pills: green ≥ 0.95, amber ≥ 0.66, red below. Lists capped at 40 rows.

**Pitfalls to design around**

- Tool calls must be captured from the agent's tool-execution event, not from conversation items.
- TTS metrics arrive when synthesis ends; audio start = metric time − duration + TTFB.
- Async events after the end event: the runner waits for a grace period before snapshotting.
- A transfer/escalation can mask a failed write: the escalation check above exists for this.
- Simulated-caller faults (LLM timeouts, broken character) must be invalid, not agent failures.

## Environment setup (first step after approval)

- `brew install node ffmpeg livekit-cli` (Node for optional front-end tooling, ffmpeg for audio resampling/mixing, `lk` CLI for rooms and dispatch checks)
- `uv python install 3.12`
- Add `~/.docker/bin` to PATH in `~/.zprofile` so `docker` resolves in new shells
- Delete the research clones in the scratchpad (left in place because plan mode blocked `rm`)
- `.env` with the dedicated LiveKit project keys, Deepgram and OpenAI keys (user supplies)

## PRD amendments to apply on approval

1. Key decisions table: Deepgram STT+TTS, OpenAI only, Python-only UI, repeats = 3, immutable sessions.
2. Practice sessions: remove "edit" wording; add id-by-hash and immutability rule; the UI form creates new sessions only.
3. Simulated caller: accent variety limited to Deepgram Aura voice catalogue; noise/pace/interruptions carry most variation.
4. UI and reports: add the four-layer report structure and plain-language check labels.
5. Stack: replace React/Vite with FastAPI + Jinja2 + htmx; remove Node.
6. Open questions: mark the five as resolved.
7. Add the metrics catalogue summary and a link to `docs/spec.md`.

## Verification of the whole plan

After approval: write `docs/spec.md` from this file, update the PRD doc, commit. Then build Part 0 → Part 2 and stop at Gate 2 for review before the harness.

## Risk review responses (2026-10-08)

| # | Risk | Decision |
| --- | --- | --- |
| 1 | Mutual-silence watchdog at 10 s inflates run time and cost | Mutual silence is a broken interaction after 3 s. Rule: at 4 s of mutual silence the caller re-prompts once ("Hello?"); at 8 s cumulative the call ends and is marked invalid (reason `mutual_silence`). Both values live in `thresholds.yaml` (`mutual_silence_reprompt_s`, `mutual_silence_abort_s`). The agent's own 20 s caller-silence hang-up is a separate, agent-side behaviour under test. |
| 2 | Clock skew; are audio timestamps stream-relative? | Timestamps are stream-relative, never file-length based. Each channel is placed by sample count from one shared origin T0 (the recorder's first frame); frames that arrive late open an explicit silence gap of the right length, and packet loss shows up as concealment frames from the SDK's jitter buffer, so the timeline keeps its length. Event timestamps (tool calls, transcripts) are mapped onto the same T0 via the clock-sync marker. `timeline.json` stores both `t_stream_ms` and the raw event time so any drift is inspectable. |
| 3 | Forced alignment adds processing time | Alignment never runs in the call loop. The runner writes raw records only; `gf score` runs post-call and asynchronously (per-attempt tasks, bounded concurrency), so a slow alignment delays a score, not the next call. Cost is small: Deepgram pre-recorded STT on the caller's clean TTS audio takes about 1 s per minute of audio. When the reference text already comes with turn timestamps (LiveKit simulation export + agent-side STT finals), alignment is skipped. |
| 4 | Is 30 labelled examples enough? | No. 30 is the smoke-calibration set that proves the pipeline; judges stay advisory (never gate) until κ ≥ 0.75 on ≥ 100 labelled items, measured per category. The set grows by active labelling: every judge/human disagreement and every flagged production-style call is added. Business logic stays in code (claims matched against the backend log), so judge variance only affects extraction. |
| 5 | Run cost in CI | `make smoke` (`--repeat 1`, 3 sessions, text mode where possible) is the only target CI runs on pull requests; the full matrix (`--repeat 3`, all sessions, audio) runs nightly and on merges to `main`. Every run has a hard budget (`GF_MAX_COST_USD`, default 10) and a concurrency cap; the runner stops scheduling new calls when the projected cost exceeds the budget and marks the run partial. |
| 6 | Threshold for a significant regression | Automatic flag only when (a) the Wilson 95% intervals of the current and baseline run pass rates do not overlap, or (b) a zero-tolerance category appears (claimed-without-acting, wrong write, invalid-call rate > 20%). A drop inside overlapping intervals (for example 95% → 91% with N = 36) is shown as "within noise" and triggers an automatic single re-run of the affected sessions; it is flagged only if the re-run confirms it. Per session, pass^k → any fail is listed as "regressed, unconfirmed" until re-run. |

## Simulated caller parameterization (reproducibility)

A session file is the only input; nothing about the caller is implicit.

```yaml
caller:
  persona: {name, style, accent}
  facts: {order_id, zip, amount, address}   # the only facts the caller may use
  goal: "..."
  stop_when: "the agent confirms the refund was issued"
  voice: aura-2-luna-en                      # TTS voice id
  pace: 1.0                                  # TTS speed multiplier
  llm: {model: gpt-4.1-mini, temperature: 0, seed: 101}
  conditions: {noise: cafe@15dB, phone_line: true, packet_loss: 0.02, low_quality_mic: false,
               interruptions: 0.2, patience_s: 20}
  limits: {max_turns: 12, max_duration_s: 180, mutual_silence_reprompt_s: 4, mutual_silence_abort_s: 8}
engine: livekit-simulate | gf-caller          # which simulator ran it
```

The run manifest records: session id (content hash), agent `config_hash`, engine and its version, every caller parameter above, the LiveKit run/job ids, model ids and the scoring `method_version`. Two attempts are comparable only when all of these match. Where the engine cannot honour a parameter (for example LiveKit simulation exposes noise/mic/packet-loss as switches, not levels, and no LLM seed), the manifest records the parameter as `unsupported_by_engine` rather than silently dropping it.
