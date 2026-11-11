#!/usr/bin/env python3
"""Upgrade 7b: the Changes card - file list with +/- counts and a diff viewer.

    card_html(jid, active)   the empty card (data-jid / data-live drive the script)
    CSS, JS                  styles and the page script (inserted by jobboard.job_page)

The script talks to /api/changes/<jid> (the list + the view picker) and
/api/changes/<jid>/file?path=...&ref=... (one file at a time), which are diffview.py.
Big diffs stay responsive because only CHUNK rows at a time ever reach the DOM, runs of
unchanged lines collapse to a clickable line, and the "Next change" button renders ahead
of itself. A running job refreshes the list every 10 s and whenever the live stream
(7a) reports something. No CDN, no external JS.
"""


def _esc(x):
    from html import escape
    return escape(str(x if x is not None else ""), quote=True)


def card_html(jid, active):
    return (f'<div class="card" id="changescard" data-jid="{_esc(jid)}" data-live="{1 if active else 0}">'
            f'<h2>Changes</h2>'
            f'<div class="brow"><select id="chref" class="chref" aria-label="Which changes to show"></select>'
            f'<button type="button" id="chview" class="ghost2">Side by side</button>'
            f'<button type="button" id="chnext" class="ghost2">Next change</button>'
            f'<button type="button" id="chreload" class="ghost2">Refresh</button>'
            f'<span class="hint" id="chhow"></span></div>'
            f'<div class="tbl"><table id="chfiles"><thead><tr><th>File</th><th class="now">+</th>'
            f'<th class="now">&#8722;</th><th></th></tr></thead><tbody></tbody></table></div>'
            f'<p class="hint" id="chtot"></p>'
            f'<div class="diffbox" id="chdiff"></div>'
            f'<details class="hint chold"><summary>Git status of the folder</summary>'
            f'<pre id="git" class="mut">Loading...</pre></details></div>')


CSS = """<style>
select.chref{width:auto;min-width:220px;padding:6px}
#chfiles td{vertical-align:top}#chfiles td.p{word-break:break-all;cursor:pointer}
#chfiles tr.hid td{color:var(--mut)}#chfiles tr.hid td.p{cursor:default}
#chfiles tr.here td.p{font-weight:700}
#chfiles td.n{white-space:nowrap;font:12px ui-monospace,monospace;text-align:right}
#chfiles td.pa{color:var(--ok)}#chfiles td.pd{color:var(--bad)}
.diffbox{max-height:70vh;overflow:auto;margin-top:10px;border:1px solid var(--line);border-radius:8px;background:var(--card)}
table.df{border-collapse:collapse;width:100%;font:12.5px/1.45 ui-monospace,monospace}
table.df td{padding:1px 6px;border-bottom:1px solid var(--code);vertical-align:top;white-space:pre-wrap;word-break:break-word}
table.df td.ln{width:1%;min-width:44px;color:var(--mut);text-align:right;background:var(--code);user-select:none}
table.df td.tx{width:50%}table.df td.empty{background:repeating-linear-gradient(-45deg,var(--code),var(--code) 6px,transparent 6px,transparent 12px)}
tr.add td.tx{background:rgba(46,125,79,.16)}tr.del td.tx{background:rgba(192,57,43,.14)}
tr.add td.ln{background:rgba(46,125,79,.28)}tr.del td.ln{background:rgba(192,57,43,.24)}
tr.nl td{color:var(--mut);font-style:italic}
tr.hunk td{background:var(--code);color:var(--acc);font-weight:600}
tr.gap td{background:var(--code);color:var(--mut);font-size:12px}
tr.gap.exp{cursor:pointer}tr.gap.exp:hover{color:var(--acc)}
tr.now td{outline:2px solid var(--acc);outline-offset:-2px}
.dmore{display:flex;gap:10px;align-items:center;padding:8px;color:var(--mut);font-size:13px}
.dhead{padding:8px;font-weight:600;border-bottom:1px solid var(--line);word-break:break-all}
.dmore2{margin:8px}
.chold{margin-top:12px}
</style>"""


JS = """
(function(){
// U07b: check badges in the banner - the live status carries them (7a's stream/poller
// dispatches 'jobbadges'), so they update the moment a check run lands.
document.addEventListener('jobbadges',function(ev){
  var box=document.getElementById('bchecks');
  if(!box||!ev.detail||!ev.detail.check_badges){return;}
  box.textContent='';
  ev.detail.check_badges.forEach(function(b){
    var a=document.createElement('a');
    a.className='cbadge '+(b.ok?'ok':'bad'); a.href=b.href; a.target='_blank'; a.title=b.cmd||'';
    a.textContent=b.text+' '+(b.ok?'\\u2713':'\\u2717');
    box.appendChild(a);
  });
});

// U07b: the Changes card. Its own IIFE, like every other page script here.
var card=document.getElementById('changescard'); if(!card){return;}
var jid=card.dataset.jid, live=card.dataset.live==='1';
var sel=card.querySelector('#chref'), howEl=card.querySelector('#chhow'), totEl=card.querySelector('#chtot');
var listBody=card.querySelector('#chfiles').tBodies[0], box=card.querySelector('#chdiff');
var viewBtn=card.querySelector('#chview'), nextBtn=card.querySelector('#chnext'), reloadBtn=card.querySelector('#chreload');
var table=document.createElement('table'); table.className='df';
var tbody=table.createTBody();
var CHUNK=300, CTX=6;                    // rows per insert / unchanged lines kept around a change
var ref='', refsKey='', openName='', openTag='', sbs=false, busy=false;
var items=[], at=0, chg=[], chgAt={}, gaps={}, moreBtn=null, now=null, atChange=-1, diff=null;

function esc(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function say(msg,cls){
  box.textContent=''; moreBtn=null;
  var d=document.createElement('div'); d.className=cls||'mut'; d.textContent=msg; box.appendChild(d);
}

// ---- one diff line -> a table row (unified: 1 text column; side by side: 2)
function lineU(L){ var k=L[0],o=L[1],n=L[2];
  var cls=k==='+'?'add':k==='-'?'del':k==='nl'?'nl':'ctx';
  return '<tr class="'+cls+(k==='+'||k==='-'?' chg':'')+'"><td class="ln">'+(o==null?'':o)+'</td>'
       + '<td class="ln">'+(n==null?'':n)+'</td><td class="tx">'+esc(L[3])+'</td></tr>'; }
function side(S){ if(!S){return '<td class="ln"></td><td class="tx empty"></td>';}
  var k=S[1];
  return '<td class="ln">'+(S[0]==null?'':S[0])+'</td><td class="tx '+
         (k==='+'?'add':k==='-'?'del':'ctx')+'">'+esc(S[2])+'</td>'; }
function lineP(P){ var L=P[0],R=P[1],ch=(L&&L[1]!==' ')||(R&&R[1]!==' ');
  return '<tr class="'+(ch?'chg':'ctx')+'">'+side(L)+side(R)+'</tr>'; }
function line(L){ return sbs?lineP(L):lineU(L); }
function row(cls,txt,attr){ var n=sbs?4:3;
  return '<tr class="'+cls+'"'+(attr?(' '+attr):'')+'><td colspan="'+n+'">'+esc(txt)+'</td></tr>'; }

// ---- the rows for one file, with unchanged runs collapsed
function build(d){
  items=[]; at=0; chg=[]; chgAt={}; gaps={}; now=null; atChange=-1;
  tbody.innerHTML='';
  var lastNew=null, gid=0;
  d.hunks.forEach(function(h){
    if(lastNew!==null&&h.n>lastNew+1){items.push({h:row('gap',(h.n-lastNew-1)+' unchanged lines')});}
    items.push({h:row('hunk','@@ -'+h.o+' +'+h.n+(h.header?(' '+h.header):''))});
    var run=[];
    function flush(){
      if(!run.length){return;}
      if(run.length<=CTX){run.forEach(function(L){items.push({h:line(L)});});}
      else{
        for(var k=0;k<CTX;k++){items.push({h:line(run[k])});}
        var extra=run.slice(CTX), id='g'+(++gid);
        gaps[id]=extra.map(line).join('');
        items.push({h:row('gap exp','Show '+extra.length+' more unchanged lines','data-g="'+id+'"')});
      }
      run=[];
    }
    h.lines.forEach(function(L){ if(L[0]===' '){run.push(L);return;} flush(); items.push({h:line(L),ch:true}); });
    flush();
    h.lines.forEach(function(L){ if(L[2]!=null&&(lastNew===null||L[2]>lastNew)){lastNew=L[2];} });
  });
  if(!items.length){items.push({h:row('gap','no text changes here')});}
  for(var i=0;i<items.length;i++){if(items[i].ch){chg.push(i);}}
}

// ---- render lazily: only CHUNK rows reach the DOM at a time
function pump(to){
  var stop=(to==null)?Math.min(items.length,at+CHUNK):Math.min(items.length,to+1);
  if(stop<=at){return;}
  var html='';
  for(var k=at;k<stop;k++){html+=items[k].h;}
  var first=tbody.rows.length, added=stop-at;
  tbody.insertAdjacentHTML('beforeend',html);
  for(var m=at;m<stop;m++){if(items[m].ch){chgAt[m]=tbody.rows[first+(m-at)];}}
  at=stop;
  tell();
}
function tell(){
  if(at>=items.length){if(moreBtn){moreBtn.remove();moreBtn=null;}return;}
  if(!moreBtn){moreBtn=document.createElement('button');moreBtn.type='button';moreBtn.className='ghost2 dmore2';
    moreBtn.onclick=function(){pump();}; box.appendChild(moreBtn);}
  moreBtn.textContent='Show '+(items.length-at)+' more lines';
}
box.addEventListener('scroll',function(){if(box.scrollTop+box.clientHeight>box.scrollHeight-500){pump();}});
box.addEventListener('click',function(ev){
  var tr=ev.target&&ev.target.closest?ev.target.closest('tr.gap.exp'):null;
  if(!tr){return;}
  var id=tr.getAttribute('data-g');
  if(!id||!gaps[id]){return;}
  tr.insertAdjacentHTML('beforeend',gaps[id]);
  tr.parentNode.removeChild(tr);
  delete gaps[id];
});

// ---- "Next change" jumps (and renders ahead of itself)
function nextChange(){
  if(!chg.length){return;}
  atChange=(atChange+1)%chg.length;
  var idx=chg[atChange];
  if(idx>=at){pump(idx);}
  var el=chgAt[idx];
  if(!el){return;}
  if(now){now.classList.remove('now');}
  now=el; el.classList.add('now');
  try{el.scrollIntoView({block:'center'});}catch(e){el.scrollIntoView();}
  nextBtn.textContent='Next change '+(atChange+1)+'/'+chg.length;
}
nextBtn.onclick=nextChange;
viewBtn.onclick=function(){ sbs=!sbs; viewBtn.textContent=sbs?'One column':'Side by side';
  if(diff){build(diff); showTable();} };

function showTable(){
  box.textContent=''; moreBtn=null;
  var head=document.createElement('div'); head.className='dhead';
  head.textContent=diff.path+'   +'+diff.added+' / -'+diff.removed+
    (diff.status!=='modified'?('   ('+diff.status+(diff.old&&diff.old!==diff.path?(' from '+diff.old):'')+')'):'');
  box.appendChild(head);
  table.className='df'+(sbs?' sbs':'');
  box.appendChild(table);
  pump();
  while(at<items.length&&at<3000&&box.scrollHeight<=box.clientHeight+40){pump();}
  tell();
  if(diff.truncated){var n=document.createElement('div');n.className='dmore';
    n.textContent='More changed lines than the page shows - the review bundle has them all.'; box.appendChild(n);}
}
function showDiff(d){
  diff=d;
  if(!d.shown){
    say((d.why==='binary'?'Binary file':d.why==='generated'?'Generated file':'Also left out of the review bundle')+
        ' - listed, not shown.','mut');
    return;
  }
  if(!d.hunks.length){
    say((d.status==='renamed'?'Renamed':d.status)+' - no text changes.','mut');
    return;
  }
  build(d);
  showTable();
}

async function getDiff(path){
  var mine=++getDiff.n;
  try{
    var r=await fetch('/api/changes/'+jid+'/file?path='+encodeURIComponent(path)+(ref?('&ref='+encodeURIComponent(ref)):''));
    var d=await r.json();
    if(mine!==getDiff.n){return;}
    if(d.error){say(d.error,'bad');return;}
    openTag=d.added+':'+d.removed;
    showDiff(d);
  }catch(e){say('Could not read that file.','bad');}
}
getDiff.n=0;

function pick(path){
  openName=path; openTag='';
  for(var i=0;i<listBody.rows.length;i++){listBody.rows[i].classList.toggle('here',listBody.rows[i].dataset.p===path);}
  getDiff(path);
}

function paintFiles(files){
  var frag=document.createDocumentFragment();
  files.forEach(function(f){
    var tr=document.createElement('tr');
    if(!f.shown){tr.className='hid';}
    tr.dataset.p=f.path;
    var a=document.createElement('td'); a.className='p';
    a.textContent=(f.status==='added'?'+ ':f.status==='deleted'?'- ':f.status==='renamed'?'\u21b4 '
                   :f.status==='untracked'?'? ':'') + f.path;
    a.title=f.shown?('Show this file'+(f.why?(' ('+f.why+')'):'')):(f.why+' - listed but not shown');
    tr.appendChild(a);
    var p=document.createElement('td'); p.className='n pa'; p.textContent=f.added||''; tr.appendChild(p);
    var d=document.createElement('td'); d.className='n pd'; d.textContent=f.removed||''; tr.appendChild(d);
    var w=document.createElement('td'); w.className='n'; w.textContent=f.why||''; tr.appendChild(w);
    if(f.shown){ tr.onclick=function(){pick(f.path);}; }
    frag.appendChild(tr);
  });
  listBody.textContent='';
  listBody.appendChild(frag);
}

async function load(){
  try{
    var r=await fetch('/api/changes/'+jid+(ref?('?ref='+encodeURIComponent(ref)):''));
    if(!r.ok){return;}
    var d=await r.json();
    if(!d.ok){paintFiles([]);totEl.textContent='';howEl.textContent='';say(d.error||'Nothing to show yet.','mut');return;}
    var k=JSON.stringify(d.refs.map(function(x){return x.ref;}));
    if(k!==refsKey){refsKey=k;
      sel.innerHTML=d.refs.map(function(x){return '<option value="'+esc(x.ref)+'">'+esc(x.label)+'</option>';}).join('');}
    sel.value=d.ref;
    howEl.textContent=d.how;
    totEl.textContent=(d.totals.files===1?'1 file':d.totals.files+' files')+' changed: +'
      +d.totals.added+' / -'+d.totals.removed+(d.more?(' - '+(d.more)+' more not listed'):'');
    paintFiles(d.files);
    // keep the open diff unless the file itself moved (then re-read it)
    if(openName){
      var f=null;
      for(var i=0;i<d.files.length;i++){if(d.files[i].path===openName){f=d.files[i];break;}}
      if(!f){openName='';say('That file is not in this view any more.','mut');}
      else if(f.added+':'+f.removed!==openTag){getDiff(openName);}
    }
  }catch(e){}
}

sel.onchange=function(){ ref=(sel.value==='all')?'':sel.value; diff=null; openName=''; openTag=''; load(); };
reloadBtn.onclick=function(){ load(); };
load();
if(live){
  setInterval(load,10000);                                 // the job may still be writing
  document.addEventListener('jobactivity',function(){      // ... or the live stream just moved
    if(busy){return;} busy=true; setTimeout(function(){busy=false;load();},1500);
  });
}
})();
"""
