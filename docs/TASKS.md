# Task tracker

Status of every task in [spec.md](spec.md). A task is marked done only after its verification step has passed. Gates require a human review before the next part starts.

Legend: `[ ]` not started · `[~]` in progress · `[x]` done and verified · `[!]` blocked

## Part 0 — Scaffold and environment
- [x] T0.1 Project skeleton (uv, ruff, pytest, `gf` CLI, Makefile, compose, thresholds.yaml) — `make test` 3 passed, `ruff` clean, `gf --help` lists commands
- [x] T0.2 `gf doctor` environment check — all 11 checks green with keys in `.env` (LiveKit, Deepgram, OpenAI, Docker 29.8, ffmpeg, lk)
- [x] Gate 0 — passed 2026-10-08

## Part 1 — Mock backend
- [x] T1.1 Data model and per-call seeding — `fixtures/orders_basic.yaml`; per-call state isolated; auto-seed for unknown call ids
- [x] T1.2 Tool endpoints and business rules — zip-verified lookup, refund rules, address lock after shipping, escalation ends call, strict argument formats
- [x] T1.3 Request log and state endpoints — `/calls/{id}/log`, `/state` (before/after), `/seed`, `DELETE`
- [x] T1.4 Fault injection — latency_ms, error_500, timeout, reject on the nth call; tagged in the log
- [x] Gate 1 — 18 unit tests pass; `docker compose up backend` + curl lookup/refund both 200 and logged

## Part 2 — Support agent (real LiveKit voice agent)
- [x] T2.0 LiveKit Cloud project setup — worker registers as `gf-support-agent` (explicit dispatch); `CreateAgentDispatch` from the probe is picked up
- [x] T2.1 Agent config and `config_hash` — `gf/agent/config.yaml`, `gf config`, 5 unit tests
- [x] T2.2 Agent worker with voice behaviours — verified over real audio with `gf probe` (scripted caller, no human): greeting first, spoken digits → `lookup_order(GW-48213, 94110)`, read-back + yes → `issue_refund(89.99)`, honest confirmation. Console check optional
- [x] T2.3 Agent event capture — `agent_events.jsonl` (messages, final transcripts, tool calls, states) and `agent_session_report.json` written per call; data-channel relay fixed (publish_data awaited)
- [x] T2.4 Conversation test cases (text mode) — 6 behaviours in `tests/live/test_agent_text.py`; 6/6 on most runs, `test_unknown_order_asks_to_repeat_digits` intermittently fails (agent asks a question before looking up) — a real agent flaw, kept as a finding for the report
- Findings from the first voice probe: greeting latency 6.3 s (includes cold process spawn in dev mode), reply latency 3.6–4.7 s from end of caller speech to first agent audio
- [ ] Gate 2 — POC review

## Part 3 — Simulated caller (engine `gf-caller`)
- [x] T3.1 Caller brain — `gf/caller/simulator.py`: persona prompt from the session file, OpenAI at temperature 0 + seed, `end_call(summary, goal_met, giving_up)` tool; prompt unit-tested
- [x] T3.2 Scripted caller — `gf probe` (no LLM) proved audio plumbing on 2026-10-08
- [x] T3.3 Caller session — runs inside the runner process as a LiveKit `AgentSession` (STT + LLM + TTS), hidden recorder captures both tracks; `gf call` produced full records (audio.wav, caller.json, caller_events.jsonl, agent_events.jsonl, backend log/state, meta.json)
- [x] T3.4 Audio conditions — noise at exact SNR (measured 15.0 dB for `cafe@15dB`), phone-line band-limit, seeded packet loss, low-quality mic; unit tests for SNR ±1 dB, band-limit, loss extremes. `interruptions` is recorded as unsupported for now
- [~] T3.5 Validity — mutual-silence re-prompt/abort (4 s / 8 s), max duration, max turns, runner errors are recorded as `ended_by`; LLM-error and broke-character detection still to add in scoring
- [x] Gate 3 — refund-basic, refund-noisy-cafe and refund-backend-fault all completed with goal met and correct tool calls
- Design change vs. the spec: the caller is not a second worker; it runs in-process in the runner (pattern from the reference voice suite), which removes a dispatch and gives direct access to the caller's events. LiveKit's own `lk agent simulate` is available as a second engine (text/audio, judged, WER + entity metrics) and will be importable into reports

## Part 4 — Sessions, runner, call record
- [x] T4.1 Session schema — `gf/sessions/schema.py`: id = hash of the parsed session; tampered files refused; unknown tools and missing fixtures rejected; 4 unit tests
- [x] T4.2 Runner — `gf run --all --repeat N --concurrency K`: one room per call, per-call backend seeding, record folder per attempt, `manifest.json` with config/session hashes. Verified: 6 calls in 175 s at concurrency 3, per-call backend logs isolated, all agent records complete. Provider interface deferred (single engine today)
- [x] T4.3 Timeline builder — `gf/record/timeline.py`: energy VAD per channel on the stereo WAV, response latency, dead air, talk-over, barge-in stop, tool calls and transcripts merged; synthetic-audio tests exact to ±50 ms
- [x] T4.4 Call record schema — `gf/record/model.py` `CallRecord.load(folder)`; four real records committed under `fixtures/records/` (audio stripped)
- [ ] T4.5 Session generation
- [~] Gate 4 — runner + record + timeline verified on real calls; generation pending

## Part 5 — Scoring
- [x] T5.1 Tool and outcome checks — required args, order, forbidden, final-state assertions, wrong writes, arg problems (`gf/scoring/tools.py`)
- [x] T5.2 Claimed-without-acting — pattern-based claim extraction with negation handling, matched against successful backend writes; plus "honest about failure" after a failed write (`claims.py`). LLM extractor + labelled set still to do
- [x] T5.3 Speech accuracy — WER (jiwer, digits spelled out), entity intact check per fact, misheard-value → tool-argument causal link (`speech.py`)
- [x] T5.4 UX metrics — latency p95, dead air, talk-over, barge-in stop, repeats (incl. "are you still there?"), time to resolution, would-hang-up, greeting first; quality gates (stutter, tool-name leak, truncated, repeated question, filler-only); validity (`ux.py`)
- [x] T5.5 Statistics — Wilson 95%, pass^k / pass@k, flaky, interval overlap (`stats.py`), unit-tested against known values
- [x] T5.6 `gf score <run_id>` — scores.json per call, summary.json per run with the comparability stamp; batch-test-1 scored 6/6 (CI 61–100%)
- [~] Gate 5 — all scoring tests pass on real records; the "claimed without acting" catch is proven on a synthetic liar record, still to be caught on a real call (a fault session where the agent lies has not occurred yet)

## Part 6 — Reports and UI
- [ ] T6.1 Layered report model
- [ ] T6.2 Templates and static report
- [ ] T6.3 Live UI
- [ ] Gate 6

## Part 7 — Deliverables
- [ ] T7.1 Session set
- [ ] T7.2 Full run and sample report
- [ ] T7.3 README, design note, clean-clone check
