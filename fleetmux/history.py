"""Retired generations of every agent, as one searchable page.

Each rotation appends a line to ``<run>/history.jsonl`` (which slot, which label retired, where its
screen snapshot and handoff note are). ``write_page`` renders ``<run>/history.html``: pick an agent
(supervisor-1 / worker-1 / ...), pick a generation (supervisor-1, supervisor-1.1, ...), read its screen or
its handoff note, or search all of them at once. The page is self-contained (data embedded), so the
cmux browser pane only needs a reload after each rotation; the selection lives in the URL hash.
Sub-fleets (design.md §19): ``links`` adds the sub-fleet list (state + jump) and, on a child's page,
'back to the parent fleet'. A click reloads this page with an explicit ``?fleet-jump=…&rel=…&to=<target page>`` marker that the daemon
turns into a cmux workspace switch (``subfleet.follow_jump``); a plain reload carries no marker.
"""
import json
import os
import time
from pathlib import Path

from fleetmux import terminal_theme

SNAPSHOT_LIMIT = 1_500_000  # chars kept per screen snapshot (the tail; scrollback can be huge)


def page_path(run_dir):
    return Path(run_dir) / 'history.html'


def page_url(run_dir):
    return page_path(run_dir).resolve().as_uri()


def record(run_dir, **entry):
    entry.setdefault('at', time.time())
    with (Path(run_dir) / 'history.jsonl').open('a') as f:
        f.write(json.dumps(entry, ensure_ascii=False) + '\n')


def _read(path, limit=None):
    try:
        text = Path(path).read_text(errors='replace')
    except (OSError, TypeError):
        return ''
    return text[-limit:] if limit and len(text) > limit else text


def entries(run_dir):
    out = []
    try:
        lines = (Path(run_dir) / 'history.jsonl').read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def write_page(run_dir, slots, links=None):
    """``slots``: [(slot, live label, title)] in display order (title = the slot's first-generation name).

    ``links``: ``subfleet.page_links`` ({'parent': {...}|None, 'children': [...]}); None = no links row."""
    data = []
    rows = entries(run_dir)
    for slot, live, title in slots:
        gens = [{'label': e.get('label'), 'generation': e.get('generation'), 'at': e.get('at'),
                 'source': e.get('source'), 'timed_out': bool(e.get('timed_out')),
                 'screen': _read(e.get('snapshot'), SNAPSHOT_LIMIT), 'note': _read(e.get('handoff'))}
                for e in rows if e.get('slot') == slot]
        cards = _read(Path(run_dir) / 'cards' / (slot + '.log'), SNAPSHOT_LIMIT)  # tui_cards (official CLIs)
        data.append({'slot': slot, 'title': title, 'live': live, 'gens': gens, 'cards': cards})
    links = links or {}
    blob = json.dumps({'run': str(run_dir), 'updated': time.time(), 'slots': data,
                       'parent': links.get('parent'), 'children': links.get('children') or []},
                      ensure_ascii=False)
    blob = blob.replace('<', '\\u003c')  # no </script>, <!-- or <script inside the script block
    theme_css = terminal_theme.css(terminal_theme.read(run_dir) or {})
    style = '<style id="terminal-theme">%s</style>' % theme_css if theme_css else ''
    path = page_path(run_dir)
    tmp = path.with_suffix('.tmp')
    # Always replaced, never left behind: the placeholder is not valid CSS and the page must not
    # contain '<!--' (test_history_page_escapes_script_end). No theme file -> empty string.
    tmp.write_text(PAGE.replace('/*DATA*/null', blob).replace('<!--THEME-->', style))
    os.replace(tmp, path)
    return path


PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>Fleet history</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{--bg:#fbfbfa;--fg:#1d1d1b;--muted:#6b6b66;--line:#e2e1dc;--panel:#f2f1ed;--accent:#2f5d8a;--mark:#ffe08a;--cur:#ff9f43}
@media (prefers-color-scheme:dark){:root{--bg:#171716;--fg:#e8e6e1;--muted:#9a978f;--line:#2e2d2a;--panel:#201f1d;--accent:#7fb0e0;--mark:#6b5a1a;--cur:#b8641c}}
*{box-sizing:border-box}html,body{height:100%;margin:0}
body{background:var(--bg);color:var(--fg);font:13px/1.4 -apple-system,system-ui,sans-serif;display:flex;flex-direction:column}
header{display:flex;flex-wrap:wrap;gap:6px;align-items:center;padding:6px 8px;border-bottom:1px solid var(--line)}
button{font:inherit;color:inherit;background:var(--panel);border:1px solid var(--line);border-radius:5px;padding:3px 9px;cursor:pointer}
button.on{border-color:var(--accent);color:var(--accent);font-weight:600}
input{font:inherit;color:inherit;background:var(--bg);border:1px solid var(--line);border-radius:5px;padding:3px 8px;min-width:180px;flex:1}
.muted{color:var(--muted)}
main{flex:1;display:flex;min-height:0}
nav{width:230px;border-right:1px solid var(--line);overflow:auto;flex-shrink:0}
nav div{padding:5px 9px;border-bottom:1px solid var(--line);cursor:pointer}
nav div.on{background:var(--panel);border-left:3px solid var(--accent)}
nav b{display:block}
section{flex:1;display:flex;flex-direction:column;min-width:0}
section header{border-bottom:1px solid var(--line)}
pre{flex:1;margin:0;padding:8px 10px;overflow:auto;font:12px/1.35 ui-monospace,Menlo,monospace;white-space:pre-wrap;word-break:break-word}
mark{background:var(--mark);color:inherit}mark.cur{background:var(--cur)}
.empty{padding:16px}
#fleets a{color:var(--accent);text-decoration:none;border:1px solid var(--line);border-radius:5px;padding:3px 9px}
#fleets a:hover{border-color:var(--accent)}
#fleets:empty{display:none}
</style><!--THEME--></head><body>
<header id="fleets"></header>
<header id="agents"></header>
<header><input id="q" placeholder="Search every generation of every agent (Enter = next match)">
<span id="hits" class="muted"></span><button id="prev">&uarr;</button><button id="next">&darr;</button></header>
<main><nav id="gens"></nav>
<section><header id="views"><button data-v="screen">Screen</button><button data-v="note">Handoff note</button><button data-v="cards">Cards</button>
<span id="title" class="muted"></span></header><pre id="text"></pre></section></main>
<script>
const DATA=/*DATA*/null;
const st=Object.assign({agent:DATA.slots.length?DATA.slots[0].slot:'',gen:'',view:'screen',q:''},
  Object.fromEntries(new URLSearchParams(location.hash.slice(1))));
let marks=[],cur=-1;
const $=id=>document.getElementById(id);
const esc=s=>s.replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const when=t=>t?new Date(t*1000).toLocaleString():'';
const count=(s,q)=>{if(!q)return 0;let n=0,i=0;s=s.toLowerCase();while((i=s.indexOf(q,i))>=0){n++;i+=q.length}return n};
function save(){location.replace('#'+new URLSearchParams(st).toString())}
function slot(){return DATA.slots.find(s=>s.slot===st.agent)}
function rows(){
  const q=st.q.toLowerCase();
  if(q){const out=[];for(const s of DATA.slots)for(const g of s.gens){const n=count(g.screen,q)+count(g.note,q);if(n)out.push([s,g,n])}return out}
  const s=slot();return s?s.gens.slice().reverse().map(g=>[s,g,0]):[];
}
function fleets(){  // a click asks this fleet's daemon to switch to the other fleet's workspace (subfleet.follow_jump)
  const q=s=>esc(String(s)).replace(/"/g,'&quot;');
  const a=(href,rel,text,tip)=>`<a href="${q(href)}" data-rel="${rel}" title="${q(tip||'')}">${q(text)}</a>`;
  const p=DATA.parent,kids=DATA.children||[];
  const going=new URLSearchParams(location.search).has('fleet-jump');
  $('fleets').innerHTML=(going?'<span class="muted">正在切到对应工作区…</span>':'')+
    (p?a(p.url,'parent','← 回到父 fleet'+(p.task_id?' '+p.task_id:''),'切到侧栏：'+p.title)
      +`<span class="muted">${q(p.title)}</span>`:'')+
    (kids.length?`<span class="muted">子 fleet ${kids.length}：</span>`+kids.map(c=>a(c.url,'child',c.task_id+
      (c.attempt>1?' #'+c.attempt:'')+' · '+(c.state||'?')+(c.outcome&&c.outcome!==c.state?' ('+c.outcome+')':'')+' · '+(c.slot||''),
      '切到侧栏：'+c.title)).join(''):'');
}
function render(){
  $('agents').innerHTML=DATA.slots.map(s=>`<button data-a="${s.slot}" class="${s.slot===st.agent&&!st.q?'on':''}">${s.title||s.slot}
    <span class="muted">· ${s.gens.length} retired · live ${s.live}</span></button>`).join('')+
    `<span class="muted">updated ${when(DATA.updated)}</span>`;
  const list=rows();
  if(!list.find(([s,g])=>s.slot===st.agent&&g.label===st.gen)&&list.length){st.agent=list[0][0].slot;st.gen=list[0][1].label}
  $('gens').innerHTML=list.length?list.map(([s,g,n])=>`<div data-a="${s.slot}" data-g="${g.label}" class="${s.slot===st.agent&&g.label===st.gen?'on':''}">
    <b>${g.label}${n?` <span class="muted">(${n})</span>`:''}</b><span class="muted">${when(g.at)} · note: ${g.source}${g.timed_out?' (timed out)':''}</span></div>`).join('')
    :`<div class="empty muted">${st.q?'No match.':'No retired generation yet. The live session is '+(slot()?slot().live:'')+'.'}</div>`;
  const s=slot(),g=s&&s.gens.find(x=>x.label===st.gen);
  const ql=st.q.toLowerCase();  // show the half that holds the matches
  if(g&&ql&&st.view!=='cards'&&!count(st.view==='note'?g.note:g.screen,ql))st.view=st.view==='note'?'screen':'note';
  for(const b of document.querySelectorAll('#views button'))b.className=b.dataset.v===st.view?'on':'';
  const cards=st.view==='cards';  // UI-only result cards of an official-CLI slot (live, not per generation)
  $('title').textContent=cards?`${s?s.title||s.slot:''} result cards`:g?`${g.label} retired ${when(g.at)}`:'';
  const text=cards?(s&&s.cards)||'(no cards)':g?(st.view==='note'?g.note:g.screen)||'(empty)':'';
  const q=st.q;
  if(q){const re=new RegExp(q.replace(/[.*+?^${}()|[\]\\]/g,'\\$&'),'gi');let out='',last=0,m;
    while((m=re.exec(text))){out+=esc(text.slice(last,m.index))+'<mark>'+esc(m[0])+'</mark>';last=m.index+m[0].length}
    $('text').innerHTML=out+esc(text.slice(last));}
  else $('text').textContent=text;
  marks=[...document.querySelectorAll('#text mark')];cur=-1;
  $('hits').textContent=q?`${list.reduce((a,r)=>a+r[2],0)} matches in ${list.length} generation(s)`:'';
  if(marks.length)step(1);else if(!q)$('text').scrollTop=$('text').scrollHeight;
  save();
}
function step(d){if(!marks.length)return;if(cur>=0)marks[cur].classList.remove('cur');
  cur=(cur+d+marks.length)%marks.length;marks[cur].classList.add('cur');marks[cur].scrollIntoView({block:'center'})}
document.addEventListener('click',e=>{const l=e.target.closest('#fleets a[data-rel]');if(!l)return;e.preventDefault();
  // stay on this file (WebKit may not read a file outside this page's directory); the daemon reads the marker
  location.href=location.href.split(/[?#]/)[0]+'?'+new URLSearchParams({'fleet-jump':Date.now().toString(36)+'-'+
    Math.random().toString(36).slice(2,8),rel:l.dataset.rel,to:l.getAttribute('href')})+location.hash});
document.addEventListener('click',e=>{const a=e.target.closest('[data-a]'),v=e.target.closest('[data-v]');
  if(a&&a.dataset.g){st.agent=a.dataset.a;st.gen=a.dataset.g;render()}
  else if(a){st.agent=a.dataset.a;st.gen='';st.q='';$('q').value='';render()}
  else if(v){st.view=v.dataset.v;render()}});
fleets();
$('q').value=st.q;
$('q').addEventListener('input',()=>{st.q=$('q').value;render()});
$('q').addEventListener('keydown',e=>{if(e.key==='Enter')step(e.shiftKey?-1:1)});
$('next').onclick=()=>step(1);$('prev').onclick=()=>step(-1);
render();
</script></body></html>
"""
