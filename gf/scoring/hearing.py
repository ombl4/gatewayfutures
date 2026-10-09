"""Simulator hearing check (spec T5.8): did the simulated caller hear the agent correctly?

The caller listens to the agent through its own speech recognition. When that mishears an
order number, an amount or an address and the caller then acts on the wrong value, the call
goes off the rails through no fault of the agent, so it must count as a simulator fault
(invalid), not an agent failure. We have both sides on record: the agent's own text per turn
and the caller's transcript of it.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

import jiwer

from gf.record.model import CallRecord
from gf.scoring.checks import Check
from gf.scoring.speech import normalize

MATCH_WINDOW_MS = (
    12_000  # a heard turn belongs to the agent turn that started up to this long before it
)
ACTED_ON_TURNS = (
    2  # the caller "acted on" a mishearing if it says the wrong value within its next N turns
)
WER_SOFT_FLAG = 0.4  # numbers read as words vs written inflate plain WER
MIN_ENTITY_DIGITS = 3


def pair_turns(record: CallRecord) -> list[dict[str, Any]]:  # noqa: D417
    """Agent turn → the caller's transcript of it. A heard turn (timestamped when the caller's
    speech recognition finalised it) belongs to the latest agent turn that started before it."""
    starts = [_agent_start(at, record.t0_ms) for at in record.agent_turns]
    heard_for: dict[int, dict[str, Any]] = {}
    for h in sorted(record.caller_heard, key=lambda x: x["t_ms"]):
        owner = None
        for idx, st in enumerate(starts):
            if st <= h["t_ms"] + 300:
                owner = idx
            else:
                break
        if owner is not None and owner not in heard_for:
            heard_for[owner] = h
    return [
        {
            "n": at.n,
            "t_ms": starts[idx],
            "said": at.text,
            "heard": heard_for[idx]["text"] if idx in heard_for else None,
            "heard_t_ms": heard_for[idx]["t_ms"] if idx in heard_for else None,
        }
        for idx, at in enumerate(record.agent_turns)
    ]


def _agent_start(turn, t0_ms: int) -> int:
    """Audio start of an agent turn relative to the record origin (metrics are epoch seconds)."""
    at = turn.metrics.get("started_speaking_at")
    if at:
        rel = int(float(at) * 1000) - t0_ms
        if 0 <= rel <= 3_600_000:
            return rel
    return turn.t_ms


_MONEY = re.compile(r"\$?\s*(\d{1,4})(?:[.,](\d{2}))?\s*(?:dollars?|usd)?", re.I)


def entities_of(text: str) -> list[str]:
    """Digit entities worth checking in an agent turn: digit groups (order ids, zips) and
    amounts, all as digit strings ("89.99" -> "8999")."""
    out: list[str] = []
    for run in _numeric_runs(text):
        if len(run) >= MIN_ENTITY_DIGITS:
            out.append(run)
    return out


TENS = {
    "twenty": 2,
    "thirty": 3,
    "forty": 4,
    "fifty": 5,
    "sixty": 6,
    "seventy": 7,
    "eighty": 8,
    "ninety": 9,
}
TEENS = {
    "ten": "10",
    "eleven": "11",
    "twelve": "12",
    "thirteen": "13",
    "fourteen": "14",
    "fifteen": "15",
    "sixteen": "16",
    "seventeen": "17",
    "eighteen": "18",
    "nineteen": "19",
}


def _numeric_runs(text: str) -> list[str]:
    """Digit runs from spoken or written numbers. Digits spoken one at a time ("four eight two")
    concatenate; number words with tens/teens/hundreds ("one hundred twenty", "eighty nine")
    are evaluated, so a spoken amount compares with "$89.99"."""
    from gf.scoring.speech import WORD2DIGIT

    toks = re.findall(r"[a-z]+|\d+|[.$]", text.lower())
    runs: list[str] = []
    group: list[str] = []

    def flush() -> None:
        if not group:
            return
        if any(t in TENS or t in TEENS or t == "hundred" for t in group):
            runs.append(str(_words_to_int(group)))
        else:
            runs.append("".join(t if t.isdigit() else WORD2DIGIT[t] for t in group))
        group.clear()

    for tok in toks:
        if tok.isdigit() or tok in WORD2DIGIT or tok in TENS or tok in TEENS or tok == "hundred":
            group.append(tok)
        elif tok in (".", "$", "point") or (tok == "and" and group and group[-1] == "hundred"):
            continue  # "89.99" and "one hundred and twenty" stay one group
        else:
            flush()
    flush()
    return runs


def _words_to_int(group: list[str]) -> int:
    from gf.scoring.speech import WORD2DIGIT

    total, cur = 0, 0
    for t in group:
        if t.isdigit():
            cur = cur * (10 ** len(t)) + int(t) if cur else int(t)
        elif t in TEENS:
            cur += int(TEENS[t])
        elif t in TENS:
            cur += TENS[t] * 10
        elif t == "hundred":
            cur = (cur or 1) * 100
        elif t in WORD2DIGIT:
            cur += int(WORD2DIGIT[t])
    return total + cur


def attribute(
    record: CallRecord, timeline: dict[str, Any], pairs: list[dict[str, Any]], acted: list[dict]
) -> None:
    """Whose fault is each acted-on mishearing (T5.14)? Ask an independent recogniser what the
    agent's audio for that turn says. Sets m["side"] to "simulator" (the second opinion hears
    what the agent meant), "agent" (it hears what the caller heard, or not what the agent meant)
    or "unverified"; m["second_opinion"] carries the transcript."""
    from gf.scoring.second_opinion import second_opinion

    by_turn = {p["n"]: p for p in pairs}
    for m in acted:
        p = by_turn.get(m["turn"])
        m["side"], m["second_opinion"] = "unverified", None
        if not p or p.get("heard_t_ms") is None:
            continue
        text = second_opinion(record, timeline, m["turn"], p["t_ms"], p["heard_t_ms"])
        if text is None:
            continue
        m["second_opinion"] = text
        if m.get("kind") == "word":
            words = set(normalize(text).split())
            said_ok, heard_too = m["said"] in words, m["heard"] in words
        else:
            runs = _numeric_runs(text)
            said_ok, heard_too = m["said"] in runs, m["heard"] in runs
        m["side"] = "simulator" if said_ok and not heard_too else "agent"


def check_hearing(
    record: CallRecord, timeline: dict[str, Any] | None = None
) -> tuple[list[Check], list[str]]:
    """(checks, validity reasons). A misheard entity the caller acted on is a validity reason
    when the simulator's recogniser is at fault; when the agent's own audio is heard the same
    way by an independent recogniser, it is the agent's failure (T5.14)."""
    pairs = pair_turns(record)
    refs, hyps = [], []
    mishearings = []
    for p in pairs:
        if not p["heard"]:
            continue
        ref, hyp = normalize(p["said"]), normalize(p["heard"])
        if ref and hyp:
            refs.append(ref)
            hyps.append(hyp)
        said_ents = entities_of(p["said"])
        heard_digits = "".join(_numeric_runs(p["heard"]))
        for ent in said_ents:
            if ent in heard_digits:
                continue
            nearest = _nearest_run(ent, _numeric_runs(p["heard"]))
            mishearings.append(
                {
                    "turn": p["n"],
                    "t_ms": p["t_ms"],
                    "said": ent,
                    "heard": nearest,
                    "acted_on": False,
                }
            )
        for said_w, heard_w in word_substitutions(p["said"], p["heard"]):
            if _is_number_word(said_w) or _is_number_word(heard_w):
                continue  # "eighty" vs "eight": digits are compared as runs above, never as words
            mishearings.append(
                {
                    "turn": p["n"],
                    "t_ms": p["t_ms"],
                    "said": said_w,
                    "heard": heard_w,
                    "acted_on": False,
                    "kind": "word",
                }
            )
    # did the caller act on a wrong value? (said the misheard digits/word in its next turns)
    for m in mishearings:
        if not m["heard"] or m["heard"] == m["said"]:
            continue
        later = sorted(
            (t for t in record.caller_turns if t.t_start_ms >= m["t_ms"]),
            key=lambda t: t.t_start_ms,
        )[:ACTED_ON_TURNS]
        for t in later:
            # the caller repeating the wrong value ("Boston, not Austin") means it heard and
            # reacted to the mishearing, whether or not it also gives the right one
            if m.get("kind") == "word":
                hit = m["heard"] in set(normalize(t.text).split())
            else:
                runs = _numeric_runs(t.text)
                # the wrong number said as a number of its own, and the right one not said:
                # "4 8 2 1 3" contains "4821" but is the caller giving the correct order id
                hit = m["heard"] in runs and m["said"] not in runs
            if hit:
                m["acted_on"] = True
                m["caller_turn"] = t.n
                break
    wer = jiwer.wer(refs, hyps) if refs else None
    acted_all = [m for m in mishearings if m["acted_on"]]
    attribute(record, timeline or {}, pairs, acted_all)
    acted = [m for m in acted_all if m["side"] != "agent"]  # the simulator's (or unverified)
    agent_side = [m for m in acted_all if m["side"] == "agent"]
    reasons = [
        f"simulator misheard the agent: agent said {m['said']} in turn {m['turn']}, the caller "
        f"heard {m['heard']} and used it in its turn {m.get('caller_turn')}"
        + (
            " (a second recogniser hears the agent correctly)"
            if m["side"] == "simulator"
            else " (not verified by a second recogniser)"
        )
        for m in acted
    ]
    soft = wer is not None and wer > WER_SOFT_FLAG and not acted
    checks = [
        Check(
            id="speech.caller_hearing",
            group="validity",
            label="The simulated caller heard the agent correctly",
            passed=not acted and not soft,
            severity="hard" if acted else "soft",
            what_happened=(
                "; ".join(reasons)
                if acted
                else (
                    f"caller-side WER {wer:.2f} over {len(refs)} agent turns"
                    + (
                        f"; {len(mishearings)} digit mishearing(s) not acted on"
                        if mishearings
                        else ""
                    )
                    if wer is not None
                    else "nothing to compare"
                )
            ),
            why_it_matters="A caller that mishears the agent argues about the wrong value; that is the simulator's fault, not the agent's, so such calls are excluded.",
            evidence={
                "pairs": pairs,
                "mishearings": mishearings,
                "t_ms": acted[0]["t_ms"] if acted else None,
            },
            value=round(wer, 3) if wer is not None else None,
        ),
    ]
    checks.append(
        Check(
            id="speech.agent_intelligible",
            group="speech",
            label="The agent's words were understood as spoken",
            passed=not agent_side,
            severity="hard" if agent_side else "soft",
            what_happened="; ".join(
                f"the agent meant {m['said']} in turn {m['turn']} but its audio is heard as "
                f"{m['heard']} by the caller and by an independent recogniser "
                f'("{(m.get("second_opinion") or "")[:80]}"); the caller argued about it in '
                f"its turn {m.get('caller_turn')}"
                for m in agent_side
            )
            or "no mishearing of the agent was traced to its own audio",
            why_it_matters=(
                "When the agent's voice is not intelligible on a key value, a real caller "
                "mishears it too; this is the agent's pronunciation, not the simulator."
            ),
            evidence={
                "mishearings": agent_side,
                "t_ms": agent_side[0]["t_ms"] if agent_side else None,
                "turn_ns": [m["turn"] for m in agent_side],
            },
            value=len(agent_side),
        )
    )
    return checks, reasons


def _is_number_word(w: str) -> bool:
    from gf.scoring.speech import WORD2DIGIT

    return w in WORD2DIGIT or w in TENS or w in TEENS or w in ("hundred", "thousand")


def _nearest_run(target: str, runs: list[str]) -> str | None:
    cands = [r for r in runs if abs(len(r) - len(target)) <= 1]
    return max(cands, key=lambda r: SequenceMatcher(None, r, target).ratio(), default=None)


STOPWORDS = {
    "the",
    "a",
    "an",
    "and",
    "to",
    "of",
    "is",
    "it",
    "in",
    "on",
    "for",
    "your",
    "you",
    "that",
    "this",
    "with",
    "please",
    "can",
    "could",
    "would",
    "will",
    "have",
    "has",
    "was",
    "were",
    "are",
    "be",
    "do",
    "did",
    "not",
    "okay",
    "just",
    "like",
    "so",
    "if",
    "or",
    "at",
    "by",
    "from",
    "we",
    "i",
    "me",
    "my",
    "our",
    "us",
    "yes",
    "no",
    "there",
    "here",
    "what",
    "which",
    "when",
    "how",
    "than",
    "then",
    "also",
    "about",
}


def word_substitutions(said: str, heard: str, min_len: int = 4) -> list[tuple[str, str]]:
    """Words the caller heard in place of what the agent said (names, places, products):
    aligned word substitutions of similar length, ignoring digits (handled above) and stopwords."""
    a, b = normalize(said).split(), normalize(heard).split()
    out = []
    for tag, i1, i2, j1, j2 in SequenceMatcher(a=a, b=b).get_opcodes():
        if tag != "replace" or (i2 - i1) != (j2 - j1):
            continue
        for sw, hw in zip(a[i1:i2], b[j1:j2], strict=False):
            if len(sw) < min_len or len(hw) < min_len or sw in STOPWORDS or hw in STOPWORDS:
                continue
            if sw in b or hw in a:
                continue  # merely reordered
            if SequenceMatcher(None, sw, hw).ratio() < 0.3:
                continue  # unrelated words: more likely a dropped/added phrase than a mishearing
            out.append((sw, hw))
    return out
