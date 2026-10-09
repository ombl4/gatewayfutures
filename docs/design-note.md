# Design note

What was chosen and why, what the first real runs showed, what the next week would hold, and how a second voice provider plugs in.

## Design choices

**The simulated caller is a second voice agent in the same room, not a script.** It runs the same kind of pipeline as the agent under test (Deepgram STT → OpenAI → Deepgram TTS) inside the runner process, so every call goes through real audio, real turn-taking and real speech recognition on both sides. A scripted caller (`gf probe`) still exists for plumbing checks. Running the caller in-process rather than as a second LiveKit worker removed one dispatch and gave the runner direct access to the caller's state (what it said, what it heard, when it decided to hang up), which is what the validity rules need.

**Audio is the ground truth for timing; the backend is the ground truth for actions.** Reply latency, dead air, talk-over and barge-in are measured on a stereo recording made by a hidden participant (left = caller as sent, right = agent as heard), never from event timestamps. The agent's own reported latency is shown next to it because it understates what the caller hears by 0.5–1 s. Whether a refund happened is read from the mock order system's request log and final state, never from the transcript, so an agent that says "done" without doing it is caught by construction.

**Scoring is deterministic first, judged second.** Every check that decides pass/fail is code over files: required tool calls and arguments, order, forbidden tools, final-state assertions, claims matched to backend writes, WER with digits normalised, latency percentiles. The only LLM judge (caller persona consistency) is advisory and never changes a verdict. This keeps re-scoring free and makes a failure explainable in one sentence with evidence.

**Sessions are immutable and reproducible.** A session's id is a hash of its parsed content; editing a file changes its id, so a result can never drift away from the definition that produced it. The caller is fully parameterised (model, temperature 0, seed, voice, pace, noise at a measured SNR, phone-line filter, seeded packet loss, patience, limits). Every run is stamped with the agent config hash, the session-set hash and the scoring method version; only runs with equal stamps are compared.

**Invalid is not failed.** A call where the simulation itself broke (the caller invented a number, spoke about being an AI, never spoke, a runner error) is excluded from pass rates and listed with its reason. Otherwise the agent gets blamed for the harness.

**Small numbers are shown honestly.** Three repeats give wide Wilson intervals (3/3 reads as 44–100%). The report always shows the interval, flags flaky sessions as the first thing to rerun, and marks regressions "unconfirmed, rerun to confirm".

**One rendering path.** The live UI and the static report are the same Jinja templates over the same dicts; the static folder is what gets published. No JavaScript build, no CDN: the call page draws its lanes on a canvas from precomputed loudness envelopes and uses the native audio element. The pages follow one debugging progression on a single workbench screen: KPI cards → practice sessions (expand in place) → issues to investigate → the call inspector, where clicking a transcript turn, a tool event, a check or the lanes seeks the recording to that moment. A Playwright test drives every control, so a redesign cannot silently break replay or drill-down.

**Concurrency with isolation.** Calls run in parallel (default 4) with one LiveKit room per call, a per-call copy of the backend state keyed by call id, and one record folder per attempt. The full 11-session set, twice, took 8.3 minutes.

## What the first real runs showed

From runs `full-3` and `full-4` (the same 15 sessions × 3 on the same environment tag; 36/44 then 37/43 valid calls passed, 82% then 86%, intervals 68–90% and 73–93%; see `docs/sample-report`):

- Two real failures: an address change that was confirmed four times but never written (the agent kept re-reading the address until the call timed out), and a refund never issued when the caller opened with an amount above the order total. Both show up with the exact moment in the timeline.
- Reply latency p95 was 2.4–4.4 s per session against a 2.0 s target: the agent waits for the end-of-turn model plus the LLM, then TTS. This is the main UX finding and it is visible on every call.
- Speech recognition of spoken digits is the weak point: order numbers read one digit at a time are sometimes merged or dropped, which produced a 404 on the first lookup in the noisy-street sessions (caught as "misheard value reached a tool").
- The caller's own speech recognition also mishears the agent (it heard "Austin" for "Boston" once), so part of a long confirmation loop can be the simulator's fault. The persona judge and the transcript side-by-side make this visible.
- LiveKit's own simulator, on the same agent, reached the same conclusion from a different angle: its judge failed an address-change call because the agent looped on the order number, and its issues list names spoken order-number handling as the top problem.

**Interruptions are the caller's job, not the provider's.** LiveKit's own simulator exposes noise, poor-microphone and packet-loss flags but no barge-in control, so the caller plans interruptions itself: a seeded draw per agent turn decides whether and when (1.2–2.5 s in) to cut in with one short in-character sentence, spoken with interruptions disabled so the agent's continuing audio cannot cancel it. On the first real call the agent stopped within 200–500 ms on all eight barge-ins and still completed the refund.

**Two engines, one record format.** LiveKit Cloud's `lk agent simulate` runs judged sessions against the same worker; its export is imported as a run (`gf import-simulate`) with records built from the worker's own events and the order system's log, so the same checks and pages apply. Its verdict and metrics sit next to ours; checks that need the stereo recording are marked "not measured" for that engine. Sessions export to its scenario format (`gf sessions export-simulate`), so one session set runs on both.

## With another week

1. **Cross-check the two engines on the full set.** Run every session on both engines and report where LiveKit's judge and our deterministic checks disagree; those calls are the ones worth listening to.
2. **Labelled set for claims.** Thirty hand-labelled agent turns to measure the claim extractor, then an LLM extractor with quote-or-drop gated on agreement.
3. **Trend view.** Pass rate and latency per session across runs with the same stamp, and an "unstable" flag over the last five runs.
4. **Cost and usage.** Token and audio minutes per call from the session usage events, shown per run.
5. **Hosted instance.** The compose stack behind a reverse proxy with the access token, so the team shares one always-on UI with run control.

## Adding a second provider

The runner already treats the agent as a black box reached through a room: it creates the room, dispatches the agent by name with the call id in the metadata, lets the simulated caller talk, and collects the agent's events from the data channel plus the backend log. A second provider needs three things:

1. **A `Provider` interface** (`gf/providers/base.py`): `agent_config() -> hash + description`, `start_call(session, call_id) -> handle` that gets the provider's agent into a place the caller can reach, and `collect(handle) -> record files`. The LiveKit implementation is what `gf/runner/call.py` does today, moved behind that interface.
2. **A way for the caller to reach the agent.** For another LiveKit-based stack it is the same room. For a telephony provider (Vapi, Retell, Bland) the caller joins a LiveKit room with a SIP trunk and dials the provider's number; the recording and timing stay exactly the same because they are made on the caller's side.
3. **Event capture.** The provider's tool calls are read from the mock backend's log (unchanged, as long as the provider's tools call it with the call id), and its transcripts from the provider's call-end webhook or API instead of the data channel. Checks that depend only on the backend log and the audio work immediately; the agent-reported latency breakdown is the only provider-specific metric.

Scoring, the record schema and the UI do not change: they only ever see a record folder.
