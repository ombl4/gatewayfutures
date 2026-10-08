# Task tracker

Status of every task in [spec.md](spec.md). A task is marked done only after its verification step has passed. Gates require a human review before the next part starts.

Legend: `[ ]` not started · `[~]` in progress · `[x]` done and verified · `[!]` blocked

## Part 0 — Scaffold and environment
- [x] T0.1 Project skeleton (uv, ruff, pytest, `gf` CLI, Makefile, compose, thresholds.yaml) — `make test` 3 passed, `ruff` clean, `gf --help` lists commands
- [~] T0.2 `gf doctor` environment check — implemented; waiting on API keys in `.env` to verify all green
- [ ] Gate 0 — blocked on T0.2 keys

## Part 1 — Mock backend
- [ ] T1.1 Data model and per-call seeding
- [ ] T1.2 Tool endpoints and business rules
- [ ] T1.3 Request log and state endpoints
- [ ] T1.4 Fault injection
- [ ] Gate 1

## Part 2 — Support agent (real LiveKit voice agent)
- [ ] T2.0 LiveKit Cloud project setup (explicit dispatch, `lk` CLI)
- [ ] T2.1 Agent config and `config_hash`
- [ ] T2.2 Agent worker with voice behaviours
- [ ] T2.3 Agent event capture
- [ ] T2.4 Conversation test cases (text mode)
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
