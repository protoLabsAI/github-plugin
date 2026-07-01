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
  .bar{display:flex;align-items:center;gap:6px;padding:6px 8px;flex-wrap:wrap;
    border-bottom:var(--pl-border-width,1px) solid var(--pl-color-border)}
  .tabs{display:inline-flex;border:1px solid var(--pl-color-border);border-radius:var(--pl-radius,8px);overflow:hidden}
  .tab{padding:4px 11px;cursor:pointer;background:transparent;border:0;color:var(--pl-color-fg-muted);
    font-size:12px;line-height:1.6;white-space:nowrap}
  .tab[aria-selected="true"]{background:var(--pl-color-bg);color:var(--pl-color-fg);font-weight:600}
  select{background:var(--pl-color-bg);color:var(--pl-color-fg);border:1px solid var(--pl-color-border);
    border-radius:var(--pl-radius,8px);padding:4px 6px;font-size:12px;max-width:220px}
  .ico{width:1em;height:1em;flex:none;vertical-align:-0.15em}
  #refresh{display:inline-flex;align-items:center;justify-content:center;padding:4px 7px;color:var(--pl-color-fg-muted)}
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
  .cmt{display:inline-flex;align-items:center;gap:3px}
  .pill{font-size:10px;padding:1px 7px;border-radius:999px;border:1px solid var(--pl-color-border)}
  .empty,.hint{padding:24px 14px;text-align:center;color:var(--pl-color-fg-muted)}
</style></head><body>
<div id="wrap">
  <div class="bar">
    <select id="repo" title="Repository"></select>
    <div class="tabs" role="tablist">
      <button class="tab" id="t-issues" role="tab" aria-selected="true" title="Issues">Issues</button>
      <button class="tab" id="t-prs" role="tab" aria-selected="false" title="Pull Requests">PRs</button>
    </div>
    <select id="state" title="State"><option value="open">Open</option><option value="closed">Closed</option><option value="all">All</option></select>
    <span class="spacer"></span>
    <button class="pl-btn pl-btn--sm" id="refresh" type="button" title="Refresh" aria-label="Refresh"></button>
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
  // Inline Lucide (v0.468) SVGs — themed via currentColor, no runtime dep / kit icon API.
  const svg = (paths) => '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'+paths+'</svg>';
  const ICON = {
    comment: svg('<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>'),
    refresh: svg('<path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/>'),
  };

  function setTab(t){ tab = t; $("t-issues").setAttribute("aria-selected", t==="issues"); $("t-prs").setAttribute("aria-selected", t==="prs"); load(); }
  function labelPills(labels){ return (labels||[]).slice(0,5).map(l => '<span class="pill">'+esc(l.name||l)+'</span>').join(" "); }

  function issueRow(it){
    const st = (it.state||"").toLowerCase();
    // `gh issue list --json comments` returns an ARRAY of comment objects, not a count —
    // rendering it directly was the "[object Object]" bug (#16). Show its length.
    const cc = Array.isArray(it.comments) ? it.comments.length : (Number(it.comments)||0);
    return '<a class="row" href="'+esc(it.url)+'" target="_blank" rel="noreferrer">'
      + '<div class="top"><span class="dot '+(st==="closed"?"closed":"open")+'"></span>'
      + '<span class="title">'+esc(it.title)+' <span class="num">#'+esc(it.number)+'</span></span></div>'
      + '<div class="meta"><span>'+esc((it.author&&it.author.login)||"?")+'</span><span>'+fmtDate(it.createdAt)+'</span>'
      + (cc?('<span class="cmt" title="'+esc(cc)+' comment'+(cc===1?'':'s')+'">'+ICON.comment+esc(cc)+'</span>'):'')+' '+labelPills(it.labels)+'</div></a>';
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

  // A monotonic token so an in-flight load() whose fetch resolves AFTER a newer load()
  // started (rapid tab/filter clicks, or any double-trigger) drops its stale result instead
  // of clobbering the fresh list — no thrash.
  let loadSeq = 0;
  async function load(){
    const my = ++loadSeq;
    const repo = $("repo").value, list = $("list");
    if(!repo){ list.innerHTML = '<div class="hint">No repositories configured. Add some under <b>Settings ▸ GitHub</b> (github.repos).</div>'; return; }
    list.innerHTML = '<div class="hint">Loading…</div>';
    const path = (tab==="issues"?"/api/plugins/github/issues":"/api/plugins/github/prs")
      + "?repo="+encodeURIComponent(repo)+"&state="+encodeURIComponent($("state").value);
    try {
      const data = await kit.apiFetch(path).then(r => r.json());
      if(my !== loadSeq) return;  // a newer load() superseded this one — drop the stale result
      if(data.error){ list.innerHTML = '<div class="empty">'+esc(data.error)+'</div>'; return; }
      const items = data.items||[];
      if(!items.length){ list.innerHTML = '<div class="empty">No '+tab+' for this filter.</div>'; return; }
      list.innerHTML = items.map(tab==="issues"?issueRow:prRow).join("");
    } catch(e){ if(my===loadSeq) list.innerHTML = '<div class="empty">Failed to load — is the agent reachable?</div>'; }
  }

  let booted = false;
  async function boot(){
    if (booted) return;  // the kit re-fires this on every re-theme + the handshake re-send;
    booted = true;       // build the picker and first-load EXACTLY once, or the list thrashes (#15).
    let cfg = { repos: [], default_repo: "" };
    try { cfg = await kit.apiFetch("/api/plugins/github/config").then(r => r.json()); } catch(e){}
    const sel = $("repo");
    sel.innerHTML = (cfg.repos||[]).map(r => '<option value="'+esc(r)+'">'+esc(r)+'</option>').join("");
    if(cfg.default_repo){ sel.value = cfg.default_repo; }
    load();
  }

  $("refresh").innerHTML = ICON.refresh;
  $("t-issues").onclick = () => setTab("issues");
  $("t-prs").onclick = () => setTab("prs");
  $("repo").onchange = load; $("state").onchange = load; $("refresh").onclick = load;
  // Boot via the kit so it runs after the theme/auth handshake (the fallback kit calls it
  // immediately). initPluginView's callback fires on the initial init AND every re-theme, so
  // boot() itself is guarded (booted) to run once — that's what actually kills the mount
  // thrash; a second direct boot() call was removed in #13.
  kit.initPluginView(boot);
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

  let booted = false;
  async function boot(){
    if (booted) return;  // kit re-fires on every re-theme; populate the picker once so it never
    booted = true;       // clobbers an in-progress repo selection (#15).
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
  // Boot ONCE via the kit (after the theme/auth handshake) — not also directly (#13).
  kit.initPluginView(boot);
</script></body></html>"""
