"""PDF report of a run for a business reader (spec T6.39).

Built from the same report model as the overview, rendered through a print stylesheet and
Chromium (Playwright). `build_html(run_id)` returns the page; `write_pdf(run_id)` writes
`runs/<run>/report.pdf` and returns its path.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gf.config import settings
from gf.report import model
from gf.report.render import Links, render

EXPERIENCE_WORDS = {
    "ux.latency_p95": "slow replies",
    "ux.dead_air": "dead air after the caller spoke",
    "speech.agent_intelligible": "the agent's own words were misheard",
}
AREA_ORDER = ["adversarial", "denial", "refund", "address change", "escalation", "other"]


def _plain(reason: str) -> str:
    """The failing check's words, trimmed of ids and backend jargon."""
    r = (reason or "").replace("_", " ")
    for pre in ("invalid: ",):
        r = r.replace(pre, "")
    if r.strip() == "never said":
        r = "never gave the statement the session requires"
    return r.strip()[:240]


def report_data(run_id: str) -> dict[str, Any]:
    r = model.run_report(run_id)
    rows = r["sessions"]
    n_valid = sum(x.get("n") or 0 for x in rows)
    summ_o = next((k for k in r["kpis"] if k["id"] == "rate"), None)
    exp = next((k for k in r["kpis"] if k["id"] == "experience_rate"), None)
    p95 = next((k for k in r["kpis"] if k["id"] == "p95_ms"), None)
    wer = next((k for k in r["kpis"] if k["id"] == "wer"), None)

    def area_of(row: dict[str, Any]) -> str:
        for a in AREA_ORDER:
            if a in (row.get("areas") or []):
                return a
        return (row.get("areas") or ["other"])[0]

    struggled, held = [], []
    for row in sorted(
        rows, key=lambda x: (x.get("rate") if x.get("rate") is not None else 2, x["title"])
    ):
        att = [a for a in row.get("attempts_detail", []) if a.get("valid")]
        fails = [a for a in att if not a.get("passed")]
        reasons = Counter(_plain(a.get("failure_reason")) for a in fails if a.get("failure_reason"))
        item = {
            "title": row["title"],
            "area": area_of(row).capitalize(),
            "n": len(att),
            "passed": len(att) - len(fails),
            "invalid": row.get("n_invalid", 0),
            "reasons": [
                f"{k} ({v} of {len(att)})" if v > 1 else k for k, v in reasons.most_common(3)
            ],
            "experience_causes": row.get("experience_causes") or [],
            "n_experience_fail": row.get("n_experience_fail", 0),
            "flaky": bool(row.get("flaky")),
        }
        (struggled if fails else held).append(item)
    held.sort(
        key=lambda i: (AREA_ORDER.index(i["area"]) if i["area"] in AREA_ORDER else 99, i["title"])
    )
    exp_causes = Counter()
    for row in rows:
        for a in row.get("attempts_detail", []):
            for f in a.get("experience_fails") or []:
                exp_causes[EXPERIENCE_WORDS.get(f, f)] += 1
    areas = Counter(area_of(x) for x in rows)
    invalid = r.get("invalid", [])
    sim = r.get("simulation") or {}
    task_rate = summ_o["value"] if summ_o else "–"
    verdict = _verdict(r, struggled, held, exp)
    return {
        "run_id": run_id,
        "agent": (r.get("target") or {}).get("name") or "the reference agent",
        "generated": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "started_at": r.get("started_at"),
        "n_sessions": len(rows),
        "repeat": r.get("repeat") or (max((x.get("attempts") or 0) for x in rows) if rows else 0),
        "calls": r.get("summary", {}).get("calls")
        if isinstance(r.get("summary"), dict)
        else n_valid + len(invalid),
        "n_valid": n_valid,
        "n_invalid": len(invalid),
        "minutes": round((r.get("duration_s") or 0) / 60, 1),
        "cost": (r.get("cost") or {}).get("usd")
        if isinstance(r.get("cost"), dict)
        else r.get("cost"),
        "areas": [
            (a, n)
            for a, n in sorted(
                areas.items(), key=lambda kv: AREA_ORDER.index(kv[0]) if kv[0] in AREA_ORDER else 99
            )
        ],
        "task": {
            "value": task_rate,
            "sub": summ_o["sub"] if summ_o else "",
            "cls": summ_o["cls"] if summ_o else "grey",
        },
        "experience": {
            "value": exp["value"] if exp else "–",
            "sub": exp["sub"] if exp else "",
            "cls": exp["cls"] if exp else "grey",
        },
        "p95": {"value": p95["value"] if p95 else "–", "cls": p95["cls"] if p95 else "grey"},
        "wer": {"value": wer["value"] if wer else "–", "cls": wer["cls"] if wer else "grey"},
        "verdict": verdict,
        "struggled": struggled,
        "held": held,
        "exp_causes": exp_causes.most_common(),
        "n_experience_fail": sum(x.get("n_experience_fail", 0) for x in rows),
        "flaky": [x["title"] for x in rows if x.get("flaky")],
        "simulation": sim,
        "previous": r.get("previous"),
        "stamp": r.get("tags") or {},
        "security_sessions": [i for i in held + struggled if i["area"] == "Adversarial"],
    }


def _verdict(r, struggled, held, exp) -> str:
    n = len(struggled) + len(held)
    if not n:
        return "No valid calls were scored in this run."
    parts = [
        f"The agent completed the task on every attempt in {len(held)} of {n} practice sessions"
        + (f" and fell short in {len(struggled)}." if struggled else ".")
    ]
    sec_all = [i for i in struggled + held if i["area"] == "Adversarial"]
    sec = [i for i in struggled if i["area"] == "Adversarial"]
    breach_words = ("forbidden", "must not", "wrong write", "security", "disclos", "spoke the")
    breached = [i for i in sec if any(w in " ".join(i["reasons"]).lower() for w in breach_words)]
    if breached:
        parts.append(
            f"{len(breached)} of the {len(sec_all)} security and policy sessions let something "
            "through (a forbidden action, a leak or a statement it must not make), which needs "
            "attention before launch."
        )
    elif sec:
        parts.append(
            f"No security or policy session let anything through, though {len(sec)} of "
            f"{len(sec_all)} had a failed attempt for other reasons."
        )
    elif sec_all:
        parts.append(
            "Every security and policy session held: no data leaked and no unverified change "
            "was made."
        )
    if exp and exp.get("cls") in ("amber", "red"):
        parts.append(
            f"The experience bars were met on {exp['value']} of calls; slow replies are the "
            "main cause."
        )
    return " ".join(parts)


def build_html(run_id: str) -> str:
    links = Links("static")
    return render(
        "report_pdf.html", links, p=report_data(run_id), generated=datetime.now(UTC).isoformat()
    )


def pdf_path(run_id: str) -> Path:
    return settings().runs_dir / run_id / "report.pdf"


def write_pdf(run_id: str, out: Path | None = None) -> Path:
    """Render the report to PDF with Chromium (Playwright). Returns the file path."""
    from playwright.sync_api import sync_playwright

    html = build_html(run_id)
    out = out or pdf_path(run_id)
    out.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content(html, wait_until="load")
        page.pdf(
            path=str(out),
            format="A4",
            print_background=True,
            margin={"top": "16mm", "bottom": "16mm", "left": "14mm", "right": "14mm"},
            display_header_footer=True,
            header_template="<span></span>",
            footer_template=(
                '<div style="font-size:9px;color:#888;width:100%;text-align:center;font-family:Helvetica,Arial">'
                f"Voice agent evaluation · {run_id} · page "
                '<span class="pageNumber"></span> of <span class="totalPages"></span></div>'
            ),
        )
        browser.close()
    return out


def fresh(run_id: str) -> bool:
    """True when report.pdf exists and is newer than the run's summary."""
    out = pdf_path(run_id)
    summ = settings().runs_dir / run_id / "summary.json"
    return out.exists() and summ.exists() and out.stat().st_mtime >= summ.stat().st_mtime
