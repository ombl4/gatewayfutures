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
WORD2DIGIT = {v: k for k, v in DIGITS.items()} | {"oh": "0", "o": "0"}


def normalize(text: str) -> str:
    """Lower-case, strip punctuation, spell every digit out, split letter groups ("GW" → "g w")."""
    t = text.lower().replace("$", " dollars ")
    t = re.sub(r"(\d)[,.](\d)", r"\1 point \2", t)
    t = re.sub(r"\d", lambda m: f" {DIGITS[m.group(0)]} ", t)
    t = re.sub(r"\b([a-z])([a-z])\b", r"\1 \2", t)  # "gw" -> "g w"
    t = re.sub(r"[^a-z\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def digits_of(text: str) -> str:
    """Digit string from spoken or written numbers: "G W four eight 2 1 3" -> "48213"."""
    out = []
    for tok in re.findall(r"[a-z]+|\d", text.lower()):
        if tok.isdigit():
            out.append(tok)
        elif tok in WORD2DIGIT:
            out.append(WORD2DIGIT[tok])
    return "".join(out)


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
    # Only numeric facts the caller actually spoke digit by digit are checked (an amount said
    # as "eighty-nine ninety-nine" or an item name has no exact form to be heard intact).
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
