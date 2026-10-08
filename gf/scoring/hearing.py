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


def pair_turns(record: CallRecord) -> list[dict[str, Any]]:
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
    """Digit runs from spoken or written numbers, understanding tens ("eighty nine" -> 89) so
    an amount read as words compares with "$89.99"."""
    from gf.scoring.speech import WORD2DIGIT

    runs, cur = [], []
    toks = re.findall(r"[a-z]+|\d+|[.$]", text.lower())
    i = 0
    while i < len(toks):
        tok = toks[i]
        d = None
        if tok.isdigit():
            d = tok
        elif tok in TEENS:
            d = TEENS[tok]
        elif tok in TENS:
            nxt = toks[i + 1] if i + 1 < len(toks) else ""
            if nxt in WORD2DIGIT and nxt not in ("zero", "oh", "o"):
                d = f"{TENS[tok]}{WORD2DIGIT[nxt]}"
                i += 1
            else:
                d = f"{TENS[tok]}0"
        elif tok in WORD2DIGIT:
            d = WORD2DIGIT[tok]
        elif tok == "hundred" and cur:
            d = "00"
        if d is None:
            if tok in (".", "$", "point"):
                i += 1
                continue  # "89.99" stays one run
            if cur:
                runs.append("".join(cur))
                cur = []
            i += 1
            continue
        cur.append(d)
        i += 1
    if cur:
        runs.append("".join(cur))
    return runs


def check_hearing(record: CallRecord) -> tuple[list[Check], list[str]]:
    """(checks, validity reasons). A misheard entity the caller acted on is a validity reason."""
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
                hit = m["heard"] in "".join(_numeric_runs(t.text))
            if hit:
                m["acted_on"] = True
                m["caller_turn"] = t.n
                break
    wer = jiwer.wer(refs, hyps) if refs else None
    acted = [m for m in mishearings if m["acted_on"]]
    reasons = [
        f"simulator misheard the agent: agent said {m['said']} in turn {m['turn']}, the caller heard {m['heard']} and used it in its turn {m.get('caller_turn')}"
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
        )
    ]
    return checks, reasons


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
