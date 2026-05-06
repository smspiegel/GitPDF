// gitpdf frontend. ESM, no build step.
//
// PDF.js is loaded from /vendor/pdfjs/. Run `python scripts/fetch_pdfjs.py`
// once after install to populate that directory.

import * as pdfjsLib from "/vendor/pdfjs/build/pdf.mjs";
pdfjsLib.GlobalWorkerOptions.workerSrc = "/vendor/pdfjs/build/pdf.worker.mjs";

const RENDER_SCALE = 1.5;

const state = {
  loadedSides: { A: false, B: false },
  overlays: [],                // flat list of overlay records from server
  diffOrder: [],               // unique diff_ids in document order
  focusIdx: -1,                // index into diffOrder
  pages: { A: [], B: [] },     // per-side: [{ page, viewport, wrapEl, layerEl }]
  pageCount: { A: 0, B: 0 },
  syncingScroll: false,
};

// -------- DOM helpers --------
const $ = (id) => document.getElementById(id);

function setStatus(msg) { $("status").textContent = msg; }

// -------- Upload --------
async function uploadFile(side, file) {
  const fd = new FormData();
  fd.append("side", side);
  fd.append("file", file);
  const r = await fetch("/api/upload", { method: "POST", body: fd });
  if (!r.ok) throw new Error(`upload failed: ${r.status}`);
  state.loadedSides[side] = true;
  setPaneFilename(side, file.name);
  await renderSide(side);
  $("run").disabled = !(state.loadedSides.A && state.loadedSides.B);
}

function setPaneFilename(side, name) {
  const el = $(`filename-${side.toLowerCase()}`);
  if (!el) return;
  el.textContent = name;
  el.classList.remove("empty");
  el.title = name;
}

// -------- Render PDF for one side --------
async function renderSide(side) {
  setStatus(`Rendering ${side}...`);
  const url = `/api/pdf/${side}?t=${Date.now()}`;
  const pdf = await pdfjsLib.getDocument(url).promise;
  state.pageCount[side] = pdf.numPages;
  const container = $(`pages-${side.toLowerCase()}`);
  container.replaceChildren();
  state.pages[side] = [];
  for (let p = 1; p <= pdf.numPages; p++) {
    const page = await pdf.getPage(p);
    const viewport = page.getViewport({ scale: RENDER_SCALE });
    const wrap = document.createElement("div");
    wrap.className = "page-wrap";
    wrap.dataset.page = String(p);
    wrap.style.width = `${viewport.width}px`;
    wrap.style.height = `${viewport.height}px`;
    const canvas = document.createElement("canvas");
    canvas.width = viewport.width;
    canvas.height = viewport.height;
    const layer = document.createElement("div");
    layer.className = "overlay-layer";
    wrap.append(canvas, layer);
    container.append(wrap);
    await page.render({ canvasContext: canvas.getContext("2d"), viewport }).promise;
    state.pages[side].push({ page: p, viewport, wrapEl: wrap, layerEl: layer });
  }
  setStatus(`${side} ready (${pdf.numPages} pages)`);
  applyOverlays(side);
}

// -------- Run diff --------
async function runDiff() {
  setStatus("Comparing...");
  $("run").disabled = true;
  try {
    const fd = new FormData();
    fd.append("mode", $("mode").value);
    const r = await fetch("/api/diff", { method: "POST", body: fd });
    if (!r.ok) throw new Error(`diff failed: ${r.status}`);
    const data = await r.json();
    state.overlays = data.overlays;
    buildDiffOrder(data.overlays);
    clearOverlayLayers();
    applyOverlays("A");
    applyOverlays("B");
    renderSummary(data.summary, data.mode_used, data.similarity);
    $("prev").disabled = state.diffOrder.length === 0;
    $("next").disabled = state.diffOrder.length === 0;
    state.focusIdx = -1;
    // When pages don't line up 1:1 -- either different page counts or low
    // similarity -- syncing the two scrollbars by ratio gives a slow drift
    // away from the diff the user clicked. Default to off so summary clicks
    // jump straight to the change. The user can still re-enable manually.
    const unalignedDocs =
      data.page_count_a !== data.page_count_b || data.mode_used === "diff-only";
    if (unalignedDocs && $("sync-scroll").checked) {
      $("sync-scroll").checked = false;
    }
    if (state.diffOrder.length) gotoDiff(0);
    const syncNote = unalignedDocs ? "  ·  sync scroll off (unaligned)" : "";
    setStatus(
      `Mode: ${data.mode_used}  ·  similarity ${(data.similarity * 100).toFixed(0)}%  ·  ${state.diffOrder.length} change(s)${syncNote}`
    );
  } catch (err) {
    setStatus(`Error: ${err.message}`);
  } finally {
    $("run").disabled = false;
  }
}

function buildDiffOrder(overlays) {
  // Order diffs by (preferred-side page, top), so prev/next reads top-to-bottom.
  const byId = new Map();
  for (const o of overlays) {
    const cur = byId.get(o.diff_id);
    const score = o.page * 100000 + o.bbox.y0;
    if (!cur || score < cur.score) byId.set(o.diff_id, { score, side: o.side });
  }
  state.diffOrder = [...byId.entries()]
    .sort((a, b) => a[1].score - b[1].score)
    .map(([id]) => id);
}

function clearOverlayLayers() {
  for (const side of ["A", "B"]) {
    for (const p of state.pages[side]) p.layerEl.replaceChildren();
  }
}

function applyOverlays(side) {
  for (const o of state.overlays) {
    if (o.side !== side) continue;
    const pageRec = state.pages[side][o.page - 1];
    if (!pageRec) continue;
    const el = document.createElement("div");
    el.className = `overlay ${o.kind}`;
    el.dataset.diffId = String(o.diff_id);
    el.style.left = `${o.bbox.x0 * RENDER_SCALE}px`;
    el.style.top = `${o.bbox.y0 * RENDER_SCALE}px`;
    el.style.width = `${(o.bbox.x1 - o.bbox.x0) * RENDER_SCALE}px`;
    el.style.height = `${(o.bbox.y1 - o.bbox.y0) * RENDER_SCALE}px`;
    el.title = `Diff ${o.diff_id} (${o.kind})`;
    el.addEventListener("click", () => focusDiffId(o.diff_id));
    pageRec.layerEl.append(el);
  }
}

// -------- Navigation --------
function focusDiffId(diffId) {
  const idx = state.diffOrder.indexOf(diffId);
  if (idx >= 0) gotoDiff(idx);
}

function gotoDiff(idx) {
  if (idx < 0 || idx >= state.diffOrder.length) return;
  state.focusIdx = idx;
  const diffId = state.diffOrder[idx];
  // Update focused styling on both sides + summary list.
  document.querySelectorAll(".overlay.focused").forEach(el => el.classList.remove("focused"));
  document.querySelectorAll("#summary-list li.focused").forEach(el => el.classList.remove("focused"));
  let firstScrollTarget = null;
  for (const o of state.overlays) {
    if (o.diff_id !== diffId) continue;
    const pageRec = state.pages[o.side][o.page - 1];
    if (!pageRec) continue;
    const el = pageRec.layerEl.querySelector(`[data-diff-id="${diffId}"]`);
    if (el) {
      el.classList.add("focused");
      if (!firstScrollTarget) firstScrollTarget = { side: o.side, el };
    }
  }
  if (firstScrollTarget) {
    firstScrollTarget.el.scrollIntoView({ behavior: "smooth", block: "center" });
  }
  const li = document.querySelector(`#summary-list li[data-diff-id="${diffId}"]`);
  if (li) {
    li.classList.add("focused");
    li.scrollIntoView({ block: "nearest" });
  }
}

// -------- Summary panel --------
function renderSummary(entries, modeUsed, similarity) {
  const list = $("summary-list");
  list.replaceChildren();
  for (const e of entries) {
    const li = document.createElement("li");
    li.className = e.kind;
    li.dataset.diffId = String(e.diff_id);
    const kind = document.createElement("span");
    kind.className = "kind";
    kind.textContent = e.kind;
    const snippet = document.createElement("span");
    snippet.className = "snippet";
    snippet.textContent = (e.text_a || e.text_b || "").slice(0, 100);
    const pageref = document.createElement("div");
    pageref.className = "pageref";
    const a = e.page_a != null ? `A p.${e.page_a}` : "";
    const b = e.page_b != null ? `B p.${e.page_b}` : "";
    pageref.textContent = [a, b].filter(Boolean).join("  ·  ");
    li.append(kind, snippet, pageref);
    li.addEventListener("click", () => focusDiffId(e.diff_id));
    list.append(li);
  }
}

// -------- Synced scroll --------
function setupSyncScroll() {
  const a = $("pane-a"), b = $("pane-b");
  function sync(from, to) {
    if (state.syncingScroll || !$("sync-scroll").checked) return;
    const ratio = from.scrollTop / Math.max(from.scrollHeight - from.clientHeight, 1);
    state.syncingScroll = true;
    to.scrollTop = ratio * (to.scrollHeight - to.clientHeight);
    requestAnimationFrame(() => { state.syncingScroll = false; });
  }
  a.addEventListener("scroll", () => sync(a, b));
  b.addEventListener("scroll", () => sync(b, a));
}

// -------- Wire up --------
$("file-a").addEventListener("change", (e) => e.target.files[0] && uploadFile("A", e.target.files[0]));
$("file-b").addEventListener("change", (e) => e.target.files[0] && uploadFile("B", e.target.files[0]));
$("run").addEventListener("click", runDiff);
$("prev").addEventListener("click", () => gotoDiff(Math.max(state.focusIdx - 1, 0)));
$("next").addEventListener("click", () => gotoDiff(Math.min(state.focusIdx + 1, state.diffOrder.length - 1)));
$("toggle-summary").addEventListener("click", () => {
  const aside = $("summary");
  const open = aside.hasAttribute("hidden");
  if (open) {
    aside.removeAttribute("hidden");
    document.querySelector("main").classList.add("with-summary");
  } else {
    aside.setAttribute("hidden", "");
    document.querySelector("main").classList.remove("with-summary");
  }
});
window.addEventListener("keydown", (e) => {
  if (!e.ctrlKey) return;
  if (e.key === "ArrowDown") { e.preventDefault(); gotoDiff(Math.min(state.focusIdx + 1, state.diffOrder.length - 1)); }
  if (e.key === "ArrowUp")   { e.preventDefault(); gotoDiff(Math.max(state.focusIdx - 1, 0)); }
});
$("run").disabled = true;
$("prev").disabled = true;
$("next").disabled = true;
setupSyncScroll();
setStatus("Open two PDFs to begin.");

// -------- Heartbeat / window-close detection --------
// The server tracks the most recent ping. If pings stop (tab closed,
// browser quit, machine slept), the server shuts itself down so the
// background gitpdf.exe process doesn't linger.
const HEARTBEAT_MS = 3000;
function sendHeartbeat() {
  fetch("/api/heartbeat", { method: "POST", keepalive: true }).catch(() => {});
}
sendHeartbeat();
setInterval(sendHeartbeat, HEARTBEAT_MS);

// Best-effort immediate-shutdown signal when the page actually unloads
// (not on bfcache navigation, where the page might come back).
window.addEventListener("pagehide", (e) => {
  if (e.persisted) return;
  if (navigator.sendBeacon) {
    navigator.sendBeacon("/api/shutdown");
  } else {
    fetch("/api/shutdown", { method: "POST", keepalive: true }).catch(() => {});
  }
});
