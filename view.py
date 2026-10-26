"""The GitHub console pages — served at ``/plugins/github/*`` (by api.py) and iframed
by the console. Two pages, shared across console surfaces:

- ``PAGE`` (``/view`` and ``/workspace``) — the **read-only board**: a quick look at a project's board,
  two tabs (Issues / Pull Requests) over a repo picker (the configured ``github.repos``)
  + an open/closed/all filter. No writes — it's a viewer. Declared in the manifest as a
  right-dock view AND a ⌘K palette morph (``palette: inline``), plus a full-width rail view.
- ``NEW_ISSUE_PAGE`` (``/new-issue``) — the compact **file-an-issue** form (title / kind /
  repo / body → the gate-checked ``POST /issue``). Declared as a util-bar widget
  (``utility``) AND a distinct ⌘K palette page (``palette: { path }``) — so filing lives
  in the util bar + the palette, NOT in the board (the "view is read-only" split).

FOUR-RULES COMPLIANT (docs/how-to/build-a-plugin-view.md): served on the PUBLIC path;
DATA is the gated channel (the DS kit's ``apiFetch`` attaches the operator bearer from
the postMessage handshake); slug-aware base (host window AND the fleet proxy); links the
DS plugin-kit so the page is themed from the operator's live ``--pl-*`` tokens. Vanilla
JS, no host build (ADR 0038).

Both pages render a **setup card** (v0.6.0) from ``GET /status`` when `gh` is missing
or not authenticated — or ``default_repo`` is malformed (#23) — telling the person what
to do (install gh / `gh auth login` / paste a token in Settings ▸ GitHub). The card's
CSS + JS are shared (``_SETUP_CSS`` / ``_SETUP_JS``, spliced into both pages) so the
two surfaces can't drift. The fetch happens only inside the kit's ``initPluginView``
boot (after the bearer handshake — #2926), like every other data call.
"""

from __future__ import annotations

# --- the shared setup card (spliced into both pages) --------------------------
_SETUP_CSS = r"""
  #setup{display:none;margin:8px;padding:10px 12px;border-radius:var(--pl-radius,8px);
    border:1px solid var(--pl-color-border);background:var(--pl-color-bg);line-height:1.45}
  #setup.show{display:block}
  #setup .st{font-weight:600;margin-bottom:3px;display:flex;align-items:center;gap:6px}
  #setup .sd{color:var(--pl-color-fg-muted);font-size:12px}
  #setup code{font-family:var(--pl-font-mono,ui-monospace,Menlo,monospace);font-size:11px;
    padding:1px 4px;border-radius:4px;background:var(--pl-color-bg-raised)}
  #setup .sa{margin-top:7px;display:flex;gap:6px;align-items:center;flex-wrap:wrap}
  #setup .warn{color:#d29922}
"""

_SETUP_JS = r"""
  // The first-run setup card — rendered from /status (gh missing / not signed in /
  // malformed default repo). Fetched ONLY from inside the kit boot (post-handshake).
  let statusSeq = 0;
  function setupCard(st){
    const el = $("setup"); if(!el) return;
    const lines = [];
    if(!st || st.__failed){
      lines.push('<div class="st"><span class="warn">!</span> Could not check GitHub CLI status</div>'
        + '<div class="sd">The agent may be unreachable, or the status route is missing. Use Re-check.</div>');
    } else if(!st.gh_path){
      lines.push('<div class="st"><span class="warn">!</span> GitHub CLI (<code>gh</code>) is not installed</div>'
        + '<div class="sd">The GitHub rail and tools need it. Install from <a href="https://cli.github.com" target="_blank" rel="noreferrer">cli.github.com</a>'
        + ' (macOS: <code>brew install gh</code>; Debian/Ubuntu: <code>sudo apt install gh</code>), then sign in with <code>gh auth login</code>'
        + ' in a terminal — or paste a personal access token in <b>Settings ▸ GitHub</b> (github.token).</div>');
    } else if(!st.authenticated){
      lines.push('<div class="st"><span class="warn">!</span> GitHub CLI is not signed in</div>'
        + '<div class="sd">Found <code>gh</code>' + (st.gh_version ? ' v' + esc(st.gh_version) : '') + ' at <code>' + esc(st.gh_path) + '</code>'
        + (st.error ? ' — ' + esc(st.error) : '') + '.<br>'
        + (st.token_source === 'config'
            ? 'The token saved in <b>Settings ▸ GitHub</b> (github.token) was rejected — replace it there, or clear it to fall back to <code>gh auth login</code>.'
            : st.token_source === 'env'
              ? 'The <code>GH_TOKEN</code> / <code>GITHUB_TOKEN</code> in the agent\'s environment was rejected — fix or unset it, or paste a working token in <b>Settings ▸ GitHub</b> (github.token).'
              : 'Run <code>gh auth login</code> in a terminal, or paste a personal access token in <b>Settings ▸ GitHub</b> (github.token).')
        + '</div>');
    }
    if(st && st.default_repo_error){
      lines.push('<div class="st"><span class="warn">!</span> Default repo is malformed</div>'
        + '<div class="sd">' + esc(st.default_repo_error) + '</div>');
    }
    if(!lines.length){ el.className = ""; el.innerHTML = ""; return; }
    lines.push('<div class="sa"><button class="pl-btn pl-btn--sm" id="recheck" type="button">Re-check</button></div>');
    el.innerHTML = lines.join(""); el.className = "show";
    const b = $("recheck"); if(b) b.onclick = () => { b.disabled = true; b.textContent = "Checking…"; checkStatus(); };
  }
  async function checkStatus(){
    const my = ++statusSeq;
    try {
      const st = await kit.apiFetch("/api/plugins/github/status").then(r => r.ok ? r.json() : { __failed: true });
      if(my === statusSeq) setupCard(st);
    } catch(e){ if(my === statusSeq) setupCard({ __failed: true }); }
  }
"""

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
  #search{background:var(--pl-color-bg);color:var(--pl-color-fg);border:1px solid var(--pl-color-border);
    border-radius:var(--pl-radius,8px);padding:4px 7px;font-size:12px;min-width:120px;max-width:220px;flex:1 1 140px}
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
  /* CI rollup chip on PR rows (bd-nm78.8) — ✓/✗/● tinted from the list dot palette. */
  .ci{font-weight:700;line-height:1;font-size:12px}
  .ci-passing{color:#3fb950}.ci-failing{color:#f85149}.ci-pending{color:#d29922}
  .more{padding:10px 12px;text-align:center}
  .empty,.hint{padding:24px 14px;text-align:center;color:var(--pl-color-fg-muted)}
  #scrim{display:none;position:fixed;inset:0;background:#0006;z-index:10}
  #scrim.open{display:block}
  #drawer{position:fixed;inset:0 0 0 auto;width:min(460px,92vw);z-index:11;
    background:var(--pl-color-bg-raised);border-left:1px solid var(--pl-color-border);
    display:none;flex-direction:column;box-shadow:-8px 0 30px #0003}
  #drawer.open{display:flex}
  @media(min-width:1000px){#drawer.wide{width:min(780px,96vw)}}
  .dh{padding:12px;display:flex;align-items:flex-start;gap:8px}
  .dh h2{font-size:16px;margin:0;overflow-wrap:anywhere;flex:1}
  .dx{background:transparent;border:0;color:inherit;cursor:pointer;font-size:18px}
  #drawer-meta{padding:0 12px 12px;display:flex;gap:12px;align-items:center}
  #drawer-tabs{padding:0 12px;border-bottom:1px solid var(--pl-color-border)}
  #drawer-body{padding:12px;overflow:auto;min-height:0;overflow-wrap:anywhere}
  .pr-body{margin-top:16px;overflow-wrap:anywhere;min-width:0}
  .md:not(.md-on){white-space:pre-wrap}
  .md h1{font-size:18px}.md h2{font-size:16px}.md h3{font-size:14px}
  .md h4,.md h5,.md h6{font-size:13px}
  .md h1,.md h2,.md h3,.md h4,.md h5,.md h6{margin:12px 0 6px}
  .md p{margin:6px 0}.md ul,.md ol{padding-left:24px;margin:6px 0}
  .md blockquote{margin:8px 0;padding-left:10px;border-left:3px solid var(--pl-color-border);
    color:var(--pl-color-fg-muted)}
  .md code,.md pre{font-family:var(--pl-font-mono,ui-monospace,Menlo,monospace);
    font-size:12px;background:var(--pl-color-bg-raised);border-radius:var(--pl-radius,8px)}
  .md code{padding:1px 4px}.md pre{padding:8px;white-space:pre;overflow-x:auto}
  .md pre code{padding:0;overflow-wrap:normal}
  .md .md-table{overflow-x:auto;margin:8px 0}
  .md table{border-collapse:collapse}.md th,.md td{border:1px solid var(--pl-color-border);padding:4px 8px}
  .conversation-item{padding:10px 0;border-bottom:1px solid var(--pl-color-border)}
  .thread-hunk{font-family:var(--pl-font-mono,monospace);font-size:11px;white-space:pre-wrap}
  .pr-facts{display:grid;grid-template-columns:auto 1fr;gap:6px 12px;margin:0}
  .pr-facts dd{margin:0}
  /* Checks + Files tabs (bd-nm78.5) — dots reuse the list palette; diff tints from --pl-* with fallbacks. */
  .d-sum{font-size:12px;color:var(--pl-color-fg-muted);margin-bottom:10px;display:flex;gap:10px;flex-wrap:wrap;align-items:baseline}
  .d-sum b{color:var(--pl-color-fg);font-weight:600}
  .chk-badge{margin-left:5px;font-size:10px;font-weight:600;padding:0 6px;border-radius:999px;
    background:var(--pl-color-danger-muted,#f8514926);color:var(--pl-color-danger,#f85149)}
  .dot.pass{background:#3fb950}.dot.fail{background:#f85149}.dot.pending{background:#d29922}.dot.skipped{background:var(--pl-color-fg-muted)}
  .chk{display:flex;align-items:center;gap:8px;padding:7px 2px;border-bottom:1px solid var(--pl-color-border)}
  .chk .dot{margin-top:0}
  .chk-name{flex:1;overflow-wrap:anywhere}
  .chk-dur{font-size:11px;color:var(--pl-color-fg-muted);white-space:nowrap}
  .lnk{color:var(--pl-color-accent,#2f81f7);text-decoration:none;white-space:nowrap}
  .lnk:hover{text-decoration:underline}
  .logbtn{background:transparent;border:1px solid var(--pl-color-border);border-radius:var(--pl-radius,8px);
    color:inherit;cursor:pointer;font-size:11px;padding:2px 8px;white-space:nowrap}
  .log{margin:2px 0 10px;padding:8px;border-radius:var(--pl-radius,8px);background:var(--pl-color-bg);
    font-family:var(--pl-font-mono,ui-monospace,Menlo,monospace);font-size:11px;white-space:pre;overflow-x:auto;max-height:320px}
  .file{border-bottom:1px solid var(--pl-color-border)}
  .file-head{display:flex;align-items:center;gap:8px;width:100%;background:transparent;border:0;color:inherit;
    cursor:pointer;text-align:left;padding:8px 2px;font-size:12px;font-family:inherit}
  .file-head:hover{background:var(--pl-color-bg)}
  .fstat{font-family:var(--pl-font-mono,ui-monospace,Menlo,monospace);font-weight:600;width:1.2em;flex:none;text-align:center}
  .fstat.added{color:#3fb950}.fstat.deleted{color:#f85149}.fstat.renamed{color:#a371f7}.fstat.modified{color:#d29922}
  .fpath{flex:1;overflow-wrap:anywhere}
  .fnum{font-size:11px;white-space:nowrap}.fnum .add{color:#3fb950}.fnum .del{color:#f85149}
  .diff{margin:0 0 8px;overflow-x:auto;background:var(--pl-color-bg);border-radius:var(--pl-radius,8px)}
  .dl{font-family:var(--pl-font-mono,ui-monospace,Menlo,monospace);font-size:11px;white-space:pre;padding:0 8px;min-width:max-content}
  .dl.add{background:var(--pl-color-success-muted,#3fb95026)}
  .dl.del{background:var(--pl-color-danger-muted,#f8514926)}
  .dl.hunk{color:var(--pl-color-fg-muted)}
  .note{padding:6px 8px 10px;color:var(--pl-color-fg-muted);font-size:12px}
  /* Issue drawer (bd-nm78.7) — neutral count badge + linked-PR chips. */
  .d-count{margin-left:5px;font-size:10px;font-weight:600;padding:0 6px;border-radius:999px;
    background:var(--pl-color-bg);border:1px solid var(--pl-color-border);color:var(--pl-color-fg-muted)}
  .linked-prs{margin-top:14px}
  .linked-prs h3{font-size:13px;margin:0 0 6px}
  .chip{display:inline-block;margin:0 5px 5px 0;padding:2px 9px;border-radius:999px;cursor:pointer;
    font:inherit;font-size:12px;background:var(--pl-color-bg);border:1px solid var(--pl-color-border);
    color:var(--pl-color-accent,#2f81f7)}
  .chip:hover{text-decoration:underline}
__SETUP_CSS__
</style></head><body>
<div id="wrap">
  <div class="bar">
    <select id="repo" title="Repository"></select>
    <div class="tabs" role="tablist">
      <button class="tab" id="t-issues" role="tab" aria-selected="true" title="Issues">Issues</button>
      <button class="tab" id="t-prs" role="tab" aria-selected="false" title="Pull Requests">PRs</button>
    </div>
    <select id="state" title="State"><option value="open">Open</option><option value="closed">Closed</option><option value="all">All</option></select>
    <input id="search" type="search" placeholder="Search…" title="Search (Enter to run, Esc to clear)" aria-label="Search" autocomplete="off" />
    <span class="spacer"></span>
    <button class="pl-btn pl-btn--sm" id="refresh" type="button" title="Refresh" aria-label="Refresh"></button>
  </div>
  <div id="setup" role="status"></div>
  <div id="list"><div class="hint">Loading…</div></div>
</div>
<!-- Drawer shell copied from projectBoard-plugin view/board.html. -->
<div id="scrim"></div>
<div id="drawer" class="wide" role="dialog" aria-modal="true" aria-labelledby="drawer-title" hidden>
  <div class="dh"><h2 id="drawer-title">Pull request</h2>
    <button class="dx" id="drawer-close" type="button" aria-label="Close">✕</button></div>
  <div id="drawer-meta"></div>
  <div id="drawer-tabs" role="tablist" aria-label="Pull request views"></div>
  <div id="drawer-body" role="tabpanel" tabindex="0"></div>
</div>
<script type="module">
  "use strict";
  let kit;
  try { kit = await import(window.__base + "/_ds/plugin-kit.js"); }
  catch (e) { kit = { initPluginView(cb){ cb && cb(); }, apiFetch:(p,i)=>fetch(window.__base+p,i) }; }

  const $ = (id) => document.getElementById(id);
  let tab = "issues";
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) => ({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;" }[c]));
  // Copied/adapted from projectBoard-plugin view/board.js (bd-p87t).
  const MARKED_CDN = "https://cdnjs.cloudflare.com/ajax/libs/marked/12.0.2/marked.min.js";
  const MARKED_SRI = "sha512-xeUh+KxNyTufZOje++oQHstlMQ8/rpyzPuM+gjMFYK3z5ILJGE7l2NvYL+XfliKURMpBIKKp1XoPN/qswlSMFA==";
  let MARKED = null, MARKED_LOAD = null;
  function loadMarked(){
    if(MARKED_LOAD) return MARKED_LOAD;
    MARKED_LOAD = new Promise((resolve) => {
      if(window.marked) return resolve(window.marked);
      const s = document.createElement("script");
      s.src = MARKED_CDN;
      s.integrity = MARKED_SRI;
      s.crossOrigin = "anonymous";
      s.async = true;
      s.onload = () => resolve(window.marked || null);
      s.onerror = () => resolve(null);
      (document.head || document.documentElement).appendChild(s);
    }).then((m) => (MARKED = m || null));
    return MARKED_LOAD;
  }
  function renderMarkdown(marked, src){
    const safe = String(src).replace(/</g, "&lt;").replace(/>/g, "&gt;");
    const parse = (marked && marked.parse) ? marked.parse : marked;
    return parse(safe, {breaks: true, gfm: true});
  }
  function sanitizeMarkdown(el){
    el.querySelectorAll("a[href],img[src]").forEach((n) => {
      const attr = n.tagName === "A" ? "href" : "src";
      // Browsers ignore ASCII tabs/newlines within schemes as well as leading whitespace.
      const url = (n.getAttribute(attr) || "").replace(/[\u0000-\u0020\u007f]/g, "");
      if(/^(?:javascript|data|vbscript):/i.test(url)) n.removeAttribute(attr);
    });
    el.querySelectorAll("img").forEach((n) => {
      const link = document.createElement("a"), src = n.getAttribute("src");
      if(src) link.setAttribute("href", src);
      link.textContent = n.getAttribute("alt") || "image";
      n.replaceWith(link);
    });
    el.querySelectorAll("a").forEach((n) => {
      n.setAttribute("target", "_blank");
      n.setAttribute("rel", "noopener noreferrer nofollow");
    });
    el.querySelectorAll('input[type="checkbox"]').forEach((n) => { n.disabled = true; });
    el.querySelectorAll("table").forEach((n) => {
      const wrap = document.createElement("div");
      wrap.className = "md-table";
      n.replaceWith(wrap); wrap.appendChild(n);
    });
  }
  function enhanceMarkdown(root){
    const nodes = root.querySelectorAll(".md");
    if(!nodes.length) return;
    const apply = (marked) => {
      if(!marked) return;
      nodes.forEach((el) => {
        if(el.dataset.mdOn || !el.isConnected) return;
        try {
          // Template contents are inert: even images cannot fetch before sanitization.
          const template = document.createElement("template");
          template.innerHTML = renderMarkdown(marked, el.getAttribute("data-md") || "");
          sanitizeMarkdown(template.content);
          el.replaceChildren(template.content);
          el.classList.add("md-on");
          el.dataset.mdOn = "1";
        } catch(e){ /* Keep the escaped fallback if parsing fails. */ }
      });
    };
    if(MARKED) apply(MARKED);
    else loadMarked().then(apply);
  }
  const fmtDate = (iso) => { try { return new Date(iso).toLocaleDateString(undefined,{month:"short",day:"numeric",year:"numeric"}); } catch(e){ return ""; } };
  const fmtDur = (a,b) => { if(!a||!b) return ""; const ms = new Date(b)-new Date(a); if(!(ms>=0)) return ""; const s = Math.round(ms/1000); return s<60 ? s+"s" : Math.floor(s/60)+"m "+(s%60)+"s"; };
  // Mirror read_tools.classify_check for the CheckRun shape (the server already folds a
  // StatusContext into status/conclusion), so the dot/ordering match the tool summary.
  const CHECK_FAIL = new Set(["FAILURE","ERROR","TIMED_OUT","CANCELLED","ACTION_REQUIRED","STARTUP_FAILURE"]);
  const CHECK_SKIP = new Set(["SKIPPED","NEUTRAL","STALE"]);
  function checkCat(c){
    const status = String(c.status||"").toUpperCase(), concl = String(c.conclusion||"").toUpperCase();
    if(status && status !== "COMPLETED") return "pending";
    if(concl === "SUCCESS") return "pass";
    if(CHECK_FAIL.has(concl)) return "fail";
    if(CHECK_SKIP.has(concl)) return "skipped";
    return "pending";
  }
  const FILE_LETTER = {added:"A",modified:"M",deleted:"D",renamed:"R"};
  // Inline Lucide (v0.468) SVGs — themed via currentColor, no runtime dep / kit icon API.
  const svg = (paths) => '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'+paths+'</svg>';
  const ICON = {
    comment: svg('<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>'),
    refresh: svg('<path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/>'),
  };

  function setTab(t){ tab = t; $("t-issues").setAttribute("aria-selected", t==="issues"); $("t-prs").setAttribute("aria-selected", t==="prs"); reload(); }
  function labelPills(labels){ return (labels||[]).slice(0,5).map(l => '<span class="pill">'+esc(l.name||l)+'</span>').join(" "); }

  function issueRow(it, repo){
    const st = (it.state||"").toLowerCase();
    // `gh issue list --json comments` returns an ARRAY of comment objects, not a count —
    // rendering it directly was the "[object Object]" bug (#16). Show its length.
    const cc = Array.isArray(it.comments) ? it.comments.length : (Number(it.comments)||0);
    // data-issue/data-repo let a plain click open the drawer (bd-nm78.7); the href stays so a
    // modified (cmd/ctrl/middle) click still follows through to github.com — same split as PR rows.
    return '<a class="row" data-issue="'+esc(it.number)+'" data-repo="'+esc(repo)+'" href="'+esc(it.url)+'" target="_blank" rel="noreferrer">'
      + '<div class="top"><span class="dot '+(st==="closed"?"closed":"open")+'"></span>'
      + '<span class="title">'+esc(it.title)+' <span class="num">#'+esc(it.number)+'</span></span></div>'
      + '<div class="meta"><span>'+esc((it.author&&it.author.login)||"?")+'</span><span>'+esc(fmtDate(it.createdAt))+'</span>'
      + (cc?('<span class="cmt" title="'+esc(cc)+' comment'+(cc===1?'':'s')+'">'+ICON.comment+esc(cc)+'</span>'):'')+' '+labelPills(it.labels)+'</div></a>';
  }
  // The CI rollup chip for a PR row (bd-nm78.8): ✓ passing / ✗ failing / ● pending, with a
  // "<fail> of <total> checks <state>" tooltip. `none` (no checks) renders nothing.
  const CI_SYM = {passing:"✓", failing:"✗", pending:"●"};
  function checksChip(checks){
    if(!checks || !CI_SYM[checks.state]) return "";
    const total = Number(checks.total)||0, fail = Number(checks.fail)||0;
    const noun = "check"+(total===1?"":"s");
    const tip = checks.state==="failing" ? (fail+" of "+total+" "+noun+" failing")
      : checks.state==="pending" ? (total+" "+noun+" pending")
      : (total+" "+noun+" passing");
    return '<span class="ci ci-'+esc(checks.state)+'" title="'+esc(tip)+'">'+CI_SYM[checks.state]+'</span>';
  }
  function prRow(it, repo){
    const merged = (it.state||"").toLowerCase()==="merged", draft = !!it.isDraft;
    const review = it.reviewDecision ? String(it.reviewDecision).toLowerCase().replace(/_/g," ") : "";
    return '<a class="row" data-pr="'+esc(it.number)+'" data-repo="'+esc(repo)+'" href="'+esc(it.url)+'" target="_blank" rel="noreferrer">'
      + '<div class="top"><span class="dot '+(merged?"merged":(draft?"draft":"open"))+'"></span>'
      + '<span class="title">'+esc(it.title)+' <span class="num">#'+esc(it.number)+'</span></span></div>'
      + '<div class="meta"><span>'+esc((it.author&&it.author.login)||"?")+'</span><span>'+esc(fmtDate(it.createdAt))+'</span>'
      + checksChip(it.checks)
      + (it.headRefName?('<span class="pill">'+esc(it.headRefName)+'</span>'):'')
      + (draft?'<span class="pill">draft</span>':'')+(review?('<span class="pill">'+esc(review)+'</span>'):'')
      + ' '+labelPills(it.labels)+'</div></a>';
  }

  // Adapted from projectBoard-plugin view/board.js: shared shell, automatic tab
  // activation, identity + sequence fences, and unchanged snapshot suppression.
  const DET = "/api/plugins/github/detail/pr";
  const DRAWER_TABS = {
    overview: {label: "Overview", path: DET, render: prOverview, poll: 15000},
    checks: {label: "Checks", path: DET+"/checks", render: prChecks, poll: 15000},
    // The diff rarely changes — poll it no faster than 30s (bd-nm78.5 acceptance).
    files: {label: "Files", path: DET+"/diff", render: prFiles, poll: 30000},
    conversation: {label: "Conversation", path: DET+"/conversation", render: prConversation, poll: 15000},
  };
  // Issue mode reuses the SAME drawer, tablist and fenced poller (bd-nm78.7). Both tabs read the
  // ONE /detail/issue endpoint (body + comments arrive together), so there's no second poller.
  const ISS = "/api/plugins/github/detail/issue";
  const ISSUE_TABS = {
    overview: {label: "Overview", path: ISS, render: issueOverview, poll: 15000},
    comments: {label: "Comments", path: ISS, render: issueComments, poll: 15000},
  };
  let drawerId = 0, drawerSeq = 0, drawer = null, drawerTimer = null, drawerLast = "";
  let drawerOpener = null;
  function prOverview(it){
    return '<dl class="pr-facts"><dt>Author</dt><dd>'+esc((it.author&&it.author.login)||"?")+'</dd>'
      + '<dt>Branches</dt><dd>'+esc(it.headRefName)+' → '+esc(it.baseRefName)+'</dd>'
      + '<dt>Created</dt><dd>'+esc(fmtDate(it.createdAt))+'</dd>'
      + '<dt>Updated</dt><dd>'+esc(fmtDate(it.updatedAt))+'</dd></dl>'
      + '<p class="reviewers">'+reviewerSummary(it)+'</p>'
      + '<div class="pr-body md" data-md="'+esc(it.body||"")+'">'+esc(it.body||"")+'</div>'
      + (it.body_truncated ? '<p>Body truncated — read the rest on GitHub.</p>' : '');
  }
  // --- issue mode (bd-nm78.7) -------------------------------------------------
  function issueOverview(it){
    const assignees = (Array.isArray(it.assignees) ? it.assignees : [])
      .map(a => esc((a && a.login) || a || "?")).join(", ");
    const labels = labelPills(it.labels);
    const facts = '<dl class="pr-facts"><dt>Author</dt><dd>'+esc((it.author&&it.author.login)||"?")+'</dd>'
      + '<dt>Created</dt><dd>'+esc(fmtDate(it.createdAt))+'</dd>'
      + '<dt>Updated</dt><dd>'+esc(fmtDate(it.updatedAt))+'</dd>'
      + (assignees ? '<dt>Assignees</dt><dd>'+assignees+'</dd>' : '')
      + (it.milestone && it.milestone.title ? '<dt>Milestone</dt><dd>'+esc(it.milestone.title)+'</dd>' : '')
      + (labels ? '<dt>Labels</dt><dd>'+labels+'</dd>' : '')+'</dl>';
    // closedByPullRequestsReferences carries no title on current gh — the chip shows #N (and a
    // title only if a future gh adds one). Preserve its repository for cross-repo closing PRs.
    const prs = (Array.isArray(it.closedByPullRequestsReferences) ? it.closedByPullRequestsReferences : [])
      .filter(p => p && p.number != null);
    const linked = prs.length
      ? '<div class="linked-prs"><h3>Linked pull requests</h3>'
        + prs.map(p => '<button type="button" class="chip" data-linked-pr="'+esc(p.number)
            +'" data-linked-repo="'+esc(p.repository && p.repository.owner && p.repository.owner.login && p.repository.name
              ? p.repository.owner.login+'/'+p.repository.name : '')+'">#'
            + esc(p.number)+(p.title ? ' '+esc(p.title) : '')+'</button>').join("")+'</div>'
      : '';
    return facts + linked
      + '<div class="pr-body md" data-md="'+esc(it.body||"")+'">'+esc(it.body||"")+'</div>'
      + (it.body_truncated ? '<p>Body truncated — see GitHub.</p>' : '');
  }
  function issueComments(data){
    // Chronological, each with a ↗ link — reuses conversationItem (mode-aware fallback href).
    const comments = (Array.isArray(data.comments) ? data.comments : []).slice()
      .sort((a,b) => (Date.parse(a.createdAt)||0)-(Date.parse(b.createdAt)||0));
    return comments.map(c => conversationItem(c)).join("") || '<p>No comments.</p>';
  }
  function updateIssueHeader(data){
    const state = String(data.state||"").toLowerCase()==="closed" ? "closed" : "open";
    const reason = data.stateReason ? String(data.stateReason).toLowerCase().replace(/_/g," ") : "";
    $("drawer-title").innerHTML = '#'+esc(data.number)+' '+esc(data.title);
    const href = "https://github.com/"+drawer.repo.split("/").map(encodeURIComponent).join("/")+"/issues/"+encodeURIComponent(drawer.number);
    $("drawer-meta").innerHTML = '<span class="pill">'+esc(reason ? state+" · "+reason : state)+'</span> '
      + '<a href="'+esc(href)+'" target="_blank" rel="noopener noreferrer">Open on GitHub ↗</a>';
  }
  function reviewState(state){
    return state === "APPROVED" ? "approved" : state === "CHANGES_REQUESTED" ? "changes requested" : "commented";
  }
  function reviewerSummary(it){
    const reviews = Array.isArray(it.latestReviews) ? it.latestReviews : [];
    const requests = Array.isArray(it.reviewRequests) ? it.reviewRequests : [];
    return reviews.map(r => esc((r.author && r.author.login)||"?")+" "
      + (r.state==="APPROVED" ? "✓ " : r.state==="CHANGES_REQUESTED" ? "✗ " : "")+reviewState(r.state))
      .concat(requests.map(r => esc(r.login||r.name||r.slug||"?")+" requested")).join(" · ") || "No reviewers yet.";
  }
  function relativeTime(iso){
    const seconds = Math.max(0, Math.floor((Date.now()-Date.parse(iso))/1000));
    if(!Number.isFinite(seconds)) return "";
    if(seconds < 60) return "just now";
    if(seconds < 3600) return Math.floor(seconds/60)+"m ago";
    if(seconds < 86400) return Math.floor(seconds/3600)+"h ago";
    return Math.floor(seconds/86400)+"d ago";
  }
  function conversationItem(c, review=false){
    const seg = drawer.mode === "issue" ? "issues" : "pull";
    const fallback = "https://github.com/"+drawer.repo.split("/").map(encodeURIComponent).join("/")+"/"+seg+"/"+encodeURIComponent(drawer.number);
    // Escape attributes AND reject executable URL schemes in the bearer-holding iframe.
    const href = typeof c.url === "string" && c.url.startsWith("https://") ? c.url : fallback;
    const author = typeof c.author === "string" ? c.author : (c.author && c.author.login)||"?";
    return '<article class="conversation-item"><div class="meta">'+esc(author)+' · '+esc(relativeTime(c.submittedAt||c.createdAt))
      + (review ? ' <span class="pill">'+esc(reviewState(c.state))+'</span>' : '')
      + ' <a class="lnk" href="'+esc(href)+'" target="_blank" rel="noopener noreferrer">↗</a></div>'
      + '<div class="pr-body md" data-md="'+esc(c.body||"")+'">'+esc(c.body||"")+'</div>'
      + (c.truncated ? '<p>Body truncated — read the rest on GitHub.</p>' : '')+'</article>';
  }
  function prConversation(data){
    const comments = Array.isArray(data.comments) ? data.comments : [];
    const reviews = Array.isArray(data.reviews) ? data.reviews : [];
    const timeline = comments.map(c => ({item:c, review:false, date:c.createdAt}))
      .concat(reviews.filter(r => r.body || r.state!=="COMMENTED").map(r => ({item:r, review:true, date:r.submittedAt})))
      .sort((a,b) => (Date.parse(a.date)||0)-(Date.parse(b.date)||0));
    const threads = (Array.isArray(data.threads) ? data.threads : []).slice()
      .sort((a,b) => Number(!!a.isResolved)-Number(!!b.isResolved));
    return (timeline.map(t => conversationItem(t.item,t.review)).join("") || '<p>No comments or reviews.</p>')
      + '<h3>Review threads</h3>' + (threads.map(t => {
        if(!drawer.threadOpen.has(t.id)) drawer.threadOpen.set(t.id,!t.isResolved);
        const open = drawer.threadOpen.get(t.id), comments = Array.isArray(t.comments) ? t.comments : [];
        return '<section class="review-thread"><button type="button" class="file-head" data-thread="'+esc(t.id)+'" aria-expanded="'+open+'">'
          + esc(t.path)+':'+esc(t.line == null ? "?" : t.line)
          + (t.isResolved ? ' <span class="pill">resolved</span>' : '')
          + (t.isOutdated ? ' <span class="pill">outdated</span>' : '')+'</button>'
          + (open ? (comments[0] && comments[0].diffHunk ? '<pre class="thread-hunk">'+esc(comments[0].diffHunk)+'</pre>' : '')
            + comments.map(c => conversationItem(c)).join("") : '')+'</section>';
      }).join("") || '<p>No review threads.</p>');
  }
  function prFilesHref(){
    // Built from the requested identity — never trust an API URL scheme (same as the header link).
    return "https://github.com/"+drawer.repo.split("/").map(encodeURIComponent).join("/")
      + "/pull/"+encodeURIComponent(drawer.number)+"/files";
  }
  function prChecks(data){
    const checks = Array.isArray(data.checks) ? data.checks : [];
    const sum = data.summary || {};
    const summary = '<div class="d-sum"><span><b>'+esc(sum.pass||0)+'</b> passing</span>'
      + '<span><b>'+esc(sum.fail||0)+'</b> failing</span>'
      + '<span><b>'+esc(sum.pending||0)+'</b> pending</span>'
      + '<span><b>'+esc(sum.skipped||0)+'</b> skipped</span></div>';
    if(!checks.length) return summary + '<div class="empty">No checks for this pull request.</div>';
    const logs = drawer.checkLogs || {};
    // Failing first; otherwise keep the server's order (index tiebreaker for a stable sort).
    const ordered = checks.map((c,i) => [c,i])
      .sort((a,b) => (checkCat(a[0])==="fail"?0:1) - (checkCat(b[0])==="fail"?0:1) || a[1]-b[1]);
    const rows = ordered.map(([c]) => {
      const cat = checkCat(c);
      const name = (c.workflow ? esc(c.workflow)+" / " : "") + esc(c.name||"?");
      const dur = fmtDur(c.started_at, c.completed_at);
      const failing = cat==="fail" && c.run_id != null;
      const st = failing ? logs[c.run_id] : null;
      let row = '<div class="chk"><span class="dot '+cat+'"></span><span class="chk-name">'+name+'</span>'
        + (dur ? '<span class="chk-dur">'+esc(dur)+'</span>' : '')
        + (c.url ? '<a class="lnk" href="'+esc(c.url)+'" target="_blank" rel="noreferrer">details ↗</a>' : '')
        + (failing ? '<button type="button" class="logbtn" data-run-id="'+esc(c.run_id)+'" aria-expanded="'
            + (st&&st.open?"true":"false")+'">'+(st&&st.open?"Hide failure log":"Show failure log")+'</button>' : '')
        + '</div>';
      if(st && st.open){
        if(st.log != null) row += '<pre class="log">'+esc(st.log)+'</pre>';
        else if(st.error) row += '<div class="note">'+esc(st.error)+'</div>';
        else row += '<div class="hint">Loading…</div>';
      }
      return row;
    }).join("");
    return summary + rows;
  }
  function renderPatch(patch){
    const lines = String(patch).split("\n");
    if(lines.length && lines[lines.length-1]==="") lines.pop();  // drop the trailing split artifact
    return lines.map(line => {
      const cls = line.startsWith("@@") ? "hunk" : line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : "";
      return '<div class="dl'+(cls?" "+cls:"")+'">'+esc(line)+'</div>';
    }).join("");
  }
  function prFiles(data){
    const files = Array.isArray(data.files) ? data.files : [];
    // Collapse everything by default once there are more than 20 files; otherwise open them.
    // Initialised ONCE per drawer so the person's expand/collapse survives polling re-renders.
    if(!drawer.filesInit){
      drawer.filesInit = true; drawer.filesOpen = new Set();
      if(files.length <= 20) files.forEach(f => drawer.filesOpen.add(f.path));
    }
    const open = drawer.filesOpen;
    let add = 0, del = 0;
    for(const f of files){ add += Number(f.additions)||0; del += Number(f.deletions)||0; }
    const href = prFilesHref();
    const head = '<div class="d-sum"><span><b>'+esc(files.length)+'</b> file'+(files.length===1?"":"s")+' changed</span>'
      + '<span class="fnum"><span class="add">+'+esc(add)+'</span> <span class="del">−'+esc(del)+'</span></span>'
      + (data.total_truncated ? '<span><a class="lnk" href="'+esc(href)+'" target="_blank" rel="noreferrer">'
          + 'some diffs truncated — view on GitHub ↗</a></span>' : '')+'</div>';
    if(!files.length) return head + '<div class="empty">No file changes.</div>';
    const rows = files.map((f,i) => {
      const o = open.has(f.path);
      const path = f.status==="renamed" && f.old_path ? esc(f.old_path)+" → "+esc(f.path) : esc(f.path);
      let row = '<div class="file"><button type="button" class="file-head" data-file="'+i+'" aria-expanded="'+(o?"true":"false")+'">'
        + '<span class="fstat '+esc(f.status||"")+'">'+esc(FILE_LETTER[f.status]||"?")+'</span>'
        + '<span class="fpath">'+path+'</span>'
        + '<span class="fnum"><span class="add">+'+esc(f.additions||0)+'</span> <span class="del">−'+esc(f.deletions||0)+'</span></span>'
        + '</button>';
      if(o){
        if(f.binary || f.truncated || f.patch == null){
          const why = f.binary ? "Binary file" : "Diff too large to show here";
          row += '<div class="note">'+esc(why)+' — <a class="lnk" href="'+esc(href)+'" target="_blank" rel="noreferrer">view on GitHub ↗</a></div>';
        } else {
          row += '<div class="diff">'+renderPatch(f.patch)+'</div>';
        }
      }
      return row + '</div>';
    }).join("");
    return head + rows;
  }
  function tabBadge(t){
    // Comments carries a neutral count (even 0, once loaded); checks/conversation keep the alert badge.
    if(t==="comments") return drawer.commentCount == null ? "" : '<span class="d-count">'+esc(drawer.commentCount)+'</span>';
    const count = t==="checks" ? drawer.checksFail : t==="conversation" ? drawer.unresolved : 0;
    return count > 0 ? '<span class="chk-badge">'+esc(count)+'</span>' : '';
  }
  function renderDrawerTabs(){
    $("drawer-tabs").innerHTML = Object.entries(drawer.tabs).map(([t, spec]) => {
      return '<button type="button" role="tab" id="dtab-'+esc(t)+'" class="tab"'
        + ' aria-selected="'+(t===drawer.tab)+'" aria-controls="drawer-body" tabindex="'+(t===drawer.tab?0:-1)+'"'
        + ' data-drawer-tab="'+esc(t)+'">'+esc(spec.label)+tabBadge(t)+'</button>';
    }).join("");
    $("drawer-body").setAttribute("aria-labelledby", "dtab-"+drawer.tab);
  }
  function selectDrawerTab(t, focus){
    if(!drawer || !Object.hasOwn(drawer.tabs, t)) return;
    if(t !== drawer.tab){
      drawer.tab = t; ++drawerSeq; drawerLast = ""; drawer.lastData = undefined;
      renderDrawerTabs();
      $("drawer-body").innerHTML = '<div class="hint">Loading…</div>';
      $("drawer-body").scrollTop = 0;
      startDrawerPolling();  // restart at the new tab's interval (Files polls slower, 30s)
    }
    if(focus) $("dtab-"+t).focus();
  }
  async function pollDrawer(){
    if(!drawer || document.hidden) return;
    const id = drawer.id, seq = ++drawerSeq, active = drawer.tab;
    const spec = drawer.tabs[active];
    let data;
    try {
      const response = await kit.apiFetch(spec.path+"?repo="+encodeURIComponent(drawer.repo)+"&number="+encodeURIComponent(drawer.number));
      data = response.ok ? await response.json() : {error: "Could not load this "+(drawer.mode==="issue"?"issue":"pull request")+"."};
    } catch(e){ data = {error: "Failed to load — is the agent reachable?"}; }
    if(!drawer || drawer.id !== id || seq !== drawerSeq || document.hidden) return;
    const sig = JSON.stringify(data);
    if(sig === drawerLast) return;
    drawerLast = sig;
    if(!data.error){
      if(drawer.mode === "issue"){
        // Both issue tabs return the whole object, so the header refreshes from either; the
        // Comments tab label carries a live count.
        updateIssueHeader(data);
        const n = Array.isArray(data.comments) ? data.comments.length : 0;
        if(n !== drawer.commentCount){ drawer.commentCount = n; renderDrawerTabs(); }
      } else {
        // Only the Overview response carries the PR's identity (title/state); the Checks and
        // Files responses have their own shapes, so they leave the header untouched.
        if(active === "overview") updatePrHeader(data);
        // The Checks tab label carries a fail-count badge; refresh it when the count changes.
        if(active === "checks"){
          const fail = (data.summary && data.summary.fail) || 0;
          if(fail !== drawer.checksFail){ drawer.checksFail = fail; renderDrawerTabs(); }
        }
        if(active === "conversation"){
          const count = (data.counts && data.counts.unresolved) || 0;
          if(count !== drawer.unresolved){ drawer.unresolved = count; renderDrawerTabs(); }
        }
      }
    }
    drawer.lastData = data;
    renderDrawerBody();
  }
  function updatePrHeader(data){
    const state = String(data.state||"").toLowerCase();
    const pill = state==="merged" ? "merged" : state==="closed" ? "closed" : data.isDraft ? "draft" : "open";
    $("drawer-title").innerHTML = '#'+esc(data.number)+' '+esc(data.title);
    // Construct the link from the requested identity; never trust an API URL scheme.
    const href = "https://github.com/"+drawer.repo.split("/").map(encodeURIComponent).join("/")+"/pull/"+encodeURIComponent(drawer.number);
    $("drawer-meta").innerHTML = '<span class="pill">'+esc(pill)+'</span> <a href="'+esc(href)+'" target="_blank" rel="noopener noreferrer">Open on GitHub ↗</a>';
  }
  // Re-render the active tab's body from the last snapshot — called by the poller AND by the
  // in-body toggles (log / file / thread expand), so a toggle survives the next unchanged-snapshot poll.
  function renderDrawerBody(){
    if(!drawer) return;
    const spec = drawer.tabs[drawer.tab], data = drawer.lastData, body = $("drawer-body"), top = body.scrollTop;
    if(data === undefined){ body.innerHTML = '<div class="hint">Loading…</div>'; return; }
    if(data.error){ body.innerHTML = '<div class="empty">'+esc(data.error)+'</div>'; return; }
    body.innerHTML = spec.render(data);
    enhanceMarkdown(body);
    body.scrollTop = top;
  }
  async function loadCheckLog(runId){
    const id = drawer.id, st = drawer.checkLogs[runId];
    st.loading = true;
    let data;
    try {
      const path = DRAWER_TABS.checks.path+"?repo="+encodeURIComponent(drawer.repo)
        + "&number="+encodeURIComponent(drawer.number)+"&run_id="+encodeURIComponent(runId);
      const r = await kit.apiFetch(path);
      data = r.ok ? await r.json() : {error: "Could not load the failure log."};
    } catch(e){ data = {error: "Failed to load the log — is the agent reachable?"}; }
    if(!drawer || drawer.id !== id) return;  // drawer closed / reopened — drop the late log
    st.loading = false;
    if(data.error) st.error = data.error; else st.log = data.log_tail || "";
    if(drawer.tab === "checks") renderDrawerBody();
  }
  function toggleCheckLog(runId){
    if(!drawer) return;
    const logs = drawer.checkLogs || (drawer.checkLogs = {});
    const st = logs[runId] || (logs[runId] = {open:false, log:null, error:null, loading:false});
    st.open = !st.open;
    if(st.open && st.log === null && !st.loading && !st.error) loadCheckLog(runId);
    renderDrawerBody();
  }
  function toggleFile(i){
    if(!drawer || !drawer.lastData || !Array.isArray(drawer.lastData.files)) return;
    const f = drawer.lastData.files[i]; if(!f) return;
    if(drawer.filesOpen.has(f.path)) drawer.filesOpen.delete(f.path); else drawer.filesOpen.add(f.path);
    renderDrawerBody();
  }
  function stopDrawerPolling(){
    if(drawerTimer !== null){ clearInterval(drawerTimer); drawerTimer = null; }
  }
  function startDrawerPolling(){
    stopDrawerPolling();
    if(!drawer || document.hidden) return;
    pollDrawer();
    const spec = drawer.tabs[drawer.tab];
    drawerTimer = setInterval(pollDrawer, (spec && spec.poll) || 15000);
  }
  function openDrawerShell(mode, repo, number, opener){
    // Logs, expansion choices, and tab counts survive polling re-renders; they're per-drawer,
    // reset on each open. `mode` picks the tab set (DRAWER_TABS for PRs, ISSUE_TABS for issues) —
    // everything downstream (poller, tablist, header) branches on it, so there's one drawer element.
    const tabs = mode === "issue" ? ISSUE_TABS : DRAWER_TABS;
    drawer = {id: ++drawerId, mode, tabs, repo, number, tab: "overview", lastData: undefined,
      checkLogs: {}, checksFail: 0, filesInit: false, filesOpen: new Set(), threadOpen: new Map(),
      unresolved: 0, commentCount: null};
    drawerLast = ""; drawerOpener = opener;
    $("drawer-title").innerHTML = '#'+esc(number)+(mode === "issue" ? ' Issue' : ' Pull request');
    $("drawer-meta").innerHTML = "";
    $("drawer-body").innerHTML = '<div class="hint">Loading…</div>';
    $("drawer-body").scrollTop = 0;
    $("drawer-tabs").setAttribute("aria-label", mode === "issue" ? "Issue views" : "Pull request views");
    renderDrawerTabs();
    $("drawer").hidden = false;
    $("drawer").classList.add("open"); $("scrim").classList.add("open");
    $("wrap").inert = true;
    $("drawer-close").focus();
    startDrawerPolling();
  }
  function openDrawer(repo, number, opener){ openDrawerShell("pr", repo, number, opener); }
  function openIssueDrawer(repo, number, opener){ openDrawerShell("issue", repo, number, opener); }
  function closeDrawer(){
    if(!drawer) return;
    drawer = null; ++drawerSeq; drawerLast = "";
    stopDrawerPolling();
    $("drawer").classList.remove("open"); $("scrim").classList.remove("open");
    $("drawer").hidden = true; $("wrap").inert = false;
    if(drawerOpener && drawerOpener.isConnected) drawerOpener.focus();
    drawerOpener = null;
  }
  $("list").addEventListener("click", (e) => {
    if(e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    const prRow = e.target.closest("[data-pr]");
    if(prRow){ e.preventDefault(); openDrawer(prRow.dataset.repo, prRow.dataset.pr, prRow); return; }
    const issueRowEl = e.target.closest("[data-issue]");
    if(issueRowEl){ e.preventDefault(); openIssueDrawer(issueRowEl.dataset.repo, issueRowEl.dataset.issue, issueRowEl); }
    // Any other (modified) click keeps the row's href → github.com.
  });
  $("drawer-close").onclick = closeDrawer;
  $("scrim").onclick = closeDrawer;
  document.addEventListener("keydown", (e) => {
    if(!drawer) return;
    if(e.key === "Escape"){ e.preventDefault(); closeDrawer(); }
    if(e.key === "Tab"){
      const nodes = [...$("drawer").querySelectorAll('button:not([tabindex="-1"]), a[href], [tabindex="0"]')];
      const first = nodes[0], last = nodes[nodes.length-1];
      if(e.shiftKey && document.activeElement === first){ e.preventDefault(); last.focus(); }
      else if(!e.shiftKey && document.activeElement === last){ e.preventDefault(); first.focus(); }
    }
  });
  $("drawer-tabs").addEventListener("click", (e) => {
    const t = e.target.closest("[data-drawer-tab]");
    if(t) selectDrawerTab(t.getAttribute("data-drawer-tab"), false);
  });
  // Delegated so toggles survive the body's full re-render on each poll (the "details ↗" /
  // "view on GitHub ↗" links carry neither attribute, so they navigate instead of toggling).
  $("drawer-body").addEventListener("click", (e) => {
    // Older gh may omit the linked repository; retain the issue-repo fallback in that case.
    const linkedPr = e.target.closest("[data-linked-pr]");
    if(linkedPr && drawer){
      const repo = linkedPr.getAttribute("data-linked-repo") || drawer.repo, opener = drawerOpener;
      openDrawer(repo, linkedPr.getAttribute("data-linked-pr"), opener); return;
    }
    const logBtn = e.target.closest("[data-run-id]");
    if(logBtn){ toggleCheckLog(logBtn.getAttribute("data-run-id")); return; }
    const threadBtn = e.target.closest("[data-thread]");
    if(threadBtn){
      const id = threadBtn.getAttribute("data-thread");
      drawer.threadOpen.set(id,!drawer.threadOpen.get(id)); renderDrawerBody(); return;
    }
    const fileBtn = e.target.closest("[data-file]");
    if(fileBtn){ toggleFile(Number(fileBtn.getAttribute("data-file"))); }
  });
  $("drawer-tabs").addEventListener("keydown", (e) => {
    const tabs = [...$("drawer-tabs").querySelectorAll("[data-drawer-tab]")].map(b => b.getAttribute("data-drawer-tab"));
    const i = tabs.indexOf(drawer ? drawer.tab : "");
    const next = {ArrowRight: i+1, ArrowLeft: i-1, Home: 0, End: tabs.length-1}[e.key];
    if(next === undefined || !tabs.length) return;
    e.preventDefault(); selectDrawerTab(tabs[(next+tabs.length)%tabs.length], true);
  });
  document.addEventListener("visibilitychange", () => {
    ++drawerSeq; // a response started before hiding cannot paint after resuming
    startDrawerPolling();
  });

  // A monotonic token so an in-flight load() whose fetch resolves AFTER a newer load()
  // started (rapid tab/filter/search clicks, or any double-trigger) drops its stale result
  // instead of clobbering the fresh list — no thrash.
  let loadSeq = 0;
  // Load-more (bd-nm78.8): the page starts at 30 and grows by 30 up to 100. A fresh load
  // (tab / repo / state / search / refresh) resets it via reload(); "Load more" bumps it.
  const PAGE_STEP = 30, MAX_PAGE = 100;
  let pageLimit = PAGE_STEP;
  function listPath(repo){
    const q = ($("search").value || "").trim();
    return (tab==="issues"?"/api/plugins/github/issues":"/api/plugins/github/prs")
      + "?repo="+encodeURIComponent(repo)+"&state="+encodeURIComponent($("state").value)
      + "&limit="+pageLimit + (q ? "&search="+encodeURIComponent(q) : "");
  }
  function reload(){ pageLimit = PAGE_STEP; load(false); }
  async function load(more){
    const my = ++loadSeq;
    const repo = $("repo").value, list = $("list");
    if(!repo){ list.innerHTML = '<div class="hint">No repositories configured. Add some under <b>Settings ▸ GitHub</b> (github.repos).</div>'; return; }
    const keepTop = more ? list.scrollTop : 0;  // "Load more" re-renders the whole list — keep the scroll
    if(!more) list.innerHTML = '<div class="hint">Loading…</div>';
    try {
      const data = await kit.apiFetch(listPath(repo)).then(r => r.json());
      if(my !== loadSeq) return;  // a newer load() superseded this one — drop the stale result
      if(data.error){ list.innerHTML = '<div class="empty">'+esc(data.error)+'</div>'; return; }
      const items = data.items||[];
      if(!items.length){ list.innerHTML = '<div class="empty">No '+tab+' for this filter.</div>'; return; }
      let html = items.map(it => tab==="issues" ? issueRow(it, repo) : prRow(it, repo)).join("");
      // A full page means there may be more. Grow until 100, then stop with a refine hint.
      if(pageLimit >= MAX_PAGE){
        html += '<div class="hint">Showing first 100 — refine with search</div>';
      } else if(items.length >= pageLimit){
        html += '<div class="more"><button class="pl-btn pl-btn--sm" id="more" type="button">Load more</button></div>';
      }
      list.innerHTML = html;
      list.scrollTop = keepTop;
      const mb = $("more");
      if(mb) mb.onclick = () => { pageLimit = Math.min(pageLimit + PAGE_STEP, MAX_PAGE); load(true); };
    } catch(e){ if(my===loadSeq) list.innerHTML = '<div class="empty">Failed to load — is the agent reachable?</div>'; }
  }

__SETUP_JS__
  let booted = false;
  async function boot(){
    if (booted) return;  // the kit re-fires this on every re-theme + the handshake re-send;
    booted = true;       // build the picker and first-load EXACTLY once, or the list thrashes (#15).
    checkStatus();       // the setup card, in parallel — never blocks the list
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
  $("repo").onchange = reload; $("state").onchange = reload; $("refresh").onclick = reload;
  // Search: debounced 300ms; Enter runs it now; Esc clears the box (it only has focus here).
  let searchTimer = null;
  $("search").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(reload, 300); });
  $("search").addEventListener("keydown", (e) => {
    if(e.key === "Enter"){ e.preventDefault(); clearTimeout(searchTimer); reload(); }
    else if(e.key === "Escape"){ e.preventDefault(); if($("search").value){ $("search").value = ""; clearTimeout(searchTimer); reload(); } }
  });
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
  #setup{margin:0}
__SETUP_CSS__
</style></head><body>
<div id="wrap">
  <div id="setup" role="status"></div>
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

__SETUP_JS__
  let booted = false;
  async function boot(){
    if (booted) return;  // kit re-fires on every re-theme; populate the picker once so it never
    booted = true;       // clobbers an in-progress repo selection (#15).
    checkStatus();       // the setup card, in parallel — never blocks the form
    let cfg = { repos: [], default_repo: "" };
    try { cfg = await kit.apiFetch("/api/plugins/github/config").then(r => r.json()); } catch(e){}
    const sel = $("repo");
    sel.innerHTML = (cfg.repos||[]).map(r => '<option value="'+esc(r)+'">'+esc(r)+'</option>').join("");
    if(cfg.default_repo){ sel.value = cfg.default_repo; }
    if(!(cfg.repos||[]).length){ $("res").textContent = "No repositories configured — add one under Settings ▸ GitHub (github.default_repo / repos)."; }
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

# Splice the shared setup card into both pages — one source, two surfaces.
PAGE = PAGE.replace("__SETUP_CSS__", _SETUP_CSS).replace("__SETUP_JS__", _SETUP_JS)
NEW_ISSUE_PAGE = NEW_ISSUE_PAGE.replace("__SETUP_CSS__", _SETUP_CSS).replace("__SETUP_JS__", _SETUP_JS)
