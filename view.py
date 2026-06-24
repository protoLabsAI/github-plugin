"""The GitHub board console view — a self-contained page served at
``/plugins/github/view`` (by api.py's view router) and iframed by the console.

A quick read of a project's board: two tabs (Issues / Pull Requests) over a repo
picker sourced from the plugin's configured ``github.repos`` (the same registration
the ``/issue`` command uses), an open/closed/all state filter, and a "New issue" form
that posts through the SAME gate-checked path as ``/issue``.

FOUR-RULES COMPLIANT (docs/how-to/build-a-plugin-view.md, the notes/chat_example
pattern): served on the PUBLIC path · DATA is the gated channel (the DS kit's
``apiFetch`` attaches the operator bearer from the postMessage handshake) · slug-aware
base (host window AND the fleet ``/agents/<slug>`` proxy) · links the DS plugin-kit so
the whole page is themed from the operator's live ``--pl-*`` tokens (no host build —
vanilla JS, git-installable per ADR 0038).
"""

from __future__ import annotations

PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GitHub</title>
<script>
  "use strict";
  // Slug-aware base, computed FIRST (the kit's own assets load before the kit exists).
  window.__base = location.pathname.split("/plugins/")[0];
  document.write('<link rel="stylesheet" href="' + window.__base + '/_ds/plugin-kit.css">');
</script>
<style>
  /* Layout only — colours/type come from plugin-kit.css's --pl-* tokens (re-skinned to
     the operator's live theme by plugin-kit.js on the handshake). */
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
  #panel{display:none;border-bottom:1px solid var(--pl-color-border);padding:10px;background:var(--pl-color-bg)}
  #panel.on{display:block}
  #panel input,#panel textarea,#panel select{width:100%;margin:4px 0;background:var(--pl-color-bg-raised);
    color:var(--pl-color-fg);border:1px solid var(--pl-color-border);border-radius:var(--pl-radius,8px);padding:6px;font-size:12px}
  #panel textarea{min-height:120px;resize:vertical;font-family:var(--pl-font-mono,ui-monospace,Menlo,monospace)}
  .prow{display:flex;gap:6px;align-items:center}
  #res{font-size:11px;margin-top:4px;color:var(--pl-color-fg-muted);white-space:pre-wrap}
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
    <button class="pl-btn pl-btn--sm" id="new" type="button">New issue</button>
    <button class="pl-btn pl-btn--sm" id="refresh" type="button" title="Refresh">↻</button>
  </div>
  <div id="panel">
    <div class="prow">
      <input id="f-title" placeholder="Issue title" />
      <select id="f-kind" style="max-width:130px"><option value="generic">Generic</option><option value="bug">Bug</option><option value="feature">Feature</option></select>
    </div>
    <textarea id="f-body" placeholder="## Problem&#10;What's wrong or what you want, and why it matters.&#10;&#10;## Acceptance&#10;How we'll know it's done."></textarea>
    <div class="prow">
      <button class="pl-btn pl-btn--sm" id="f-submit" type="button">File issue</button>
      <button class="pl-btn pl-btn--sm" id="f-cancel" type="button">Cancel</button>
      <span id="res"></span>
    </div>
  </div>
  <div id="list"><div class="hint">Loading…</div></div>
</div>
<script type="module">
  "use strict";
  // plugin-kit.js is an ES module; dynamic import is the no-build way to load it with a
  // slug-aware URL. Older host with no /_ds route → tokenless same-origin shim (local dev).
  let kit;
  try { kit = await import(window.__base + "/_ds/plugin-kit.js"); }
  catch (e) { kit = { initPluginView(cb){ cb && cb(); }, apiFetch:(p,i)=>fetch(window.__base+p,i) }; }

  const $ = (id) => document.getElementById(id);
  let tab = "issues", cfg = { repos: [], default_repo: "", gh_available: true };
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
    const repo = $("repo").value;
    const list = $("list");
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

  async function submitIssue(){
    const repo = $("repo").value, title = $("f-title").value.trim();
    if(!title){ $("res").textContent = "Title is required."; return; }
    $("res").textContent = "Filing…";
    try {
      const r = await kit.apiFetch("/api/plugins/github/issue", {
        method:"POST", headers:{"Content-Type":"application/json"},
        body: JSON.stringify({ repo, title, body: $("f-body").value, kind: $("f-kind").value })
      }).then(x => x.json());
      if(r.ok && r.url){ $("res").textContent = "Filed ✓"; $("f-title").value=""; $("f-body").value=""; $("panel").classList.remove("on"); if(tab==="issues") load(); }
      else if(r.missing){ $("res").textContent = "Missing: "+r.missing.join("; "); }
      else { $("res").textContent = r.error || "Could not file."; }
    } catch(e){ $("res").textContent = "Request failed."; }
  }

  async function boot(){
    try { cfg = await kit.apiFetch("/api/plugins/github/config").then(r => r.json()); } catch(e){}
    const sel = $("repo");
    sel.innerHTML = (cfg.repos||[]).map(r => '<option value="'+esc(r)+'">'+esc(r)+'</option>').join("");
    if(cfg.default_repo){ sel.value = cfg.default_repo; }
    load();
  }

  $("t-issues").onclick = () => setTab("issues");
  $("t-prs").onclick = () => setTab("prs");
  $("repo").onchange = load; $("state").onchange = load; $("refresh").onclick = load;
  $("new").onclick = () => { $("panel").classList.toggle("on"); $("res").textContent=""; if($("panel").classList.contains("on")) $("f-title").focus(); };
  $("f-cancel").onclick = () => $("panel").classList.remove("on");
  $("f-submit").onclick = submitIssue;

  // The kit owns the handshake (bearer + theme, incl. live re-themes); re-boot when the
  // token first arrives on a gated instance (the immediate boot() covers tokenless local).
  kit.initPluginView(boot);
  boot();
</script></body></html>"""
