"""Terminal text of `queuetruth data add` and `data fetch iex`: the answer first, then one line per session date
(venue-local session span), the format and checks, how each input was identified, and the next command."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def text(doc: dict[str, Any]) -> str:
    from queuetruth.cli import _table

    lines = [str(doc["answer"]), ""]
    rows = [
        [
            d["date"],
            f"{d['first'] or '-'} to {d['last'] or '-'}",
            f"{d['symbols']:,}",
            f"{d['records']:,}",
        ]
        for d in doc["dates"]
    ]
    tz = sorted({d["timezone"] for d in doc["dates"]})
    lines += _table(["date", f"session ({', '.join(tz) or 'local time'})", "symbols", "records"], rows, right={2, 3})
    ds = doc["dataset"]
    rules = ds.get("rule_set") or ds["venue"]
    venue = rules if ds.get("source_venue") in (None, rules) else f"{rules} ({ds['source_venue']})"
    lines += [
        "",
        f"format {ds['format']} ({ds['format_title']}), venue {venue}, dataset id {str(ds['dataset_id'])[:12]}",
    ]
    dq = doc["data_quality"]
    reasons = f": {'; '.join(dq['reasons'][:3])}" if dq["reasons"] else ""
    lines.append(f"data quality {dq['verdict']}{reasons}")
    chk = doc["checks"]
    c = chk["counts"]
    lines.append(
        f"checks {chk['status']}: {c['pass']} passed, {c['warn']} warned, {c['fail']} failed, {c['skip']} skipped "
        f"(report {doc['files']['dq_html']})"
    )
    for x in chk["not_passed"][:5]:
        lines.append(f"  {x['status']} {x['id']}: {x['summary']}")
    if doc.get("added_to_existing"):
        lines.append(f"added to the existing dataset: {', '.join(doc.get('added_dates') or []) or 'new records'}")
    inputs = doc.get("inputs") or []
    fetched = doc.get("fetch")
    if fetched:
        lines.append(
            f"fetched: {fetched['bytes_read'] / 1e6:,.1f} MB streamed from {fetched['url']}, "
            f"{fetched['kept_messages']:,} of {fetched['messages']:,} messages kept; the day's file was not stored"
        )
    elif inputs:
        lines.append("inputs:")
        for i in inputs:
            how = i["how"]
            bits = [f"{i['format']} ({how.get('format')})", f"venue {i['venue']} ({how.get('venue')})"]
            bits.append(f"date {i['date']} ({how.get('date')})" if i.get("date") else f"dates from {how.get('date')}")
            lines.append(f"  {Path(i['path']).name}: {', '.join(bits)}")
    for a in doc.get("attribution") or []:
        lines.append(f"{a.get('text')} {a.get('terms_url')}")
    lines.append(f"next: {doc['next']}")
    return "\n".join(lines)


__all__ = ["text"]
