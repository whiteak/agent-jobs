#!/usr/bin/env python3
"""benchpages: the HTML for the bench auto-run pages (built by jobboard.py).

Same look as the rest of the board: it only uses classes from jobboard's PAGE
CSS (card, pill s-*, banner, bar, hint, ghost buttons), so light/dark and the
phone layout come for free. jobboard.py supplies the data (run records, specs,
env snapshots, benchstats output); this module never touches the files.

  bench_body(...)     the /bench page (form, live card, history table)
  rows_body(...)      just the history tbody, for the 5s refresh
  run_body(...)       /bench/run/<id> detail page
  compare_body(...)   /bench/compare?a=&b= verdict page
"""
import html
import json


def esc(s):
    return html.escape(str(s), quote=True)


def pts(x):
    return "-" if x is None else f"{x:.1f}"


def signed(x):
    return "-" if x is None else f"{x:+.1f}"


def rng(lo, hi):
    return f"{signed(lo)} to {signed(hi)}"


def p_num(p):
    """The permutation p as plain words: the chance of a gap this big if
    nothing really changed. Never 0 (a permutation test always has the
    observed arrangement in its own sample space)."""
    if p is None:
        return "unknown"
    if p < 0.001:
        return "under 0.1%"
    if p < 0.01:
        return f"{p * 100:.1f}%"
    return f"{p * 100:.0f}%"


def p_cell(p):
    return "-" if p is None else p_num(p)


def duration(secs):
    if secs is None:
        return "-"
    secs = int(secs)
    if secs < 90:
        return f"{secs}s"
    m, s = divmod(secs, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    return f"{m // 60}h {m % 60:02d}m"


def long_duration(secs):
    m = int(secs) // 60
    if m < 60:
        return f"{m} min"
    return f"{m // 60} h {m % 60:02d} min"


# ---------------------------------------------------------------- verdict wording

def verdict_texts(cmp):
    """(css_word, headline, one-sentence explanation) in plain words. A verdict
    needs two independent signals - the 95% range entirely past zero AND a small
    permutation p - so the sentence names both."""
    o = cmp.get("overall")
    if not o:
        return "mut", "Nothing to compare: the two runs have no task in common.", \
            "Pick two runs that measured the same tasks."
    v, diff, chance = o["verdict"], o["diff"], p_num(o.get("p"))
    if v == "not enough samples":
        return ("warn",
                f"Not enough samples: at least one task has fewer than 3 samples in one run. "
                f"The numbers are shown, but a verdict would be guesswork.",
                "Add more samples with Resume, then compare again.")
    if v == "better":
        return "ok", f"After is better: {signed(diff)} points (95% range {rng(o['lo'], o['hi'])})", \
            ("A single sample swings 10-20 points, so one run proves nothing; here the whole "
             f"95% range ({rng(o['lo'], o['hi'])} points) sits above zero and the chance of a "
             f"gap this big if nothing really changed is only {chance}, so the gain is "
             "bigger than the run-to-run noise.")
    if v == "worse":
        return "bad", f"After is worse: {signed(diff)} points (95% range {rng(o['lo'], o['hi'])})", \
            ("A single sample swings 10-20 points, so one run proves nothing; here the whole "
             f"95% range ({rng(o['lo'], o['hi'])} points) sits below zero and the chance of a "
             f"gap this big if nothing really changed is only {chance}, so the change "
             "really did cost points.")
    clears = o["lo"] is not None and o["hi"] is not None and (o["lo"] > 0 or o["hi"] < 0)
    if clears:  # the range points one way, the p says it could still be luck
        return "mut", f"No real difference: {signed(diff)} points, a gap this big happens by luck", \
            (f"The 95% range ({rng(o['lo'], o['hi'])}) stays on one side of zero, but a gap "
             f"that size also turns up in {chance} of comparisons where nothing changed, so "
             "on its own it is not proof.")
    return "mut", f"No real difference: {signed(diff)} points, the 95% range {rng(o['lo'], o['hi'])} includes zero", \
        ("If you ran this comparison many times, 95% of the results would land in that range - "
         f"since it crosses zero, and the chance of a gap this big if nothing changed is "
         f"{chance}, so the difference is within the normal run-to-run noise.")


def chance_line(cmp):
    """The banner's plain-words p sentence (what the user asked to see up top)."""
    o = cmp.get("overall")
    if not o or o.get("p") is None:
        return ""
    return f"Chance of a gap this big if nothing really changed: {p_num(o['p'])}"


def cut_note(n):
    """'<b>N</b> answers hit the token limit', '' when N is 0. Truncated answers
    are real scores, but they are not the same experiment as a finished answer."""
    if not n:
        return ""
    return f"<b>{n}</b> answer{'s' if n != 1 else ''} hit the token limit"


def ignored_note(n):
    if not n:
        return ""
    return (f"<b>{n}</b> failed request{'s' if n != 1 else ''} ignored "
            f"(not scored - a stopped server is not a 0)")


def plain_counts(cmp, side):
    """Plain-text version of the token-limit / ignored-request counts."""
    bits = []
    n = (cmp.get("length") or {}).get(side) or 0
    if n:
        bits.append(f"{n} answers hit the token limit")
    m = (cmp.get("ignored") or {}).get(side) or 0
    if m:
        bits.append(f"{m} failed requests ignored (not scored)")
    return "; ".join(bits)


def summary_text(cmp, a, b):
    """Compact plain-text comparison for pasting into Claude."""
    o = cmp.get("overall")
    lines = [f"BENCH A/B: {a['label']} ({a['id']}) vs {b['label']} ({b['id']})",
             f"A: {a['model']} ctx {a['n_ctx']}   B: {b['model']} ctx {b['n_ctx']}"]
    if o:
        lines.append(f"VERDICT: {o['verdict']}  overall A {pts(o['mean_a'])} -> B {pts(o['mean_b'])} "
                     f"({signed(o['diff'])} pts, 95% {rng(o['lo'], o['hi'])}, chance of a gap "
                     f"this big by luck {p_num(o.get('p'))})")
        for r in cmp["tasks"]:
            lines.append(f"  {r['task']}: {pts(r['mean_a'])} -> {pts(r['mean_b'])} "
                         f"({signed(r['diff'])}, 95% {rng(r['lo'], r['hi'])}, "
                         f"chance {p_num(r.get('p'))}, n {r['n_a']}/{r['n_b']}) {r['verdict']}")
        sp = cmp.get("speed") or {}
        d, s2 = sp.get("decode_tps"), sp.get("secs")
        if d:
            lines.append(f"  decode t/s: {pts(d['mean_a'])} -> {pts(d['mean_b'])} ({d['verdict']})")
        if s2:
            lines.append(f"  s/answer: {pts(s2['mean_a'])} -> {pts(s2['mean_b'])} ({s2['verdict']})")
    for side, lab in (("a", a), ("b", b)):
        note = plain_counts(cmp, side)
        if note:
            lines.append(f"  {lab['label']}: {note}")
    for k, va, vb in cmp.get("diffs") or []:
        lines.append(f"  changed model/config: {k}: {va} -> {vb}")
    for k, va, vb in cmp.get("spec_diffs") or []:
        lines.append(f"  changed run settings: {k}: {va} -> {vb}")
    return "\n".join(lines)


# ---------------------------------------------------------------- /bench page

def bench_body(groups, runs, live, tasksecs, estimate, more=0):
    """Form + live card + history table. groups: [{title, hint, tasks, checked}].
    live: the live bench run (the running one, else the oldest queued) or None;
    more: how many MORE bench runs sit in the queue behind it. estimate: (answers, secs)."""
    g_html = ""
    for g in groups:
        boxes = "".join(
            f'<label style="font-weight:400;display:inline-flex;gap:6px;align-items:center;margin-right:16px">'
            f'<input type="checkbox" name="tasks" value="{esc(t)}" data-kind="{esc(g["kind"])}" '
            f'{"checked" if g["checked"] else ""}>{esc(t)}</label>' for t in g["tasks"])
        g_html += (f'<div style="margin:10px 0"><b>{esc(g["title"])}</b>'
                   f'<div class="hint">{esc(g["hint"])}</div>{boxes}</div>')
    sels = "".join(f'<option value="{n}"{" selected" if n == 6 else ""}>{n}</option>' for n in range(1, 13))
    est_txt = f"about {estimate[0]} answers, roughly {long_duration(estimate[1])}"
    form = f"""<div class="card"><h2>New bench run</h2><form method="post" action="/bench/new" id="benchform">
<label>Label (required)</label>
<input type="text" name="label" required maxlength="80" placeholder="before: 256K ctx">
{g_html}
<div class="row"><div><label>Samples per task</label><select name="samples">{sels}</select>
<p class="hint">6-8 needed for a fair comparison: scores swing 10-20 points run to run.</p></div>
<div><label>Max tokens per answer</label><input type="number" name="max_tokens" value="32768" min="1024" max="131072" step="1024"></div></div>
<label>Notes (optional)</label><textarea name="notes" placeholder="What changed vs the previous run?"></textarea>
<p class="hint" id="est">{esc(est_txt)}</p>
<p class="hint">Runs go in the same queue as agent jobs - never at the same time (the GPU serves one stream).
A run takes as long as N answers x the table averages; you can Stop it and Resume later, it keeps what it finished.</p>
<button>Start bench run</button></form></div>
<script>
(function(){{
// Live estimate: same averages the server used, recomputed as you tick tasks.
// Every page script lives in its own IIFE: classic <script> blocks share one
// global scope, so a second script that reuses a name (live, est...) used to
// die with "Identifier 'live' has already been declared" and the whole page
// stopped refreshing. Only handlers the HTML calls go on window.
const TASKSECS={json.dumps(tasksecs)};const DEFAULTS={{hard:240,main:90,agent:400}};
function grp(id){{return DEFAULTS[id]||240}}
function est(){{const f=document.getElementById('benchform');
 const boxes=[...f.querySelectorAll('input[name=tasks]:checked')];
 const n=parseInt(f.elements.samples.value)||6;let secs=0;
 for(const b of boxes){{secs+=(TASKSECS[b.value]||grp(b.dataset.kind||'main'))*n}}
 document.getElementById('est').textContent='about '+(boxes.length*n)+' answers, roughly '+(secs<5400?Math.round(secs/60)+' min':(Math.floor(secs/3600)+' h '+String(Math.round(secs%3600/60)).padStart(2,'0')+' min'));}}
document.getElementById('benchform').addEventListener('change',est);est();
}})();
</script>"""
    live_html = live_card(live, more)
    return form + live_html + f"""
<div class="card"><h2>Runs</h2><div class="tbl"><table><thead><tr>
<th class="now">Date</th><th>Label</th><th>Model</th><th>Samples</th><th>Overall</th><th>Time</th><th class="st">Status</th>
<th>Before</th><th>After</th><th></th></tr></thead><tbody id="runrows">{rows_body(runs)}</tbody></table></div>
<p style="margin-top:12px"><button id="cmpbtn" onclick="window.goCompare()" disabled>Compare selected runs</button>
<span class="hint" id="cmphint">Tick one run as <b>Before</b> and one as <b>After</b> first.</span></p>
<p class="hint">Numbers are points (100 = every test passed); overall = mean of the per-task means,
&plusmn; = bootstrap spread. A verdict needs both a small chance column and a 95% range past zero.</p></div>
<script>
(function(){{
// Refresh the history + live card every 5s while a run is going; never while
// Before/After radios are being picked (that would wipe the selection).
let live={json.dumps(bool(live))};
async function refresh(){{if(live&&!document.querySelector('input[name=a]:checked, input[name=b]:checked')){{
 try{{const r=await fetch('/api/bench/rows');if(r.ok){{const h=await r.text();const tb=document.getElementById('runrows');if(h!==tb.innerHTML)tb.innerHTML=h}}}}catch(e){{}}}}
 setTimeout(refresh,5000)}}
setTimeout(refresh,5000);
// The button only lights up with exactly one Before and one different After. The
// listener sits on document so it keeps working after the table is refreshed.
function cmpState(){{const a=document.querySelector('input[name=a]:checked'),b=document.querySelector('input[name=b]:checked');
 const btn=document.getElementById('cmpbtn'),hint=document.getElementById('cmphint');if(!btn)return;
 btn.disabled=!(a&&b&&a.value!==b.value);
 hint.textContent=(!a||!b)?'Tick one run as Before and one as After first.':
  (a.value===b.value)?'Pick two different runs: one Before, one After.':'Ready: comparing the ticked runs.'}}
document.addEventListener('change',cmpState);cmpState();
function goCompare(){{const a=document.querySelector('input[name=a]:checked'),b=document.querySelector('input[name=b]:checked');
 if(!a||!b){{alert('Tick one run as Before and one as After first.');return}}
 if(a.value===b.value){{alert('Pick two different runs: one Before, one After.');return}}
 location='/bench/compare?a='+encodeURIComponent(a.value)+'&b='+encodeURIComponent(b.value)}}
window.goCompare=goCompare;   // the button's onclick is the only thing outside
}})();
</script>"""


def live_card(live, more=0):
    """Running/queued bench run: progress bar, current task, ETA, running scores,
    refreshed every 5s without a reload (the detail page has the same card).
    more: bench runs still queued behind this one (their own +N line)."""
    if not live:
        return ""
    scores = "".join(f'<td>{esc(s["task"])}</td><td>{pts(s["mean"])}</td><td>{s["n"]}</td>'
                     for s in live["scores"])
    stop = (f'<form method="post" action="/stop/{esc(live["id"])}" style="display:inline">'
            f'<button class="ghost">Stop</button></form>')
    return f"""<div class="card banner QUEUED" id="livecard" data-live="{esc(live["id"])}">
<div class="brow"><b>{esc(live["label"])}</b><span class="mut">bench run</span><span style="flex:1"></span>{stop}</div>
<div class="prog"><b id="ldone">{live["done"]}/{live["total"]}</b> samples &middot; <span id="lcur">{esc(live["current"] or "starting")}</span> &middot; <span id="leta">{eta_text(live["eta_s"])}</span></div>
<div class="bar s-QUEUED"><i id="lbar" style="width:{pct(live["done"], live["total"])}%"></i></div>
<table><thead><tr><th>Task</th><th>So far</th><th>n</th></tr></thead><tbody id="lscores">{scores}</tbody></table>
<p class="hint">Scores so far use only the samples finished; they move a lot early on.</p>
{f'<p class="hint" id="lmore">+{more} more bench runs queued</p>' if more else ''}</div>
<script>
(function(){{
// The live card refreshes itself from /api/bench/live/<id> every 5s.
const LIVE={json.dumps(live["id"])};
function escj(s){{const d=document.createElement('div');d.textContent=(s==null?'':String(s));return d.innerHTML}}
function pctj(d,t){{return t?Math.round(100*d/t):0}}
function etaj(x){{if(x==null)return 'time left: unknown yet';const m=Math.round(x/60);return m<1?'time left: <1 min':'time left: ~'+m+' min'}}
async function live(){{try{{const r=await fetch('/api/bench/live/'+LIVE);if(!r.ok)return;const j=await r.json();
 document.getElementById('ldone').textContent=j.done+'/'+j.total;
 document.getElementById('lcur').textContent=escj(j.current||'starting');
 document.getElementById('leta').textContent=etaj(j.eta_s);
 document.getElementById('lbar').style.width=pctj(j.done,j.total)+'%';
 document.getElementById('lscores').innerHTML=j.scores.map(s=>'<tr><td>'+escj(s.task)+'</td><td>'+s.mean.toFixed(1)+'</td><td>'+s.n+'</td></tr>').join('');
 if(j.state==='QUEUED'){{document.getElementById('lcur').textContent='waiting in the queue'}}
 if(!['QUEUED','WORKING','STUCK'].includes(j.state)){{setTimeout(()=>location.reload(),1500)}}
}}catch(e){{}}setTimeout(live,5000)}}
live();
}})();
</script>"""


def eta_text(secs):
    if secs is None:
        return "time left: unknown yet"
    m = max(1, round(secs / 60))
    return f"time left: ~{m} min" if secs else "time left: none"


def pct(done, total):
    return round(100 * done / total) if total else 0


def rows_body(runs):
    """History tbody: newest first, with the Before/After radios."""
    out = ""
    for r in runs:
        act = ""
        if r["can_resume"]:
            act += (f'<form method="post" action="/bench/resume/{esc(r["id"])}" style="display:inline">'
                    f'<button class="ghost2" style="margin-right:6px">Resume</button></form>')
        act += (f'<form method="post" action="/bench/delete/{esc(r["id"])}" style="display:inline" '
                f'onsubmit="return confirm(\'Move this run to the trash? It stays in .trash, nothing is deleted.\')">'
                f'<button class="ghost">Delete</button></form>')
        out += (f'<tr><td class="now">{esc(r["date"])}</td>'
                f'<td><a href="/bench/run/{esc(r["id"])}">{esc(r["label"])}</a></td>'
                f'<td class="mut">{esc(r["model"])}{f" ctx {esc(r['ctx'])}" if r["ctx"] else ""}</td>'
                f'<td>{esc(r["samples"])}</td>'
                f'<td><b>{esc(r["overall"])}</b></td>'
                f'<td>{esc(r["duration"])}</td>'
                f'<td class="st" id="rp-{esc(r["id"])}"><span class="pill s-{esc(r["state"])}">'
                f'{esc(r["state_label"])}{f" <span class=\'hint\'>{esc(r['pill_extra'])}</span>" if r["pill_extra"] else ""}</span></td>'
                f'<td><input type="radio" name="a" value="{esc(r["id"])}" title="Before"></td>'
                f'<td><input type="radio" name="b" value="{esc(r["id"])}" title="After"></td>'
                f'<td>{act}</td></tr>')
    return out or '<tr><td colspan=10 class=mut>No bench runs yet.</td></tr>'


# ---------------------------------------------------------------- /bench/run/<id>

def run_body(run):
    """Detail: notes, env snapshot, per-task stats with score chips, errors."""
    env = run["env"] or {}
    cfg = env.get("config") or {}
    cfg_line = ", ".join(f"{esc(k)}={esc(v)}" for k, v in sorted(cfg.items())[:40]) or "-"
    env_bits = [f"model <b>{esc(env.get('model') or '?')}</b>",
                f"ctx {esc(env.get('n_ctx') or '?')}",
                f"config sha {esc((env.get('config_sha256') or '')[:12] or '?')}",
                f"Basalt {esc(env.get('basalt_commit') or '?')}"]
    s = run["status"]
    stop = ""
    if s["state"] in ("QUEUED", "WORKING", "STUCK"):
        stop = (f'<form method="post" action="/stop/{esc(run["id"])}" style="display:inline">'
                f'<button class="ghost">Stop</button></form>')
    if run["can_resume"]:
        stop += (f' <form method="post" action="/bench/resume/{esc(run["id"])}" style="display:inline">'
                 f'<button class="ghost2">Resume</button></form>')
    stop += (f' <form method="post" action="/bench/delete/{esc(run["id"])}" style="display:inline" '
             f'onsubmit="return confirm(\'Move this run to the trash?\')">'
             f'<button class="ghost">Delete</button></form>')

    rows = ""
    summ = run["summary"] or {}
    for t in run["tasks"]:
        st = summ.get(t)
        if not st:
            continue
        chips = "".join(
            f'<span class="pill {"ok" if v >= 99.9 else ("bad" if v <= 0.5 else "warn")}" '
            f'style="margin:1px 2px;font-weight:500">{v:.0f}</span>' for v in st["scores"])
        rows += (f'<tr><td>{esc(t)}</td><td>{st["n"]}</td><td><b>{pts(st["mean"])}</b></td>'
                 f'<td>{pts(st["sd"])}</td><td>{pts(st["min"])} - {pts(st["max"])}</td>'
                 f'<td>{chips}</td>'
                 f'<td>{pts(st["decode_tps_mean"])}</td><td>{pts(st["secs_mean"])}</td></tr>')
    o = summ.get("overall")
    overall = f'<p><b>Overall {pts(o["mean"])} &plusmn; {pts(o["sd"])}</b> <span class="hint">' \
              f'(mean of the per-task means; &plusmn; = bootstrap spread)</span></p>' if o else ""
    counts = " &middot; ".join(x for x in (cut_note(run.get("length", 0)),
                                           ignored_note(run.get("ignored", 0))) if x)
    counts_line = f'<p class="hint">{counts}</p>' if counts else ""
    fail_box = f'<div class="bwarn">{esc(run.get("fail_note") or "")}</div>' \
        if run.get("fail_note") else ""
    err_rows = "".join(f'<tr><td>{esc(e["task"])}</td><td>{e["sample"]}</td>'
                       f'<td class="bad">{esc(e["error"])}</td>'
                       f'<td class="mut">{esc(e["tail"])[:200]}</td></tr>' for e in run["errors"])
    errors = f'<div class="card"><h2>Errors</h2><div class="tbl"><table><thead><tr><th>Task</th><th>Sample</th><th>Error</th><th>Last output</th></tr></thead><tbody>{err_rows}</tbody></table></div></div>' if err_rows else ""
    tail = ""
    if run.get("log_tail"):
        tail = f'<div class="card"><h2>Runner log (last lines)</h2><pre>{esc(run["log_tail"])}</pre></div>'
    return f"""{live_card(run["live"]) if run.get("live") else f'''<div class="card banner {esc(s['state'])}">
<div class="brow"><span class="btitle s-{esc(s['state'])}">{esc(s['label'])}</span><span style="flex:1"></span>{stop}</div>
<div class="bact">{esc(s.get('current_action') or '')}</div>
{fail_box}
<div class="bstats mut">{esc(run['status_line'])}</div></div>'''}
<div class="card"><h2>{esc(run['label'])}</h2>
<p class="mut">{esc(run['meta_line'])}</p>
{'<pre>' + esc(run['notes']) + '</pre>' if run['notes'] else '<p class="mut">No notes.</p>'}
<p class="hint">Env snapshot: {' &middot; '.join(env_bits)}</p>
<p class="hint">Config: {cfg_line}</p></div>
<div class="card"><h2>Scores</h2>{overall}{counts_line}
<div class="tbl"><table><thead><tr><th>Task</th><th>n</th><th>Mean</th><th>SD</th><th>Min-Max</th><th>Per sample</th><th>Dec t/s</th><th>s/answer</th></tr></thead>
<tbody>{rows or '<tr><td colspan=8 class=mut>No samples yet.</td></tr>'}</tbody></table></div></div>
{errors}{tail}
<p><a href="/bench">All bench runs</a> &middot; <a href="/">Agent jobs</a></p>"""


# ---------------------------------------------------------------- /bench/compare

def compare_body(data):
    cls, headline, explain = verdict_texts(data["cmp"])
    a, b, cmp = data["a"], data["b"], data["cmp"]
    rows = ""
    for r in cmp["tasks"]:
        vcls = {"better": "ok", "worse": "bad"}.get(r["verdict"], "mut")
        rows += (f'<tr><td>{esc(r["task"])}</td>'
                 f'<td>{pts(r["mean_a"])} &plusmn; {pts(r["sd_a"])} <span class="hint">({r["n_a"]})</span></td>'
                 f'<td>{pts(r["mean_b"])} &plusmn; {pts(r["sd_b"])} <span class="hint">({r["n_b"]})</span></td>'
                 f'<td><b>{signed(r["diff"])}</b></td><td>{rng(r["lo"], r["hi"])}</td>'
                 f'<td>{p_cell(r.get("p"))}</td>'
                 f'<td class="{vcls}">{esc(r["verdict"])}</td></tr>')
    o = cmp.get("overall")
    if o:
        vcls = {"better": "ok", "worse": "bad"}.get(o["verdict"], "mut")
        rows += (f'<tr><td><b>Overall</b></td><td><b>{pts(o["mean_a"])}</b> <span class="hint">({o["n_a"]})</span></td>'
                 f'<td><b>{pts(o["mean_b"])}</b> <span class="hint">({o["n_b"]})</span></td>'
                 f'<td><b>{signed(o["diff"])}</b></td><td><b>{rng(o["lo"], o["hi"])}</b></td>'
                 f'<td><b>{p_cell(o.get("p"))}</b></td>'
                 f'<td class="{vcls}"><b>{esc(o["verdict"])}</b></td></tr>')
    sp = cmp.get("speed") or {}
    speed = ""
    for label, row, unit in (("Decode speed (t/s)", sp.get("decode_tps"), ""),
                             ("Seconds per answer", sp.get("secs"), "")):
        if not row:
            continue
        pct = f" ({row['pct']:+.0f}%)" if row.get("pct") is not None else ""
        speed += (f'<tr><td>{esc(label)}</td><td>{pts(row["mean_a"])}{unit}</td>'
                  f'<td>{pts(row["mean_b"])}{unit}</td><td>{signed(row["diff"])}{pct}</td>'
                  f'<td>{rng(row["lo"], row["hi"])}</td><td>{p_cell(row.get("p"))}</td>'
                  f'<td>{esc(row["verdict"])}</td></tr>')

    def diff_list(items):
        return "<ul>" + "".join(f'<li><b>{esc(k)}</b>: {esc(va)} &rarr; {esc(vb)}</li>'
                                for k, va, vb in items) + "</ul>"

    diffs, spec_diffs = data["diffs"] or [], cmp.get("spec_diffs") or []
    changed = (f'<h3 style="margin:6px 0">Model and config</h3>'
               + (diff_list(diffs) if diffs else
                  '<p class="mut">No difference in the model, its context or the Basalt config.</p>')
               + '<h3 style="margin:14px 0 6px">Run settings</h3>'
               + (diff_list(spec_diffs) if spec_diffs else
                  '<p class="mut">Same tasks, same number of samples, same token limit.</p>')
               + ('<p class="warn" style="font-weight:600">Nothing detected in the model, config or '
                  'run settings - make sure you changed what you meant to.</p>'
                  if not diffs and not spec_diffs else ""))
    chance = chance_line(cmp)
    bits = []
    for run, k in ((a, "a"), (b, "b")):
        txt = " &middot; ".join(x for x in (
            cut_note((cmp.get("length") or {}).get(k) or 0),
            ignored_note((cmp.get("ignored") or {}).get(k) or 0)) if x)
        if txt:
            bits.append(f"{esc(run['label'])}: {txt}")
    notes = '<p class="hint">' + "<br>".join(bits) + "</p>" if bits else ""
    return f"""<div class="card banner {cls}" style="border-left-width:8px">
<div class="btitle s-{cls}" style="font-size:28px">{esc(headline)}</div>
<p style="font-size:16px">{esc(explain)}</p>
{f'<p style="font-size:16px"><b>{esc(chance)}</b></p>' if chance else ''}</div>
<div class="card"><h2>Before / After</h2>
<p><b>Before:</b> <a href="/bench/run/{esc(a['id'])}">{esc(a['label'])}</a> <span class="mut">{esc(a['date'])}, {esc(a['model'])} ctx {esc(a['n_ctx'])}</span><br>
<b>After:</b> <a href="/bench/run/{esc(b['id'])}">{esc(b['label'])}</a> <span class="mut">{esc(b['date'])}, {esc(b['model'])} ctx {esc(b['n_ctx'])}</span></p>
{notes}
<button id="copysum">Copy summary</button> <span class="hint">compact plain text for pasting into Claude</span></div>
<div class="card"><h2>Per task</h2><p class="hint">Points per task. The 95% range is the bootstrap spread
of the After-minus-Before difference; the chance column is the chance of a gap this big if nothing really
changed. A task row only says better/worse when both point the same way.</p>
<div class="tbl"><table><thead><tr><th>Task</th><th>Before</th><th>After</th><th>Diff</th><th>95% range</th><th>Chance</th><th>Verdict</th></tr></thead>
<tbody>{rows or '<tr><td colspan=7 class=mut>No tasks in common.</td></tr>'}</tbody></table></div>
<p class="hint">With several tasks, one can differ by chance: trust the overall verdict.</p></div>
<div class="card"><h2>Speed (not part of the verdict)</h2>
<div class="tbl"><table><thead><tr><th></th><th>Before</th><th>After</th><th>Diff</th><th>95% range</th><th>Chance</th><th></th></tr></thead>
<tbody>{speed or '<tr><td colspan=7 class=mut>No timing data.</td></tr>'}</tbody></table></div></div>
<div class="card"><h2>What changed between the runs</h2>{changed}</div>
<p><a href="/bench">All bench runs</a></p>
<script>
(function(){{
const SUM={json.dumps(summary_text(cmp, a, b))};
const cb=document.getElementById('copysum');
cb.onclick=()=>{{let w=null;
 try{{if(window.ClipboardItem)w=navigator.clipboard.write([new ClipboardItem({{'text/plain':new Blob([SUM],{{type:'text/plain'}})}})])}}catch(e){{w=null}}
 const done=()=>{{cb.textContent='Copied';setTimeout(()=>cb.textContent='Copy summary',2500)}};
 if(w&&w.then)w.then(done,()=>fallback());else fallback();
 function fallback(){{if(navigator.clipboard&&navigator.clipboard.writeText){{navigator.clipboard.writeText(SUM).then(done,manual)}}else manual()}}
 function manual(){{const ta=document.createElement('textarea');ta.value=SUM;ta.className='bundle-paste';
  ta.previousSibling&&0;cb.parentNode.appendChild(ta);ta.focus();ta.select();cb.textContent='Clipboard refused - copy below'}}}};
}})();
</script>"""
