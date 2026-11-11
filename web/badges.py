#!/usr/bin/env python3
"""Upgrade 7b: one badge per check - "build ✓", "tests 41/42 ✗" - each linking to its output.

The counts come from the job record (U01's verify rounds, or a later "Run checks again"),
never from a fresh run, so showing them never starts a container. The output file named in
the record is only used when it stays inside JOBS/<job id>/ - a tampered record cannot
make the board read any other file. Used by the job page and the jobs list.
"""
from __future__ import annotations

import html
from pathlib import Path

MAX_BYTES = 200_000          # the tail of a check log the page shows


def source(m):
    """Which result set the badges show: a later recheck wins, else the last verify round."""
    rc = m.get("recheck") or {}
    if rc.get("summary"):
        return "recheck", rc["summary"]
    rounds = (m.get("verify") or {}).get("rounds") or []
    if rounds:
        try:
            n = int(rounds[-1].get("round") or 0)
        except (TypeError, ValueError):
            n = 0
        return "round%d" % n, rounds[-1].get("summary") or {}
    return None, None


def results(m, src):
    """The result list for one badge link ('recheck' or 'round<N>')."""
    if src == "recheck":
        return ((m.get("recheck") or {}).get("summary") or {}).get("results")
    if str(src).startswith("round"):
        try:
            want = int(str(src)[5:])
        except ValueError:
            return None
        for r in (m.get("verify") or {}).get("rounds") or []:
            try:
                if int(r.get("round") or 0) == want:
                    return (r.get("summary") or {}).get("results")
            except (TypeError, ValueError):
                continue
    return None


def badges(m, jid=None):
    """[{text, ok, href, title}] - one entry per check, in the order they ran."""
    src, summ = source(m)
    if not src or not summ or summ.get("results") is None:
        return []
    out = []
    for i, r in enumerate(summ.get("results") or []):
        name = str(r.get("name") or ("check %d" % (i + 1)))[:40]
        p, f = int(r.get("passed") or 0), int(r.get("failed") or 0)
        text = name + (f" {p}/{p + f}" if (p or f) else "")
        if r.get("timed_out"):
            text += " timed out"
        if r.get("type") in ("vision", "android-smoke"):      # U06/U06b: a screen/device check
            if r.get("verdict") == "inconclusive":
                text += " (not sure)"                        # U06: the model could not tell
            elif r.get("verdict") == "skipped":
                text += " (no device)"                       # U06b: nothing to check on
        out.append({"text": text, "ok": bool(r.get("ok")), "name": name,
                    "cmd": str(r.get("cmd") or "")[:300],
                    "seconds": r.get("seconds"),
                    "href": f"/api/check/{jid}?src={src}&n={i}" if jid else ""})
    return out


def _esc(x):
    return html.escape(str(x if x is not None else ""), quote=True)


CSS = """<style>
.cbadges{margin:8px 0 0;display:flex;gap:6px;flex-wrap:wrap}
a.cbadge,span.cbadge{display:inline-block;padding:2px 8px;border-radius:99px;font-size:12px;font-weight:600;
 border:1px solid currentColor;text-decoration:none;white-space:nowrap}
.cbadge.ok{color:var(--ok)}.cbadge.bad{color:var(--bad)}.cbadge .cbm{font-weight:700}
td.chk .cbadges{margin:4px 0 0}
</style>"""


def badges_html(m, jid=None):
    """The same badges as HTML for the jobs list (the job page builds them from the JSON)."""
    out = ""
    for b in badges(m, jid):
        cls = "cbadge ok" if b["ok"] else "cbadge bad"
        mark = "&#10003;" if b["ok"] else "&#10007;"
        inner = f'{_esc(b["text"])} <span class="cbm">{mark}</span>'
        out += (f'<a class="{cls}" href="{_esc(b["href"])}" target="_blank" '
                f'title="{_esc(b["cmd"])}">{inner}</a>') if b["href"] else \
               f'<span class="{cls}">{inner}</span>'
    return out


def check_text(m, src, n, job_root):
    """The output of one check: (text, cut) or None. Only files inside JOBS/<id>/ are read."""
    res = results(m, src)
    if res is None:
        return None
    try:
        item = res[int(n)]
    except (IndexError, ValueError, TypeError):
        return None
    p = Path(str(item.get("output_file") or ""))
    if not str(p):
        return None
    try:
        p = p.resolve()
        root = Path(job_root).resolve()
    except OSError:
        return None
    if not p.is_file() or root not in p.parents:
        return None
    try:
        size = p.stat().st_size
        with open(p, "rb") as f:
            if size > MAX_BYTES:
                f.seek(size - MAX_BYTES)
                return "… (the first part is cut - the raw log has it all)\n" + f.read().decode("utf-8", "replace"), True
            return f.read().decode("utf-8", "replace"), False
    except OSError:
        return None
