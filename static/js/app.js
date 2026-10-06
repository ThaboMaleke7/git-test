/* RAT · Repo Analysis Tool — dashboard client.
 *
 * Talks to the stdlib HTTP API in rat/server.py.  No external dependencies:
 * charts come from charts.js (hand-rolled SVG), tooltips use native <title>.
 */
"use strict";

(function () {

  /* ── DOM + formatting helpers ─────────────────────────────────── */

  const $ = (id) => document.getElementById(id);
  const FILES_KEYS = ["path"];

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    }[c]));
  }

  const fmtInt = (n) => Number(n || 0).toLocaleString("en-US");
  const fmtRate = (x) => (Number(x) || 0).toFixed(2);
  const fmtPct1 = (x) => (100 * (Number(x) || 0)).toFixed(1) + "%";
  const fmtSigned = (n) => (n > 0 ? "+" : n < 0 ? "−" : "") + fmtInt(Math.abs(Number(n) || 0));
  const pad2 = (n) => (n < 10 ? "0" : "") + n;

  function fmtDateTime(ts) {
    if (ts == null) return "—";
    const d = new Date(ts * 1000);
    return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} `
      + `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
  }

  function fmtDate(ts) {
    if (ts == null) return "—";
    const d = new Date(ts * 1000);
    return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
  }

  function tsToLocalInput(ts) {
    if (ts == null) return "";
    const d = new Date(ts * 1000);
    return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}T`
      + `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
  }

  function localInputToTs(v) {
    if (!v) return null;
    const t = new Date(v).getTime();
    return Number.isNaN(t) ? null : Math.floor(t / 1000);
  }

  function fmtBytes(n) {
    if (n >= 1 << 30) return (n / (1 << 30)).toFixed(1) + " GB";
    if (n >= 1 << 20) return (n / (1 << 20)).toFixed(1) + " MB";
    if (n >= 1 << 10) return (n / (1 << 10)).toFixed(1) + " KB";
    return n + " B";
  }

  function debounce(fn, ms) {
    let h = null;
    return function (...args) {
      clearTimeout(h);
      h = setTimeout(() => fn.apply(null, args), ms);
    };
  }

  async function api(path, opts) {
    const res = await fetch(path, opts || {});
    const text = await res.text();
    let body = null;
    if (text) {
      try { body = JSON.parse(text); } catch (e) { body = { error: text.slice(0, 300) }; }
    }
    if (!res.ok) throw new Error((body && (body.error || body.message)) || ("HTTP " + res.status));
    return body;
  }

  function toast(msg, kind) {
    const box = document.createElement("div");
    box.className = "toast" + (kind ? " " + kind : "");
    box.textContent = msg;
    $("toasts").appendChild(box);
    setTimeout(() => { box.style.transition = "opacity 300ms"; box.style.opacity = "0"; }, 4200);
    setTimeout(() => box.remove(), 4600);
  }

  let overlayDepth = 0;
  function overlay(on, msg) {
    if (on) {
      overlayDepth += 1;
      if (msg) $("overlayMsg").textContent = msg;
      $("overlay").classList.remove("hidden");
    } else {
      overlayDepth = Math.max(0, overlayDepth - 1);
      if (!overlayDepth) $("overlay").classList.add("hidden");
    }
  }

  function downloadCSV(filename, header, rows) {
    const q = (v) => `"${String(v == null ? "" : v).replace(/"/g, '""')}"`;
    const csv = [header, ...rows].map((r) => r.map(q).join(",")).join("\r\n");
    const blob = new Blob(["\uFEFF" + csv], { type: "text/csv;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  }

  /* ── state ────────────────────────────────────────────────────── */

  const state = {
    repos: [], repoId: null, repo: null,
    ref: "HEAD", sha: null,
    mode: "all", from: null, to: null,
    picked: [], pickedSet: new Set(),
    author: "", pathFilter: "",
    data: null, authorsInfo: null, tree: null, scope: null,
    tab: "overview",
    cs: { rows: [], total: 0, q: "", limit: 100, loaded: false },
    picker: { rows: [], total: 0, q: "", limit: 100, loaded: false },
    tables: {
      files: { sort: "churn", dir: -1, q: "", limit: 100 },
      dirs: { sort: "churn", dir: -1, q: "", limit: 100 },
      authors: { sort: "churn", dir: -1, q: "" }
    },
    addMode: "clone", zipFile: null, poll: null
  };

  /* ── query + scope helpers ────────────────────────────────────── */

  function filtersParams() {
    const p = new URLSearchParams();
    p.set("ref", state.ref || "HEAD");
    p.set("mode", state.mode);
    if (state.mode === "range") {
      if (state.from != null) p.set("from", String(state.from));
      if (state.to != null) p.set("to", String(state.to + 1)); /* inclusive To */
    } else if (state.mode === "list") {
      p.set("commits", state.picked.join(","));
    }
    if (state.author) p.set("author", state.author);
    return p;
  }

  function currentScope() {
    const d = state.data;
    if (!d) return { scoped: false, totals: null, files: [], dirs: [], timeline: { bucket: "day", points: [] } };
    const av = d.author_view;
    return {
      scoped: !!av,
      totals: av ? av.totals : d.totals,
      files: (av ? av.files : d.files) || [],
      dirs: (av ? av.dirs : d.dirs) || [],
      timeline: (av ? av.timeline : d.timeline) || { bucket: "day", points: [] }
    };
  }

  /* ── data loading ─────────────────────────────────────────────── */

  async function loadRepos(preferId) {
    const res = await api("/api/repos");
    state.repos = res.repos || [];
    const sel = $("repoSelect");
    if (!state.repos.length) {
      sel.innerHTML = '<option value="">— no repository —</option>';
      state.repoId = null; state.repo = null; state.data = null;
      showEmpty(true);
      return;
    }
    sel.innerHTML = state.repos.map((r) =>
      `<option value="${esc(r.id)}">${esc(r.name)}${r.kind === "zip" ? " (zip)" : ""}</option>`).join("");
    const want = (preferId && state.repos.some((r) => r.id === preferId)) ? preferId
      : (state.repos.some((r) => r.id === state.repoId) ? state.repoId : state.repos[0].id);
    sel.value = want;
    await selectRepo(want);
  }

  function showEmpty(on) {
    $("emptyState").classList.toggle("hidden", !on);
    $("workspace").classList.toggle("hidden", on);
    $("sidebar").classList.toggle("hidden", on);
    if (on) closeDrawer();
  }

  async function selectRepo(id) {
    state.repoId = id;
    state.repo = state.repos.find((r) => r.id === id) || null;
    state.author = "";
    state.picked = []; state.pickedSet.clear();
    state.data = null; state.tree = null; state.authorsInfo = null;
    state.cs = { rows: [], total: 0, q: "", limit: 100, loaded: false };
    state.picker = { rows: [], total: 0, q: "", limit: 100, loaded: false };
    state.pathFilter = ""; $("pathInput").value = "";
    $("commitSearch").value = ""; $("csSearch").value = "";
    state.ref = (state.repo && state.repo.default_ref) || "HEAD";
    $("refInput").value = state.ref;
    setMode("all");
    syncPickedUi();
    showEmpty(false);
    await loadRefs(false);
    await refreshAll();
  }

  async function loadRefs(refresh) {
    const res = await api(`/api/repos/${state.repoId}/refs?refresh=${refresh ? 1 : 0}`);
    const opts = ["HEAD"];
    (res.branches || []).forEach((b) => opts.push(b.name));
    (res.tags || []).forEach((t) => opts.push(t.name));
    $("refOptions").innerHTML = opts.map((o) => `<option value="${esc(o)}"></option>`).join("");
    return res;
  }

  async function loadAuthors() {
    if (!state.repoId) return;
    const info = await api(`/api/repos/${state.repoId}/authors?ref=${encodeURIComponent(state.ref)}`);
    state.authorsInfo = info;
    const sel = $("authorSelect");
    const cur = state.author;
    sel.innerHTML = '<option value="">All authors</option>'
      + (info.canonical || []).map((a) =>
        `<option value="${esc(a.key)}">${esc(a.name)} &lt;${esc(a.email)}&gt; · ${fmtInt(a.commits)}</option>`).join("");
    const present = (info.canonical || []).some((a) => a.key === cur);
    if (cur && !present) state.author = "";
    sel.value = present ? cur : "";
    const n = Object.keys(info.merges || {}).length;
    $("mergeInfo").textContent =
      `${(info.identities || []).length} raw identities · ${n} manual merge${n === 1 ? "" : "s"}`;
  }

  async function loadTree() {
    if (!state.repoId) return;
    try {
      state.tree = await api(`/api/repos/${state.repoId}/stats?ref=${encodeURIComponent(state.ref)}`);
    } catch (e) { state.tree = null; }
    renderRepoInfo();
  }

  async function loadMetrics() {
    if (!state.repoId) return;
    const res = await api(`/api/repos/${state.repoId}/metrics?${filtersParams()}`);
    state.data = res;
    state.sha = res.meta ? res.meta.sha : null;
    renderAll();
  }

  const scheduleMetrics = debounce(() => {
    loadMetrics().catch((e) => toast(e.message, "error"));
  }, 400);

  async function refreshAll() {
    if (!state.repoId) return;
    overlay(true, `computing metrics for ${(state.repo || {}).name || "repository"} @ ${state.ref}…`);
    try {
      await loadMetrics();
    } catch (e) {
      toast(e.message, "error");
    } finally {
      overlay(false);
    }
    loadAuthors().catch((e) => toast(e.message, "error"));
    loadTree();
  }

  /* ── commit lists (picker + commit-set tab) ───────────────────── */

  async function loadPicker(reset) {
    if (!state.repoId) return;
    const p = new URLSearchParams({
      ref: state.ref,
      limit: String(state.picker.limit),
      offset: reset ? "0" : String(state.picker.rows.length)
    });
    if (state.picker.q) p.set("q", state.picker.q);
    const res = await api(`/api/repos/${state.repoId}/commits?${p}`);
    state.picker.total = res.total;
    state.picker.rows = reset ? res.rows : state.picker.rows.concat(res.rows);
    state.picker.loaded = true;
    renderPicker();
  }

  async function loadCs(reset) {
    if (!state.repoId) return;
    const p = new URLSearchParams({
      ref: state.ref,
      limit: String(state.cs.limit),
      offset: reset ? "0" : String(state.cs.rows.length)
    });
    if (state.cs.q) p.set("q", state.cs.q);
    const res = await api(`/api/repos/${state.repoId}/commits?${p}`);
    state.cs.total = res.total;
    state.cs.rows = reset ? res.rows : state.cs.rows.concat(res.rows);
    state.cs.loaded = true;
    renderCommitSetTab();
  }

  function pickRowHtml(c) {
    const checked = state.pickedSet.has(c.h) ? " checked" : "";
    return `<label class="pick-row">
      <input type="checkbox" data-h="${esc(c.h)}"${checked}>
      <span class="pr-main">
        <span class="pr-subj">${esc(c.s)}</span>
        <span class="pr-meta">${esc(c.n)} · ${fmtDateTime(c.t)}</span>
      </span>
      <span class="pr-hash">${esc(c.h.slice(0, 7))}</span>
    </label>`;
  }

  function commitRowHtml(c) {
    const checked = state.pickedSet.has(c.h) ? " checked" : "";
    return `<tr data-h="${esc(c.h)}">
      <td class="ck"><input type="checkbox" data-h="${esc(c.h)}"${checked}></td>
      <td class="dim">${fmtDateTime(c.t)}</td>
      <td class="path-cell" style="max-width:110px">${esc(c.h.slice(0, 10))}</td>
      <td class="dim">${esc(c.n)}</td>
      <td class="subj" title="${esc(c.s)}">${esc(c.s)}</td>
      <td>${fmtInt(c.files)}</td>
    </tr>`;
  }

  function renderPicker() {
    $("commitPicker").innerHTML = state.picker.rows.map(pickRowHtml).join("")
      || '<div class="pick-row muted">no commits</div>';
  }

  function togglePicked(h, on) {
    if (on) {
      if (!state.pickedSet.has(h)) { state.pickedSet.add(h); state.picked.push(h); }
    } else {
      state.pickedSet.delete(h);
      state.picked = state.picked.filter((x) => x !== h);
    }
    syncPickedUi();
    syncCheckboxes();
    if (state.mode === "list") scheduleMetrics();
  }

  function addMany(hashes) {
    let added = 0;
    hashes.forEach((h) => {
      if (!state.pickedSet.has(h)) { state.pickedSet.add(h); state.picked.push(h); added += 1; }
    });
    syncPickedUi();
    syncCheckboxes();
    if (state.mode !== "list") setMode("list");
    if (added) {
      toast(`${added} commit${added === 1 ? "" : "s"} added to the commit set`);
      loadMetrics().catch((e) => toast(e.message, "error"));
    } else {
      toast("nothing new to add");
    }
  }

  function syncPickedUi() {
    $("pickedCount").textContent =
      `${state.picked.length} commit${state.picked.length === 1 ? "" : "s"} selected`;
    renderChips();
  }

  function syncCheckboxes() {
    document.querySelectorAll("input[data-h]").forEach((i) => {
      i.checked = state.pickedSet.has(i.dataset.h);
    });
  }

  async function addCurrentRange() {
    if (state.from == null && state.to == null) {
      toast("set a time range first (Commit set → Time range)", "error");
      return;
    }
    const lo = state.from, hi = state.to;
    const found = [];
    let offset = 0;
    overlay(true, "collecting commits in range…");
    try {
      for (let guard = 0; guard < 40; guard += 1) {
        const p = new URLSearchParams({ ref: state.ref, offset: String(offset), limit: "500" });
        const res = await api(`/api/repos/${state.repoId}/commits?${p}`);
        if (!res.rows.length) break;
        let stop = false;
        res.rows.forEach((c) => {
          if ((lo == null || c.t >= lo) && (hi == null || c.t <= hi)) found.push(c.h);
          if (lo != null && c.t < lo) stop = true; /* rows are newest-first */
        });
        offset += res.rows.length;
        if (stop || offset >= res.total) break;
      }
    } finally {
      overlay(false);
    }
    addMany(found);
  }

  /* ── rendering: chips / KPIs / charts / bars ──────────────────── */

  function chip(box, text, onClear, title) {
    const c = document.createElement("span");
    c.className = "chip";
    if (title) c.title = title;
    const label = document.createElement("span");
    label.className = "chip-label";
    label.textContent = text;
    c.appendChild(label);
    if (onClear) {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = "✕";
      b.title = "clear filter";
      b.addEventListener("click", onClear);
      c.appendChild(b);
    }
    box.appendChild(c);
  }

  function renderChips() {
    const box = $("filterChips");
    box.innerHTML = "";
    if (!state.repo) return;
    chip(box, state.repo.name, null, "repository");
    if (state.data && state.data.meta) {
      chip(box, "@" + state.data.meta.sha.slice(0, 7), null, "reference " + state.ref);
    }
    if (state.mode === "range") {
      chip(box, `H: ${state.from != null ? fmtDate(state.from) : "…"} → ${state.to != null ? fmtDate(state.to) : "…"}`,
        () => {
          state.from = null; state.to = null;
          $("fromInput").value = ""; $("toInput").value = "";
          setMode("all");
          loadMetrics().catch((e) => toast(e.message, "error"));
        }, "commit set: time range");
    } else if (state.mode === "list") {
      chip(box, `H: ${state.picked.length} selected commit${state.picked.length === 1 ? "" : "s"}`,
        () => {
          state.picked = []; state.pickedSet.clear();
          syncPickedUi(); syncCheckboxes();
          setMode("all");
          loadMetrics().catch((e) => toast(e.message, "error"));
        }, "commit set: manually selected commits");
    } else {
      chip(box, "H: all non-merge commits", null, "commit set");
    }
    if (state.author) {
      const found = ((state.authorsInfo || {}).canonical || []).find((a) => a.key === state.author);
      chip(box, "author: " + (found ? found.name : "selected"), () => setAuthor(""), "author filter");
    }
    if (state.pathFilter) {
      chip(box, "path: " + state.pathFilter, () => {
        $("pathInput").value = "";
        state.pathFilter = "";
        renderFiles(); renderDirs(); renderChips();
      }, "path filter");
    }
  }

  function renderKpis() {
    const d = state.data, s = state.scope;
    if (!d || !s.totals) { $("kpis").innerHTML = ""; return; }
    const t = s.totals;
    const tiles = [
      [fmtInt(t.commits), "commits", ""],
      [fmtInt(d.commit_set.contributors), "contributors", ""],
      [fmtInt(t.files), "files touched", ""],
      [fmtInt(t.dirs), "directories", ""],
      ["+" + fmtInt(t.added), "lines added", "added"],
      ["−" + fmtInt(t.removed), "lines removed", "removed"],
      [fmtSigned(t.growth), "net growth", "growth"],
      [fmtInt(t.churn), "churn", "churn"],
      [fmtInt(t.mods), "modifications", ""]
    ];
    $("kpis").innerHTML = tiles.map(([v, k, cls]) =>
      `<div class="kpi ${cls}"><div class="v">${v}</div><div class="k">${k}</div></div>`).join("");
  }

  function renderCharts() {
    const tl = state.scope.timeline;
    Charts.stackedBars($("chartChurn"), tl, { height: 250 });
    Charts.areaNet($("chartGrowth"), tl, { height: 220 });
    $("churnBucketNote").textContent = tl.points.length
      ? `bucket: ${tl.bucket} · ${tl.points.length} intervals` : "";
  }

  function barList(container, rows) {
    if (!rows.length) {
      container.innerHTML = '<div class="bar-empty">nothing to show</div>';
      return;
    }
    const max = Math.max.apply(null, rows.map((r) => r.value)) || 1;
    container.innerHTML = rows.map((r) => `
      <div class="bar-row" title="${esc(r.title || r.label)}">
        <div class="bar-label">${esc(r.label)}</div>
        <div class="bar-track"><div class="bar-fill ${r.warm ? "warm" : ""}" style="width:${Math.max(1, (100 * r.value) / max).toFixed(1)}%"></div></div>
        <div class="bar-val">${esc(r.display)}</div>
      </div>`).join("");
  }

  function renderTopLists() {
    const d = state.data, s = state.scope;
    if (!d) return;
    const authors = (d.authors || []).slice()
      .sort((a, b) => b.churn - a.churn).slice(0, 8)
      .map((a) => ({
        label: a.name, value: a.churn,
        display: fmtInt(a.churn),
        title: `${a.name} <${a.email}> — churn ${fmtInt(a.churn)} · ownership ${fmtPct1(a.ownership)}`
      }));
    barList($("topAuthors"), authors);
    const files = (s.files || []).slice()
      .sort((a, b) => b.churn - a.churn).slice(0, 8)
      .map((r) => ({ label: r.path, value: r.churn, display: fmtInt(r.churn), title: r.path }));
    barList($("topFiles"), files);
    const dirs = (s.dirs || []).filter((r) => r.path).slice()
      .sort((a, b) => b.churn - a.churn).slice(0, 8)
      .map((r) => ({ label: r.path, value: r.churn, display: fmtInt(r.churn), title: r.path, warm: true }));
    barList($("topDirs"), dirs);
  }

  /* ── tables ───────────────────────────────────────────────────── */

  function filterRows(rows, q, keys) {
    const needle = (q || "").trim().toLowerCase();
    if (!needle) return rows;
    return rows.filter((r) => keys.some((k) => String(r[k] == null ? "" : r[k]).toLowerCase().includes(needle)));
  }

  function sortRows(rows, tstate, textKeys) {
    const col = tstate.sort, dir = tstate.dir;
    const isText = textKeys.indexOf(col) !== -1;
    const copy = rows.slice();
    copy.sort((a, b) => {
      if (isText) return dir * String(a[col] == null ? "" : a[col]).localeCompare(String(b[col] == null ? "" : b[col]));
      return dir * ((Number(a[col]) || 0) - (Number(b[col]) || 0));
    });
    return copy;
  }

  function fileCols(withOwn) {
    const cols = [
      { key: "path", label: "File", cell: (r) => esc(r.path), cellClass: () => "path-cell" },
      { key: "added", label: "Added", cell: (r) => "+" + fmtInt(r.added) },
      { key: "removed", label: "Removed", cell: (r) => "−" + fmtInt(r.removed) },
      { key: "growth", label: "Net", cell: (r) => fmtSigned(r.growth) },
      { key: "churn", label: "Churn", cell: (r) => fmtInt(r.churn) },
      { key: "mods", label: "Mods", cell: (r) => fmtInt(r.mods) },
      { key: "modfreq", label: "Modfreq", cell: (r) => fmtRate(r.modfreq) },
      { key: "churnrate", label: "Churnrate", cell: (r) => fmtRate(r.churnrate) }
    ];
    if (withOwn) cols.push({ key: "ownership", label: "Ownership", cell: (r) => fmtPct1(r.ownership) });
    return cols;
  }

  function dirCols(withOwn) {
    const cols = fileCols(withOwn);
    cols[0] = { key: "path", label: "Directory", cell: (r) => esc(r.path || "(root)"), cellClass: () => "path-cell" };
    return cols;
  }

  const AUTHOR_COLS = [
    { key: "name", label: "Author", cell: (r) => esc(r.name), cellClass: () => "path-cell" },
    { key: "email", label: "Email", cell: (r) => esc(r.email), cellClass: () => "dim" },
    { key: "commits", label: "Commits", cell: (r) => fmtInt(r.commits) },
    { key: "added", label: "Added", cell: (r) => "+" + fmtInt(r.added) },
    { key: "removed", label: "Removed", cell: (r) => "−" + fmtInt(r.removed) },
    { key: "growth", label: "Net", cell: (r) => fmtSigned(r.growth) },
    { key: "churn", label: "Churn", cell: (r) => fmtInt(r.churn) },
    { key: "mods", label: "Mods", cell: (r) => fmtInt(r.mods) },
    { key: "ownership", label: "Ownership", cell: (r) => fmtPct1(r.ownership) }
  ];

  function fileRows() {
    let rows = filterRows(state.scope.files || [], state.tables.files.q, FILES_KEYS);
    const pf = state.pathFilter.trim().toLowerCase();
    if (pf) rows = rows.filter((r) => r.path.toLowerCase().includes(pf));
    return sortRows(rows, state.tables.files, ["path"]);
  }

  function dirRows() {
    let rows = filterRows(state.scope.dirs || [], state.tables.dirs.q, FILES_KEYS);
    const pf = state.pathFilter.trim().toLowerCase();
    if (pf) rows = rows.filter((r) => r.path.toLowerCase().includes(pf));
    return sortRows(rows, state.tables.dirs, ["path"]);
  }

  function authorRows() {
    const rows = filterRows((state.data || {}).authors || [], state.tables.authors.q, ["name", "email"]);
    return sortRows(rows, state.tables.authors, ["name", "email"]);
  }

  function renderDataTable(tableId, moreId, countId, cols, rows, total, tstate) {
    const table = $(tableId);
    const head = cols.map((c) => {
      const active = tstate.sort === c.key;
      const arrow = active ? (tstate.dir < 0 ? " ▼" : " ▲") : "";
      return `<th class="${active ? "sorted" : ""}" data-col="${c.key}" title="sort by ${esc(c.label)}">${esc(c.label)}${arrow}</th>`;
    }).join("");
    const body = rows.map((r) => `<tr ${r.__attrs || ""}>${cols.map((c) =>
      `<td class="${c.cellClass ? c.cellClass(r) : ""}">${c.cell(r)}</td>`).join("")}</tr>`).join("");
    table.innerHTML = `<thead><tr>${head}</tr></thead><tbody>${body
      || `<tr><td colspan="${cols.length}" style="text-align:center;padding:18px" class="muted">no matching rows</td></tr>`}</tbody>`;
    $(countId).textContent = total ? `${rows.length} of ${total} rows` : "0 rows";
    const more = $(moreId);
    if (more) more.classList.toggle("hidden", rows.length >= total);
  }

  function renderFiles() {
    const s = state.scope;
    if (!s) return;
    const rows = fileRows();
    rows.forEach((r) => { r.__attrs = `data-path="${esc(r.path)}" data-kind="file"`; });
    const t = state.tables.files;
    renderDataTable("filesTable", "filesMore", "filesCount", fileCols(s.scoped), rows.slice(0, t.limit), rows.length, t);
  }

  function renderDirs() {
    const s = state.scope;
    if (!s) return;
    const rows = dirRows();
    rows.forEach((r) => { r.__attrs = `data-path="${esc(r.path)}" data-kind="dir"`; });
    const t = state.tables.dirs;
    renderDataTable("dirsTable", "dirsMore", "dirsCount", dirCols(s.scoped), rows.slice(0, t.limit), rows.length, t);
  }

  function renderAuthors() {
    if (!state.data) return;
    const rows = authorRows();
    rows.forEach((r) => { r.__attrs = `data-key="${esc(r.key)}"`; });
    renderDataTable("authorsTable", null, "authorsCount", AUTHOR_COLS, rows, rows.length, state.tables.authors);
  }

  function renderCommitSetTab() {
    const d = state.data;
    const kpis = $("commitSetKpis");
    if (!d) { kpis.innerHTML = ""; return; }
    const H = d.commit_set, s = state.scope;
    const items = [
      [fmtInt(H.count), "|H| commits"],
      [fmtDateTime(H.from), "first commit"],
      [fmtDateTime(H.to), "last commit"],
      [fmtInt(H.contributors), "contributors"],
      ["+" + fmtInt(s.totals.added), "added"],
      ["−" + fmtInt(s.totals.removed), "removed"],
      [fmtInt(s.totals.churn), "churn"],
      [fmtInt(s.totals.mods), "modifications"]
    ];
    kpis.innerHTML = items.map(([v, k]) => `<div class="kpi"><div class="v">${v}</div><div class="k">${k}</div></div>`).join("");

    const table = $("csTable");
    const rows = state.cs.rows;
    table.innerHTML = `<thead><tr>
        <th class="ck"></th><th>Date ▼</th><th>Hash</th><th>Author</th><th>Subject</th><th>Files</th>
      </tr></thead><tbody>${rows.map(commitRowHtml).join("")
      || '<tr><td colspan="6" style="text-align:center;padding:18px" class="muted">no commits</td></tr>'}</tbody>`;
    $("csCount").textContent = `${rows.length} of ${fmtInt(state.cs.total)} commits loaded`;
    $("csMore").classList.toggle("hidden", rows.length >= state.cs.total);
  }

  function renderRepoInfo() {
    const kv = $("repoInfo");
    const r = state.repo;
    if (!r) { kv.innerHTML = ""; return; }
    const meta = state.data && state.data.meta;
    const rows = [];
    rows.push(["Name", esc(r.name)]);
    rows.push(["Kind", r.kind === "zip" ? "zip upload" : "git clone"]);
    if (r.source) {
      const src = r.source.startsWith("http")
        ? `<a href="${esc(r.source)}" target="_blank" rel="noopener">${esc(r.source.length > 42 ? r.source.slice(0, 42) + "…" : r.source)}</a>`
        : esc(r.source);
      rows.push(["Source", src]);
    }
    rows.push(["Created", fmtDateTime(r.created)]);
    if (meta) {
      rows.push(["Commit", `<code>${esc(meta.sha.slice(0, 10))}</code>`]);
      rows.push(["Commits in ref", fmtInt(meta.commits_scanned)]);
      rows.push(["Mailmap", meta.mailmap ? '<span class="badge ok">applied</span>' : '<span class="badge">none</span>']);
      if (meta.shallow) rows.push(["Shallow", '<span class="badge warn">yes</span>']);
      rows.push(["Metrics time", fmtInt(meta.took_ms) + " ms"]);
      rows.push(["Parsed", fmtDateTime(meta.parsed_at)]);
    }
    if (state.tree) {
      rows.push(["Files @ ref", fmtInt(state.tree.files)]);
      rows.push(["Dirs @ ref", fmtInt(state.tree.dirs)]);
    }
    kv.innerHTML = rows.map(([k, v]) => `<div class="k">${k}</div><div class="v">${v}</div>`).join("");

    const actions = document.createElement("div");
    actions.className = "row";
    actions.style.gridColumn = "1 / -1";
    actions.style.marginTop = "8px";
    actions.innerHTML = '<button class="btn tiny" id="refreshRefsBtn" title="re-read branches and tags">↻ refs</button>'
      + '<button class="btn tiny" id="deleteRepoBtn" title="remove this repository">🗑 delete</button>';
    kv.appendChild(actions);
    $("refreshRefsBtn").addEventListener("click", async () => {
      try { await loadRefs(true); toast("references refreshed", "ok"); }
      catch (e) { toast(e.message, "error"); }
    });
    $("deleteRepoBtn").addEventListener("click", deleteRepo);
  }

  function renderPathOptions() {
    const dl = $("pathOptions");
    if (!state.data) { dl.innerHTML = ""; return; }
    const dirs = (state.scope.dirs || []).map((r) => r.path).filter(Boolean);
    const files = (state.scope.files || []).map((r) => r.path);
    const opts = dirs.concat(files).slice(0, 500);
    dl.innerHTML = opts.map((p) => `<option value="${esc(p)}"></option>`).join("");
  }

  function renderAll() {
    state.scope = currentScope();
    renderChips();
    renderKpis();
    renderCharts();
    renderTopLists();
    renderFiles();
    renderDirs();
    renderAuthors();
    renderCommitSetTab();
    renderRepoInfo();
    renderPathOptions();
  }

  /* ── drawer (object detail) ───────────────────────────────────── */

  function renderDrawer(res) {
    $("drawerKind").textContent = res.kind === "file" ? "file" : (res.path ? "directory" : "repository root");
    $("drawerTitle").textContent = res.path || "(repository root)";
    const t = res.totals || {};
    const tiles = [
      ["+" + fmtInt(t.added), "added", "added"],
      ["−" + fmtInt(t.removed), "removed", "removed"],
      [fmtSigned(t.growth), "net", "growth"],
      [fmtInt(t.churn), "churn", "churn"],
      [fmtInt(t.mods), "mods", ""],
      [fmtInt(res.scope_commits), "commits", ""]
    ];
    $("drawerKpis").innerHTML = tiles.map(([v, k, cls]) =>
      `<div class="kpi ${cls}"><div class="v">${v}</div><div class="k">${k}</div></div>`).join("")
      + `<div class="kpi"><div class="v">${fmtRate(t.modfreq)}</div><div class="k">modfreq</div></div>`
      + `<div class="kpi"><div class="v">${fmtRate(t.churnrate)}</div><div class="k">churnrate</div></div>`;

    const owners = res.owners || [];
    const top = owners.slice(0, 8);
    const rest = owners.slice(8);
    const slices = top.map((o, i) => ({ label: o.name, value: o.churn, color: Charts.palette(i) }));
    if (rest.length) {
      slices.push({
        label: `other (${rest.length})`,
        value: rest.reduce((acc, o) => acc + o.churn, 0),
        color: Charts.otherColor
      });
    }
    Charts.donut($("drawerDonut"), slices, { size: 150 });

    $("drawerOwners").innerHTML = `<thead><tr>
        <th>Author</th><th>Churn</th><th>Mods</th><th>Ownership</th>
      </tr></thead><tbody>${owners.map((o) => `<tr data-key="${esc(o.key)}" title="filter the dashboard by this author">
        <td class="path-cell">${esc(o.name)}</td>
        <td>${fmtInt(o.churn)}</td>
        <td>${fmtInt(o.mods)}</td>
        <td>${fmtPct1(o.ownership)}</td>
      </tr>`).join("") || '<tr><td colspan="4" style="text-align:center;padding:14px" class="muted">no authors</td></tr>'}</tbody>`;
    $("drawerOwners").querySelectorAll("tr[data-key]").forEach((tr) => {
      tr.addEventListener("click", () => { setAuthor(tr.dataset.key); closeDrawer(); });
    });

    Charts.miniBars($("drawerChart"), res.timeline, { height: 120 });
  }

  async function openDrawer(path, kind) {
    if (!state.repoId) return;
    const p = filtersParams();
    p.set("path", path);
    p.set("kind", kind);
    overlay(true, "loading detail…");
    try {
      const res = await api(`/api/repos/${state.repoId}/object?${p}`);
      renderDrawer(res);
      $("drawer").classList.remove("hidden");
      Charts.redraw($("drawerChart"));
    } catch (e) {
      toast(e.message, "error");
    } finally {
      overlay(false);
    }
  }

  function closeDrawer() {
    $("drawer").classList.add("hidden");
  }

  function guessKind(p) {
    const d = state.data;
    if (!d) return "dir";
    if ((d.files || []).some((r) => r.path === p)) return "file";
    return "dir";
  }

  /* ── filters ──────────────────────────────────────────────────── */

  function setMode(mode) {
    state.mode = mode;
    document.querySelectorAll("#commitModeSeg button").forEach((b) =>
      b.classList.toggle("active", b.dataset.mode === mode));
    $("rangePane").classList.toggle("hidden", mode !== "range");
    $("listPane").classList.toggle("hidden", mode !== "list");
    if (mode === "list" && !state.picker.loaded) {
      loadPicker(true).catch((e) => toast(e.message, "error"));
    }
    renderChips();
  }

  function setAuthor(key) {
    state.author = key || "";
    const sel = $("authorSelect");
    sel.value = state.author;
    if (sel.value !== state.author) { state.author = ""; sel.value = ""; }
    loadMetrics().catch((e) => toast(e.message, "error"));
  }

  async function applyRef() {
    const v = ($("refInput").value || "").trim() || "HEAD";
    if (v !== state.ref) {
      state.ref = v;
      state.picked = []; state.pickedSet.clear();
      syncPickedUi();
      state.cs.rows = []; state.cs.loaded = false;
      state.picker.rows = []; state.picker.loaded = false;
    }
    await refreshAll();
  }

  /* ── add-repository modal ─────────────────────────────────────── */

  function openAddModal() {
    $("jobBox").classList.add("hidden");
    $("jobError").classList.add("hidden");
    $("jobFill").style.width = "0%";
    $("addGo").disabled = false;
    $("addModal").classList.remove("hidden");
    $("cloneUrl").focus();
  }

  function closeAddModal() {
    $("addModal").classList.add("hidden");
    if (state.poll) { clearInterval(state.poll); state.poll = null; }
  }

  function setAddMode(mode) {
    state.addMode = mode;
    document.querySelectorAll("#addModeSeg button").forEach((b) =>
      b.classList.toggle("active", b.dataset.addmode === mode));
    $("addClonePane").classList.toggle("hidden", mode !== "clone");
    $("addZipPane").classList.toggle("hidden", mode !== "zip");
  }

  function setZipFile(f) {
    if (!/\.zip$/i.test(f.name)) { toast("only .zip archives are accepted", "error"); return; }
    state.zipFile = f;
    $("zipPicked").textContent = `selected: ${f.name} (${fmtBytes(f.size)})`;
  }

  function pollJob(jobId) {
    if (state.poll) clearInterval(state.poll);
    state.poll = setInterval(async () => {
      let job;
      try {
        job = await api(`/api/jobs/${jobId}`);
      } catch (e) {
        clearInterval(state.poll); state.poll = null;
        $("jobError").textContent = e.message;
        $("jobError").classList.remove("hidden");
        $("addGo").disabled = false;
        return;
      }
      $("jobFill").style.width = Math.max(0, Math.min(100, job.progress || 0)) + "%";
      $("jobMsg").textContent = job.message || "";
      if (job.state === "done") {
        clearInterval(state.poll); state.poll = null;
        closeAddModal();
        toast(`${job.label || "repository"} added`, "ok");
        state.zipFile = null;
        $("zipPicked").textContent = ""; $("zipFile").value = "";
        $("cloneUrl").value = ""; $("cloneName").value = "";
        await loadRepos(job.repo_id);
      } else if (job.state === "error") {
        clearInterval(state.poll); state.poll = null;
        $("jobError").textContent = job.error || "ingestion failed";
        $("jobError").classList.remove("hidden");
        $("addGo").disabled = false;
      }
    }, 450);
  }

  async function startAdd() {
    const go = $("addGo");
    go.disabled = true;
    $("jobError").classList.add("hidden");
    try {
      let jobId;
      if (state.addMode === "clone") {
        const url = ($("cloneUrl").value || "").trim();
        if (!url) throw new Error("enter a repository URL");
        const res = await api("/api/repos/clone", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ url, name: ($("cloneName").value || "").trim() })
        });
        jobId = res.job_id;
      } else {
        if (!state.zipFile) throw new Error("choose a .zip file first");
        const res = await api("/api/repos/upload", {
          method: "POST",
          headers: {
            "X-Filename": encodeURIComponent(state.zipFile.name),
            "Content-Type": "application/zip"
          },
          body: state.zipFile
        });
        jobId = res.job_id;
      }
      $("jobBox").classList.remove("hidden");
      $("jobMsg").textContent = "starting…";
      pollJob(jobId);
    } catch (e) {
      $("jobError").textContent = e.message;
      $("jobError").classList.remove("hidden");
      go.disabled = false;
    }
  }

  /* ── merge-authors modal ──────────────────────────────────────── */

  async function openMergeModal() {
    if (!state.repoId) { toast("load a repository first", "error"); return; }
    overlay(true, "loading identities…");
    try {
      const info = await api(`/api/repos/${state.repoId}/authors?ref=${encodeURIComponent(state.ref)}`);
      state.authorsInfo = info;
      renderMergeTable(info);
      $("mergeModal").classList.remove("hidden");
    } catch (e) {
      toast(e.message, "error");
    } finally {
      overlay(false);
    }
  }

  function nameOfKey(info, key) {
    const all = (info.canonical || []).concat(info.identities || []);
    const hit = all.find((a) => a.key === key);
    return hit ? `${hit.name} <${hit.email}>` : key;
  }

  function renderMergeTable(info) {
    const merges = info.merges || {};
    const target = $("mergeTarget");
    target.innerHTML = (info.canonical || []).map((a) =>
      `<option value="${esc(a.key)}">${esc(a.name)} &lt;${esc(a.email)}&gt;</option>`).join("");
    if (state.author) target.value = state.author;

    const rows = (info.identities || []).map((a) => {
      const into = merges[a.key];
      const badge = into ? ` <span class="badge" title="merged into ${esc(nameOfKey(info, into))}">→ ${esc(nameOfKey(info, into))}</span>` : "";
      return `<tr data-key="${esc(a.key)}">
        <td class="ck"><input type="checkbox" data-key="${esc(a.key)}"></td>
        <td class="path-cell">${esc(a.name)}${badge}</td>
        <td class="dim">${esc(a.email)}</td>
        <td>${fmtInt(a.commits)}</td>
        <td class="dim">${fmtDate(a.first)}</td>
        <td class="dim">${fmtDate(a.last)}</td>
      </tr>`;
    }).join("");
    $("mergeTable").innerHTML = `<thead><tr>
        <th class="ck"></th><th>Name</th><th>Email</th><th>Commits</th><th>First</th><th>Last</th>
      </tr></thead><tbody>${rows || '<tr><td colspan="6" style="text-align:center;padding:18px" class="muted">no identities</td></tr>'}</tbody>`;
    $("mergeStatus").textContent =
      `${(info.identities || []).length} identities · ${Object.keys(merges).length} manual merge(s)`;
  }

  async function applyMerges() {
    const sources = Array.from($("mergeTable").querySelectorAll("input:checked")).map((i) => i.dataset.key);
    const target = $("mergeTarget").value;
    if (!sources.length) { toast("tick at least one identity to merge", "error"); return; }
    if (!target) { toast("choose the identity to merge into", "error"); return; }
    try {
      await api(`/api/repos/${state.repoId}/merges`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ target, sources })
      });
      $("mergeModal").classList.add("hidden");
      toast(`merged ${sources.length} identit${sources.length === 1 ? "y" : "ies"} into ${nameOfKey(state.authorsInfo || {}, target)}`, "ok");
      await loadAuthors();
      await loadMetrics();
    } catch (e) {
      toast(e.message, "error");
    }
  }

  async function clearMerges() {
    try {
      await api(`/api/repos/${state.repoId}/merges`, { method: "DELETE" });
      toast("manual merges cleared", "ok");
      $("mergeModal").classList.add("hidden");
      await loadAuthors();
      await loadMetrics();
    } catch (e) {
      toast(e.message, "error");
    }
  }

  async function deleteRepo() {
    if (!state.repo) return;
    if (!confirm(`Delete repository "${state.repo.name}" and all cached data?`)) return;
    try {
      await api(`/api/repos/${state.repoId}`, { method: "DELETE" });
      toast("repository deleted", "ok");
      state.repoId = null; state.repo = null; state.data = null;
      await loadRepos();
    } catch (e) {
      toast(e.message, "error");
    }
  }

  /* ── exports ──────────────────────────────────────────────────── */

  function exportRows(kind) {
    if (!state.data || !state.repo) { toast("nothing to export", "error"); return; }
    const name = `${state.repo.name}-${kind}.csv`;
    if (kind === "files" || kind === "dirs") {
      const rows = kind === "files" ? fileRows() : dirRows();
      const own = state.scope.scoped;
      const header = ["path", "added", "removed", "growth", "churn", "mods", "modfreq", "churnrate"]
        .concat(own ? ["ownership"] : []);
      downloadCSV(name, header, rows.map((r) => header.map((h) => r[h])));
    } else {
      const rows = authorRows();
      const header = ["name", "email", "commits", "added", "removed", "growth", "churn", "mods", "ownership"];
      downloadCSV(name, header, rows.map((r) => header.map((h) => r[h])));
    }
    toast(`${kind} exported`, "ok");
  }

  /* ── event wiring ─────────────────────────────────────────────── */

  function wireSort(tableId, tstate, rerender) {
    $(tableId).addEventListener("click", (e) => {
      const th = e.target.closest("th[data-col]");
      if (!th) return;
      const col = th.dataset.col;
      if (tstate.sort === col) {
        tstate.dir = -tstate.dir;
      } else {
        tstate.sort = col;
        tstate.dir = (col === "path" || col === "name" || col === "email") ? 1 : -1;
      }
      rerender();
    });
  }

  function wireEvents() {
    $("repoSelect").addEventListener("change", (e) => {
      selectRepo(e.target.value).catch((err) => toast(err.message, "error"));
    });
    $("applyRefBtn").addEventListener("click", () => applyRef().catch((e) => toast(e.message, "error")));
    $("refInput").addEventListener("keydown", (e) => {
      if (e.key === "Enter") applyRef().catch((err) => toast(err.message, "error"));
    });
    $("addRepoBtn").addEventListener("click", openAddModal);
    $("emptyAddBtn").addEventListener("click", openAddModal);

    /* commit set modes */
    $("commitModeSeg").addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-mode]");
      if (!btn || btn.dataset.mode === state.mode) return;
      setMode(btn.dataset.mode);
      loadMetrics().catch((err) => toast(err.message, "error"));
    });

    /* range inputs */
    $("fromInput").addEventListener("change", (e) => {
      state.from = localInputToTs(e.target.value);
      if (state.mode !== "range") setMode("range");
      loadMetrics().catch((err) => toast(err.message, "error"));
    });
    $("toInput").addEventListener("change", (e) => {
      state.to = localInputToTs(e.target.value);
      if (state.mode !== "range") setMode("range");
      loadMetrics().catch((err) => toast(err.message, "error"));
    });
    document.querySelectorAll("[data-days]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const days = Number(btn.dataset.days);
        if (!days) {
          state.from = null; state.to = null;
          $("fromInput").value = ""; $("toInput").value = "";
        } else {
          const anchor = (state.data && state.data.commit_set && state.data.commit_set.to)
            || Math.floor(Date.now() / 1000);
          state.to = anchor;
          state.from = anchor - days * 86400;
          $("fromInput").value = tsToLocalInput(state.from);
          $("toInput").value = tsToLocalInput(state.to);
        }
        if (state.mode !== "range") setMode("range");
        loadMetrics().catch((err) => toast(err.message, "error"));
      });
    });

    /* manual commit selection */
    $("commitSearch").addEventListener("input", debounce((e) => {
      state.picker.q = e.target.value.trim();
      loadPicker(true).catch((err) => toast(err.message, "error"));
    }, 250));
    $("commitPicker").addEventListener("change", (e) => {
      if (e.target.matches("input[data-h]")) togglePicked(e.target.dataset.h, e.target.checked);
    });
    $("commitPicker").addEventListener("scroll", () => {
      const box = $("commitPicker");
      if (state.picker.rows.length >= state.picker.total) return;
      if (box.scrollTop + box.clientHeight >= box.scrollHeight - 40) {
        loadPicker(false).catch((err) => toast(err.message, "error"));
      }
    });
    $("clearPicked").addEventListener("click", () => {
      state.picked = []; state.pickedSet.clear();
      syncPickedUi(); syncCheckboxes();
      if (state.mode === "list") loadMetrics().catch((err) => toast(err.message, "error"));
    });

    /* author */
    $("authorSelect").addEventListener("change", (e) => setAuthor(e.target.value));
    $("mergeAuthorsBtn").addEventListener("click", openMergeModal);

    /* path */
    $("pathInput").addEventListener("input", (e) => {
      state.pathFilter = e.target.value.trim();
      renderFiles(); renderDirs(); renderChips();
    });
    $("detailBtn").addEventListener("click", () => {
      const p = ($("pathInput").value || "").trim();
      openDrawer(p, guessKind(p));
    });
    $("pathInput").addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        const p = ($("pathInput").value || "").trim();
        openDrawer(p, guessKind(p));
      }
    });
    $("clearPathBtn").addEventListener("click", () => {
      $("pathInput").value = "";
      state.pathFilter = "";
      renderFiles(); renderDirs(); renderChips();
    });

    /* tabs */
    $("tabs").addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-tab]");
      if (!btn) return;
      state.tab = btn.dataset.tab;
      document.querySelectorAll("#tabs > button").forEach((b) => b.classList.toggle("active", b === btn));
      document.querySelectorAll(".tab-pane").forEach((p) =>
        p.classList.toggle("active", p.id === "tab-" + state.tab));
      if (state.tab === "overview") {
        Charts.redraw($("chartChurn"));
        Charts.redraw($("chartGrowth"));
      }
      if (state.tab === "commitset" && !state.cs.loaded) {
        loadCs(true).catch((err) => toast(err.message, "error"));
      }
    });

    /* tables */
    wireSort("filesTable", state.tables.files, renderFiles);
    wireSort("dirsTable", state.tables.dirs, renderDirs);
    wireSort("authorsTable", state.tables.authors, renderAuthors);
    $("filesTable").addEventListener("click", (e) => {
      const tr = e.target.closest("tr[data-path]");
      if (tr && !e.target.closest("thead")) openDrawer(tr.dataset.path, "file");
    });
    $("dirsTable").addEventListener("click", (e) => {
      const tr = e.target.closest("tr[data-path]");
      if (tr && !e.target.closest("thead")) openDrawer(tr.dataset.path, "dir");
    });
    $("authorsTable").addEventListener("click", (e) => {
      const tr = e.target.closest("tr[data-key]");
      if (tr && !e.target.closest("thead")) setAuthor(tr.dataset.key);
    });
    $("filesSearch").addEventListener("input", debounce((e) => {
      state.tables.files.q = e.target.value;
      renderFiles();
    }, 150));
    $("dirsSearch").addEventListener("input", debounce((e) => {
      state.tables.dirs.q = e.target.value;
      renderDirs();
    }, 150));
    $("authorsSearch").addEventListener("input", debounce((e) => {
      state.tables.authors.q = e.target.value;
      renderAuthors();
    }, 150));
    $("filesMore").addEventListener("click", () => {
      state.tables.files.limit += 100;
      renderFiles();
    });
    $("dirsMore").addEventListener("click", () => {
      state.tables.dirs.limit += 100;
      renderDirs();
    });
    $("filesExport").addEventListener("click", () => exportRows("files"));
    $("dirsExport").addEventListener("click", () => exportRows("dirs"));
    $("authorsExport").addEventListener("click", () => exportRows("authors"));

    /* commit-set tab */
    $("csSearch").addEventListener("input", debounce((e) => {
      state.cs.q = e.target.value.trim();
      loadCs(true).catch((err) => toast(err.message, "error"));
    }, 250));
    $("csTable").addEventListener("click", (e) => {
      const tr = e.target.closest("tr[data-h]");
      if (!tr) return;
      const h = tr.dataset.h;
      const isInput = e.target.tagName === "INPUT";
      togglePicked(h, isInput ? e.target.checked : !state.pickedSet.has(h));
    });
    $("csMore").addEventListener("click", () => {
      loadCs(false).catch((err) => toast(err.message, "error"));
    });
    $("csAddRange").addEventListener("click", () => addCurrentRange().catch((err) => toast(err.message, "error")));
    $("csAddAll").addEventListener("click", () => addMany(state.cs.rows.map((c) => c.h)));
    $("csClear").addEventListener("click", () => {
      state.picked = []; state.pickedSet.clear();
      syncPickedUi(); syncCheckboxes();
      loadMetrics().catch((err) => toast(err.message, "error"));
    });

    /* drawer */
    $("drawerClose").addEventListener("click", closeDrawer);

    /* add modal */
    $("addModeSeg").addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-addmode]");
      if (btn) setAddMode(btn.dataset.addmode);
    });
    document.querySelectorAll("[data-close]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const id = btn.dataset.close;
        if (id === "addModal") closeAddModal();
        else $(id).classList.add("hidden");
      });
    });
    $("addGo").addEventListener("click", startAdd);
    $("zipFile").addEventListener("change", (e) => {
      if (e.target.files && e.target.files[0]) setZipFile(e.target.files[0]);
    });
    ["dragenter", "dragover"].forEach((ev) => {
      $("zipDrop").addEventListener(ev, (e) => {
        e.preventDefault();
        $("zipDrop").classList.add("dragover");
      });
    });
    ["dragleave", "drop"].forEach((ev) => {
      $("zipDrop").addEventListener(ev, (e) => {
        e.preventDefault();
        $("zipDrop").classList.remove("dragover");
      });
    });
    $("zipDrop").addEventListener("drop", (e) => {
      const f = e.dataTransfer && e.dataTransfer.files[0];
      if (f) setZipFile(f);
    });
    $("cloneUrl").addEventListener("keydown", (e) => {
      if (e.key === "Enter") startAdd();
    });

    /* merge modal */
    $("mergeApply").addEventListener("click", applyMerges);
    $("mergeClear").addEventListener("click", clearMerges);

    /* keyboard */
    document.addEventListener("keydown", (e) => {
      if (e.key !== "Escape") return;
      if (!$("drawer").classList.contains("hidden")) { closeDrawer(); return; }
      if (!$("mergeModal").classList.contains("hidden")) { $("mergeModal").classList.add("hidden"); return; }
      if (!$("addModal").classList.contains("hidden") && !state.poll) closeAddModal();
    });
  }

  /* ── boot ─────────────────────────────────────────────────────── */

  async function boot() {
    wireEvents();
    setAddMode("clone");
    setMode("all");
    try {
      await loadRepos();
    } catch (e) {
      toast("cannot reach the RAT server: " + e.message, "error");
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
