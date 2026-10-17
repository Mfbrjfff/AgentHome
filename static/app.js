/* ════════════════════════════════════════════════════════════════════
   Atlas — Smart Home Command Center front-end (Vanilla JS, WebSocket)
   Liquid Glass interactivity: glow tints, ripples, spring UI, live tiles,
   chat + thought-log streaming, drag-sortable scheduler, SVG analytics.
   ════════════════════════════════════════════════════════════════════ */
"use strict";

const $ = (id) => document.getElementById(id);
const GLOW_COLORS = { hue: "#ffb340", roidmi: "#4da3ff", withings: "#37e6a0",
                      spotify: "#2dd4bf", agent: "#a78bfa", weather: "#facc15",
                      ha: "#38bdf8", system: "#94a3b8", web: "#f472b6" };

let ws = null, wsRetry = 0;
const state = { devices: {}, jobs: [], pendingBot: false };

/* ───────────────────────── toasts & glow helpers ──────────────────────── */
let toastTimer = null;
function toast(msg, ms = 2600) {
  const t = $("toast"); t.textContent = msg; t.classList.add("show");
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove("show"), ms);
}

let tintTimeout = null;
function liquidGlow(kind, strong = true) {
  const f = $("glowField");
  f.className = "glow-field tint-" + kind + (strong ? " pulse" : "");
  clearTimeout(tintTimeout);
  tintTimeout = setTimeout(() => (f.className = "glow-field"), strong ? 3800 : 1500);
}

function rippleAt(x, y, color) {
  const r = document.createElement("div");
  r.className = "ripple";
  r.style.cssText = `left:${x - 60}px;top:${y - 60}px;width:120px;height:120px;color:${color}`;
  document.body.appendChild(r);
  setTimeout(() => r.remove(), 1500);
}
function rippleTile(domain) {
  const tile = document.querySelector(`.tile[data-domain="${domain}"]`);
  if (!tile) return;
  const b = tile.getBoundingClientRect();
  rippleAt(b.left + b.width / 2, b.top + b.height / 2, GLOW_COLORS[domain] || "#fff");
  tile.classList.add("lit");
  setTimeout(() => tile.classList.remove("lit"), 2600);
}

/* ───────────────────────── tabs ───────────────────────────────────────── */
document.querySelectorAll(".tab").forEach((btn) =>
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b === btn));
    document.querySelectorAll(".page").forEach((p) =>
      p.classList.toggle("active", p.id === "page-" + btn.dataset.tab));
    if (btn.dataset.tab === "analytics") loadAnalytics();
    if (btn.dataset.tab === "tasks") loadJobs();
    if (btn.dataset.tab === "settings") { loadSystemOnce(); loadToolsOnce(); }
  }));

$("fabTerm")?.addEventListener("click", () => {
  $("terminalPanel").classList.toggle("open");
  document.querySelector('[data-tab="agent"]').click();
});

/* ───────────────────────── websocket ──────────────────────────────────── */
function connectWS() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => {
    wsRetry = 0;
    setConn(true);
    logLine("[SYS] WebSocket connected — live telemetry streaming.", "info");
  };
  ws.onclose = () => {
    setConn(false);
    wsRetry++;
    setTimeout(connectWS, Math.min(15000, 800 * 2 ** Math.min(wsRetry, 4)));
  };
  ws.onerror = () => ws.close();
  ws.onmessage = (e) => handleMessage(JSON.parse(e.data));
}
function setConn(ok) {
  const p = $("connPill");
  p.className = "status-pill " + (ok ? "pill-online" : "pill-offline");
  p.querySelector("span").textContent = ok ? "live · LAN" : "reconnecting…";
}

function handleMessage(m) {
  if (m.type === "log") return logLine(m.text, m.level, m.kind);
  if (m.type === "chat_reply") return addMsg("bot", m.text);
  if (m.type === "snapshot") {
    state.devices = m.device_state || {};
    (m.recent || []).forEach((r) => r.type === "log" && logLine(r.text, r.level, r.kind));
    renderAll();
    return;
  }
  if (m.type === "event") {
    const k = m.kind;
    state.devices[k] = Object.assign(state.devices[k] || {}, m.payload || {});
    if (m.glow) { liquidGlow(k); rippleTile(k); }
    renderDomain(k);
    if (k === "system") renderSystem(m.payload);
  }
}

/* ───────────────────────── terminal ───────────────────────────────────── */
function logLine(text, level = "info", kind = "agent") {
  const box = $("termScroll");
  if (!text) return;
  const div = document.createElement("div");
  div.className = "log-line lv-" + level + " k-" + kind;
  const ts = new Date().toLocaleTimeString([], { hour12: false });
  div.innerHTML = `<span class="log-time">${ts}</span><span class="log-text"></span>`;
  div.querySelector(".log-text").textContent = text;
  box.appendChild(div);
  while (box.children.length > 400) box.firstChild.remove();
  box.scrollTop = box.scrollHeight;
}

/* ───────────────────────── chat ───────────────────────────────────────── */
const pendingChats = [];          // FIFO of typing bubbles awaiting their reply

function addMsg(who, text) {
  const box = $("chatScroll");
  if (who === "bot") {
    // pop the oldest pending typing bubble (replies arrive in order)
    const q = pendingChats.shift();
    if (q && document.body.contains(q)) q.remove(); else typingBubble(false);
  }
  const div = document.createElement("div");
  div.className = "msg " + who;
  div.textContent = text;
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
  if (who === "user") liquidGlow("agent", false);
}
function typingBubble(on) {
  let el = $("typingMsg");
  if (on) {
    el = document.createElement("div");
    el.id = "typingMsg"; el.className = "msg bot pending";
    el.innerHTML = '<span class="typing"><i></i><i></i><i></i></span> thinking…';
    $("chatScroll").appendChild(el);
    pendingChats.push(el);
    $("chatScroll").scrollTop = 1e9;
  } else if (el) el.remove();
}
function sendChat(ev) {
  ev.preventDefault();
  const input = $("chatInput");
  const text = input.value.trim();
  if (!text || !ws || ws.readyState !== 1) return toast("Not connected yet…");
  addMsg("user", text); input.value = ""; typingBubble(true);
  ws.send(JSON.stringify({ type: "chat", text }));
  liquidGlow("agent");
}
window.fillChat = (btn) => { $("chatInput").value = btn.textContent; $("chatInput").focus(); };

/* voice input (Web Speech API) */
const micBtn = $("micBtn");
if ("webkitSpeechRecognition" in window || "SpeechRecognition" in window) {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  const rec = new SR(); rec.lang = "en-US"; rec.interimResults = false;
  rec.onresult = (e) => { $("chatInput").value = e.results[0][0].transcript; sendChat(new Event("submit")); };
  rec.onend = () => micBtn.classList.remove("recording");
  micBtn.onclick = () => { micBtn.classList.toggle("recording");
    micBtn.classList.contains("recording") ? rec.start() : rec.stop(); };
} else micBtn.onclick = () => toast("Voice input needs Chrome/Safari.");

/* ───────────────────────── quick actions ──────────────────────────────── */
window.quickTool = async (name, args) => {
  try {
    const r = await fetch(`/api/tool/${name}`, { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(args || {}) });
    const j = await r.json();
    if (!j.ok) toast("⚠ " + (j.error || "tool failed"));
  } catch (e) { toast("Network error: " + e.message); }
};
window.runRoutine = async (name) => {
  toast(`▶ Running “${name}” — watch the Agent tab…`);
  document.querySelector('[data-tab="agent"]').click();
  liquidGlow("agent");
  try {
    const r = await fetch(`/api/routines/${name}`, { method: "POST" });
    const j = await r.json();
    toast(j.summary ? j.summary.slice(0, 120) : (j.error || "done"), 5200);
  } catch (e) { toast("Routine failed: " + e.message); }
};
window.announcePrompt = async () => {
  const msg = prompt("Announce on Google Home speakers:");
  if (msg) quickTool("ha_service_call", { domain: "tts", service: "google_translate_say",
    entity_id: "media_player.kitchen_hub", data: { message: msg } });
};

/* ───────────────────────── tile rendering ─────────────────────────────── */
function renderAll() { ["hue","spotify","roidmi","withings","weather","ha"].forEach(renderDomain); }

function renderDomain(k) {
  const d = state.devices[k] || {};
  if (k === "hue") {
    const lights = d.lights || {};
    const entries = Object.entries(lights);
    const onCount = entries.filter(([, v]) => v && v.on).length;
    $("hueBadge").textContent = `${onCount}/${entries.length || "?"} on`;
    $("hueSummary").textContent = onCount ? `${onCount} room(s) illuminated` : "No lights on";
    const chips = $("hueRooms");
    if (entries.length && chips.childElementCount !== entries.length) {
      chips.innerHTML = "";
      entries.forEach(([room, v]) => {
        const c = document.createElement("button");
        c.className = "chip" + (v?.on ? " on" : "");
        c.textContent = room;
        c.onclick = () => quickTool("hue_lights", { target: room, on: !(v && v.on) });
        chips.appendChild(c);
      });
    } else [...chips.children].forEach((c, i) => {
      const v = Object.values(lights)[i]; c.classList.toggle("on", !!(v && v.on));
    });
  }
  if (k === "spotify") {
    const tr = d.track || {};
    $("spTrack").textContent = tr.name || "—";
    $("spArtist").textContent = tr.artist || "—";
    $("spBadge").textContent = d.is_playing ? "playing" : "idle";
    $("spPlayBtn").textContent = d.is_playing ? "⏸" : "▶";
    document.querySelector('[data-domain="spotify"]').classList.toggle("spinning", !!d.is_playing);
    if (tr.duration_ms && d.position_ms != null)
      $("spProgress").style.width = (100 * d.position_ms / tr.duration_ms) + "%";
  }
  if (k === "roidmi") {
    const st = d.state || "idle";
    $("vacBadge").textContent = st;
    $("vacState").textContent = ({cleaning:`🧹 Cleaning ${d.current_room || ""}`.trim(),
      returning:"↩ Returning to dock", charging:"🔌 Charging", idle:"Standby", error:"⚠ Error"})[st] || st;
    const batt = d.battery ?? 0, bin = Math.round((d.dustbin_level || 0) * 100);
    $("vacBatt").style.width = batt + "%"; $("vacBattTxt").textContent = batt + "%";
    $("vacBin").style.width = bin + "%"; $("vacBinTxt").textContent = bin + "%";
  }
  if (k === "withings") {
    const l = d.latest;
    if (l) {
      $("scaleWeight").textContent = (l.weight_kg ?? "--") + " kg";
      $("scaleMeta").textContent = `body fat ${l.fat_pct ?? "—"}% · BMI ${l.bmi ?? "—"} · HR ${l.heart_rate ?? "—"} bpm`;
      $("scaleBadge").textContent = "synced";
    }
  }
  if (k === "weather") {
    $("wxTemp").textContent = (d.temp_c ?? "--") + "°";
    $("wCond").textContent = d.condition || "—";
    $("wxMeta").textContent = `humidity ${d.humidity ?? "—"}% · wind ${d.wind_ms ?? "—"} m/s`;
    $("wxSource").textContent = d.source || "—";
  }
  if (k === "ha") {
    if (d.home !== undefined) {
      $("presState").textContent = d.home.length ? `Home: ${d.home.join(", ")}` : "House empty 🏜";
      $("presBadge").textContent = d.away ? "away" : "home";
    }
  }
}

/* ───────────────────────── scheduler UI ───────────────────────────────── */
async function loadJobs() {
  try {
    state.jobs = await (await fetch("/api/jobs")).json();
    renderJobs();
  } catch (e) { /* ignore */ }
}
async function setJobEnabled(job, enabled) {
  // simple, robust model: delete + recreate with the desired enabled flag
  if (!enabled) {
    await fetch(`/api/jobs/${job.job_id}`, { method: "DELETE" });
  } else {
    await fetch(`/api/jobs/${job.job_id}`, { method: "DELETE" });
    await fetch("/api/jobs", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: job.name, description: job.description,
        action_type: job.action_type, action_payload: job.action_payload,
        trigger_kind: job.trigger_kind, trigger_expr: job.trigger_expr }) });
  }
  loadJobs();
}
function renderJobs() {
  const list = $("taskList"); list.innerHTML = "";
  if (!state.jobs.length) list.innerHTML = '<p style="color:var(--ink-dim)">No tasks yet — ask Atlas or tap ＋ New Task.</p>';
  state.jobs.forEach((j) => {
    const card = document.createElement("article");
    card.className = "glass task-card"; card.draggable = true; card.dataset.id = j.job_id;
    card.innerHTML = `
      <span class="task-grip">⠿</span>
      <div class="task-main">
        <b>${esc(j.name)}</b>
        <small>${esc(j.description || "")}${j.next_run ? " · next " + fmt(j.next_run) : ""}</small>
        <div class="task-tags">
          <span class="tag ${j.created_by}">${j.created_by}</span>
          <span class="tag ${j.trigger_kind}">${j.trigger_kind}: ${esc(j.trigger_expr)}</span>
          <span class="tag">${esc(j.action_type)}</span>
        </div>
      </div>
      <label class="switch" title="enable/disable">
        <input type="checkbox" ${j.enabled ? "checked" : ""}>
        <span class="slider"></span>
      </label>
      <div class="task-actions">
        <button class="glass-btn icon-btn" title="Run now">▶</button>
        <button class="glass-btn icon-btn danger" title="Delete">✕</button>
      </div>`;
    const sw = card.querySelector("input");
    const [runBtn, delBtn] = card.querySelectorAll(".icon-btn");
    sw.onchange = () => { toast(sw.checked ? "Task enabled ✦" : "Task removed ⏸"); setJobEnabled(j, sw.checked); };
    runBtn.onclick = async () => { await fetch(`/api/jobs/${j.job_id}/run`, { method: "POST" }); toast("▶ fired"); };
    delBtn.onclick = async () => { await fetch(`/api/jobs/${j.job_id}`, { method: "DELETE" }); loadJobs(); };
    attachDrag(card, list);
    list.appendChild(card);
  });
}
function attachDrag(card, list) {
  card.addEventListener("dragstart", () => card.classList.add("dragging"));
  card.addEventListener("dragend", () => { card.classList.remove("dragging");
    state.jobs = [...list.children].map((c) => state.jobs.find((j) => j.job_id === c.dataset.id)).filter(Boolean); });
  list.addEventListener("dragover", (e) => {
    e.preventDefault();
    const after = [...list.querySelectorAll(".task-card:not(.dragging)")]
      .find((el) => e.clientY < el.getBoundingClientRect().top + el.offsetHeight / 2);
    const dragging = list.querySelector(".dragging");
    if (dragging) after ? list.insertBefore(dragging, after) : list.appendChild(dragging);
  });
}
window.openNewTaskModal = () => $("taskModal").classList.remove("hidden");
window.closeModal = () => $("taskModal").classList.add("hidden");
window.createTask = async () => {
  const kind = $("tkKind").value, expr = $("tkExpr").value.trim();
  const action = $("tkAction").value, payload = $("tkPayload").value.trim();
  if (!expr || !payload) return toast("Fill in schedule + action");
  let ap;
  if (action === "routine") ap = { name: payload.includes("morning") ? "morning" :
    payload.includes("evening") ? "evening_winddown" : "away_clean" };
  else ap = { prompt: payload };
  const body = { name: $("tkName").value || "Task", trigger_kind: kind,
    trigger_expr: kind === "interval" ? String(parseInt(expr) * 60) : expr,
    action_type: action, action_payload: ap };
  const r = await (await fetch("/api/jobs", { method: "POST",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })).json();
  if (r.ok) { toast("✦ Scheduled"); closeModal(); loadJobs(); liquidGlow("system", false); }
  else toast("Error: " + r.error);
};

/* ───────────────────────── analytics (SVG curves) ─────────────────────── */
async function loadAnalytics() {
  try {
    // seed demo data by syncing once if DB empty
    let h = await (await fetch("/api/analytics/health?days=90")).json();
    if (!Object.keys(h.series || {}).length) {
      await fetch("/api/tool/withings_sync", { method: "POST" });
      h = await (await fetch("/api/analytics/health?days=90")).json();
    }
    drawHealth(h);
    const en = await (await fetch("/api/analytics/energy")).json();
    drawEnergy(en);
  } catch (e) { console.warn(e); }
}
function smoothPath(pts) {
  if (pts.length < 2) return "";
  let d = `M ${pts[0][0]},${pts[0][1]}`;
  for (let i = 1; i < pts.length; i++) {
    const [x0, y0] = pts[i - 1], [x1, y1] = pts[i];
    const cx = (x0 + x1) / 2;
    d += ` C ${cx},${y0} ${cx},${y1} ${x1},${y1}`;
  }
  return d;
}
function scaleSeries(vals, W, H, pad = 14) {
  const min = Math.min(...vals), max = Math.max(...vals), rng = max - min || 1;
  return vals.map((v, i) => [pad + (i * (W - 2 * pad)) / (vals.length - 1 || 1),
                             H - pad - ((v - min) / rng) * (H - 2 * pad)]);
}
function drawHealth(data) {
  const svg = $("weightChart"); const W = 600, H = 240;
  let out = `<line class="grid-line" x1="0" y1="${H/2}" x2="${W}" y2="${H/2}"/>
             <line class="grid-line" x1="0" y1="${H/4}" x2="${W}" y2="${H/4}"/>
             <line class="grid-line" x1="0" y1="${3*H/4}" x2="${W}" y2="${3*H/4}"/>`;
  const defs = { weight: "weight", fat: "fat", heart_rate: "hr" };
  for (const [key, cls] of Object.entries(defs)) {
    const s = data.series[key]; if (!s || s.length < 2) continue;
    const pts = scaleSeries(s.map((p) => p[1]), W, H);
    out += `<path class="curve ${cls}" d="${smoothPath(pts)}"/>`;
    if (cls === "weight") out += `<path class="area" fill="url(#gBlue)" d="${smoothPath(pts)} L ${pts.at(-1)[0]},${H} L ${pts[0][0]},${H} Z"/>`;
  }
  svg.innerHTML = `<defs><linearGradient id="gBlue" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="#4da3ff"/><stop offset="1" stop-color="transparent"/></linearGradient></defs>` + out;
}
function drawEnergy(data) {
  const svg = $("energyChart"); const W = 600, H = 240;
  const pts = scaleSeries((data.history || []).map((p) => p[1]), W, H);
  if (pts.length > 1) {
    svg.innerHTML = `<path class="area" fill="url(#gTeal)" d="${smoothPath(pts)} L ${pts.at(-1)[0]},${H} L ${pts[0][0]},${H} Z"/>
      <path class="curve energy" d="${smoothPath(pts)}"/>`;
    $("energyNow").textContent = data.now_w + " W";
    const b = data.breakdown || {};
    $("energyBreak").textContent = `Hue ${b.hue_w}W · Vacuum ${b.vacuum_w}W · Pi ${b.pi_w}W`;
  }
  if (!$("energyChart").querySelector("#gTeal"))
    svg.insertAdjacentHTML("afterbegin",
      `<defs><linearGradient id="gTeal" x1="0" y1="0" x2="0" y2="1">
       <stop offset="0" stop-color="#2dd4bf"/><stop offset="1" stop-color="transparent"/></linearGradient></defs>`);
}

/* ───────────────────────── system tab ─────────────────────────────────── */
let sysLoaded = false, toolsLoaded = false;
function loadSystemOnce() { if (!sysLoaded) { fetch("/api/system").then((r) => r.json()).then(renderSystem); sysLoaded = true; } }
function loadToolsOnce() {
  if (toolsLoaded) return; toolsLoaded = true;
  fetch("/api/tools").then((r) => r.json()).then((ts) => {
    $("toolList").innerHTML = ts.map((t) =>
      `<span class="chip" title="${esc(t.description)}">${t.name} · ${t.domain}</span>`).join("");
  });
}
function renderSystem(s) {
  if (!s || s.cpu_percent === undefined) return;
  setRing("cpuRing", "cpuVal", s.cpu_percent, "%");
  setRing("ramRing", "ramVal", s.ram_percent, "%");
  $("ramDetail").textContent = `${s.ram_used_mb} / ${s.ram_total_mb} MB`;
  const temp = s.cpu_temp_c ?? 0;
  setRing("tempRing", "tempVal", Math.min(100, (temp / 85) * 100), "", Math.round(temp) + "°C");
  $("cpuCores").textContent = `${s.cores} cores`;
  $("netIp").textContent = s.lan_ip || "—";
  $("uptime").textContent = fmtUptime(s.uptime_s || 0);
  $("netIO").textContent = `↓ ${s.net_recv_mb} MB · ↑ ${s.net_sent_mb} MB`;
  $("disk").textContent = `${s.disk_used_gb} / ${s.disk_total_gb} GB`;
  $("powerW").textContent = (s.power_est_w ?? "—") + " W";
}
function setRing(ringId, valId, pct, suffix, labelOverride) {
  $(ringId).style.setProperty("--v", Math.round(pct || 0));
  $(valId).textContent = labelOverride || Math.round(pct || 0) + suffix;
}
async function refreshOllama() {
  try {
    const h = await (await fetch("/api/health")).json();
    const p = $("modelPill");
    p.className = "status-pill " + (h.ollama_reachable ? "pill-online" : "pill-offline");
    p.querySelector("span").textContent = h.model;
    $("ollamaState").textContent = h.ollama_reachable ? "online @ :11434" : "OFFLINE";
  } catch (e) { /* ignore */ }
}

/* ───────────────────────── utils ──────────────────────────────────────── */
function esc(s) { const d = document.createElement("div"); d.textContent = s ?? ""; return d.innerHTML; }
function fmt(iso) { try { return new Date(iso).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }); } catch { return iso; } }
function fmtUptime(sec) {
  const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60);
  return (d ? d + "d " : "") + h + "h " + m + "m";
}

/* keep Spotify progress ticking locally between WS pushes */
setInterval(() => {
  const d = state.devices.spotify;
  if (d?.is_playing && d.track?.duration_ms) {
    d.position_ms = ((d.position_ms || 0) + 1000) % d.track.duration_ms;
    $("spProgress").style.width = (100 * d.position_ms / d.track.duration_ms) + "%";
  }
}, 1000);

/* clear typing bubble defensively if a reply never arrives (LLM timeout) */
setTimeout(() => { while (pendingChats.length) { const q = pendingChats.shift(); q?.remove(); } }, 180000);

/* boot */
connectWS();
refreshOllama(); setInterval(refreshOllama, 30000);
loadJobs(); setInterval(loadJobs, 60000);
addMsg("bot", "Hi! I'm Atlas — your autonomous home agent. Ask me to control devices, schedule automations, or run a scenario. Tap ⚙ System for Pi vitals.");
