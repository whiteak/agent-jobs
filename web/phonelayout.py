#!/usr/bin/env python3
"""Upgrade 9a: the phone layout - one column, big enough to hit, at 700 px or narrower.

    CSS               one @media block for the board's own classes, appended in the page head
                      by jobboard.render(). Anything that styles another module's classes
                      (the timeline, the diff viewer, the check badges, the screenshot
                      thumbnails) lives in that module's own CSS, because a body-appended
                      <style> later in the document would otherwise win.

    MANIFEST, icon    a web app manifest and 192/512 px icons, so Chrome on Android offers
                      "Add to Home screen". The icons are drawn here with zlib/struct: there
                      is no image library in this sandbox and nothing can be installed, so
                      committing a PNG would mean a binary nobody can look at.

Nothing in this module changes what the page looks like on a PC: every rule is inside
@media (max-width:700px). Standard library only.
"""
from __future__ import annotations

import json
import struct
import zlib

PHONE_MAX = 700          # the spec's "under 700 px wide"
MIN_VIEW = 360           # the narrowest screen we design for (Galaxy Note10+)
TAP = 44                 # px, the smallest touch target

CSS = """<style>
/* U09a: phones. One column, nothing wider than the screen, tap targets you can hit
   with a thumb, the job's status always in reach, the jobs list as cards. */
@media (max-width:%dpx){
.wrap{max-width:none;padding:10px 8px 60px}
.card{padding:12px;margin-bottom:12px}
.row{display:block}
.row>div{width:100%%;min-width:0;margin:0 0 10px}
.jobtxt{max-width:none;white-space:normal}
.clamp{max-width:none}
.act{max-width:none}
td.st,th.st,td.now,th.now{min-width:0;white-space:normal}
.btitle{font-size:20px}
.bact{font-size:16px}
.bhead{gap:6px}
.tbl{overflow-x:visible}
header{position:sticky;top:0;z-index:20;background:var(--bg);border-bottom:1px solid var(--line);
margin:-10px -8px 12px;padding:6px 8px;gap:6px 10px}
header h1{font-size:17px}
header .nav{order:3;flex:1 1 100%%;display:flex;gap:14px;overflow-x:auto;white-space:nowrap;
font-size:14px;padding:2px 0}
.topst{margin-left:auto;font-size:13px}
.topst:empty{display:none}
/* every tap target is at least a thumb: buttons, links styled as buttons, form fields */
button,a.btn,summary{min-height:%dpx}
button,a.btn,summary{padding:10px 16px;display:inline-flex;align-items:center;justify-content:center}
select,input[type=text],input[type=password],input[type=number]{min-height:%dpx}
/* the jobs table becomes one card per job; the 3 s poller only swaps the rows, so the
   same markup has to do both jobs, and the column name comes from CSS, not the HTML.
   JOB_COLS below is that list, in table order. */
table.jobs thead{display:none}
table.jobs,table.jobs tbody,table.jobs tr,table.jobs td{display:block;width:auto;min-width:0;max-width:none}
table.jobs tr{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:10px;margin-bottom:10px}
table.jobs td{border:0;padding:2px 0}
table.jobs td::before{display:block;font-size:11px;font-weight:600;color:var(--mut);
text-transform:uppercase;letter-spacing:.04em}
table.jobs td[colspan]::before{display:none}
@@COLS@@
}</style>""" % (PHONE_MAX, TAP, TAP)


# The jobs table's columns, in the order job_rows() writes them and home_page() heads them.
JOB_COLS = ("Started", "Status", "Mode", "Folder", "Job", "Time", "Checks")


def col_rules():
    """One labelled line per column, so a card says what each line is."""
    return "\n".join('table.jobs td:nth-child(%d)::before{content:"%s"}' % (n, name)
                     for n, name in enumerate(JOB_COLS, 1))


CSS = CSS.replace("@@COLS@@", col_rules())


# ------------------------------------------------------------------ add to home screen

def manifest():
    """The manifest Chrome on Android wants for "Add to Home screen"."""
    return json.dumps({
        "name": "Agent Jobs",
        "short_name": "Jobs",
        "description": "The job board for the coding agent on this PC.",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#f6f5f2",
        "theme_color": "#2f6fdb",
        "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
                  {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"}],
    })


BG = (47, 46, 44)          # the board's dark card
FG = (236, 235, 231)       # ... and its text
MUT = (150, 149, 143)
STATUS = [(92, 194, 138), (232, 176, 79), (110, 161, 255)]    # done / waiting / working


def _fill(buf, size, x0, y0, x1, y1, rgb):
    x0, x1 = max(0, int(x0 * size)), min(size, int(x1 * size))
    y0, y1 = max(0, int(y0 * size)), min(size, int(y1 * size))
    px = bytes(rgb)
    for y in range(y0, y1):
        row = y * size * 3
        buf[row + x0 * 3:row + x1 * 3] = px * (x1 - x0)


def _png(size, buf):
    """A plain RGB PNG, written by hand (no image library here)."""
    raw = b"".join(b"\x00" + buf[y * size * 3:(y + 1) * size * 3] for y in range(size))

    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


_icons = {}


def icon_png(size):
    """The board's icon: three job rows, each with its status colour. Deterministic, so it
    is built once per size and kept."""
    size = max(32, min(int(size), 1024))
    if size not in _icons:
        buf = bytearray(bytes(BG) * size * size)
        for n, col in enumerate(STATUS):
            top = 0.14 + n * 0.24
            _fill(buf, size, 0.12, top, 0.88, top + 0.18, (62, 61, 58))          # the row card
            _fill(buf, size, 0.17, top + 0.055, 0.27, top + 0.125, col)          # its status
            _fill(buf, size, 0.33, top + 0.05, 0.82, top + 0.075, FG)            # what it is
            _fill(buf, size, 0.33, top + 0.105, 0.68, top + 0.125, MUT)          # where it got to
        _icons[size] = _png(size, buf)
    return _icons[size]
