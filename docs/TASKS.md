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

## Part 3 — Simulated caller
- [ ] T3.1 Caller brain
- [ ] T3.2 Scripted caller (audio plumbing)
- [ ] T3.3 Caller worker
- [ ] T3.4 Audio conditions
- [ ] T3.5 Validity detection
- [ ] Gate 3

## Part 4 — Sessions, runner, call record
- [ ] T4.1 Session schema, immutability, `gf sessions validate`
- [ ] T4.2 Provider interface and runner
- [ ] T4.3 Timeline builder
- [ ] T4.4 Call record schema
- [ ] T4.5 Session generation
- [ ] Gate 4

## Part 5 — Scoring
- [ ] T5.1 Tool and outcome checks
- [ ] T5.2 Claimed-without-acting
- [ ] T5.3 Speech accuracy (WER, entities, causal link)
- [ ] T5.4 UX metrics
- [ ] T5.5 Statistics and aggregation
- [ ] T5.6 `gf score`
- [ ] Gate 5

## Part 6 — Reports and UI
- [ ] T6.1 Layered report model
- [ ] T6.2 Templates and static report
- [ ] T6.3 Live UI
- [ ] Gate 6

## Part 7 — Deliverables
- [ ] T7.1 Session set
- [ ] T7.2 Full run and sample report
- [ ] T7.3 README, design note, clean-clone check
