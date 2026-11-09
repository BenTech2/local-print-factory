"use strict";
// Local Print Factory front end - plain JS, no build step.

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const ic = (n) => `<svg class="i"><use href="#i-${n}"/></svg>`;
let STATE = { printers: [], jobs: [] };
const camOn = new Set();
let currentTab = "printers";

// Only touch the DOM when the markup really changed - stops flicker and image reloads on every refresh.
function setHTML(el, html) { if (el._h !== html) { el.innerHTML = html; el._h = html; } }

// ---------------------------------------------------------------- api
async function api(path, opts = {}) {
  const o = { ...opts, headers: { ...(opts.headers || {}) } };
  if (o.json !== undefined) { o.body = JSON.stringify(o.json); o.headers["Content-Type"] = "application/json"; delete o.json; }
  const r = await fetch(path, o);
  if (r.status === 401 && path !== "/api/login") { showLogin(); throw new Error("Login required"); }
  const ct = r.headers.get("content-type") || "";
  const data = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error((data && data.detail) || `HTTP ${r.status}`);
  return data;
}
function toast(msg, bad = false) {
  const t = document.createElement("div");
  t.className = "toast" + (bad ? " bad" : "");
  t.innerHTML = ic(bad ? "alert" : "check") + `<span>${esc(msg)}</span>`;
  $("#toasts").appendChild(t); setTimeout(() => t.remove(), bad ? 7000 : 3500);
}
async function act(fn, okMsg) {
  try { const r = await fn(); if (okMsg) toast(okMsg); await refresh(); return r; }
  catch (e) { toast(e.message, true); }
}

// ---------------------------------------------------------------- formatting
function dur(s) {
  s = Math.max(0, Math.round(s || 0));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h ? `${h}h ${String(m).padStart(2, "0")}m` : `${m}m`;
}
const when = (t) => (t ? new Date(t * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "");
const size = (b) => (b > 1048576 ? (b / 1048576).toFixed(1) + " MB" : Math.ceil(b / 1024) + " KB");
const deg = (c) => (c == null ? "–" : `${Math.round(c)}°`);
function eta(sec) {
  const d = new Date(Date.now() + sec * 1000);
  const t = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return d.toDateString() === new Date().toDateString() ? t : d.toLocaleDateString([], { weekday: "short" }) + " " + t;
}
function matLabel(m, guid) {
  if (m) return `<span class="swatch" style="background:${esc(m.color_code || "#999")}"></span>${esc([m.brand, m.material].filter(Boolean).join(" "))} <span class="muted">${esc(m.color || "")}</span>`;
  return guid ? `<span class="muted">Material ${esc(guid.slice(0, 8))}…</span>` : `<span class="muted">No material</span>`;
}
function statusOf(p) {
  const st = p.state || {};
  if (st.online === null || st.online === undefined) return ["connecting", "Connecting"];
  if (!st.online) return ["offline", "Offline"];
  if (st.clear_reason === "unknown") return ["cleanup", "Check build plate"];
  const js = st.job && st.job.state;
  if (st.status === "printing" || (js && js !== "none")) {
    if (js === "paused" || js === "pausing") return ["paused", "Paused"];
    if (js === "wait_cleanup" || js === "post_print") return ["cleanup", "Clear build plate"];
    if (js === "wait_user_action") return ["attention", "Needs attention"];
    if (js === "pre_print") return ["printing", "Preparing"];
    return ["printing", "Printing"];
  }
  return [st.status, { idle: "Ready", error: "Error", maintenance: "Maintenance", booting: "Booting", unknown: "Unknown" }[st.status] || st.status];
}
const TONE = { printing: "accent", idle: "ok", paused: "warn", cleanup: "warn", attention: "warn", offline: "bad", error: "bad" };
const JOB_TONE = { queued: "idle", sending: "accent", sent: "accent", printing: "accent", paused: "warn", finished: "ok",
  failed: "bad", error: "bad", aborted: "bad", cancelled: "idle" };
const JOB_LABEL = { queued: "Queued", sending: "Uploading", sent: "Starting", printing: "Printing", paused: "Paused",
  finished: "Finished", failed: "Failed", error: "Error", aborted: "Aborted", cancelled: "Cancelled" };
const pill = (tone, label) => `<span class="pill tone-${tone}"><span class="dot"></span>${esc(label)}</span>`;
const thumb = (j) => `<div class="thumb-sm">${j && j.file_id && j.has_thumb ? `<img src="/api/files/${j.file_id}/thumb" alt="">` : ic("cube")}</div>`;
const empty = (icon, title, text) => `<div class="empty">${ic(icon)}<b>${esc(title)}</b><p>${text}</p></div>`;

// ---------------------------------------------------------------- tabs
$$("#tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
function showTab(name) {
  currentTab = name;
  $$("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tab").forEach((t) => t.classList.toggle("active", t.id === "tab-" + name));
  history.replaceState(null, "", "#" + name);
  if (name === "library") loadLibrary();
  if (name === "history") loadHistory();
  if (name === "settings") loadSettings();
}

// ---------------------------------------------------------------- printers
function renderSummary() {
  const ps = STATE.printers;
  let printing = 0, ready = 0, attention = 0, offline = 0;
  ps.forEach((p) => {
    const [k] = statusOf(p);
    if (k === "printing") printing++;
    else if (k === "idle") ready++;
    else if (["paused", "cleanup", "attention", "error"].includes(k)) attention++;
    else if (k === "offline") offline++;
  });
  const queued = STATE.jobs.filter((j) => j.status === "queued").length;
  const tile = (tone, k, v) => `<div class="stat${v ? "" : " zero"}"><div class="k"><span class="dot tone-${tone}"></span>${k}</div><div class="v">${v}</div></div>`;
  setHTML($("#fleet-summary"), ps.length ? tile("accent", "Printing", printing) + tile("ok", "Ready", ready) +
    tile("warn", "Needs attention", attention) + tile("bad", "Offline", offline) + tile("idle", "Jobs queued", queued) : "");
}

function renderPrinters() {
  renderSummary();
  const grid = $("#printer-grid");
  const ps = STATE.printers;
  if (!ps.length) {
    setHTML(grid, empty("printer", "No printers yet", `Use <b>Find printers</b> to search your network, or <b>Add printer</b>
      with its IP address. Ultimakers show it under <i>Preferences › Network</i>.`));
    return;
  }
  if (grid.querySelector(".empty")) grid.innerHTML = "";
  grid._h = null;                  // cards are managed below, not through setHTML
  // keep cards (and their camera images) stable between refreshes
  $$(".printer", grid).forEach((c) => { if (!ps.find((p) => String(p.id) === c.dataset.id)) c.remove(); });
  ps.forEach((p, i) => {
    let card = grid.querySelector(`.printer[data-id="${p.id}"]`);
    if (!card) {
      card = document.createElement("div");
      card.className = "panel printer"; card.dataset.id = p.id;
      card.innerHTML = `<div class="body"></div><div class="camwrap"></div><div class="p-foot"></div>`;
      card.addEventListener("click", onPrinterClick);
    }
    if (grid.children[i] !== card) grid.insertBefore(card, grid.children[i] || null);
    const [cls] = statusOf(p);
    card.className = `panel printer t-${TONE[cls] || "idle"}`;
    setHTML($(".body", card), printerBody(p));
    setHTML($(".p-foot", card), printerFoot(p));
    const wrap = $(".camwrap", card);
    if (camOn.has(p.id) && !wrap.firstChild) {
      wrap.innerHTML = `<img class="cam" alt="Camera" src="/api/printers/${p.id}/snapshot?t=${Date.now()}">`;
      const img = wrap.firstChild;
      let fails = 0;
      img.onload = () => { fails = 0; setTimeout(() => { if (camOn.has(p.id)) img.src = `/api/printers/${p.id}/snapshot?t=${Date.now()}`; }, 1500); };
      img.onerror = () => {      // cameras are often slow to wake - retry a few times before giving up
        if (++fails <= 3 && camOn.has(p.id)) return setTimeout(() => { img.src = `/api/printers/${p.id}/snapshot?t=${Date.now()}`; }, 2000);
        wrap.innerHTML = `<div class="callout bad">${ic("camera")}<div class="txt">Camera not available on this printer.</div></div>`;
      };
    } else if (!camOn.has(p.id)) wrap.innerHTML = "";
  });
}

function printerBody(p) {
  const st = p.state || {};
  const [cls, label] = statusOf(p);
  const job = st.job && st.job.state && st.job.state !== "none" ? st.job : null;
  const qjob = STATE.jobs.find((j) => j.printer_id === p.id && ["sending", "sent", "printing", "paused"].includes(j.status));
  const fw = st.firmware && st.firmware !== "?" ? `<span class="sep">·</span><span>fw ${esc(st.firmware)}</span>` : "";
  let html = `<div class="p-head"><div class="titles"><div class="p-title" title="${esc(p.name)}">${esc(p.name)}</div>
    <div class="p-sub"><span>${esc(st.variant || p.model || p.kind_label)}</span><span class="sep">·</span><span>${esc(p.host)}</span>${fw}</div></div>
    ${pill(TONE[cls] || "idle", label)}</div>`;

  if (st.online === false && st.error) html += `<div class="callout bad">${ic("alert")}<div class="txt"><b>Can't reach this printer</b><span class="sub">${esc(st.error)}</span></div></div>`;
  if (!p.paired && st.online) {
    html += p.pairing
      ? `<div class="callout info"><span class="spinner"></span><div class="txt"><b>Waiting for approval</b><span class="sub">Press <b style="display:inline">Allow</b> on the printer's touchscreen.</span>
          <div><button class="btn small" data-a="unpair-cancel">Cancel</button></div></div></div>`
      : `<div class="callout warn">${ic("alert")}<div class="txt"><b>Not paired</b><span class="sub">You can watch this printer, but pair it to send jobs or pause and abort prints.</span>
          <div><button class="btn small primary" data-a="pair">Pair now</button></div></div></div>`;
  }
  if (cls === "cleanup" && st.clear_reason === "unknown") {
    html += `<div class="callout warn">${ic("alert")}<div class="txt"><b>Check the bed is empty</b><span class="sub">Local Print Factory can't see what's on this printer's bed. No job is sent until you confirm it's clear.</span>
      <div><button class="btn small warn" data-a="clear">${ic("check")}Plate is clear</button></div></div></div>`;
  } else if (cls === "cleanup") {
    html += p.caps.includes("clear")
      ? `<div class="callout warn">${ic("alert")}<div class="txt"><b>Remove the print from the bed</b><span class="sub">This printer doesn't check by itself. The next job waits until you confirm.</span>
          <div><button class="btn small warn" data-a="clear">${ic("check")}Plate is clear</button></div></div></div>`
      : `<div class="callout warn">${ic("alert")}<div class="txt"><b>Remove the print from the bed</b><span class="sub">Then confirm on the printer's touchscreen to free it for the next job.</span></div></div>`;
  }
  if (cls === "attention") html += `<div class="callout warn">${ic("alert")}<div class="txt"><b>The printer is asking a question</b><span class="sub">Check its screen. Often a material or core mismatch.</span></div></div>`;

  if (job) {
    const pct = Math.round((job.progress || 0) * 100);
    const done = cls === "cleanup";
    const right = done ? `<b>${esc(job.result || "Done")}</b>` : job.remaining ? `<span><b>${dur(job.remaining)}</b> left · done ${eta(job.remaining)}</span>` : "";
    html += `<div class="jobbox">${thumb(qjob)}<div class="jobinfo">
      <div class="jobname" title="${esc(job.name)}">${esc(job.name)}</div>
      <div class="bar ${done ? "done" : cls === "paused" ? "warn" : ""}"><div style="width:${pct}%"></div></div>
      <div class="jobline"><span><b>${pct}%</b>${job.layer ? ` · layer ${esc(job.layer)}` : ""}</span>${right}</div>
      ${qjob && qjob.message ? `<div class="jobline">${esc(qjob.message)}</div>` : ""}</div></div>`;
  } else if (qjob) {
    html += `<div class="jobbox">${thumb(qjob)}<div class="jobinfo"><div class="jobname">${esc(qjob.name)}</div>
      <div class="jobline"><span class="target"><span class="spinner"></span>${esc(qjob.message || (qjob.status === "sent" ? "Sent, waiting for the printer to start…" : "Uploading to printer…"))}</span></div></div></div>`;
  } else if (st.online && cls === "idle") {
    const next = STATE.jobs.some((j) => j.status === "queued");
    html += `<div class="idlebox">${ic("check")}<span>${!p.accept_jobs ? "Ready. Not taking jobs from the queue." : next ? "Ready. Waiting for a job that fits this printer." : "Ready for the next job."}</span></div>`;
  }

  if (st.online) {
    const um = p.kind === "ultimaker";
    const r = (k, c, t, extra = "") => `<div class="reading${t ? " heat" : ""}"><div class="k">${k}</div><div class="v">${deg(c)}${t ? ` <small>/ ${deg(t)}</small>` : ""}</div>${extra}</div>`;
    const ex = (st.extruders || []).map((e) => um
      ? r(`${e.index + 1} · ${esc(e.hotend || "No core")}`, e.temp, e.target, `<div class="mat">${matLabel(e.material, e.material_guid)}</div>`)
      : r("Nozzle", e.temp, e.target)).join("");
    const bed = st.bed ? r("Bed", st.bed.temp, st.bed.target) : "";
    const ch = st.chamber != null ? r("Chamber", st.chamber, null) : "";
    html += `<div class="readings">${ex}${bed}${ch}</div>`;
  }
  return html;
}

function printerFoot(p) {
  const st = p.state || {};
  const [cls] = statusOf(p);
  const job = st.job && st.job.state && st.job.state !== "none";
  const can = p.paired && st.online;
  const has = (c) => p.caps.includes(c);
  const dis = can ? "" : "disabled";
  let left = "";
  if (job && has("pause") && ["printing", "attention"].includes(cls)) left += `<button class="btn small" data-a="pause" ${dis}>${ic("pause")}Pause</button>`;
  if (job && has("resume") && cls === "paused") left += `<button class="btn small primary" data-a="resume" ${dis}>${ic("play")}Resume</button>`;
  if (job && has("abort") && cls !== "cleanup") left += `<button class="btn small danger" data-a="abort" ${dis}>${ic("stop")}Abort</button>`;
  if (!left) left = p.accept_jobs ? `<span class="chip">Queue on</span>` : `<span class="chip warn">Queue off</span>`;
  return `${left}<div class="tools">
    ${has("camera") ? `<button class="iconbtn${camOn.has(p.id) ? " on" : ""}" data-a="cam" title="${camOn.has(p.id) ? "Hide camera" : "Show camera"}">${ic("camera")}</button>` : ""}
    ${has("identify") ? `<button class="iconbtn" data-a="identify" ${dis} title="Blink the printer's lights">${ic("bulb")}</button>` : ""}
    <button class="iconbtn" data-a="more" title="Settings and more">${ic("more")}</button></div>`;
}

async function onPrinterClick(ev) {
  const b = ev.target.closest("[data-a]"); if (!b || b.disabled) return;
  const id = Number(ev.currentTarget.dataset.id);
  const p = STATE.printers.find((x) => x.id === id);
  const a = b.dataset.a;
  if (a === "pause" || a === "resume") return act(() => api(`/api/printers/${id}/action`, { method: "POST", json: { action: a } }), a === "pause" ? "Pausing…" : "Resuming…");
  if (a === "abort") {
    if (await confirmDlg(`Abort the print on ${p.name}?`, "The print stops straight away. This can't be undone.", "Abort print", true))
      act(() => api(`/api/printers/${id}/action`, { method: "POST", json: { action: "abort" } }), "Aborting…");
    return;
  }
  if (a === "cam") { camOn.has(id) ? camOn.delete(id) : camOn.add(id); return renderPrinters(); }
  if (a === "clear") return act(() => api(`/api/printers/${id}/clear`, { method: "POST" }), "Thanks. The printer is free for the next job.");
  if (a === "identify") return act(() => api(`/api/printers/${id}/identify`, { method: "POST" }), "Blinking lights");
  if (a === "pair") return startPair(id);
  if (a === "unpair-cancel") return act(() => api(`/api/printers/${id}/pair`, { method: "DELETE" }));
  if (a === "more") return printerMenu(p);
}

async function startPair(id) {
  try { await api(`/api/printers/${id}/pair`, { method: "POST" }); }
  catch (e) { return toast(e.message, true); }
  toast("Now press Allow on the printer's touchscreen");
  await refresh();
  const poll = async () => {
    try {
      const r = await api(`/api/printers/${id}/pair`);
      if (r.status === "waiting") return setTimeout(poll, 2000);
      if (r.status === "paired") toast("Paired. This printer can now take jobs.");
      else if (r.message) toast(r.message, true);
    } catch (e) { toast(e.message, true); }
    refresh();
  };
  setTimeout(poll, 2000);
}

function printerMenu(p) {
  const has = (c) => p.caps.includes(c);
  const controls = (has("message") ? `<label class="field">Message on the printer screen<div class="row"><input name="msg" placeholder="e.g. Please collect your print"><button class="btn" value="msg">Send</button></div></label>` : "")
    + (has("led") ? `<label class="field">Light colour<div class="row"><input type="color" name="color" value="#ffffff"><button class="btn" value="led">Set colour</button><button class="btn" value="ledoff">Lights off</button></div></label>` : "");
  dialog(`<h2>${esc(p.name)}</h2><p class="dsub">${esc(p.model || p.kind_label)} · ${esc(p.host)}</p>
    <label class="field">Name<input name="name" value="${esc(p.name)}"></label>
    <label class="field">IP address or hostname<input name="host" value="${esc(p.host)}"></label>
    ${p.kind !== "ultimaker" ? `<label class="field">Model<input name="model" value="${esc(p.model || "")}" placeholder="e.g. Elegoo Neptune 4 Pro"><small>Must match the printer name your slicer writes into files.</small></label>` : ""}
    ${p.kind === "moonraker" ? `<label class="field">Moonraker API key<input name="api_key" type="password" placeholder="${p.has_api_key ? "Set. Leave blank to keep it." : "Not set. Usually not needed."}"></label>` : ""}
    <label class="checkrow switch"><input type="checkbox" name="accept_jobs" ${p.accept_jobs ? "checked" : ""}><span></span>Take jobs from the queue</label>
    ${controls ? `<div class="dsection"><div class="lbl">Printer controls</div>${controls}</div>` : ""}
    <div class="dsection"><div class="lbl">More</div><div class="btnrow">
      ${has("history") ? `<button class="btn small" value="hist">${ic("history")}Printer's history</button>` : ""}
      ${has("clear") ? `<button class="btn small" value="clear">${ic("check")}Mark plate clear</button>` : ""}
      ${has("pair") ? `<button class="btn small" value="repair">${ic("repeat")}Re-pair</button>` : ""}
      <button class="btn small danger" value="remove">${ic("trash")}Remove printer</button></div></div>`,
    [["Cancel", "close"], ["Save changes", "save", "primary"]], async (v, form) => {
      const id = p.id;
      if (v === "save") {
        const body = { name: form.name.value, host: form.host.value, accept_jobs: form.accept_jobs.checked };
        if (form.model) body.model = form.model.value;
        if (form.api_key && form.api_key.value) body.api_key = form.api_key.value;
        return act(() => api(`/api/printers/${id}`, { method: "PATCH", json: body }), "Saved");
      }
      if (v === "msg") { act(() => api(`/api/printers/${id}/message`, { method: "POST", json: { message: form.msg.value } }), "Message shown on printer"); return false; }
      if (v === "led" || v === "ledoff") {
        const [h, s, l] = hexToHsb(form.color.value);
        act(() => api(`/api/printers/${id}/led`, { method: "POST", json: v === "ledoff" ? { brightness: 0 } : { hue: h, saturation: s, brightness: l } }), "Lights updated"); return false;
      }
      if (v === "clear") return act(() => api(`/api/printers/${id}/clear`, { method: "POST" }), "Plate marked clear");
      if (v === "repair") return startPair(id);
      if (v === "hist") return printerHistory(p);
      if (v === "remove" && await confirmDlg(`Remove ${p.name}?`, "Its queue history is kept. You can add it again later.", "Remove printer", true))
        return act(() => api(`/api/printers/${id}`, { method: "DELETE" }), "Printer removed");
    });
}
function hexToHsb(hex) {
  const r = parseInt(hex.slice(1, 3), 16) / 255, g = parseInt(hex.slice(3, 5), 16) / 255, b = parseInt(hex.slice(5, 7), 16) / 255;
  const max = Math.max(r, g, b), min = Math.min(r, g, b), d = max - min;
  let h = 0;
  if (d) h = max === r ? ((g - b) / d) % 6 : max === g ? (b - r) / d + 2 : (r - g) / d + 4;
  return [Math.round(((h * 60) + 360) % 360), Math.round(max ? (d / max) * 100 : 0), Math.round(max * 100)];
}
const RESULT_TONE = (r) => JOB_TONE[(r || "").toLowerCase()] || "idle";
async function printerHistory(p) {
  let rows;
  try { rows = await api(`/api/printers/${p.id}/history`); } catch (e) { return toast(e.message, true); }
  dialog(`<h2>Recent prints</h2><p class="dsub">${esc(p.name)}, from the printer's own records</p>
    ${rows.length ? `<div class="list">${rows.map((r) => `<div class="li"><div class="grow"><div class="jobname" title="${esc(r.name)}">${esc(r.name)}</div>
      <div class="sub">${esc(r.started || "")}${r.duration ? " · " + dur(r.duration) : ""}</div></div>${pill(RESULT_TONE(r.result), r.result || "Unknown")}</div>`).join("")}</div>`
      : `<p class="muted">No prints recorded.</p>`}`, [["Close", "close"]]);
}

let KINDS = null;
$("#btn-add-printer").onclick = async () => {
  if (!KINDS) { try { KINDS = await api("/api/kinds"); } catch (e) { return toast(e.message, true); } }
  dialog(`<h2>Add printer</h2><p class="dsub">Connect a printer on your local network.</p>
  <label class="field">Printer type<select name="kind_id">${KINDS.map((k) => `<option value="${k.id}">${esc(k.label)}</option>`).join("")}</select></label>
  <label class="field">IP address or hostname<input name="host" required placeholder="192.168.1.50"></label>
  <label class="field">Name<input name="name" placeholder="Optional. Read from the printer if left blank."></label>
  <label class="field" id="f-model" hidden>Model name<input name="model" placeholder="e.g. Voron 2.4"><small>As your slicer writes it into files.</small></label>
  <label class="field" id="f-key" hidden>Moonraker API key<input name="api_key" type="password" placeholder="Usually not needed"></label>
  <div class="callout info">${ic("alert")}<div class="txt sub" id="kind-hint"></div></div>`,
  [["Cancel", "close"], ["Add printer", "add", "primary"]], async (v, f) => {
    if (v !== "add") return;
    const r = await act(() => api("/api/printers", { method: "POST", json: { kind_id: f.kind_id.value, host: f.host.value, name: f.name.value, model: f.model.value, api_key: f.api_key.value } }), "Printer added");
    if (!r) return false;            // keep the dialog open so the address can be corrected
    if (r.id && r.caps.includes("pair")) startPairPrompt(r);
  });
  const sel = $("#dlg-body [name=kind_id]");
  const hints = {
    ultimaker: "Find the IP on the printer under <i>Preferences › Network</i>. After adding it, you pair it by pressing Allow on its screen.",
    moonraker: "Use the address you open Fluidd with. It's shown in the printer's network settings.",
    sdcp: "The IP is in the printer's network settings. It must be on the same network as this server.",
  };
  const upd = () => {
    const k = KINDS.find((x) => x.id === sel.value);
    $("#f-model").hidden = !(k.kind === "moonraker" && !k.model);
    $("#f-key").hidden = k.kind !== "moonraker";
    $("#kind-hint").innerHTML = hints[k.kind] + " A fixed IP (a DHCP reservation in your router) is strongly recommended.";
  };
  sel.onchange = upd; upd();
};
function startPairPrompt(p) {
  dialog(`<h2>Pair ${esc(p.name)}?</h2><p>Pairing lets Local Print Factory send jobs and pause or abort prints. After you click <b>Pair</b>,
    go to the printer and press <b>Allow</b> on its touchscreen.</p>`, [["Later", "close"], ["Pair", "pair", "primary"]], (v) => { if (v === "pair") startPair(p.id); });
}

$("#btn-discover").onclick = async () => {
  dialog(`<h2>Find printers</h2><p class="dsub">Searching the local network…</p><div class="idlebox"><span class="spinner"></span>Looking for Ultimaker and Centauri Carbon printers</div>`, [["Close", "close"]]);
  let found;
  try { found = await api("/api/discover"); } catch (e) { $("#dlg").close(); return toast(e.message, true); }
  if (!$("#dlg").open) return;
  $("#dlg-body").innerHTML = `<h2>Find printers</h2><p class="dsub">${found.length ? `${found.length} found on your network` : "Nothing answered"}</p>
    ${found.length ? `<div class="list">${found.map((f, i) => `<div class="li"><div class="thumb-sm" style="width:36px;height:36px">${ic("printer")}</div>
      <div class="grow"><b>${esc(f.name)}</b><div class="sub">${esc(f.type)} · ${esc(f.host)}${f.firmware ? " · fw " + esc(f.firmware) : ""}</div></div>
      ${f.added ? `<span class="chip">Added</span>` : `<button class="btn small primary" type="button" data-i="${i}">${ic("plus")}Add</button>`}</div>`).join("")}</div>`
    : `<p>Check the printers are switched on and on the same network as this server. In Docker, discovery needs <code>network_mode: host</code>.</p>`}
    <p class="muted" style="font-size:12.5px">Neptune 4 and other Klipper printers don't announce themselves. Add those with <b>Add printer</b>.</p>`;
  $$("#dlg-body [data-i]").forEach((b) => b.onclick = async () => {
    const f = found[b.dataset.i];
    const r = await act(() => api("/api/printers", { method: "POST", json: { host: f.host, name: f.name, kind_id: f.kind_id } }), `Added ${f.name}`);
    if (r) b.outerHTML = `<span class="chip">Added</span>`;
  });
};

// ---------------------------------------------------------------- queue
function renderQueue() {
  const jobs = STATE.jobs;
  const queued = jobs.filter((j) => j.status === "queued");
  const active = jobs.filter((j) => j.status !== "queued");
  $("#queue-count").textContent = queued.length || "";
  const pname = (id) => (STATE.printers.find((p) => p.id === id) || {}).name || "Removed printer";
  if (!jobs.length) {
    setHTML($("#queue-list"), empty("queue", "The queue is empty", `Upload a file here, or pick one in the <b>Library</b> and click <b>Print</b>.`));
    return;
  }
  const jobCell = (j) => `<div class="cellmain">${thumb(j)}<div style="min-width:0"><b>${esc(j.name)}</b>${j.message ? `<div class="sub">${esc(j.message)}</div>` : ""}</div></div>`;
  let html = "";
  if (active.length) {
    html += `<div class="sechead"><h2>On printers now</h2><span class="muted">${active.length}</span></div>
    <div class="tablewrap"><table><thead><tr><th>Job</th><th>Printer</th><th>Status</th><th class="num hide-sm">Progress</th></tr></thead><tbody>
    ${active.map((j) => `<tr><td>${jobCell(j)}</td><td>${esc(j.printer_name || pname(j.printer_id))}</td>
      <td>${pill(JOB_TONE[j.status] || "idle", JOB_LABEL[j.status] || j.status)}</td>
      <td class="num hide-sm">${["printing", "paused"].includes(j.status) ? `<b>${Math.round(j.progress * 100)}%</b>` : `<span class="muted">–</span>`}</td></tr>`).join("")}
    </tbody></table></div>`;
  }
  html += `<div class="sechead"><h2>Up next</h2><span class="muted">${queued.length ? `${queued.length} waiting` : "Nothing waiting"}</span></div>`;
  html += queued.length ? `<div class="tablewrap"><table><thead><tr><th>#</th><th>Job</th><th>Printer</th><th class="num hide-sm">Est. time</th><th></th></tr></thead><tbody>
    ${queued.map((j, qi) => `<tr data-id="${j.id}"><td class="pos">${qi + 1}</td><td>${jobCell(j)}</td>
      <td>${j.target_printer_id ? `<span class="target">${ic("target")}${esc(pname(j.target_printer_id))}</span>` : `<span class="muted">Any suitable printer</span>`}</td>
      <td class="num hide-sm muted">${j.est_time ? dur(j.est_time) : "–"}</td>
      <td class="act">
        <button class="iconbtn" data-a="top" title="Move to top" ${qi === 0 ? "disabled" : ""}>${ic("top")}</button>
        <button class="iconbtn" data-a="up" title="Move up" ${qi === 0 ? "disabled" : ""}>${ic("up")}</button>
        <button class="iconbtn" data-a="down" title="Move down" ${qi === queued.length - 1 ? "disabled" : ""}>${ic("down")}</button>
        <button class="iconbtn" data-a="target" title="Choose printer">${ic("target")}</button>
        <button class="iconbtn danger" data-a="cancel" title="Remove from queue">${ic("x")}</button></td></tr>`).join("")}
    </tbody></table></div>` : "";
  setHTML($("#queue-list"), html);
}
$("#queue-list").addEventListener("click", async (ev) => {
  const b = ev.target.closest("[data-a]"); if (!b || b.disabled) return;
  const id = Number(b.closest("tr").dataset.id), a = b.dataset.a;
  if (["top", "up", "down"].includes(a)) return act(() => api(`/api/jobs/${id}/move`, { method: "POST", json: { direction: a } }));
  if (a === "cancel") return act(() => api(`/api/jobs/${id}/cancel`, { method: "POST" }), "Removed from queue");
  if (a === "target") {
    const j = STATE.jobs.find((x) => x.id === id);
    dialog(`<h2>Choose printer</h2><p class="dsub">${esc(j.name)}</p><label class="field">Printer<select name="t">${printerOptions(j.target_printer_id)}</select>
      <small>"Any suitable printer" sends it to the first idle printer that matches the file.</small></label>`,
      [["Cancel", "close"], ["Save", "save", "primary"]], (v, f) => { if (v === "save") act(() => api(`/api/jobs/${id}`, { method: "PATCH", json: { target_printer_id: Number(f.t.value) || null } })); });
  }
});
function printerOptions(sel) {
  return `<option value="">Any suitable printer</option>` + STATE.printers.map((p) => `<option value="${p.id}" ${p.id === sel ? "selected" : ""}>${esc(p.name)}</option>`).join("");
}
$("#queue-upload").onchange = (e) => uploadFiles(e.target.files, true);

// ---------------------------------------------------------------- library
let LIB = [];
async function loadLibrary() {
  try { LIB = await api("/api/files"); } catch (e) { return toast(e.message, true); }
  renderLibrary();
}
function renderLibrary() {
  const s = $("#lib-search").value.toLowerCase();
  const rows = LIB.filter((f) => !s || f.filename.toLowerCase().includes(s) || ((f.meta || {}).machine || "").toLowerCase().includes(s));
  if (!rows.length) {
    setHTML($("#lib-list"), LIB.length ? empty("search", "No matches", "Try a different search.") : empty("library", "No files yet", "Drop sliced files above, or save them into the hot folder."));
    return;
  }
  setHTML($("#lib-list"), rows.map((f) => {
    const m = f.meta || {};
    const exs = Object.entries(m.extruders || {}).filter(([, e]) => e.material_guid || e.nozzle);
    return `<div class="panel file" data-id="${f.id}">
      <div class="thumb">${f.has_thumb ? `<img loading="lazy" src="/api/files/${f.id}/thumb" alt="">` : ic("cube")}</div>
      <div class="fbody"><div class="fname">${esc(f.filename)}</div>
        <div class="facts"><span>${ic("printer")}${esc(m.machine || "Any printer")}</span>${m.print_time ? `<span>${ic("clock")}${dur(m.print_time)}</span>` : ""}<span>${size(f.size)}</span></div>
        ${exs.map(([i, e]) => `<div class="mat">${exs.length > 1 ? `${Number(i) + 1}: ` : ""}${e.nozzle ? esc(e.nozzle) + " · " : ""}${matLabel(e.material, e.material_guid)}</div>`).join("")}
        ${m.warning ? `<div class="callout warn">${ic("alert")}<div class="txt">${esc(m.warning)}</div></div>` : ""}</div>
      <div class="ffoot"><button class="btn small primary" data-a="queue">${ic("play")}Print</button>
        <a class="iconbtn" href="/api/files/${f.id}/download" title="Download">${ic("download")}</a>
        <button class="iconbtn danger" data-a="delete" title="Delete">${ic("trash")}</button></div></div>`;
  }).join(""));
}
$("#lib-search").oninput = renderLibrary;
$("#lib-list").addEventListener("click", async (ev) => {
  const b = ev.target.closest("[data-a]"); if (!b) return;
  const id = Number(b.closest(".file").dataset.id);
  const f = LIB.find((x) => x.id === id);
  if (b.dataset.a === "delete") {
    if (await confirmDlg(`Delete ${f.filename}?`, "The file is removed from the library. Jobs already queued from it are kept.", "Delete file", true)) { await act(() => api(`/api/files/${id}`, { method: "DELETE" }), "Deleted"); loadLibrary(); }
    return;
  }
  let checks = [];
  try { checks = await api(`/api/files/${id}/check`); } catch (e) { /* non-fatal */ }
  const notes = checks.filter((c) => c.blocking.length || c.warnings.length);
  dialog(`<h2>Print file</h2><p class="dsub">${esc(f.filename)}</p>
    <label class="field">Printer<select name="t">${printerOptions(null)}</select></label>
    <label class="field">Copies<input type="number" name="copies" value="1" min="1" max="100"></label>
    ${notes.length ? `<div class="checks">${notes.map((c) => `<div class="callout ${c.blocking.length ? "bad" : "warn"}">${ic("alert")}<div class="txt"><b>${esc(c.name)}${c.blocking.length ? ": won't be sent here" : ""}</b><span class="sub">${esc([...c.blocking, ...c.warnings].join("; "))}</span></div></div>`).join("")}</div>` : ""}`,
    [["Cancel", "close"], ["Add to queue", "q", "primary"]], (v, form) => {
      if (v === "q") act(() => api(`/api/files/${id}/queue`, { method: "POST", json: { copies: Number(form.copies.value), printer_id: Number(form.t.value) || null } }), "Added to queue");
    });
});
$("#lib-upload").onchange = (e) => uploadFiles(e.target.files, false);
const dz = $("#dropzone");
["dragenter", "dragover"].forEach((t) => dz.addEventListener(t, (e) => { e.preventDefault(); dz.classList.add("over"); }));
["dragleave", "drop"].forEach((t) => dz.addEventListener(t, (e) => { e.preventDefault(); dz.classList.remove("over"); }));
dz.addEventListener("drop", (e) => uploadFiles(e.dataTransfer.files, false));
async function uploadFiles(files, queue) {
  if (!files || !files.length) return;
  const fd = new FormData();
  [...files].forEach((f) => fd.append("files", f));
  fd.append("queue", queue ? "1" : "0");
  toast(`Uploading ${files.length} file${files.length > 1 ? "s" : ""}…`);
  await act(() => api("/api/files", { method: "POST", body: fd }), queue ? "Uploaded and queued" : "Uploaded");
  $("#lib-upload").value = ""; $("#queue-upload").value = "";
  if (currentTab === "library") loadLibrary();
}

// ---------------------------------------------------------------- history
const EVENT_TONE = (k) => /finish|online|paired/.test(k) ? "ok" : /fail|error|offline|abort/.test(k) ? "bad"
  : /pause|attention|cleanup/.test(k) ? "warn" : /start|sent/.test(k) ? "accent" : "idle";
const EVENT_LABEL = (k) => k.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
async function loadHistory() {
  const mode = $("#hist-mode").dataset.value;
  const el = $("#hist-list");
  try {
    if (mode === "events") {
      const ev = await api("/api/events");
      setHTML(el, ev.length ? `<div class="tablewrap"><table><thead><tr><th>Time</th><th>Event</th><th class="hide-sm">Printer</th><th>Detail</th></tr></thead><tbody>
        ${ev.map((e) => `<tr><td class="muted" style="white-space:nowrap">${when(e.time)}</td><td>${pill(EVENT_TONE(e.kind), EVENT_LABEL(e.kind))}</td>
        <td class="hide-sm">${esc(e.printer_name || "")}</td><td>${esc(e.message)}</td></tr>`).join("")}</tbody></table></div>`
        : empty("history", "No events yet", "Prints starting, finishing and printers going offline show up here."));
    } else {
      const jobs = await api("/api/jobs?done=1");
      setHTML(el, jobs.length ? `<div class="tablewrap"><table><thead><tr><th>Job</th><th class="hide-sm">Printer</th><th>Result</th><th class="hide-sm">Finished</th><th></th></tr></thead><tbody>
        ${jobs.map((j) => `<tr data-id="${j.id}"><td><div class="cellmain">${thumb(j)}<div style="min-width:0"><b>${esc(j.name)}</b>${j.message ? `<div class="sub">${esc(j.message)}</div>` : ""}</div></div></td>
        <td class="hide-sm">${esc(j.printer_name || "–")}</td><td>${pill(JOB_TONE[j.status] || "idle", JOB_LABEL[j.status] || j.status)}</td>
        <td class="muted hide-sm" style="white-space:nowrap">${when(j.finished_at || j.sent_at)}</td>
        <td class="act"><button class="btn small" data-a="reprint" ${j.file_id ? "" : "disabled"}>${ic("repeat")}Print again</button>
          <button class="iconbtn danger" data-a="del" title="Remove from history">${ic("x")}</button></td></tr>`).join("")}</tbody></table></div>`
        : empty("history", "No finished jobs yet", "Jobs move here once they finish, fail or are cancelled."));
    }
  } catch (e) { toast(e.message, true); }
}
$("#hist-mode").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  $("#hist-mode").dataset.value = b.dataset.v;
  $$("#hist-mode button").forEach((x) => x.classList.toggle("on", x === b));
  loadHistory();
});
$("#hist-list").addEventListener("click", async (ev) => {
  const b = ev.target.closest("[data-a]"); if (!b || b.disabled) return;
  const id = Number(b.closest("tr").dataset.id);
  if (b.dataset.a === "reprint") await act(() => api(`/api/jobs/${id}/reprint`, { method: "POST" }), "Added to queue");
  if (b.dataset.a === "del") await act(() => api(`/api/jobs/${id}`, { method: "DELETE" }));
  loadHistory();
});

// ---------------------------------------------------------------- settings
async function loadSettings() {
  try {
    const s = await api("/api/settings"); const f = $("#settings-form");
    f.webhook_url.value = s.webhook_url || "";
    f.strict_materials.checked = s.strict_materials === "1";
    f.hotfolder_autoqueue.checked = s.hotfolder_autoqueue === "1";
  } catch (e) { toast(e.message, true); }
  $("#btn-logout").hidden = !STATE.login;
  $("#about").innerHTML = `Local Print Factory ${esc(STATE.version)}. Talks directly to each printer on your network. Nothing goes through the cloud.`;
}
$("#settings-form").onsubmit = (e) => {
  e.preventDefault(); const f = e.target;
  act(() => api("/api/settings", { method: "PUT", json: { webhook_url: f.webhook_url.value.trim(), strict_materials: f.strict_materials.checked ? "1" : "0", hotfolder_autoqueue: f.hotfolder_autoqueue.checked ? "1" : "0" } }), "Settings saved");
};
$("#btn-test-hook").onclick = () => act(() => api("/api/settings/test-webhook", { method: "POST" }), "Test notification sent");
$("#btn-logout").onclick = async () => { await api("/api/logout", { method: "POST" }); location.reload(); };

// ---------------------------------------------------------------- dialogs
// Buttons are given in reading order [Cancel, ..., Primary]. They are written to the DOM reversed (and shown
// reversed again with CSS) so the primary button is the form's default: pressing Enter triggers it.
function dialog(html, buttons, onClick) {
  const dlg = $("#dlg");
  $("#dlg-body").innerHTML = html;
  $("#dlg-actions").innerHTML = [...buttons].reverse().map(([l, v, c]) => `<button class="btn ${c || ""}" value="${v}">${esc(l)}</button>`).join("");
  const form = $("#dlg-form");
  form.onsubmit = async (e) => {
    e.preventDefault();
    const v = e.submitter ? e.submitter.value : "";
    if (v === "close" || !onClick) return dlg.close();
    const keep = await onClick(v, form);
    if (keep !== false && dlg.open) dlg.close();
  };
  if (!dlg.open) dlg.showModal();
  const first = $("#dlg-body input:not([type=hidden]), #dlg-body select");
  if (first && !first.value) first.focus();
}
$("#dlg-x").onclick = () => $("#dlg").close();
function confirmDlg(title, text, okLabel, danger) {
  return new Promise((res) => {
    dialog(`<h2>${esc(title)}</h2><p>${esc(text)}</p>`, [["Cancel", "close"], [okLabel, "ok", danger ? "danger" : "primary"]], (v) => res(v === "ok"));
    $("#dlg").addEventListener("close", () => res(false), { once: true });
  });
}
function showLogin() {
  if ($("#dlg").open && $("#dlg-body [name=password]")) return;
  dialog(`<h2>Log in</h2><p class="dsub">This Local Print Factory is password protected.</p><label class="field">Password<input type="password" name="password" autofocus></label>`, [["Log in", "login", "primary"]], async (v, f) => {
    const fd = new FormData(); fd.append("password", f.password.value);
    try { await api("/api/login", { method: "POST", body: fd }); location.reload(); }
    catch (e) { toast(e.message, true); return false; }
  });
}

// ---------------------------------------------------------------- main loop
async function refresh() {
  try {
    STATE = await api("/api/state");
    $("#conn").classList.remove("down"); $("#conn-text").textContent = "Connected";
    $("#ver").textContent = "Version " + STATE.version;
    renderPrinters(); renderQueue();
  } catch (e) { $("#conn").classList.add("down"); $("#conn-text").textContent = "Server unreachable"; }
}
(async function loop() { await refresh(); setTimeout(loop, 2000); })();
const initial = location.hash.slice(1);
if (initial && $("#tab-" + initial)) showTab(initial);
