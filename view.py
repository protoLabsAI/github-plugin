"""The GitHub console pages — served at ``/plugins/github/*`` (by api.py) and iframed
by the console. Two pages, two surfaces:

- ``PAGE`` (``/view``) — the **read-only board**: a quick look at a project's board,
  two tabs (Issues / Pull Requests) over a repo picker (the configured ``github.repos``)
  + an open/closed/all filter. No writes — it's a viewer. Declared in the manifest as a
  right-dock view AND a ⌘K palette morph (``palette: inline``).
- ``NEW_ISSUE_PAGE`` (``/new-issue``) — the compact **file-an-issue** form (title / kind /
  repo / body → the gate-checked ``POST /issue``). Declared as a util-bar widget
  (``utility``) AND a distinct ⌘K palette page (``palette: { path }``) — so filing lives
  in the util bar + the palette, NOT in the board (the "view is read-only" split).

FOUR-RULES COMPLIANT (docs/how-to/build-a-plugin-view.md): served on the PUBLIC path;
DATA is the gated channel (the DS kit's ``apiFetch`` attaches the operator bearer from
the postMessage handshake); slug-aware base (host window AND the fleet proxy); links the
DS plugin-kit so the page is themed from the operator's live ``--pl-*`` tokens. Vanilla
JS, no host build (ADR 0038).
"""

from __future__ import annotations

# --- the read-only board -----------------------------------------------------
PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GitHub</title>
<script>
  "use strict";
  window.__base = location.pathname.split("/plugins/")[0];
  document.write('<link rel="stylesheet" href="' + window.__base + '/_ds/plugin-kit.css">');
</script>
<style>
  *{box-sizing:border-box}
  html,body{margin:0;height:100%;background:var(--pl-color-bg-raised);color:var(--pl-color-fg);
    font-family:var(--pl-font-sans,ui-sans-serif,system-ui,sans-serif);font-size:13px}
  #wrap{display:flex;flex-direction:column;height:100%;min-height:0}
  .bar{display:flex;align-items:center;gap:8px;padding:8px 10px;flex-wrap:wrap;
    border-bottom:var(--pl-border-width,1px) solid var(--pl-color-border)}
  .tabs{display:inline-flex;border:1px solid var(--pl-color-border);border-radius:var(--pl-radius,8px);overflow:hidden}
  .tab{padding:5px 12px;cursor:pointer;background:transparent;border:0;color:var(--pl-color-fg-muted);font-size:12px}
  .tab[aria-selected="true"]{background:var(--pl-color-bg);color:var(--pl-color-fg);font-weight:600}
  select{background:var(--pl-color-bg);color:var(--pl-color-fg);border:1px solid var(--pl-color-border);
    border-radius:var(--pl-radius,8px);padding:4px 6px;font-size:12px;max-width:240px}
  .spacer{flex:1}
  #list{flex:1;min-height:0;overflow:auto;padding:4px 0}
  .row{display:block;text-decoration:none;color:inherit;padding:9px 12px;
    border-bottom:1px solid var(--pl-color-border)}
  .row:hover{background:var(--pl-color-bg)}
  .row .top{display:flex;align-items:baseline;gap:7px}
  .dot{width:9px;height:9px;border-radius:50%;flex:none;margin-top:4px;background:var(--pl-color-fg-muted)}
  .dot.open{background:#3fb950}.dot.closed{background:#a371f7}.dot.merged{background:#a371f7}.dot.draft{background:var(--pl-color-fg-muted)}
  .title{font-weight:600;line-height:1.35}
  .num{color:var(--pl-color-fg-muted);font-weight:400}
  .meta{margin-top:3px;font-size:11px;color:var(--pl-color-fg-muted);display:flex;gap:6px;flex-wrap:wrap;align-items:center}
  .pill{font-size:10px;padding:1px 7px;border-radius:999px;border:1px solid var(--pl-color-border)}
  .empty,.hint{padding:24px 14px;text-align:center;color:var(--pl-color-fg-muted)}
</style></head><body>
<div id="wrap">
  <div class="bar">
    <select id="repo" title="Repository"></select>
    <div class="tabs" role="tablist">
      <button class="tab" id="t-issues" role="tab" aria-selected="true">Issues</button>
      <button class="tab" id="t-prs" role="tab" aria-selected="false">Pull Requests</button>
    </div>
    <select id="state" title="State"><option value="open">Open</option><option value="closed">Closed</option><option value="all">All</option></select>
    <span class="spacer"></span>
    <button class="pl-btn pl-btn--sm" id="refresh" type="button" title="Refresh">↻</button>
  </div>
  <div id="list"><div class="hint">Loading…</div></div>
</div>
<script type="module">
  "use strict";
  let kit;
  try { kit = await import(window.__base + "/_ds/plugin-kit.js"); }
  catch (e) { kit = { initPluginView(cb){ cb && cb(); }, apiFetch:(p,i)=>fetch(window.__base+p,i) }; }

  const $ = (id) => document.getElementById(id);
  let tab = "issues";
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) => ({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;" }[c]));
  const fmtDate = (iso) => { try { return new Date(iso).toLocaleDateString(undefined,{month:"short",day:"numeric",year:"numeric"}); } catch(e){ return ""; } };

  function setTab(t){ tab = t; $("t-issues").setAttribute("aria-selected", t==="issues"); $("t-prs").setAttribute("aria-selected", t==="prs"); load(); }
  function labelPills(labels){ return (labels||[]).slice(0,5).map(l => '<span class="pill">'+esc(l.name||l)+'</span>').join(" "); }

  function issueRow(it){
    const st = (it.state||"").toLowerCase();
    return '<a class="row" href="'+esc(it.url)+'" target="_blank" rel="noreferrer">'
      + '<div class="top"><span class="dot '+(st==="closed"?"closed":"open")+'"></span>'
      + '<span class="title">'+esc(it.title)+' <span class="num">#'+esc(it.number)+'</span></span></div>'
      + '<div class="meta"><span>'+esc((it.author&&it.author.login)||"?")+'</span><span>'+fmtDate(it.createdAt)+'</span>'
      + (it.comments?('<span>💬 '+esc(it.comments)+'</span>'):'')+' '+labelPills(it.labels)+'</div></a>';
  }
  function prRow(it){
    const merged = (it.state||"").toLowerCase()==="merged", draft = !!it.isDraft;
    const review = it.reviewDecision ? String(it.reviewDecision).toLowerCase().replace(/_/g," ") : "";
    return '<a class="row" href="'+esc(it.url)+'" target="_blank" rel="noreferrer">'
      + '<div class="top"><span class="dot '+(merged?"merged":(draft?"draft":"open"))+'"></span>'
      + '<span class="title">'+esc(it.title)+' <span class="num">#'+esc(it.number)+'</span></span></div>'
      + '<div class="meta"><span>'+esc((it.author&&it.author.login)||"?")+'</span><span>'+fmtDate(it.createdAt)+'</span>'
      + (it.headRefName?('<span class="pill">'+esc(it.headRefName)+'</span>'):'')
      + (draft?'<span class="pill">draft</span>':'')+(review?('<span class="pill">'+esc(review)+'</span>'):'')
      + ' '+labelPills(it.labels)+'</div></a>';
  }

  async function load(){
    const repo = $("repo").value, list = $("list");
    if(!repo){ list.innerHTML = '<div class="hint">No repositories configured. Add some under <b>Settings ▸ GitHub</b> (github.repos).</div>'; return; }
    list.innerHTML = '<div class="hint">Loading…</div>';
    const path = (tab==="issues"?"/api/plugins/github/issues":"/api/plugins/github/prs")
      + "?repo="+encodeURIComponent(repo)+"&state="+encodeURIComponent($("state").value);
    try {
      const data = await kit.apiFetch(path).then(r => r.json());
      if(data.error){ list.innerHTML = '<div class="empty">'+esc(data.error)+'</div>'; return; }
      const items = data.items||[];
      if(!items.length){ list.innerHTML = '<div class="empty">No '+tab+' for this filter.</div>'; return; }
      list.innerHTML = items.map(tab==="issues"?issueRow:prRow).join("");
    } catch(e){ list.innerHTML = '<div class="empty">Failed to load — is the agent reachable?</div>'; }
  }

  async function boot(){
    let cfg = { repos: [], default_repo: "" };
    try { cfg = await kit.apiFetch("/api/plugins/github/config").then(r => r.json()); } catch(e){}
    const sel = $("repo");
    sel.innerHTML = (cfg.repos||[]).map(r => '<option value="'+esc(r)+'">'+esc(r)+'</option>').join("");
    if(cfg.default_repo){ sel.value = cfg.default_repo; }
    load();
  }

  $("t-issues").onclick = () => setTab("issues");
  $("t-prs").onclick = () => setTab("prs");
  $("repo").onchange = load; $("state").onchange = load; $("refresh").onclick = load;
  kit.initPluginView(boot);
  boot();
</script></body></html>"""


# --- the compact "file an issue" page (util-bar widget + ⌘K palette) ---------
NEW_ISSUE_PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>New issue</title>
<script>
  "use strict";
  window.__base = location.pathname.split("/plugins/")[0];
  document.write('<link rel="stylesheet" href="' + window.__base + '/_ds/plugin-kit.css">');
</script>
<style>
  *{box-sizing:border-box}
  html,body{margin:0;height:100%;background:var(--pl-color-bg-raised);color:var(--pl-color-fg);
    font-family:var(--pl-font-sans,ui-sans-serif,system-ui,sans-serif);font-size:13px}
  #wrap{display:flex;flex-direction:column;gap:6px;padding:12px;height:100%;min-height:0}
  .row{display:flex;gap:6px;align-items:center}
  input,textarea,select{width:100%;background:var(--pl-color-bg);color:var(--pl-color-fg);
    border:1px solid var(--pl-color-border);border-radius:var(--pl-radius,8px);padding:7px;font-size:12px}
  textarea{flex:1;min-height:140px;resize:vertical;font-family:var(--pl-font-mono,ui-monospace,Menlo,monospace)}
  #res{font-size:11px;color:var(--pl-color-fg-muted);white-space:pre-wrap;min-height:16px}
</style></head><body>
<div id="wrap">
  <div class="row">
    <select id="repo" title="Repository"></select>
    <select id="kind" style="max-width:130px"><option value="generic">Generic</option><option value="bug">Bug</option><option value="feature">Feature</option></select>
  </div>
  <input id="title" placeholder="Issue title" autofocus />
  <textarea id="body" placeholder="## Problem&#10;What's wrong or what you want, and why it matters.&#10;&#10;## Acceptance&#10;How we'll know it's done."></textarea>
  <div class="row">
    <button class="pl-btn pl-btn--sm" id="submit" type="button">File issue</button>
    <span class="spacer" style="flex:1"></span>
    <span id="res"></span>
  </div>
</div>
<script type="module">
  "use strict";
  let kit;
  try { kit = await import(window.__base + "/_ds/plugin-kit.js"); }
  catch (e) { kit = { initPluginView(cb){ cb && cb(); }, apiFetch:(p,i)=>fetch(window.__base+p,i) }; }
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) => ({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;" }[c]));

  async function boot(){
    let cfg = { repos: [], default_repo: "" };
    try { cfg = await kit.apiFetch("/api/plugins/github/config").then(r => r.json()); } catch(e){}
    $("repo").innerHTML = (cfg.repos||[]).map(r => '<option value="'+esc(r)+'">'+esc(r)+'</option>').join("");
    if(cfg.default_repo){ $("repo").value = cfg.default_repo; }
  }
  async function submit(){
    const title = $("title").value.trim();
    if(!title){ $("res").textContent = "Title is required."; return; }
    $("res").textContent = "Filing…";
    try {
      const r = await kit.apiFetch("/api/plugins/github/issue", {
        method:"POST", headers:{"Content-Type":"application/json"},
        body: JSON.stringify({ repo: $("repo").value, title, body: $("body").value, kind: $("kind").value })
      }).then(x => x.json());
      if(r.ok && r.url){ $("res").innerHTML = 'Filed ✓ <a href="'+esc(r.url)+'" target="_blank" rel="noreferrer">view</a>'; $("title").value=""; $("body").value=""; }
      else if(r.missing){ $("res").textContent = "Missing: "+r.missing.join("; "); }
      else { $("res").textContent = r.error || "Could not file."; }
    } catch(e){ $("res").textContent = "Request failed."; }
  }
  $("submit").onclick = submit;
  kit.initPluginView(boot);
  boot();
</script></body></html>"""
