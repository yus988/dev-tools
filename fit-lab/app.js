/* fit-lab — CAD vs measured log for printed parts. Plain JS, no build step. */
"use strict";

const SCHEMA = "fit-lab/1";
const KINDS = ["hole", "boss", "gap", "height", "other"];
const ALL = "*";
const ID_RE = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;

/* UI labels (data keeps the English keys) */
const KIND_LABEL = { hole: "穴", boss: "ボス・軸", gap: "隙間", height: "高さ", other: "その他" };
const METHOD_LABEL = {
  caliper: "ノギス", micrometer: "マイクロメータ", "pin gauge": "ピンゲージ", "feeler gauge": "シックネスゲージ",
  "height gauge": "ハイトゲージ", "fit test": "はめあい確認", visual: "目視",
};
const BY_LABEL = { user: "本人" };
const PROV_TEXT = "仮の値（音声メモなど・未確認）";
const kindLabel = (k) => KIND_LABEL[k] || k || "";
const methodLabel = (m) => METHOD_LABEL[m] || m || "";
const byLabel = (b) => BY_LABEL[b] || b || "";

const state = {
  backend: null,       // serverBackend or a folder backend
  serverOk: false,     // serve.py answered /api/campaigns
  campaign: null,      // current campaign.json
  cid: null,
  dirty: 0,            // number of edits since last save
  saving: false,
  selNo: null,         // selected item number
  imageId: null,       // image shown in the panel
  includeProvisional: true,
  lastHash: location.hash,
};

/* hands-free voice entry (see the "voice entry" section) */
const voice = {
  on: false,
  rec: null,
  variant: null,       // variant id for per-variant items ("*" = 共通)
  cursor: null,        // item number that a bare "<値>" goes to
  pending: null,       // {no, key, value, raw} waiting for はい / a new value
  log: [],             // [{time, raw, action, undo: {no, m}, undone}]
  interim: "",
  status: "",
  saveTimer: null,
  ignoreUntil: 0,      // ignore results while our own read-back is playing
};

/* ---------- helpers ---------- */
const $ = (sel, root = document) => root.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const isNum = (v) => typeof v === "number" && Number.isFinite(v);
const round6 = (v) => Math.round(v * 1e6) / 1e6;
function fmt(v) {
  if (!isNum(v)) return "";
  let s = v.toFixed(3).replace(/0+$/, "");
  if ((s.split(".")[1] || "").length < 2) s = v.toFixed(2);
  return s;
}
const fmtD = (v) => (isNum(v) ? (v > 0 ? "+" : v < 0 ? "−" : "±") + fmt(Math.abs(v)) : "");
function today() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}
function prefGet(k, dflt) { try { return localStorage.getItem("fit-lab." + k) ?? dflt; } catch { return dflt; } }
function prefSet(k, v) { try { if (v == null) localStorage.removeItem("fit-lab." + k); else localStorage.setItem("fit-lab." + k, v); } catch { /* ignore */ } }
function prefJson(k, dflt) { try { return JSON.parse(prefGet(k, "")) || dflt; } catch { return dflt; } }

function toast(msg, err = false) {
  const el = document.createElement("div");
  el.className = "toast" + (err ? " err" : "");
  el.textContent = msg;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), err ? 6000 : 2500);
}

async function api(path, opts) {
  const r = await fetch(path, opts);
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.error || `${r.status} ${r.statusText}`);
  return body;
}

/* ---------- theme ---------- */
const THEMES = [["auto", "◐ 自動"], ["light", "☀ ライト"], ["dark", "☾ ダーク"]];
function applyTheme(t) {
  if (t === "light" || t === "dark") document.documentElement.dataset.theme = t;
  else delete document.documentElement.dataset.theme;
  $("#theme-btn").textContent = (THEMES.find((x) => x[0] === t) || THEMES[0])[1];
}
$("#theme-btn").onclick = () => {
  const cur = prefGet("theme", "auto");
  const next = THEMES[(THEMES.findIndex((x) => x[0] === cur) + 1) % THEMES.length][0];
  prefSet("theme", next === "auto" ? null : next);
  applyTheme(next);
};
applyTheme(prefGet("theme", "auto"));

/* ---------- data model ---------- */
function variantIds(c) {
  const v = (c.variants || []).map((x) => x.id).filter(Boolean);
  return v.length ? v : [ALL];
}
/** Variant keys an item is measured under: ["*"] for shared-CAD items, else the campaign variants. */
function itemKeys(c, it) {
  return ALL in (it.cad || {}) ? [ALL] : variantIds(c);
}
function cadOf(it, key) {
  const cad = it.cad || {};
  return isNum(cad[key]) ? cad[key] : isNum(cad[ALL]) ? cad[ALL] : null;
}
function measOf(it, key) {
  return (it.measurements || []).map((m, i) => ({ m, i })).filter((x) => x.m.variant === key);
}
function latest(it, key) {
  const list = measOf(it, key);
  if (!list.length) return null;
  list.sort((a, b) => String(a.m.date || "").localeCompare(String(b.m.date || "")) || a.i - b.i);
  return list[list.length - 1].m;
}
function deltaOf(it, key) {
  const m = latest(it, key);
  const cad = cadOf(it, key);
  if (!m || !isNum(m.value) || !isNum(cad)) return null;
  return round6(m.value - cad);
}
function dClass(d) {
  if (!isNum(d)) return "";
  const a = Math.abs(d);
  if (a <= 0.02) return "z";
  const lvl = a <= 0.05 ? 1 : a <= 0.1 ? 2 : a <= 0.2 ? 3 : 4;
  return (d < 0 ? "n" : "p") + lvl;
}
function campaignSummary(id, c) {
  const variants = (c.variants || []).map((v) => v.id).filter(Boolean);
  let cellsN = 0, measured = 0;
  for (const it of c.items || []) {
    const keys = itemKeys(c, it);
    const got = new Set((it.measurements || []).map((m) => m.variant));
    cellsN += keys.length;
    measured += keys.filter((k) => got.has(k)).length;
  }
  return {
    id, title: c.title || id, project: c.project || "", material: c.material || "", process: c.process || "",
    vendor: c.vendor || "", ordered: c.ordered || "", received: c.received || "", variants,
    items: (c.items || []).length, cells: cellsN, measured,
  };
}

/** Latest-per-cell records: one per item × key that has a numeric measurement. */
function cells(c, includeProvisional = true) {
  const out = [];
  for (const it of c.items || []) {
    for (const key of itemKeys(c, it)) {
      const m = latest(it, key);
      const cad = cadOf(it, key);
      if (!m || !isNum(m.value)) continue;
      if (!includeProvisional && m.provisional) continue;
      out.push({ item: it, key, cad, m, delta: isNum(cad) ? round6(m.value - cad) : null });
    }
  }
  return out;
}
function stats(values) {
  const v = values.filter(isNum);
  if (!v.length) return { n: 0 };
  const n = v.length;
  const mean = v.reduce((a, b) => a + b, 0) / n;
  const sd = n > 1 ? Math.sqrt(v.reduce((a, b) => a + (b - mean) ** 2, 0) / (n - 1)) : null;
  return { n, mean: round6(mean), sd: sd == null ? null : round6(sd), min: Math.min(...v), max: Math.max(...v) };
}

/* ---------- backends: serve.py, or a local folder (File System Access API) ---------- */
const serverBackend = {
  mode: "server",
  label: "サーバー",
  async list() { return api("/api/campaigns"); },
  async get(id) { return api(`/api/campaigns/${encodeURIComponent(id)}`); },
  async put(id, data) {
    return api(`/api/campaigns/${encodeURIComponent(id)}`, {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data),
    });
  },
  async imageUrl(id, file) { return `/data/${encodeURIComponent(id)}/${file.split("/").map(encodeURIComponent).join("/")}`; },
};

async function readJsonFile(dir, name) {
  const fh = await dir.getFileHandle(name);
  return JSON.parse(await (await fh.getFile()).text());
}
async function writeTextFile(dir, name, text) {
  const fh = await dir.getFileHandle(name, { create: true });
  const w = await fh.createWritable();   // writes to a swap file, replaced on close()
  await w.write(text);
  await w.close();
}

/** Folder mode. `root` is either a data root (folders of campaigns) or a single campaign folder. */
function folderBackend(root) {
  let dirs = null;            // id -> directory handle
  const urls = new Map();     // object URLs for images
  const scan = async () => {
    dirs = new Map();
    try {
      await root.getFileHandle("campaign.json");
      dirs.set(root.name, root);
      return;
    } catch { /* not a campaign folder: scan children */ }
    for await (const [name, h] of root.entries()) {
      if (h.kind !== "directory" || !ID_RE.test(name)) continue;
      try { await h.getFileHandle("campaign.json"); dirs.set(name, h); } catch { /* skip */ }
    }
  };
  const dirOf = async (id) => {
    if (!dirs) await scan();
    const d = dirs.get(id);
    if (!d) throw new Error(`campaign "${id}" が見つかりません`);
    return d;
  };
  return {
    mode: "folder",
    label: "フォルダ",
    handle: root,
    async list() {
      await scan();
      const campaigns = [];
      for (const [id, d] of [...dirs.entries()].sort()) {
        try { campaigns.push(campaignSummary(id, await readJsonFile(d, "campaign.json"))); }
        catch (e) { campaigns.push({ id, title: id, error: String(e.message || e) }); }
      }
      return { root: root.name + "（ローカルフォルダ）", campaigns };
    },
    async get(id) { return readJsonFile(await dirOf(id), "campaign.json"); },
    async put(id, data) {
      if (!data || data.schema !== SCHEMA) throw new Error(`"schema" は "${SCHEMA}" が必要です`);
      if (data.id !== id) throw new Error('"id" がフォルダ名と一致しません');
      const d = await dirOf(id);
      try {
        const old = await (await (await d.getFileHandle("campaign.json")).getFile()).text();
        await writeTextFile(d, "campaign.json.bak", old);
      } catch { /* first save: nothing to back up */ }
      await writeTextFile(d, "campaign.json", JSON.stringify(data, null, 2) + "\n");
      return { ok: true };
    },
    async imageUrl(id, file) {
      const key = id + "/" + file;
      if (!urls.has(key)) {
        let dir = await dirOf(id);
        const parts = file.split("/").filter((p) => p && p !== ".");
        if (parts.includes("..")) throw new Error("bad image path");
        for (const p of parts.slice(0, -1)) dir = await dir.getDirectoryHandle(p);
        const f = await (await dir.getFileHandle(parts[parts.length - 1])).getFile();
        urls.set(key, URL.createObjectURL(f));
      }
      return urls.get(key);
    },
  };
}

/* remember the last folder handle in IndexedDB (browser-local convenience only) */
function idb(op, value) {
  return new Promise((resolve) => {
    try {
      const req = indexedDB.open("fit-lab", 1);
      req.onupgradeneeded = () => req.result.createObjectStore("kv");
      req.onerror = () => resolve(null);
      req.onsuccess = () => {
        try {
          const tx = req.result.transaction("kv", op === "get" ? "readonly" : "readwrite");
          const st = tx.objectStore("kv");
          const r = op === "get" ? st.get("folder") : st.put(value, "folder");
          r.onsuccess = () => resolve(op === "get" ? r.result : true);
          r.onerror = () => resolve(null);
        } catch { resolve(null); }
      };
    } catch { resolve(null); }
  });
}
const fsSupported = () => typeof window.showDirectoryPicker === "function";

async function useFolder(handle) {
  const perm = await handle.requestPermission({ mode: "readwrite" });
  if (perm !== "granted") { toast("フォルダへのアクセスが許可されませんでした", true); return; }
  state.backend = folderBackend(handle);
  idb("put", handle);
  renderModeTag();
  if (location.hash && location.hash !== "#/") location.hash = "#/"; else route();
}
async function pickFolder() {
  if (state.dirty && !confirm("未保存の測定値があります。破棄してフォルダを開きますか？")) return;
  state.dirty = 0;
  try { await useFolder(await window.showDirectoryPicker({ id: "fit-lab", mode: "readwrite" })); }
  catch (e) { if (e.name !== "AbortError") toast("フォルダを開けません: " + e.message, true); }
}
function renderModeTag() {
  $("#mode-tag").textContent = state.backend ? state.backend.label : "未接続";
}

/* ---------- CSV ---------- */
const CSV_COLS = ["campaign", "material", "item_no", "name", "part", "feature", "kind", "direction", "unit", "variant",
  "cad", "mate", "measured", "delta", "latest", "provisional", "date", "by", "method", "note"];
function csvRows(c) {
  const rows = [];
  for (const it of c.items || []) {
    for (const key of itemKeys(c, it)) {
      const cad = cadOf(it, key);
      const lt = latest(it, key);
      const base = {
        campaign: c.id, material: c.material || "", item_no: it.no, name: it.name, part: it.part || "",
        feature: it.feature || "", kind: it.kind || "", direction: it.direction || "", unit: it.unit || "mm",
        variant: key, cad: isNum(cad) ? cad : "", mate: it.mate && isNum(it.mate.value) ? it.mate.value : "",
      };
      const list = measOf(it, key);
      if (!list.length) rows.push({ ...base, measured: "", delta: "", latest: "", provisional: "", date: "", by: "", method: "", note: "" });
      for (const { m } of list) {
        rows.push({
          ...base, measured: isNum(m.value) ? m.value : "",
          delta: isNum(m.value) && isNum(cad) ? round6(m.value - cad) : "",
          latest: m === lt ? 1 : 0, provisional: m.provisional ? 1 : 0,
          date: m.date || "", by: m.by || "", method: m.method || "", note: m.note || "",
        });
      }
    }
  }
  return rows;
}
function csvText(rows) {
  const q = (v) => { const s = String(v ?? ""); return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s; };
  return "﻿" + [CSV_COLS.join(","), ...rows.map((r) => CSV_COLS.map((k) => q(r[k])).join(","))].join("\r\n") + "\r\n";
}
function download(name, text) {
  const url = URL.createObjectURL(new Blob([text], { type: "text/csv;charset=utf-8" }));
  const a = document.createElement("a");
  a.href = url; a.download = name; document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/* ---------- routing ---------- */
function parseRoute(h = location.hash) {
  const p = h.replace(/^#\/?/, "").split("/").filter(Boolean).map(decodeURIComponent);
  if (p[0] === "c" && p[1]) return { view: p[2] === "summary" ? "csummary" : p[2] === "details" ? "details" : "items", cid: p[1] };
  if (p[0] === "summary") return { view: "xsummary" };
  return { view: "list" };
}
window.addEventListener("hashchange", () => {
  const next = parseRoute();
  if (state.dirty && next.cid !== state.cid) {
    if (!confirm("未保存の測定値は失われます。このキャンペーンを離れますか？")) {
      history.replaceState(null, "", state.lastHash || "#/");
      return;
    }
    state.dirty = 0;
  }
  state.lastHash = location.hash;
  route();
});
window.addEventListener("beforeunload", (e) => { if (state.dirty) { e.preventDefault(); e.returnValue = ""; } });
window.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s" && state.campaign) { e.preventDefault(); save(); }
  if (e.key === "Escape") closePopover();
});

async function route() {
  closePopover();
  const r = parseRoute();
  if (voice.on && (r.view !== "items" || r.cid !== state.cid)) stopVoice();
  try {
    if (!state.backend || r.view === "list") return await viewList();
    if (r.view === "xsummary") return await viewCross();
    if (state.cid !== r.cid || !state.campaign) await loadCampaign(r.cid);
    if (r.view === "items") viewItems();
    else if (r.view === "details") viewDetails();
    else viewCampaignSummary();
  } catch (err) {
    $("#view").innerHTML = `<div class="page"><p class="empty">${esc(err.message)}</p></div>`;
  }
}

async function loadCampaign(cid) {
  state.campaign = await state.backend.get(cid);
  state.cid = cid;
  state.dirty = 0;
  state.selNo = null;
  state.imageId = (state.campaign.images || [])[0]?.id || null;
}

function setChrome(crumbs, actionsHtml = "") {
  $("#crumbs").innerHTML = crumbs;
  $("#actions").innerHTML = actionsHtml;
}

/* ---------- campaign list ---------- */
async function viewList() {
  state.campaign = null; state.cid = null;
  const be = state.backend;
  setChrome(`<span class="crumb-title">キャンペーン一覧</span>`, be ? `<a href="#/summary">知見（全キャンペーン集計）→</a>` : "");
  const remembered = fsSupported() ? await idb("get") : null;
  const folderBox = `<div class="open-folder">
      ${fsSupported()
        ? `<p>ローカルフォルダを開いて、サーバーなしで読み書きできます（Chrome / Edge）。データのルート（キャンペーンのフォルダが並ぶ場所）か、1 つのキャンペーンのフォルダを選んでください。</p>
           <button type="button" id="pick-folder">フォルダを開く…</button>
           ${remembered && (!be || be.mode !== "folder") ? ` <button type="button" id="reopen-folder">前回のフォルダ「${esc(remembered.name)}」を開く</button>` : ""}
           ${state.serverOk && be && be.mode === "folder" ? ` <button type="button" id="use-server">サーバーのデータに戻る</button>` : ""}`
        : `<p>このブラウザはローカルフォルダの読み書き（File System Access API）に対応していません。Chrome / Edge を使うか、<code>python serve.py --root &lt;データの場所&gt;</code> で起動してください。</p>`}
    </div>`;
  if (!be) {
    $("#view").innerHTML = `<div class="page">
      <p class="meta">サーバー（serve.py）が見つかりません。ローカルフォルダを開いてください。</p>${folderBox}</div>`;
    bindFolderButtons(remembered);
    return;
  }
  const data = await be.list();
  const cards = data.campaigns.map((c) => {
    if (c.error) return `<div class="card"><div class="title">${esc(c.id)}</div><div class="meta">読み込みエラー: ${esc(c.error)}</div></div>`;
    const pct = c.cells ? Math.round((100 * c.measured) / c.cells) : 0;
    return `<a class="card" href="#/c/${encodeURIComponent(c.id)}">
      <div class="title">${esc(c.title)}</div>
      <div class="meta"><span>${esc(c.material)}</span><span>${esc(c.process)}</span>${c.vendor ? `<span>${esc(c.vendor)}</span>` : ""}</div>
      <div class="meta"><span>発注 ${esc(c.ordered || "—")}</span><span>受領 ${esc(c.received || "—")}</span></div>
      <div class="meta"><span>${c.items} 項目</span><span>${esc(c.variants.join(", ") || "バリアントなし")}</span><span>実測 ${c.measured}/${c.cells}</span></div>
      <div class="bar"><div style="width:${pct}%"></div></div></a>`;
  }).join("");
  $("#view").innerHTML = `<div class="page">
    <div class="meta">データ: <code>${esc(data.root)}</code></div>
    ${data.campaigns.length ? `<div class="cards">${cards}</div>` : `<p class="empty">キャンペーンがありません。データのルートに <code>&lt;id&gt;/campaign.json</code> を置いてください。</p>`}
    ${folderBox}</div>`;
  bindFolderButtons(remembered);
}
function bindFolderButtons(remembered) {
  const pick = $("#pick-folder"); if (pick) pick.onclick = pickFolder;
  const re = $("#reopen-folder"); if (re) re.onclick = () => useFolder(remembered).catch((e) => toast(e.message, true));
  const sv = $("#use-server");
  if (sv) sv.onclick = () => { state.backend = serverBackend; renderModeTag(); route(); };
}

/* ---------- campaign: shared header ---------- */
function statusText() {
  if (state.saving) return ["保存中…", ""];
  if (state.dirty) return [`未保存の変更 ${state.dirty} 件`, "dirty"];
  return ["保存済み", ""];
}
function renderStatus() {
  const el = $("#save-status");
  if (!el) return;
  const [t, cls] = statusText();
  el.textContent = t;
  el.className = "status " + cls;
  const btn = $("#save-btn");
  if (btn) btn.disabled = !state.dirty || state.saving;
}
function campaignChrome(tab) {
  const c = state.campaign;
  const href = `#/c/${encodeURIComponent(c.id)}`;
  setChrome(`<a href="#/">キャンペーン一覧</a> / <span class="crumb-title" title="${esc(c.title || c.id)}">${esc(c.title || c.id)}</span>`,
    `<span id="save-status" class="status"></span>
     <button id="csv-btn" type="button">CSV 出力</button>
     <button id="save-btn" class="primary" type="button" title="Ctrl+S">保存</button>`);
  $("#save-btn").onclick = save;
  $("#csv-btn").onclick = () => download(`${c.id}.csv`, csvText(csvRows(c)));
  renderStatus();
  const t = (id, path, label) => `<a href="${href}${path}" class="${tab === id ? "on" : ""}">${label}</a>`;
  return `<div class="tabs">${t("items", "", "項目")}${t("summary", "/summary", "集計")}${t("details", "/details", "詳細")}</div>`;
}

/** Variant × parameter table. */
function variantsTable(c) {
  const vs = c.variants || [];
  if (!vs.length) return `<p class="meta">バリアントなし（全項目共通）</p>`;
  const keys = [...new Set(vs.flatMap((v) => Object.keys(v.params || {})))];
  return `<table class="params"><thead><tr><th>パラメータ</th>${vs.map((v) => `<th>${esc(v.id)}</th>`).join("")}</tr></thead><tbody>
    <tr><td>説明</td>${vs.map((v) => `<td>${esc(v.label || "")}</td>`).join("")}</tr>
    ${keys.map((k) => `<tr><td><code>${esc(k)}</code></td>${vs.map((v) => `<td class="num">${esc(v.params?.[k] ?? "")}</td>`).join("")}</tr>`).join("")}
  </tbody></table>`;
}

async function save() {
  if (!state.campaign || state.saving || !state.dirty) return;
  state.saving = true; renderStatus();
  const edits = state.dirty;
  try {
    await state.backend.put(state.cid, state.campaign);
    state.dirty = Math.max(0, state.dirty - edits);
    toast("campaign.json を保存しました");
  } catch (err) {
    toast("保存に失敗: " + err.message, true);
  } finally {
    state.saving = false; renderStatus();
  }
}
function markDirty() {
  state.dirty++;
  renderStatus();
  if (voice.on) {   // autosave while hands-free
    clearTimeout(voice.saveTimer);
    voice.saveTimer = setTimeout(save, 2000);
  }
}

/* ---------- layout prefs: splitter + column widths ---------- */
const COLS = ["no", "name", "part", "kind", "dir", "mate"];
const VCOLS = ["cad", "meas", "d"];
function applyColWidths() {
  const w = prefJson("colw", {});
  const root = document.documentElement.style;
  for (const k of [...COLS, ...VCOLS]) {
    if (isNum(w[k])) root.setProperty(`--w-${k}`, w[k] + "px"); else root.removeProperty(`--w-${k}`);
  }
}
applyColWidths();

function bindSplitter() {
  const split = $(".split");
  const bar = $(".splitter");
  if (!split || !bar) return;
  const saved = parseFloat(prefGet("imgw", ""));
  if (Number.isFinite(saved)) split.style.setProperty("--img-w", saved + "%");
  bar.onpointerdown = (e) => {
    e.preventDefault();
    try { bar.setPointerCapture(e.pointerId); } catch { /* synthetic pointer */ }
    bar.classList.add("drag");
    const rect = split.getBoundingClientRect();
    let pct = null;
    bar.onpointermove = (ev) => {
      pct = Math.min(75, Math.max(15, ((rect.right - ev.clientX) / rect.width) * 100));
      split.style.setProperty("--img-w", pct.toFixed(1) + "%");
    };
    bar.onpointerup = () => {
      bar.onpointermove = bar.onpointerup = null;
      bar.classList.remove("drag");
      if (pct != null) prefSet("imgw", pct.toFixed(1));
      fitImage();
    };
  };
  bar.ondblclick = () => { split.style.removeProperty("--img-w"); prefSet("imgw", null); fitImage(); };
}

function bindColGrips() {
  document.querySelectorAll("table.items .col-grip").forEach((g) => {
    g.onclick = (e) => e.stopPropagation();
    g.onpointerdown = (e) => {
      e.preventDefault(); e.stopPropagation();
      const key = g.dataset.col;
      const th = g.parentElement;
      const startX = e.clientX, startW = th.getBoundingClientRect().width;
      try { g.setPointerCapture(e.pointerId); } catch { /* synthetic pointer */ }
      g.classList.add("drag");
      let w = null;
      g.onpointermove = (ev) => {
        w = Math.max(28, Math.round(startW + ev.clientX - startX));
        document.documentElement.style.setProperty(`--w-${key}`, w + "px");
      };
      g.onpointerup = () => {
        g.onpointermove = g.onpointerup = null;
        g.classList.remove("drag");
        if (w != null) { const all = prefJson("colw", {}); all[key] = w; prefSet("colw", JSON.stringify(all)); }
      };
    };
    g.ondblclick = (e) => {
      e.stopPropagation();
      const all = prefJson("colw", {}); delete all[g.dataset.col]; prefSet("colw", JSON.stringify(all)); applyColWidths();
    };
  });
}

/* ---------- campaign: items view ---------- */
function viewItems() {
  const c = state.campaign;
  const vids = variantIds(c).filter((v) => v !== ALL);
  if (!voice.variant || ![...vids, ALL].includes(voice.variant)) voice.variant = vids[0] || ALL;
  const step = prefGet("step", "0.01");
  const varOpts = [...vids, ALL].map((v) => `<option value="${esc(v)}"${v === voice.variant ? " selected" : ""}>${v === ALL ? "共通" : esc(v)}</option>`).join("");
  $("#view").innerHTML = `<div class="campaign">${campaignChrome("items")}
    <div class="toolbar">
      <label title="↑↓・ホイールの刻み（Shift で 10 倍）">刻み <select id="step-sel">${["0.01", "0.05", "0.1"].map((s) => `<option${s === step ? " selected" : ""}>${s}</option>`).join("")}</select></label>
      <label title="音声入力で値を入れるバリアント">バリアント <select id="var-sel">${varOpts}</select></label>
      <button type="button" id="voice-btn" class="voice-btn" title="マイクで連続入力（Chrome / Edge）"></button>
      <button type="button" id="voice-help-btn" class="small help-btn" title="音声コマンドの一覧">?</button>
      <span class="mini-meter" title="マイクの入力レベル"><span class="meter-bar"></span></span>
      <label title="記録した値を読み上げる"><input type="checkbox" id="readback"${prefGet("readback", "") === "1" ? " checked" : ""}> 読み上げ</label>
      <div class="live" id="live"><span class="st" id="live-st"></span><span id="live-text"></span></div>
    </div>
    <div class="voice-log" id="voice-log"${voice.on ? "" : " hidden"}></div>
    <details class="diag" id="diag"${prefGet("diag-open", "") === "1" ? " open" : ""}><summary>音声の診断</summary>
      <div class="diag-body">
        <div class="diag-info" id="diag-info"></div>
        <div class="diag-meter"><span class="meter"><span class="meter-bar"></span></span><span id="meter-db">—</span><button type="button" class="small" id="diag-copy">診断をコピー</button><button type="button" class="small" id="diag-clear">ログを消去</button></div>
        <pre class="diag-log" id="diag-log"></pre>
      </div>
    </details>
    <div class="split"><div class="table-wrap" id="table-wrap"></div><div class="splitter" title="ドラッグで幅を変更（ダブルクリックで既定に戻す）"></div><aside class="image-pane" id="image-pane"></aside></div></div>`;
  renderTable();
  bindSplitter();
  renderImage();
  $("#step-sel").onchange = (e) => prefSet("step", e.target.value);
  $("#var-sel").onchange = (e) => { voice.variant = e.target.value; voice.pending = null; renderLive(); };
  $("#readback").onchange = (e) => prefSet("readback", e.target.checked ? "1" : null);
  $("#voice-btn").onclick = () => (voice.on ? stopVoice() : startVoice());
  $("#voice-help-btn").onclick = (e) => openVoiceHelp(e.currentTarget);
  $("#diag").ontoggle = (e) => prefSet("diag-open", e.target.open ? "1" : null);
  $("#diag-copy").onclick = () => navigator.clipboard.writeText(diagText()).then(() => toast("診断をコピーしました"), () => toast("コピーできませんでした", true));
  $("#diag-clear").onclick = () => { diag.log = []; renderDiag(); };
  renderDiag();
  renderVoiceUi();
}

function measInput(it, key) {
  const m = latest(it, key);
  const n = measOf(it, key).length;
  const tip = m
    ? [m.provisional ? PROV_TEXT : "", [m.date, methodLabel(m.method), m.by ? `測定者: ${byLabel(m.by)}` : ""].filter(Boolean).join(" · "), m.note, n > 1 ? `履歴 ${n} 件` : ""].filter(Boolean).join("\n")
    : "";
  const val = m && isNum(m.value) ? m.value.toFixed(2) : "";
  return `<span class="meas-cell">${n > 1 ? `<span class="hist">${n}×</span>` : ""}<input type="text" class="meas-in${m ? " has" : ""}${m?.provisional ? " prov" : ""}" data-no="${it.no}" data-key="${esc(key)}" value="${val}" placeholder="＋" inputmode="decimal" autocomplete="off" spellcheck="false" ${tip ? ` title="${esc(tip)}"` : ""}></span>`;
}

/* ---------- inline measured-value input: starts from CAD, steps with ↑↓ / wheel ---------- */
const itemByNo = (no) => (state.campaign.items || []).find((x) => x.no === no);
const norm2 = (s) => { const v = parseFloat(s); return s === "" || !Number.isFinite(v) ? String(s).trim() : v.toFixed(2); };

function stepInput(inp, dir, coarse) {
  const step = Number(prefGet("step", "0.01")) * (coarse ? 10 : 1);
  const it = itemByNo(Number(inp.dataset.no));
  let v = parseFloat(inp.value);
  if (!Number.isFinite(v)) v = cadOf(it, inp.dataset.key) ?? 0;
  inp.value = Math.max(0, Math.round((v + dir * step) * 100) / 100).toFixed(2);
}

function addMeasurement(no, key, value, extra = {}) {
  const it = itemByNo(no);
  const m = { variant: key, value: Math.round(value * 1e4) / 1e4, date: today(), by: "user", method: "caliper", ...extra };
  (it.measurements ||= []).push(m);
  markDirty();
  return m;
}

/** Commit if changed, re-render, then focus the next cell: "down" = same column next row, ±1 = DOM order. */
function commitInput(inp, move) {
  const no = Number(inp.dataset.no), key = inp.dataset.key;
  const v = parseFloat(inp.value);
  let changed = false;
  if (Number.isFinite(v) && norm2(inp.value) !== norm2(inp.dataset.start ?? "")) {
    addMeasurement(no, key, v);
    changed = true;
  }
  const all = [...document.querySelectorAll(".meas-in")];
  const i = all.indexOf(inp);
  let next = null;
  if (move === "down") next = all.slice(i + 1).find((x) => x.dataset.key === key) || all[i + 1];
  else if (typeof move === "number") next = all[i + move];
  const target = next ? [next.dataset.no, next.dataset.key] : null;
  inp.dataset.done = "1";
  if (changed) { renderTable(); flashRow(no); }
  else { inp.value = inp.dataset.orig ?? inp.value; }
  if (target) document.querySelector(`.meas-in[data-no="${target[0]}"][data-key="${CSS.escape(target[1])}"]`)?.focus();
  else if (!changed) inp.blur();
}

function bindMeasInputs(root) {
  root.querySelectorAll(".meas-in").forEach((inp) => {
    const no = Number(inp.dataset.no), key = inp.dataset.key;
    inp.onfocus = () => {
      inp.dataset.orig = inp.value;
      delete inp.dataset.done;
      const cad = cadOf(itemByNo(no), key);
      if (!inp.value && isNum(cad)) inp.value = cad.toFixed(2);
      inp.dataset.start = inp.value;
      inp.select();
      if (state.selNo !== no) select(no, false);
      if (voice.on) setCursor(no, false);
    };
    inp.onblur = () => { if (!inp.dataset.done) inp.value = inp.dataset.orig ?? inp.value; };
    inp.onkeydown = (e) => {
      if (e.key === "ArrowUp" || e.key === "ArrowDown") { e.preventDefault(); stepInput(inp, e.key === "ArrowUp" ? 1 : -1, e.shiftKey); }
      else if (e.key === "Enter") { e.preventDefault(); commitInput(inp, "down"); }
      else if (e.key === "Tab") { e.preventDefault(); commitInput(inp, e.shiftKey ? -1 : 1); }
      else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); inp.value = inp.dataset.orig ?? ""; inp.dataset.done = "1"; inp.blur(); }
    };
    inp.addEventListener("wheel", (e) => {
      if (document.activeElement !== inp) return;   // never hijack page scrolling
      e.preventDefault();
      stepInput(inp, e.deltaY < 0 ? 1 : -1, e.shiftKey);
    }, { passive: false });
    inp.ondblclick = () => openEditor(no, key, inp);
  });
}

function flashRow(no) {
  const tr = document.querySelector(`table.items tbody tr[data-no="${no}"]`);
  if (!tr) return;
  tr.classList.remove("flash");
  void tr.offsetWidth;   // restart the animation
  tr.classList.add("flash");
}

function renderTable() {
  const c = state.campaign;
  const vids = variantIds(c);
  const vmeta = Object.fromEntries((c.variants || []).map((v) => [v.id, v]));
  const grip = (k) => `<span class="col-grip" data-col="${k}" title="ドラッグで列幅を変更（ダブルクリックで既定）"></span>`;
  const colgroup = `<colgroup>${COLS.map((k) => `<col style="width:var(--w-${k})">`).join("")}${vids.map(() => VCOLS.map((k) => `<col style="width:var(--w-${k})">`).join("")).join("")}</colgroup>`;
  const tableW = `calc(${[...COLS.map((k) => `var(--w-${k})`), ...vids.flatMap(() => VCOLS.map((k) => `var(--w-${k})`))].join(" + ")})`;
  const fixedHead = [["no", "No"], ["name", "名称"], ["part", "部品"], ["kind", "種類"], ["dir", "方向"], ["mate", "相手部品"]]
    .map(([k, t]) => `<th rowspan="2">${t}${grip(k)}</th>`).join("");
  const head1 = vids.map((v) => `<th class="vg" colspan="3" title="${esc(vmeta[v]?.label || "")}">${v === ALL ? "全共通" : esc(v)}</th>`).join("");
  const head2 = vids.map(() => `<th class="c-first">CAD${grip("cad")}</th><th>実測${grip("meas")}</th><th>Δ${grip("d")}</th>`).join("");
  const rows = (c.items || []).map((it) => {
    const keys = itemKeys(c, it);
    let groups;
    if (keys[0] === ALL && vids[0] !== ALL) {
      const d = deltaOf(it, ALL);
      groups = `<td class="c-first meas" colspan="${vids.length * 3}"><div class="all-group">
        <span class="num">${esc(fmt(cadOf(it, ALL)))}</span>${measInput(it, ALL)}
        <span class="d ${dClass(d)}">${esc(fmtD(d))}</span><span class="lbl">全バリアント共通</span></div></td>`;
    } else {
      groups = vids.map((k) => {
        const d = deltaOf(it, k);
        return `<td class="num c-first">${esc(fmt(cadOf(it, k)))}</td><td class="meas">${measInput(it, k)}</td><td class="num d ${dClass(d)}">${esc(fmtD(d))}</td>`;
      }).join("");
    }
    const mate = it.mate ? `${esc(it.mate.name || "")}${isNum(it.mate.value) ? ` <b>${esc(fmt(it.mate.value))}</b>` : ""}` : "";
    const tip = [it.name, it.feature, it.note].filter(Boolean).join("\n");
    return `<tr data-no="${it.no}" class="${state.selNo === it.no ? "sel" : ""}${voice.on && voice.cursor === it.no ? " cursor" : ""}">
      <td class="num">${esc(it.no)}</td>
      <td title="${esc(tip)}">${esc(it.name)}${it.note ? " ✎" : ""}${it.image ? "" : ' <span class="noimg" title="画像なし">∅</span>'}</td>
      <td title="${esc(it.part || "")}">${esc(it.part || "")}</td><td title="${esc(it.kind || "")}"><span class="kind">${esc(kindLabel(it.kind))}</span></td><td>${esc(it.direction || "")}</td>
      <td title="${esc(it.mate ? `${it.mate.name || ""} ${fmt(it.mate.value)}` : "")}">${mate}</td>${groups}</tr>`;
  }).join("");
  const wrap = $("#table-wrap");
  wrap.innerHTML = `<table class="items" style="width:${tableW}">${colgroup}<thead>
      <tr>${fixedHead}${head1}</tr>
      <tr>${head2}</tr></thead><tbody>${rows || `<tr><td colspan="${6 + vids.length * 3}" class="empty">項目なし</td></tr>`}</tbody></table>
    <div class="legend">Δ = 実測 − CAD（${esc((c.items || [])[0]?.unit || "mm")}）:
      <span class="sw d n4">≤ −0.2</span><span class="sw d n3">−0.2</span><span class="sw d n2">−0.1</span><span class="sw d n1">−0.05</span>
      <span class="sw d z">±0.02</span><span class="sw d p1">+0.05</span><span class="sw d p2">+0.1</span><span class="sw d p3">+0.2</span><span class="sw d p4">&gt; +0.2</span>
      <span class="sep"></span><span class="sw prov">斜体</span> = ${PROV_TEXT}
      <span class="sep"></span>実測: クリックで CAD 値から入力、↑↓ / ホイールで増減、Enter で記録、ダブルクリックで履歴</div>`;
  wrap.querySelectorAll("tbody tr[data-no]").forEach((tr) => {
    tr.onclick = (e) => {
      if (e.target.closest(".meas-in")) return;   // the input's focus handler selects
      const no = Number(tr.dataset.no);
      select(no, false);
      if (voice.on) setCursor(no, false);
    };
  });
  bindMeasInputs(wrap);
  bindColGrips();
}

function select(no, scrollRow) {
  const c = state.campaign;
  state.selNo = no;
  const it = (c.items || []).find((x) => x.no === no);
  document.querySelectorAll("table.items tbody tr").forEach((tr) => tr.classList.toggle("sel", Number(tr.dataset.no) === no));
  if (it && it.image && it.image !== state.imageId) { state.imageId = it.image; renderImage(); }
  else redrawOverlay();
  if (scrollRow) document.querySelector(`table.items tbody tr[data-no="${no}"]`)?.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

/* ---------- image panel + annotation overlay ---------- */
let currentImg = null;   // {meta, W, H}
async function renderImage() {
  const c = state.campaign;
  const pane = $("#image-pane");
  if (!pane) return;
  const imgs = c.images || [];
  if (!imgs.length) { pane.innerHTML = `<div class="empty">このキャンペーンには画像がありません。</div>`; return; }
  const img = imgs.find((x) => x.id === state.imageId) || imgs[0];
  state.imageId = img.id;
  const tabs = imgs.map((x) => {
    const n = (c.items || []).filter((it) => it.image === x.id).length;
    return `<button type="button" class="small${x.id === img.id ? " on" : ""}" data-img="${esc(x.id)}" title="${esc(x.caption || "")}">${esc(x.id)} (${n})</button>`;
  }).join("");
  pane.innerHTML = `<div class="img-tabs">${tabs}</div>
    <div class="img-stage"><div class="img-box"><img id="the-img" alt="${esc(img.caption || img.id)}"><svg id="overlay"></svg></div></div>
    <div class="img-caption">${esc(img.caption || "")}</div>`;
  pane.querySelectorAll("[data-img]").forEach((b) => (b.onclick = () => { state.imageId = b.dataset.img; renderImage(); }));
  const el = $("#the-img");
  currentImg = { meta: img, W: img.width, H: img.height };
  const ready = () => {
    if (state.imageId !== img.id) return;
    currentImg.W = img.width || el.naturalWidth || 1000;
    currentImg.H = img.height || el.naturalHeight || 750;
    fitImage();
    redrawOverlay();
  };
  if (img.width && img.height) ready();
  el.addEventListener("load", ready, { once: true });
  try { el.src = await state.backend.imageUrl(c.id, img.file); }
  catch (e) { $(".img-stage").innerHTML = `<p class="empty">画像を読めません: ${esc(img.file)} (${esc(e.message)})</p>`; }
}
/** Contain the image in the stage: full panel width, but never taller than the visible stage. */
function fitImage() {
  const box = $(".img-box"), stage = $(".img-stage");
  if (!box || !stage || !currentImg || !currentImg.W) return;
  const ratio = currentImg.W / currentImg.H;
  const stacked = window.matchMedia("(max-width: 1000px)").matches;
  const availH = stacked ? window.innerHeight * 0.8 : stage.clientHeight - 16;
  box.style.aspectRatio = `${currentImg.W} / ${currentImg.H}`;
  box.style.width = `min(100%, ${Math.max(120, availH * ratio)}px)`;
}
window.addEventListener("resize", fitImage);

function redrawOverlay() {
  if (currentImg && currentImg.W) drawOverlay(currentImg.meta, currentImg.W, currentImg.H);
}
function drawOverlay(img, W, H) {
  const c = state.campaign;
  const svg = $("#overlay");
  if (!svg || !c) return;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("preserveAspectRatio", "none");
  const r = Math.max(W, H) * 0.022;     // badge radius in image units
  const ah = r * 0.8;                   // arrowhead length
  const P = (p) => [p[0] * W, p[1] * H];
  const head = (tip, from) => {
    const dx = tip[0] - from[0], dy = tip[1] - from[1];
    const L = Math.hypot(dx, dy) || 1, ux = dx / L, uy = dy / L;
    const bx = tip[0] - ux * ah, by = tip[1] - uy * ah, w = ah * 0.45;
    return `<path class="head" d="M${tip[0]},${tip[1]} L${bx - uy * w},${by + ux * w} L${bx + uy * w},${by - ux * w} Z"/>`;
  };
  const stroke = `vector-effect="non-scaling-stroke"`;
  const items = (c.items || []).filter((it) => it.image === img.id && it.annotation && Array.isArray(it.annotation.points) && it.annotation.points.length);
  items.sort((a, b) => (a.no === state.selNo) - (b.no === state.selNo));   // selected on top
  svg.innerHTML = items.map((it) => {
    const a = it.annotation;
    const pts = a.points.map(P);
    let body = "", anchor, auto;
    if (a.type === "dim" && pts.length >= 2) {
      const [p1, p2] = pts;
      anchor = [(p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2];
      body = `<line class="halo" x1="${p1[0]}" y1="${p1[1]}" x2="${p2[0]}" y2="${p2[1]}" stroke-width="5" ${stroke}/>
        <line x1="${p1[0]}" y1="${p1[1]}" x2="${p2[0]}" y2="${p2[1]}" stroke-width="2" ${stroke}/>${head(p1, p2)}${head(p2, p1)}`;
      const dx = p2[0] - p1[0], dy = p2[1] - p1[1], L = Math.hypot(dx, dy) || 1;
      auto = [anchor[0] + (dy / L) * r * 2.4, anchor[1] - (dx / L) * r * 2.4];
    } else {
      anchor = pts[0];
      body = `<circle class="dot" cx="${anchor[0]}" cy="${anchor[1]}" r="${r * 0.32}"/>`;
      auto = [anchor[0] + r * 2.4, anchor[1] - r * 2.4];
    }
    const lp = a.label_pos ? P(a.label_pos) : auto;
    const leader = Math.hypot(lp[0] - anchor[0], lp[1] - anchor[1]) > r * 1.1
      ? `<line class="halo" x1="${lp[0]}" y1="${lp[1]}" x2="${anchor[0]}" y2="${anchor[1]}" stroke-width="4" ${stroke}/><line x1="${lp[0]}" y1="${lp[1]}" x2="${anchor[0]}" y2="${anchor[1]}" stroke-width="1.5" stroke-dasharray="4 3" ${stroke}/>` : "";
    return `<g class="callout${it.no === state.selNo ? " sel" : ""}" data-no="${it.no}"><title>${esc(`${it.no}. ${it.name}`)}</title>${leader}${body}
      <circle class="badge" cx="${lp[0]}" cy="${lp[1]}" r="${r}" stroke-width="1.5" ${stroke}/>
      <text x="${lp[0]}" y="${lp[1]}" font-size="${r * 1.15}">${esc(it.no)}</text></g>`;
  }).join("");
  svg.querySelectorAll(".callout").forEach((g) => (g.onclick = () => select(Number(g.dataset.no), true)));
}

/* ---------- measurement editor popover ---------- */
let popAnchor = null;
function closePopover() {
  const p = $("#popover");
  if (p) { p.hidden = true; p.innerHTML = ""; }
  popAnchor = null;
}
document.addEventListener("mousedown", (e) => {
  const p = $("#popover");
  if (!p.hidden && !p.contains(e.target) && !(popAnchor && popAnchor.contains(e.target))) closePopover();
});

function methodOptions(selected) {
  const keys = Object.keys(METHOD_LABEL);
  if (selected && !keys.includes(selected)) keys.push(selected);
  return keys.map((k) => `<option value="${esc(k)}"${k === selected ? " selected" : ""}>${esc(methodLabel(k))}</option>`).join("");
}

function openEditor(no, key, anchor) {
  const c = state.campaign;
  const it = c.items.find((x) => x.no === no);
  if (!it) return;
  popAnchor = anchor;
  const p = $("#popover");
  const cad = cadOf(it, key);
  const render = () => {
    const list = measOf(it, key);
    const lt = latest(it, key);
    p.innerHTML = `<h3>#${esc(it.no)} ${esc(it.name)} — ${key === ALL ? "全バリアント共通" : esc(key)}</h3>
      <div class="meta" style="margin-bottom:8px">CAD ${esc(fmt(cad) || "—")} ${esc(it.unit || "mm")}${it.mate ? ` · 相手部品 ${esc(it.mate.name || "")} ${esc(fmt(it.mate.value))}` : ""}</div>
      <div class="row"><label for="pv-value">実測値</label><input id="pv-value" type="number" step="0.01" inputmode="decimal" placeholder="${esc(fmt(cad))}"></div>
      <div class="row"><label for="pv-date">日付</label><input id="pv-date" type="date" value="${today()}"></div>
      <div class="row"><label for="pv-method">測定方法</label><select id="pv-method">${methodOptions(prefGet("method", "caliper"))}</select></div>
      <div class="row"><label for="pv-by">測定者</label><input id="pv-by" type="text" placeholder="（任意）" value="${esc(prefGet("by", ""))}"></div>
      <div class="row"><label for="pv-note">メモ</label><input id="pv-note" type="text"></div>
      <div class="row"><label></label><label><input id="pv-prov" type="checkbox"> ${PROV_TEXT}</label></div>
      <div class="btns"><button type="button" id="pv-close">閉じる</button><button type="button" class="primary" id="pv-add">追加</button></div>
      ${list.length ? `<ul class="hist-list">${list.map(({ m, i }) => `<li>
        <span class="v${m.provisional ? " prov" : ""}">${esc(fmt(m.value))}</span><span>${esc(m.date || "")}</span>
        <span class="n" title="${esc(m.note || "")}">${m === lt ? "★ " : ""}${esc([methodLabel(m.method), byLabel(m.by), m.note].filter(Boolean).join(" · "))}</span>
        <label title="${PROV_TEXT}"><input type="checkbox" data-prov="${i}" ${m.provisional ? "checked" : ""}>仮</label>
        <button type="button" class="small" data-del="${i}" title="この測定値を削除">×</button></li>`).join("")}</ul>` : ""}`;
    const add = () => {
      const v = parseFloat($("#pv-value").value);
      if (!Number.isFinite(v)) { $("#pv-value").focus(); return; }
      const m = { variant: key, value: v, date: $("#pv-date").value || today() };
      const by = $("#pv-by").value.trim(), method = $("#pv-method").value, note = $("#pv-note").value.trim();
      if (by) m.by = by;
      if (method) m.method = method;
      if (note) m.note = note;
      if ($("#pv-prov").checked) m.provisional = true;
      prefSet("method", method || null); prefSet("by", by || null);
      (it.measurements ||= []).push(m);
      markDirty(); refreshItems(); render();
      $("#pv-value").focus();
    };
    $("#pv-add").onclick = add;
    $("#pv-value").onkeydown = (e) => { if (e.key === "Enter") add(); };
    $("#pv-close").onclick = closePopover;
    p.querySelectorAll("[data-del]").forEach((b) => (b.onclick = () => {
      it.measurements.splice(Number(b.dataset.del), 1);
      markDirty(); refreshItems(); render();
    }));
    p.querySelectorAll("[data-prov]").forEach((cb) => (cb.onchange = () => {
      const m = it.measurements[Number(cb.dataset.prov)];
      if (cb.checked) m.provisional = true; else delete m.provisional;
      markDirty(); refreshItems(); render();
    }));
  };
  render();
  p.hidden = false;
  p.style.width = "";
  const rect = anchor.getBoundingClientRect();
  const pw = p.offsetWidth, ph = p.offsetHeight;
  const left = Math.min(rect.left, window.innerWidth - pw - 8);
  let top = rect.bottom + 4;
  if (top + ph > window.innerHeight - 8) top = Math.max(8, rect.top - ph - 4);
  p.style.left = Math.max(8, left) + "px";
  p.style.top = top + "px";
  $("#pv-value").focus();
}
/** Re-render the table after an edit, keeping the popover anchored to the fresh button. */
function refreshItems() {
  const key = popAnchor?.dataset.key, no = popAnchor?.dataset.no;
  renderTable();
  if (popAnchor) popAnchor = document.querySelector(`.meas-in[data-no="${no}"][data-key="${CSS.escape(key)}"]`) || popAnchor;
}

/* ---------- voice entry (Web Speech API) ---------- */
const VOICE_HELP = [
  ["「1番 8.45」「いちばん はってんよんご」", "その番号の項目に記録（今のバリアント）。カーソルは次の項目へ"],
  ["「8.45」「はちてんよんご」", "カーソル行に記録して、次の項目へ"],
  ["「5番」", "カーソルを 5 番へ"],
  ["「次」「前」", "カーソルを移動"],
  ["「ブイ1」「V2」「バリアント3」「共通」", "バリアントを切り替え"],
  ["「はい」「OK」", "確認待ちの値を記録"],
  ["「取り消し」「戻して」", "直前の音声入力を取り消す"],
  ["「保存」", "すぐ保存"],
];
function openVoiceHelp(anchor) {
  const p = $("#popover");
  if (!p.hidden && popAnchor === anchor) { closePopover(); return; }
  popAnchor = anchor;
  p.innerHTML = `<h3>音声コマンド</h3>
    <table class="help-table"><tbody>${VOICE_HELP.map(([say, act]) => `<tr><td>${esc(say)}</td><td>${esc(act)}</td></tr>`).join("")}</tbody></table>
    <p class="meta">数字は「8.45」「八点四五」「はってんよんご」のどれでも可。CAD から大きく外れた値は「確認: …?」と出るので「はい」か言い直し。Chrome / Edge のみ（音声は各社のクラウドで認識）。</p>`;
  p.hidden = false;
  p.style.width = "460px";
  const r = anchor.getBoundingClientRect();
  p.style.left = Math.max(8, Math.min(r.left, window.innerWidth - p.offsetWidth - 8)) + "px";
  p.style.top = r.bottom + 4 + "px";
}
const speechRecCtor = () => window.SpeechRecognition || window.webkitSpeechRecognition;
const fmtV = (v) => (isNum(v) ? v.toFixed(2) : String(v));

function renderVoiceUi() {
  const btn = $("#voice-btn");
  if (!btn) return;
  btn.textContent = voice.on ? "● 音声入力: オン" : "🎤 音声入力: オフ";
  btn.classList.toggle("on", voice.on);
  $("#voice-log").hidden = !voice.on;
  renderLive();
  renderVoiceLog();
}
function renderLive() {
  const live = $("#live");
  if (!live) return;
  let st, text;
  if (voice.pending) {
    st = "確認";
    text = `${voice.pending.no}番 ${fmtV(voice.pending.value)} ?（「はい」で記録、言い直しで置き換え）`;
  } else if (!voice.on) {
    st = "停止中";
    text = voice.status || "「音声入力」を押すと、話した数値を記録します";
  } else {
    st = voice.interim ? "聞き取り中" : "待機中";
    const it = voice.cursor != null ? itemByNo(voice.cursor) : null;
    text = voice.interim || voice.status || (it ? `次: ${it.no}番 ${it.name}` : "");
  }
  live.classList.toggle("pending", !!voice.pending);
  $("#live-st").textContent = st;
  $("#live-text").textContent = text;
}
function renderVoiceLog() {
  const box = $("#voice-log");
  if (!box || box.hidden) return;
  box.innerHTML = voice.log.length
    ? voice.log.slice().reverse().map((e, ri) => {
      const i = voice.log.length - 1 - ri;
      return `<div class="vrow${e.undone ? " undone" : ""}"><span class="t">${esc(e.time)}</span><span class="raw" title="${esc(e.raw)}">「${esc(e.raw)}」</span><span class="act">${esc(e.action)}</span>${e.undo && !e.undone ? `<button type="button" class="small" data-vundo="${i}" title="この記録を取り消す">取消</button>` : ""}</div>`;
    }).join("")
    : `<div class="meta">音声ログ: まだありません。例「1番 8.45」「8.45」「次」「ブイ2」「取り消し」「保存」</div>`;
  box.querySelectorAll("[data-vundo]").forEach((b) => (b.onclick = () => undoEntry(voice.log[Number(b.dataset.vundo)])));
}
function vlog(raw, action, undo = null) {
  const d = new Date();
  voice.log.push({ time: `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}:${String(d.getSeconds()).padStart(2, "0")}`, raw, action, undo, undone: false });
  if (voice.log.length > 200) voice.log.shift();
  renderVoiceLog();
}
function speak(text) {
  if (prefGet("readback", "") !== "1" || !window.speechSynthesis) return;
  const u = new SpeechSynthesisUtterance(text);
  u.lang = "ja-JP";
  voice.ignoreUntil = Date.now() + 15000;
  u.onend = u.onerror = () => { voice.ignoreUntil = Date.now() + 500; };
  speechSynthesis.speak(u);
}

function setCursor(no, scroll = true) {
  voice.cursor = no;
  document.querySelectorAll("table.items tbody tr").forEach((tr) => tr.classList.toggle("cursor", voice.on && Number(tr.dataset.no) === no));
  if (scroll) select(no, true);
  renderLive();
}
function moveCursor(dir) {
  const items = state.campaign.items || [];
  const i = items.findIndex((x) => x.no === voice.cursor);
  const j = Math.min(items.length - 1, Math.max(0, (i < 0 ? 0 : i + dir)));
  if (items[j]) setCursor(items[j].no);
}
/** Measurement key for an item under the current variant, or an error message. */
function voiceKey(it) {
  if (itemKeys(state.campaign, it)[0] === ALL) return { key: ALL };
  if (voice.variant === ALL) return { err: `${it.no}番はバリアントごとの項目です。「ブイ1」などでバリアントを選んでください` };
  return { key: voice.variant };
}

/* ---------- voice diagnostics: event log, restart policy, mic level, browser/engine ---------- */
const diag = {
  log: [],             // [{t, ev, detail}]
  restarts: 0,
  running: false,      // between onstart and onend
  startedAt: 0,
  heardSound: false,   // soundstart/speechstart/result seen in this session
  quickEnds: 0,        // consecutive sessions that ended < 2 s without sound
  netErrors: 0,
  lastError: "",
  explain: "",         // plain-language diagnosis shown in the panel
  meter: null,         // {stream, ctx, raf}
  maxDb: -Infinity,
  device: "",
};
const ERROR_HELP = {
  network: "音声認識サービスに接続できません。Chrome は Google、Edge は Microsoft のクラウドで認識します。インターネット接続を確認してください。Vivaldi・Brave・Opera など Chrome / Edge 以外の Chromium 系ブラウザはこのサービスを使えないため、Chrome か Edge で開いてください。",
  "not-allowed": "マイクの使用がブロックされています。アドレスバー左のアイコン（またはサイトの設定）でマイクを「許可」にして、もう一度「音声入力」を押してください。",
  "service-not-allowed": "ブラウザが音声認識サービスの利用を許可していません（Chrome / Edge 以外のブラウザや、管理ポリシーによる無効化）。Chrome か Edge で開いてください。",
  "language-not-supported": "このブラウザは日本語（ja-JP）の音声認識に対応していません。Chrome / Edge を最新版にしてください。",
  "audio-capture": "マイクが見つからないか、別のアプリが使用中です。Windows の設定 > システム > サウンド > 入力 を確認してください。",
  "no-speech": "音声が検出されませんでした。下のレベルメーターが動くか確認してください。",
  aborted: "認識が中断されました（別のタブやアプリが音声認識を始めた可能性があります）。",
};
const FATAL_ERRORS = new Set(["not-allowed", "service-not-allowed", "language-not-supported", "audio-capture"]);

function detectBrowser() {
  const ua = navigator.userAgent;
  const brands = (navigator.userAgentData?.brands || []).map((b) => b.brand).filter((b) => !/not.?a.?brand/i.test(b));
  let name;
  if (navigator.brave) name = "Brave";
  else if (brands.includes("Microsoft Edge") || /Edg\//.test(ua)) name = "Edge";
  else if (/OPR\//.test(ua) || brands.includes("Opera")) name = "Opera";
  else if (/Vivaldi/.test(ua) || brands.includes("Vivaldi")) name = "Vivaldi";
  else if (brands.includes("Google Chrome")) name = "Chrome";
  else if (/Firefox\//.test(ua)) name = "Firefox";
  else if (/Safari\//.test(ua) && !/Chrome\//.test(ua)) name = "Safari";
  else if (brands.includes("Chromium") || /Chrome\//.test(ua)) name = "Chromium 系（Chrome / Edge 以外。Vivaldi など）";
  else name = "不明";
  return { name, brands, ok: name === "Chrome" || name === "Edge" };
}
const BROWSER = detectBrowser();
const engineName = () => (window.SpeechRecognition ? "SpeechRecognition" : window.webkitSpeechRecognition ? "webkitSpeechRecognition" : "なし");

function stamp() {
  const d = new Date();
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}:${String(d.getSeconds()).padStart(2, "0")}.${String(d.getMilliseconds()).padStart(3, "0")}`;
}
function dlog(ev, detail = "") {
  diag.log.push({ t: stamp(), ev, detail });
  if (diag.log.length > 400) diag.log.shift();
  renderDiag();
}
function setExplain(text) {
  diag.explain = text;
  renderDiag();
}
function renderDiag() {
  const info = $("#diag-info");
  if (!info) return;
  info.innerHTML = `
    <div><b>エンジン</b> ${esc(engineName())} · <b>ブラウザ</b> ${esc(BROWSER.name)}${BROWSER.brands.length ? `（${esc(BROWSER.brands.join(", "))}）` : ""} · <b>安全な接続</b> ${window.isSecureContext ? "はい" : "いいえ"}</div>
    <div><b>入力デバイス</b> ${esc(diag.device || "（音声入力をオンにすると表示）")} · <b>再起動</b> ${diag.restarts} 回 · <b>最大レベル</b> ${Number.isFinite(diag.maxDb) ? diag.maxDb.toFixed(0) + " dBFS" : "—"}</div>
    <div class="meta">音声認識は OS / ブラウザの既定のマイクを使います（ページからは選べません）。変更は Windows の設定 > システム > サウンド > 入力、または Chrome / Edge の設定 > プライバシーとセキュリティ > サイトの設定 > マイク。</div>
    ${diag.explain ? `<div class="diag-explain">${esc(diag.explain)}</div>` : ""}`;
  const box = $("#diag-log");
  if (box) {
    box.textContent = diag.log.slice(-200).map((e) => `${e.t}  ${e.ev}${e.detail ? "  " + e.detail : ""}`).join("\n") || "（イベントはまだありません）";
    box.scrollTop = box.scrollHeight;
  }
}
function diagText() {
  const info = $("#diag-info")?.innerText || "";
  return `fit-lab 音声の診断\n${info}\nUA: ${navigator.userAgent}\n\n` + diag.log.map((e) => `${e.t}  ${e.ev}${e.detail ? "  " + e.detail : ""}`).join("\n");
}

async function startMeter() {
  stopMeter();
  if (!navigator.mediaDevices?.getUserMedia) { dlog("meter", "getUserMedia なし"); return; }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    if (!voice.on) { stream.getTracks().forEach((t) => t.stop()); return; }
    const track = stream.getAudioTracks()[0];
    diag.device = track?.label || "（名前なし）";
    try {
      const devs = await navigator.mediaDevices.enumerateDevices();
      const def = devs.find((d) => d.kind === "audioinput" && d.deviceId === "default");
      if (def && def.label && def.label !== diag.device) diag.device += `（既定: ${def.label}）`;
    } catch { /* ignore */ }
    dlog("mic", `getUserMedia OK: ${diag.device}`);
    const ctx = new AudioContext();
    const src = ctx.createMediaStreamSource(stream);
    const an = ctx.createAnalyser();
    an.fftSize = 1024;
    src.connect(an);   // analyser only; nothing is connected to the speakers
    const buf = new Float32Array(an.fftSize);
    const m = { stream, ctx, raf: 0 };
    diag.meter = m;
    let lastLog = 0;
    const tick = () => {
      an.getFloatTimeDomainData(buf);
      let sum = 0;
      for (const v of buf) sum += v * v;
      const db = 20 * Math.log10(Math.sqrt(sum / buf.length) || 1e-9);
      if (db > diag.maxDb) diag.maxDb = db;
      const pct = Math.max(0, Math.min(100, ((db + 60) / 60) * 100));
      document.querySelectorAll(".meter-bar").forEach((b) => (b.style.width = pct + "%"));
      const lbl = $("#meter-db"); if (lbl) lbl.textContent = `${db.toFixed(0)} dBFS`;
      const now = performance.now();
      if (now - lastLog > 1000) { lastLog = now; renderDiag(); }
      m.raf = requestAnimationFrame(tick);
    };
    tick();
  } catch (err) {
    diag.device = `取得できません（${err.name}）`;
    dlog("mic", `getUserMedia 失敗: ${err.name} ${err.message}`);
    if (err.name === "NotAllowedError") setExplain(ERROR_HELP["not-allowed"]);
    else if (err.name === "NotFoundError" || err.name === "NotReadableError") setExplain(ERROR_HELP["audio-capture"]);
  }
}
function stopMeter() {
  const m = diag.meter;
  diag.meter = null;
  if (!m) return;
  cancelAnimationFrame(m.raf);
  m.stream.getTracks().forEach((t) => t.stop());
  m.ctx.close().catch(() => {});
  document.querySelectorAll(".meter-bar").forEach((b) => (b.style.width = "0%"));
  const lbl = $("#meter-db"); if (lbl) lbl.textContent = "—";
}

function startVoice() {
  const SpeechRec = speechRecCtor();
  if (!SpeechRec) {
    voice.status = "このブラウザは音声認識に対応していません（Chrome / Edge を使ってください）";
    setExplain(voice.status);
    toast(voice.status, true); renderLive(); return;
  }
  if (!window.isSecureContext) {
    voice.status = "マイクは https または localhost でのみ使えます";
    setExplain(voice.status);
    toast(voice.status, true); renderLive(); return;
  }
  Object.assign(diag, { restarts: 0, quickEnds: 0, netErrors: 0, lastError: "", explain: "", maxDb: -Infinity, running: false });
  dlog("voice", `オン（${engineName()}, ${BROWSER.name}, lang ja-JP, continuous, interimResults）`);
  if (!BROWSER.ok) setExplain(`このブラウザ（${BROWSER.name}）では音声認識が動かない可能性が高いです。Web Speech の認識は Chrome（Google）と Edge（Microsoft）のクラウドサービスで動き、Vivaldi・Brave・Opera などは使えません。Chrome か Edge で開いてください。`);

  const rec = new SpeechRec();
  rec.lang = "ja-JP";
  rec.continuous = true;
  rec.interimResults = true;
  rec.maxAlternatives = 3;
  const mine = () => voice.on && voice.rec === rec;
  rec.onstart = () => { diag.running = true; diag.startedAt = Date.now(); diag.heardSound = false; dlog("start"); };
  rec.onaudiostart = () => dlog("audiostart");
  rec.onsoundstart = () => { diag.heardSound = true; dlog("soundstart"); };
  rec.onspeechstart = () => { diag.heardSound = true; dlog("speechstart"); };
  rec.onspeechend = () => dlog("speechend");
  rec.onsoundend = () => dlog("soundend");
  rec.onaudioend = () => dlog("audioend");
  rec.onnomatch = () => dlog("nomatch");
  rec.onresult = (e) => {
    diag.heardSound = true;
    let interim = "";
    for (let i = e.resultIndex; i < e.results.length; i++) {
      const res = e.results[i];
      const alts = [...res].map((a) => a.transcript);
      dlog(res.isFinal ? "result(final)" : "result(interim)", alts.map((a, k) => (k ? `/ ${a}` : a)).join(" "));
      if (Date.now() < voice.ignoreUntil) continue;   // our own read-back
      if (res.isFinal) handleTranscripts(alts);
      else interim += res[0].transcript;
    }
    voice.interim = interim;
    renderLive();
  };
  rec.onerror = (e) => {
    diag.lastError = e.error;
    dlog("error", `${e.error}${e.message ? " — " + e.message : ""}`);
    if (ERROR_HELP[e.error] && e.error !== "aborted") setExplain(ERROR_HELP[e.error]);
    if (e.error === "network") diag.netErrors++;
    if (FATAL_ERRORS.has(e.error) || diag.netErrors >= 3) {
      voice.status = ERROR_HELP[e.error] || `音声認識エラー: ${e.error}`;
      toast("音声入力を停止しました: " + e.error, true);
      stopVoice();
      renderLive();
    } else if (e.error !== "no-speech" && e.error !== "aborted") {
      voice.status = `音声認識エラー: ${e.error}（「音声の診断」を参照）`;
      renderLive();
    }
  };
  rec.onend = () => {
    diag.running = false;
    const len = Date.now() - diag.startedAt;
    dlog("end", `${(len / 1000).toFixed(1)} 秒`);
    if (!mine()) return;
    diag.quickEnds = len < 2000 && !diag.heardSound ? diag.quickEnds + 1 : 0;
    if (diag.quickEnds >= 5) {
      const why = "認識が音を受け取らないまま、すぐ終了しています（5 回連続）。認識サービスに届いていない可能性があります。" + (BROWSER.ok ? "ネットワーク接続と、マイク入力（レベルメーター）を確認してください。" : `このブラウザ（${BROWSER.name}）は音声認識サービスを使えません。Chrome か Edge で開いてください。`);
      setExplain(why);
      voice.status = why;
      toast("音声入力を停止しました（認識がすぐ終了します）", true);
      stopVoice();
      return;
    }
    // back off when sessions keep ending immediately; never call start() while running
    const delay = diag.quickEnds ? Math.min(8000, 500 * 2 ** diag.quickEnds) : 300;
    diag.restarts++;
    dlog("restart", `#${diag.restarts}（${delay} ms 後）`);
    setTimeout(() => {
      if (!mine() || diag.running) return;
      try { rec.start(); } catch (err) { dlog("start 失敗", `${err.name} ${err.message}`); }
    }, delay);
  };
  voice.rec = rec;
  voice.on = true;
  voice.status = "";
  if (voice.cursor == null || !itemByNo(voice.cursor)) voice.cursor = state.selNo ?? (state.campaign.items || [])[0]?.no ?? null;
  try { rec.start(); dlog("start() 呼び出し"); } catch (err) { voice.status = String(err.message || err); dlog("start 失敗", `${err.name} ${err.message}`); }
  startMeter();
  renderVoiceUi();
  if (voice.cursor != null) setCursor(voice.cursor);
}
function stopVoice() {
  const rec = voice.rec;
  const wasOn = voice.on;
  voice.on = false; voice.rec = null; voice.interim = ""; voice.pending = null;
  if (rec) { try { rec.abort(); } catch { /* ignore */ } }
  stopMeter();
  if (wasOn) dlog("voice", "オフ");
  clearTimeout(voice.saveTimer);
  if (state.dirty) save();
  document.querySelectorAll("table.items tbody tr.cursor").forEach((tr) => tr.classList.remove("cursor"));
  renderVoiceUi();
}

/** Record a voice value; returns the log action text. */
function applyVoice(no, key, value, raw, advance) {
  const m = addMeasurement(no, key, value, { note: "音声入力" });
  voice.pending = null;
  renderTable();
  flashRow(no);
  const label = `${no}番${key === ALL ? "" : ` ${key}`} = ${fmtV(value)}`;
  vlog(raw, label, { no, m });
  speak(`${no}番、${fmtV(value)}`);
  if (advance) moveCursor(1); else setCursor(no);
}

/** Final transcript alternatives from the recogniser (also the entry point for tests). */
function handleTranscripts(alts) {
  if (!state.campaign) return;
  const r = FitVoice.parseAlternatives(alts);
  const raw = r.raw || alts[0] || "";
  voice.interim = "";
  voice.status = "";
  switch (r.type) {
    case "set": {
      const fromPending = r.no == null && voice.pending;
      const no = r.no ?? (fromPending ? voice.pending.no : voice.cursor);
      const it = no != null ? itemByNo(no) : null;
      if (!it) { vlog(raw, `${no ?? "?"}番の項目がありません`); break; }
      const k = fromPending ? { key: voice.pending.key } : voiceKey(it);
      if (k.err) { voice.status = k.err; vlog(raw, k.err); break; }
      const cad = cadOf(it, k.key);
      if (!FitVoice.plausible(r.value, cad)) {
        voice.pending = { no, key: k.key, value: r.value, raw };
        setCursor(no);
        vlog(raw, `確認待ち: ${no}番 ${fmtV(r.value)}（CAD ${isNum(cad) ? fmtV(cad) : "なし"}）`);
        speak(`確認、${no}番、${fmtV(r.value)}`);
        break;
      }
      // a bare value advances the cursor; "<番号>番 <値>" leaves the cursor after that item too
      applyVoice(no, k.key, r.value, raw, true);
      break;
    }
    case "confirm":
      if (voice.pending) { const p = voice.pending; applyVoice(p.no, p.key, p.value, raw, true); }
      else vlog(raw, "確認待ちの値はありません");
      break;
    case "cursor":
      if (itemByNo(r.no)) { voice.pending = null; setCursor(r.no); vlog(raw, `${r.no}番へ移動`); }
      else vlog(raw, `${r.no}番の項目がありません`);
      break;
    case "next": voice.pending = null; moveCursor(1); vlog(raw, "次へ"); break;
    case "prev": voice.pending = null; moveCursor(-1); vlog(raw, "前へ"); break;
    case "variant": {
      const vs = (state.campaign.variants || []).map((v) => v.id);
      const id = r.variant === ALL ? ALL : vs.find((v) => v.toLowerCase() === `v${r.variant}`) || vs[r.variant - 1];
      if (!id) { vlog(raw, `バリアント ${r.variant} はありません`); break; }
      voice.variant = id; voice.pending = null;
      const sel = $("#var-sel"); if (sel) sel.value = id;
      vlog(raw, `バリアント → ${id === ALL ? "共通" : id}`);
      speak(id === ALL ? "共通" : id);
      break;
    }
    case "undo": {
      if (voice.pending) { voice.pending = null; vlog(raw, "確認待ちを破棄"); break; }
      const e = [...voice.log].reverse().find((x) => x.undo && !x.undone);
      if (e) undoEntry(e); else vlog(raw, "取り消す記録がありません");
      break;
    }
    case "save": vlog(raw, "保存"); save(); break;
    default: vlog(raw, "認識できません");
  }
  renderLive();
}
function undoEntry(e) {
  if (!e || !e.undo || e.undone) return;
  const it = itemByNo(e.undo.no);
  const i = it ? (it.measurements || []).indexOf(e.undo.m) : -1;
  if (i >= 0) { it.measurements.splice(i, 1); markDirty(); renderTable(); flashRow(e.undo.no); }
  e.undone = true;
  vlog("", `取消: ${e.action}`);
  speak(`取り消し、${e.undo.no}番`);
}

/* ---------- summaries ---------- */
function statCells(s) {
  if (!s.n) return `<td class="num">0</td><td></td><td></td><td></td><td></td>`;
  return `<td class="num">${s.n}</td><td class="num d ${dClass(s.mean)}">${esc(fmtD(s.mean))}</td><td class="num">${s.sd == null ? "" : esc(fmt(s.sd))}</td>
    <td class="num">${esc(fmtD(s.min))}</td><td class="num">${esc(fmtD(s.max))}</td>`;
}
const STAT_HEAD = `<th>n</th><th>平均 Δ</th><th>標準偏差</th><th>最小 Δ</th><th>最大 Δ</th>`;
function provToggle() {
  return `<label class="controls"><input type="checkbox" id="prov-toggle" ${state.includeProvisional ? "checked" : ""}> ${PROV_TEXT}を含める</label>`;
}
function bindProvToggle(rerender) {
  $("#prov-toggle").onchange = (e) => { state.includeProvisional = e.target.checked; rerender(); };
}

function viewDetails() {
  const c = state.campaign;
  const rows = [["プロジェクト", c.project], ["材料", c.material], ["工法", c.process], ["業者", c.vendor],
    ["発注", c.ordered], ["受領", c.received], ["ID", c.id]]
    .map(([k, v]) => `<tr><th>${k}</th><td>${esc(v || "—")}</td></tr>`).join("");
  $("#view").innerHTML = `<div class="campaign">${campaignChrome("details")}
    <div class="table-wrap">
    <table class="params kv"><tbody>${rows}</tbody></table>
    ${c.notes ? `<h2>メモ</h2><p class="notes">${esc(c.notes)}</p>` : ""}
    <h2>バリアント</h2>${variantsTable(c)}</div></div>`;
}

function viewCampaignSummary() {
  const c = state.campaign;
  const all = cells(c, state.includeProvisional).filter((x) => isNum(x.delta));
  const vids = variantIds(c);
  const kindOf = (x) => x.item.kind || "other";
  const kinds = [...new Set(all.map(kindOf))].sort((a, b) => (KINDS.indexOf(a) + 1 || 99) - (KINDS.indexOf(b) + 1 || 99));
  const rows = kinds.map((kind) => {
    const sel = all.filter((x) => kindOf(x) === kind);
    const sub = [...vids, ...(vids[0] === ALL ? [] : [ALL])].map((k) => {
      const s = stats(sel.filter((x) => x.key === k).map((x) => x.delta));
      return s.n ? `<tr class="sub"><td>${k === ALL ? "全バリアント共通の項目" : esc(k)}</td>${statCells(s)}</tr>` : "";
    }).join("");
    return `<tr class="group"><td>${esc(kindLabel(kind))} <span class="meta">${esc(kind)}</span></td>${statCells(stats(sel.map((x) => x.delta)))}</tr>${sub}`;
  }).join("");
  $("#view").innerHTML = `<div class="campaign">${campaignChrome("summary")}
    <div class="table-wrap">${provToggle()}
    ${rows ? `<table class="stats"><thead><tr><th>種類 / バリアント</th>${STAT_HEAD}</tr></thead><tbody>${rows}</tbody></table>` : `<p class="empty">まだ実測値がありません。</p>`}
    <p class="meta">Δ = 実測 − CAD。項目 × バリアントごとに最新の実測値を使う。穴で負なら小さく、ボスで正なら太く出ている。</p></div></div>`;
  bindProvToggle(viewCampaignSummary);
}

let crossCache = null;
async function viewCross() {
  state.campaign = null; state.cid = null;
  setChrome(`<a href="#/">キャンペーン一覧</a> / <span class="crumb-title">知見（全キャンペーン集計）</span>`, `<button id="csv-all" type="button">全件 CSV 出力</button>`);
  $("#view").innerHTML = `<div class="page"><p class="empty">読み込み中…</p></div>`;
  const list = (await state.backend.list()).campaigns.filter((c) => !c.error);
  crossCache = await Promise.all(list.map((c) => state.backend.get(c.id)));
  $("#csv-all").onclick = () => download("fit-lab-all.csv", csvText(crossCache.flatMap(csvRows)));
  renderCross();
}
function renderCross() {
  const camps = crossCache || [];
  const groups = new Map();
  for (const c of camps) {
    for (const x of cells(c, state.includeProvisional)) {
      if (!isNum(x.delta)) continue;
      const k = `${c.material || "（材料未記入）"}\u0000${x.item.kind || "other"}`;
      if (!groups.has(k)) groups.set(k, []);
      groups.get(k).push({ c, x });
    }
  }
  const keys = [...groups.keys()].sort();
  const rows = keys.map((k) => {
    const [material, kind] = k.split("\u0000");
    const g = groups.get(k);
    const byCamp = new Map();
    g.forEach(({ c, x }) => { if (!byCamp.has(c.id)) byCamp.set(c.id, { c, d: [] }); byCamp.get(c.id).d.push(x.delta); });
    const sub = [...byCamp.values()].map(({ c, d }) => `<tr class="sub"><td><a href="#/c/${encodeURIComponent(c.id)}/summary">${esc(c.title || c.id)}</a> <span class="meta">${esc(c.process || "")} ${esc(c.received || c.ordered || "")}</span></td>${statCells(stats(d))}</tr>`).join("");
    return `<tr class="group"><td>${esc(material)} · ${esc(kindLabel(kind))} <span class="meta">（${byCamp.size} キャンペーン）</span></td>${statCells(stats(g.map((y) => y.x.delta)))}</tr>${sub}`;
  }).join("");
  $("#view").innerHTML = `<div class="page">
    <div class="meta">${camps.length} キャンペーン · Δ = 実測 − CAD、項目 × バリアントごとの最新値を材料と種類でまとめた。</div>
    ${provToggle()}
    ${rows ? `<table class="stats"><thead><tr><th>材料 · 種類 / キャンペーン</th>${STAT_HEAD}</tr></thead><tbody>${rows}</tbody></table>` : `<p class="empty">まだ実測値がありません。</p>`}</div>`;
  bindProvToggle(renderCross);
}

/* ---------- start: use serve.py when it answers, else folder mode ---------- */
(async function start() {
  try {
    const r = await fetch("/api/campaigns", { cache: "no-store" });
    if (r.ok && (r.headers.get("Content-Type") || "").includes("json")) { state.serverOk = true; state.backend = serverBackend; }
  } catch { /* static hosting: no server */ }
  renderModeTag();
  route();
})();
