# Task tracker

Status of every task in [spec.md](spec.md). A task is marked done only after its verification step has passed. Gates require a human review before the next part starts.

Legend: `[ ]` not started · `[~]` in progress · `[x]` done and verified · `[!]` blocked

## End result (the deliverables)
- [x] D1 A repository that runs with one command (`make up`: order system + agent worker + UI), README covers setup, CLI, adding a session, hosting — T7.3; clean clone: `uv sync` → 58 unit tests pass, `gf --help`, `docker compose config` lists backend/agent/ui, image builds
- [x] D2 Sample report from real calls — `docs/sample-report/` (run `full-3`, 15 sessions × 3, 36/44 valid passed, 1 invalid, 2 flaky; caught: a refund never issued on an impatient caller, an over-limit refund never issued, plus two generated sessions whose expectations contradicted the agent's policy), MP3 audio bundled
- [x] D3 `docs/design-note.md`: choices, findings from the first runs, next week, second provider

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
- [x] T3.4 Audio conditions — noise at exact SNR (measured 15.0 dB for `cafe@15dB`), phone-line band-limit, seeded packet loss, low-quality mic; unit tests for SNR ±1 dB, band-limit, loss extremes. Interruptions (added 2026-10-08 evening): seeded `InterruptPlanner` barges in on agent turns with an uninterruptible one-sentence interjection; verified on a real call (`refund-interrupting-caller`: 9 planned / 9 made, 8 barge-ins detected from the audio, agent stopped within 200–500 ms every time, refund still issued)
- [~] T3.5 Validity — mutual-silence re-prompt/abort (4 s / 8 s), max duration, max turns, runner errors are recorded as `ended_by`; LLM-error and broke-character detection still to add in scoring
- [x] Gate 3 — refund-basic, refund-noisy-cafe and refund-backend-fault all completed with goal met and correct tool calls
- Design change vs. the spec: the caller is not a second worker; it runs in-process in the runner (pattern from the reference voice suite), which removes a dispatch and gives direct access to the caller's events. LiveKit's own `lk agent simulate` is available as a second engine (text/audio, judged, WER + entity metrics) and will be importable into reports

## Part 4 — Sessions, runner, call record
- [x] T4.1 Session schema — `gf/sessions/schema.py`: id = hash of the parsed session; tampered files refused; unknown tools and missing fixtures rejected; 4 unit tests
- [x] T4.2 Runner — `gf run --all --repeat N --concurrency K`: one room per call, per-call backend seeding, record folder per attempt, `manifest.json` with config/session hashes. Verified: 6 calls in 175 s at concurrency 3, per-call backend logs isolated, all agent records complete. Provider interface deferred (single engine today)
- [x] T4.3 Timeline builder — `gf/record/timeline.py`: energy VAD per channel on the stereo WAV, response latency, dead air, talk-over, barge-in stop, tool calls and transcripts merged; synthetic-audio tests exact to ±50 ms
- [x] T4.4 Call record schema — `gf/record/model.py` `CallRecord.load(folder)`; four real records committed under `fixtures/records/` (audio stripped)
- [ ] T4.6 Provider seam — `providers/base.py` protocol, LiveKit implementation, `FakeProvider` runner test, documented skeletons for Vapi/Retell-style telephony providers and Pipecat
- [x] T4.5 Session generation — `gf sessions generate --count N --focus ...` and Sessions → Generate in the UI; proposals validated through the schema, written to `sessions/generated/`; unit test for rejection paths; 12 hand-written sessions
- [~] Gate 4 — runner + record + timeline verified on real calls; generation pending
- Persona: deterministic in-character check (facts-only numbers, no meta-talk) counts toward validity; an advisory LLM persona judge exists (`GF_PERSONA_JUDGE=1`), never gating

- [x] T6.20 Environment tags — `gf/environment.py` (`env-`/`set-` tags, `environment.json`, registry under `runs/_environments/`, backfill), stamped by the runner and the LiveKit import, chips on the overview, run page, runs table and selector, Environments page (+ `/api/environments`), `gf env [--backfill]` (runs made before manifests recorded versions are backfilled from the lockfile at the last commit before they started, with the evidence stored under `environment.backfilled`), like-for-like by tags; unit + route + browser tests. Was: `env-<hash>` (agent config + variant, models, engine, scoring method, library and Python versions) and `set-<hash>` (session files) on every run; stored in the manifest, `environment.json` and a registry under `runs/_environments/`; chips everywhere a run is named; Environments page; `gf env`

- `full-4` (2026-10-09, same 15 files as `full-3`): tags identical (`env-cc3bc997`, `set-4d233ef8`), so the comparison is like-for-like: 37/43 valid passed (86%, CI 73–93%) vs 82%, no regressions, the two flaky sessions of `full-3` passed 3/3 (marked fixed, unconfirmed until a third run), 2 invalid simulations (Boston/Beacon mishearing again; the caller spoke a zip not in its facts). Published as the sample report.

- Follow-up (2026-10-09): the two retired generated sessions are replaced by corrected files rather than dropped: `refund-backend-error-retry` (the agent's policy already said "escalate after an action has failed twice", so a retry after one injected 500 is the right expectation; the agent's instructions now make the single retry explicit, agent config hash 70ed03f335ef, env-7e21788d) and `refund-already-refunded-declined` (asserts `refunds[GW-48911] not exists` and `orders[GW-48911].refunded == true`). Retired sessions carry a reason (`sessions/retired/REASONS.yaml`) shown on run rows, issue cards and the session page. The backend now logs the fault tag on failing injected faults and the required-call message says "an injected fault from this session". The start-run form moved from the overview to the Runs page. Verified on real calls (`fix-1`, env-7e21788d): 9/9 passed; every backend-error call shows lookup → issue_refund 500 (injected) → issue_refund 200, the already-refunded calls do lookup only. Both corrected sessions are in the regression suite.

## Bugs (filed 2026-10-08 evening, fixed the same night)
- Finding from `full-3` (15 × 3, 36/44 valid passed, 82%, CI 68–90%, 1 invalid, 16.7 min): two generated sessions failed 0/3 because their expectations contradicted the agent's policy (retry after an injected 500 with escalation forbidden) or the fixture (a final-state assertion on a pre-existing refund, which the backend records as `orders[..].refunded`). Both were moved to `sessions/retired/` (sessions are never deleted: retired ones still load for earlier runs and rescoring but are not offered or run) and the generator now rejects such proposals (`_check_against_agent_policy`, `_check_final_state`); the hand-written set covers both behaviours (`refund-backend-fault`, `refund-already-refunded`)
- [x] B1 "Latest run" is chosen by folder name, not by start time, so `lk-audio-1` sorts above `full-3`; the overview, the run selector and "Issues to investigate" open on the wrong run. Fix: order runs by `started_at` from the manifest (fallback: folder mtime); the overview, selector and Runs page lead with the newest run.
- [x] B2 KPI cards say "no comparable run" whenever the stamp differs, so the delta is almost never shown. Fix: always compare with the previous real run (kind `run`) and show the delta against it; when the stamp differs, keep the delta and add a muted "not like-for-like: sessions or agent changed" note instead of hiding it. Sparkline unchanged.
- [x] B3 Practice-session accordion opens the first failing row by default (looks like it "always expands the 2nd session"). Fix: every row starts closed; the issues cards are the entry point.
- [x] B4 The run log shows one "room session transport is closed" traceback per finished call (the library's session-event writer runs after the caller's session is closed). Fix: drain and close the caller's AgentSession before leaving the room; if the library still emits it, filter that message from the `livekit.agents` logger in the runner. Logs then only show real errors.

## Next up (in order)
1. T5.9 per-session comparison → T6.10 remaining item (real tool schemas on the agent page) → T4.6 provider seam (LiveKit + Fake + Vapi/Pipecat skeletons) → T5.10 cost
2. Re-run the full matrix (`full-3`: 12 hand-written + regenerated sessions) and refresh `docs/sample-report`; prove `make up` in Docker end to end
3. Gate 6 human review of the UI and the sample report (CI green on GitHub; Pages enabled)
- T4.5 follow-up (2026-10-08): generated sessions are checked against the fixture (order exists, zip matches, refunds only on delivered and not-yet-refunded orders within the total, address changes only on processing orders and only with a `new_address` fact) and free-text arguments are never pinned; the first generated batch failed 0/8 in `full-2` for exactly these reasons, so that run was discarded and the sessions regenerated (3 written, 3 rejected with reasons)
- `make smoke` now runs `tests/e2e/test_smoke.py`: one real call, record files, timeline and a valid score
3. If time: interruptions parameter; import `lk agent simulate export` as a second engine column; T4.5 generation
- Reproducibility (2026-10-08): the scoring stamp now includes a hash of `thresholds.yaml`; every manifest records package versions, Python and the git commit (`environment`), shown on the run page

## Part 5 — Scoring
- [x] T5.1 Tool and outcome checks — required args, order, forbidden, final-state assertions, wrong writes, arg problems (`gf/scoring/tools.py`)
- [x] T5.2 Claimed-without-acting — pattern-based claim extraction with negation handling, matched against successful backend writes; plus "honest about failure" after a failed write (`claims.py`). LLM extractor + labelled set still to do
- [x] T5.3 Speech accuracy — WER (jiwer, digits spelled out), entity intact check per fact, misheard-value → tool-argument causal link (`speech.py`)
- [x] T5.4 UX metrics — latency p95, dead air, talk-over, barge-in stop, repeats (incl. "are you still there?"), time to resolution, would-hang-up, greeting first; quality gates (stutter, tool-name leak, truncated, repeated question, filler-only); validity (`ux.py`)
- [x] T5.5 Statistics — Wilson 95%, pass^k / pass@k, flaky, interval overlap (`stats.py`), unit-tested against known values
- [x] T5.6 `gf score <run_id>` — scores.json per call, summary.json per run with the comparability stamp; batch-test-1 scored 6/6 (CI 61–100%)
- [x] T5.8 Simulator hearing check — `gf/scoring/hearing.py`: agent turn ↔ caller transcript pairing, digit entities (tens-aware) and word substitutions, invalid when the caller acted on the wrong value; the `full-1` Austin/Boston call is now invalid for that reason (boston→austin, beacon→eakin); 5 unit tests
- [ ] T5.11 Caller (persona) score separate from the agent score: in character, heard the agent, goal stated early, legitimate ending, persona judge on by default (advisory); both pills on the call header; simulation quality on the run page
- [ ] T6.22 Annotated timeline: every transcript/timeline event tagged with the check that judged it and the result, with ? popovers; 'judged by' column in the Timeline tab
- [x] T6.21 Grading walkthrough per call: numbered steps (simulation sound → did what the session asks → honest → good to be on → verdict rule) as a Grading tab in the inspector; session page shows what passing looks like
- [ ] T5.9a Re-run button on a run page and on a session row: starts a new run with the same session selection, records `parent_run` in the manifest, and the new run compares against its parent (finished runs are never appended to, so their statistics stay as reported)
- [ ] T5.9 Per-session comparison across runs (same session id + agent hash), trend on the session page
- [ ] T5.10 Cost and usage per run (`pricing.yaml`)
- [x] T5.7b Detector applicability (2026-10-08 late): sessions that never ask for a tool the variant removes (escalation-only) are "not applicable", excluded from the verdict and from the Prove button's run; the first full-set check read MISSED only because of two such sessions. Progress banner shows when the last call finished
- [x] T5.7 Detector self-test — `dishonest` agent variant (write tools removed, hash `+dishonest`), `gf run --variant` / `gf check-detector`, "Prove the honesty check" on the Scoring and run pages, verdict caught / missed / inconclusive. Proven on real calls (`check-dishonest-2`): 2/2 caught ("refund of $89.99 has been issued" with no issue_refund; "address … has been updated" with no update). Claim patterns broadened and unit-tested
- [x] Gate 5 — scoring tests pass on real records; "claimed without acting" proven on real calls by T5.7

## Part 6 — Reports and UI
- [x] T6.1 Layered report model — `gf/report/model.py`: run (L1/L2), call (L3/L4), overview, agent, order system, caller, scoring catalogue; plain-language labels come from the checks
- [x] T6.2 Templates and static report — 11 templates, clickable flow diagram on the overview, canvas lanes + native audio on the call page; `gf report full-1` renders 40 pages (verified in headless Chrome)
- [x] T6.3 Live UI — `gf ui` (FastAPI) serves the same templates live over runs/ and sessions/
- [x] T6.4 Run control — `gf/ui/jobs.py`: start/stop/rescore, `job.json` per run, progress banner + pending squares + log tail, auto-refresh; verified with a run started from the UI (passed, scored itself); one-at-a-time enforced (tested)
- [x] T6.5 System status panel (keys, order system, LiveKit, agent worker, ffmpeg; cached 60 s) + `/api/status`
- [x] T6.6 New session form — validated YAML editor, template or copy of an existing session, duplicate names refused; tested
- [x] T6.7 Service hardening — compose `ui` service (health check, restart), gzip, HTML/JSON error pages, `/health`, `/version`, JSON API + `/api/docs`, `GF_UI_TOKEN` login; 9 route tests; image builds
- [x] T6.8 Publishing — `gf report --bundle`, `.github/workflows/ci.yml` and `pages.yml`, hosting section in README (workflows still to be seen green on GitHub)
- [~] T6.10 PRD gaps — attempts table on the session page and prev/next attempt links in the inspector done with the redesign; real tool schemas on the agent page still to do
- [x] T6.9 Replay button on every call row (run tables, shared bottom player), live and static
- [x] T6.11 Shell — `base.html`: 200 px left navigation with the support-agent block (prompt, voice, tools, model, order system, caller links), header with per-page breadcrumb, provider pill fed by the cached status (warmed at startup), New session / Run evaluation (live only); stacks at phone width. Route tests render every page; the static report has the same shell without live controls
- [x] T6.12 Overview and run page as one layout — `_run_body.html` macros: four KPI cards (task success with interval, tool correctness, speech WER, p95 latency) with sparklines over the last 8 runs and the delta vs the previous comparable run; `tool_ok` and `issue_t_ms` added to every attempt score (existing runs rescored); compact flow strip and collapsed start-run card on the overview. 5 unit tests in `test_report_model.py`
- [x] T6.13 Practice-session accordion (native `<details>`, pass/fail/invalid bar, attempt squares, per-attempt replay + Open call, first failing row open) and prioritised "Issues to investigate" cards (critical / high / flagged / invalid sim, evidence timestamp, link opens the inspector at that moment); Sessions page uses the same rows with per-run history
- [x] T6.14 Call inspector — `_inspector.html`: compact audio bar (play/pause, elapsed, stereo lanes with tool / issue / interruption dots, click to seek), tabs Transcript · Tool calls · Timeline · Checks · Details, "show what the agent heard" switch, misheard words highlighted, right column with root cause, selected tool event (arguments / response / raw) and check groups; previous/next attempt links (T6.10 item); `#t=<ms>` opens at a moment; embedded on overview and run pages, full on the call page, works from disk
- [x] T6.15 Secondary pages in the new shell with breadcrumbs and anchors; session page lists every attempt across runs with replay (T6.10 item); no horizontal overflow at 1400 and 400 px (browser test)
- [x] T6.19 Compact checks (tags + ? popovers; route + browser tests). Also: the run page shows a "starting" state while the runner writes its manifest (was a 404 right after pressing Prove the honesty check / Start run) and start waits up to 5 s for it. Was: the inspector's side column shows each check as a tag (its code id, status mark, measured value) with a ? popover carrying the full label, what happened, why it matters and the jump link; the Checks tab keeps the long form
- [x] T6.18b Inspector without a recording (LiveKit-simulator imports): lanes drawn from turn timestamps, single disabled play icon, heard line hidden since the engine does not export the caller's words; browser test paints the canvas and checks it
- [x] T6.18 Run selector on the overview (`?run=`, dropdown newest first, detector checks grouped) and WER falling back to the engine's own measurement (LiveKit judge WER, labelled) — route, unit and browser tests
- [x] T6.17 Per-turn latency breakdown — `gf/record/latency.py` pairs the agent's EOU / LLM / TTS metric events to each reply; Latency tab (stacked bar per turn, heard marker, p50/p95, glossary), `latency_breakdown` in scores, "where the time goes" line on the run page (full-1: end-of-turn 577 ms · transcription 220 ms · LLM TTFT 557 ms · TTS TTFB 174 ms · playback 17 ms · heard 2.34 s · unaccounted 695 ms); 2 unit + route + browser checks. Was: heard vs agent-reported stages (end-of-turn, transcription, LLM TTFT, TTS TTFB, playback, unaccounted) per agent turn; Latency tab in the inspector with stacked bars, p50/p95 and glossary; `latency_breakdown` in scores; "where the time goes" on the run page; unit + route + browser tests
- [x] T6.16 Dashboard browser test — `tests/ui/test_dashboard.py` (Playwright + Chromium, marker `ui`, `make test-ui`, CI job `dashboard`): navigation, breadcrumbs, agent-block anchors, header buttons, KPI cards, accordion toggle, issue card → inspector at its timestamp, tabs, heard switch, tool-event selection and sub-tabs, seeking from a turn / a check / the lanes, play button, replay buttons on the shared player, run-page actions, `#t=` deep link, no console errors, no dead internal links, no overflow at two widths, and the static report from disk. 10 tests pass in ~40 s
- [~] Gate 6 — pages verified in headless Chrome; human review pending
- Scoring fixes found while building the report (2026-10-08): digit groups in the in-character check are read per numeric phrase (was merging "$89.99 … GW-48213" into a fake 5-digit number → 4 false invalids); "already refunded" is prior state, not a claim (2 false fails); the "key fact misheard" flag only checks numeric facts the caller spoke as digits (was firing on every call). `full-1` after the fixes: 20/22, 0 invalid, 2 real failures

## Part 8 — Second engine: LiveKit's own simulator
- [x] T8.1 `gf import-simulate` — export JSON → run folder (records from the worker's own events + backend log, timeline from message timestamps), LiveKit verdict + metrics on the run and call pages; engine-aware scoring (recording-based checks "not measured"); fixture `fixtures/livekit/export-audio.json` + 2 tests
- [x] T8.2 `gf sessions export-simulate` — sessions → `--scenarios` YAML (label = session title so imports map back); tested

## Part 7 — Deliverables
- [x] T7.4 Suites and areas — `gf/sessions/taxonomy.py` (areas from the caller's goal, refusal, faults, line, interruptions, impatience; `sessions/suites.yaml` with smoke + regression), `gf run --suite`, `gf sessions suites`, suite picker in the start form, by-area pills on overview and run pages, area/suite chips and filters on the Sessions page, add/remove from a suite on the session page; manifest records the suite; 2 unit + route + browser tests. Was: — derived areas (refund / address change / escalation / denial + fault handling, hard line, interruptions, impatient), `sessions/suites.yaml` (smoke, regression, …), `gf run --suite`, suite picker in the start form, by-area table on runs, suite/area chips and filters on the Sessions page, add-to-suite on the session page; unit + route + browser tests
- [x] T7.1 Session set — 11 hand-written sessions (generation deferred, see T4.5)
- [x] T7.2 Full run and sample report — `full-1` bundled into `docs/sample-report/`
- [x] T7.3 README and design note written; clean-clone check done (install, tests, CLI, compose config, image build). `make smoke` has no e2e test yet: a smoke call is `gf call sessions/refund-basic.yaml` with the services up
