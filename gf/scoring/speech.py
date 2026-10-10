"""Speech accuracy: WER between what the caller said and what the agent heard, entity
accuracy for the facts that matter, and the causal link from a misheard value to a tool call."""

from __future__ import annotations

import re
from difflib import SequenceMatcher

import jiwer

from gf.record.model import CallRecord
from gf.scoring.checks import Check
from gf.sessions.schema import Session

DIGITS = {
    "0": "zero", "1": "one", "2": "two", "3": "three", "4": "four",
    "5": "five", "6": "six", "7": "seven", "8": "eight", "9": "nine",
}  # fmt: skip
UNITS = {v: int(k) for k, v in DIGITS.items()} | {"oh": 0, "o": 0}
TEENS = {
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
}  # fmt: skip
TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
    "eighty": 80, "ninety": 90,
}  # fmt: skip
SCALES = {"hundred": 100, "thousand": 1000}
NUMBER_WORDS = set(UNITS) | set(TEENS) | set(TENS) | set(SCALES)
WORD2DIGIT = {v: k for k, v in DIGITS.items()} | {"oh": "0", "o": "0"}
LETTER_GROUPS = {"gw"}  # spoken letter by letter whatever the case: the order-id prefix
# words that carry no information about what was heard
DROP = {"dollars", "dollar", "cents", "cent", "bucks", "point", "usd"}


def canon_tokens(text: str) -> list[tuple[str, int, int]]:
    """Canonical tokens of spoken text with the span of source words each came from.

    Numbers are parsed before anything is compared: "eighty nine ninety nine", "$89.99" and
    "eight nine nine nine" all become the digits 8 9 9 9; "five hundred" becomes 5 0 0;
    "one hundred and twenty" 1 2 0. Two-letter upper-case groups ("GW") split into letters,
    so "G W" and "GW" agree; longer ones ("ZIP", "USB") stay words. Currency words are dropped. Everything else is lower-cased with
    punctuation removed."""
    words = text.split()
    out: list[tuple[str, int, int]] = []
    val: int | None = None
    total = 0
    p0 = p1 = -1
    last = ""

    def flush() -> None:
        nonlocal val, total, p0, p1, last
        if p0 >= 0:
            for d in str(total + (val or 0)):
                out.append((d, p0, p1))
        val, total, p0, p1, last = None, 0, -1, -1, ""

    def start(i: int) -> None:
        nonlocal p0, p1
        if p0 < 0:
            p0 = i
        p1 = i

    for i, w in enumerate(words):
        raw = w.replace("$", "")
        for sub in re.split(r"[-/]", raw):
            acronym = bool(re.fullmatch(r"[A-Z]{2}[.,;:!?]*", sub))  # "GW", not "ZIP"
            lw = re.sub(r"[^a-z0-9.,']", "", sub.lower()).strip(".,")
            if not lw:
                continue
            if re.fullmatch(r"\d[\d.,]*", lw):
                flush()
                for d in re.sub(r"[^0-9]", "", lw):
                    out.append((d, i, i))
                continue
            if lw in UNITS or lw in TEENS or lw in TENS:
                v = UNITS.get(lw, TEENS.get(lw, TENS.get(lw)))
                kind = "unit" if lw in UNITS else ("teen" if lw in TEENS else "tens")
                joins = (kind == "unit" and last == "tens" and v != 0) or last in (
                    "hundred",
                    "thousand",
                )
                if joins and val is not None or (joins and last == "thousand"):
                    val = (val or 0) + v
                else:
                    flush()
                    val = v
                start(i)
                last = kind
                continue
            if lw in SCALES:
                if lw == "hundred":
                    val = (val if val is not None else 1) * 100
                else:
                    total += (val if val is not None else 1) * 1000
                    val = None
                start(i)
                last = lw
                continue
            if lw == "and" and last in ("hundred", "thousand"):
                continue
            flush()
            if lw in DROP:
                continue
            lw = re.sub(r"[^a-z0-9']", "", lw)
            if (acronym or lw in LETTER_GROUPS) and lw.isalpha() and len(lw) <= 3:
                for ch in lw:
                    out.append((ch, i, i))
            elif lw:
                out.append((lw, i, i))
    flush()
    return out


def normalize(text: str) -> str:
    """Canonical text: numbers as digits, acronyms as letters, lower-case, no punctuation."""
    return " ".join(t for t, _, _ in canon_tokens(text))


def digits_of(text: str) -> str:
    """Digit string from spoken or written numbers: "G W four eight 2 1 3" -> "48213",
    "eighty nine ninety nine" -> "8999"."""
    return "".join(t for t, _, _ in canon_tokens(text) if t.isdigit())


def check_speech(record: CallRecord, session: Session) -> list[Check]:
    checks: list[Check] = []
    said = " ".join(t.text for t in record.caller_turns)
    heard = " ".join(m["text"] for m in record.agent_user_messages) or " ".join(
        h["text"] for h in record.agent_heard
    )
    ref, hyp = normalize(said), normalize(heard)
    external_ref = bool(record.caller_result.get("reference_is_agent_transcript"))
    if ref and hyp and not external_ref:
        wer = jiwer.wer(ref, hyp)
    else:
        wer = None
    checks.append(
        Check(
            id="speech.wer",
            group="speech",
            label="Word error rate, caller's words vs what the agent heard",
            passed=True,
            severity="info",
            what_happened=f"WER {wer:.2f} over {len(ref.split())} reference words"
            if wer is not None
            else (
                "not measured by this engine (the caller's exact words are not exported; "
                "see the LiveKit verdict for its own WER)"
                if external_ref
                else "no transcript to compare"
            ),
            why_it_matters="High WER explains wrong tool arguments and repeats downstream.",
            evidence={"reference_words": len(ref.split()), "hypothesis_words": len(hyp.split())},
            value=round(wer, 3) if wer is not None else None,
        )
    )

    # entities: did each fact reach the agent intact?
    # Numeric facts the caller actually spoke are checked on their digits, however they were
    # said ("eighty-nine ninety-nine" and "8 9 9 9" are the same number).
    heard_digits_all = digits_of(heard)
    said_digits_all = digits_of(said)
    entity_rows = []
    for key, val in session.caller.facts.items():
        sval = str(val)
        d = digits_of(sval)
        if not d or len(d) < 3 or d not in said_digits_all:
            continue
        intact = d in heard_digits_all
        # nearest digit run of the same length in what was heard
        runs = re.findall(rf"(?=(\d{{{len(d)}}}))", heard_digits_all)
        nearest = max(runs, key=lambda r: SequenceMatcher(None, r, d).ratio(), default=None)
        entity_rows.append({"fact": key, "value": sval, "intact": intact, "nearest_heard": nearest})
    missed = [r for r in entity_rows if not r["intact"] and r["value"]]
    checks.append(
        Check(
            id="speech.entities",
            group="speech",
            label="Key facts (order id, zip, amount) reached the agent intact",
            passed=not missed,
            severity="soft",
            what_happened="; ".join(
                f"{r['fact']} {r['value']} was never heard intact (closest: {r['nearest_heard']})"
                for r in missed
            )
            or "all facts heard intact",
            why_it_matters="A misheard order number or zip is the usual root cause of a failed lookup.",
            evidence={"entities": entity_rows},
            value=len(missed),
        )
    )

    # causal link: a tool argument that does not match a fact but is close to one
    links = []
    facts_by_digits = {
        digits_of(str(v)): (k, str(v)) for k, v in session.caller.facts.items() if digits_of(str(v))
    }
    for c in record.tool_calls:
        for arg, aval in c.args.items():
            ad = digits_of(str(aval))
            if not ad or ad in facts_by_digits:
                continue
            best = max(
                facts_by_digits.items(),
                key=lambda kv: SequenceMatcher(None, kv[0], ad).ratio(),
                default=None,
            )
            if (
                best
                and len(best[0]) == len(ad)
                and SequenceMatcher(None, best[0], ad).ratio() >= 0.6
            ):
                links.append(
                    {
                        "tool": c.tool,
                        "tool_id": c.id,
                        "t_ms": c.t_ms,
                        "arg": arg,
                        "used": str(aval),
                        "fact": best[1][0],
                        "truth": best[1][1],
                        "status": c.status,
                    }
                )
    checks.append(
        Check(
            id="speech.misheard_to_tool",
            group="speech",
            label="No misheard value was passed into a tool call",
            passed=not links,
            severity="soft",
            what_happened="; ".join(
                f"misheard {lk['truth']} as {lk['used']} → {lk['tool']}({lk['arg']}={lk['used']}) returned {lk['status']}"
                for lk in links
            )
            or "none",
            why_it_matters="This is where speech errors turn into wrong actions.",
            evidence={
                "tool_ids": [lk["tool_id"] for lk in links],
                "t_ms": links[0]["t_ms"] if links else None,
                "links": links,
            },
            value=len(links),
        )
    )
    return checks
