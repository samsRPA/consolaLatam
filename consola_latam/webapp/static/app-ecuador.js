/* Consola CEJ — cliente vanilla JS multi-módulo. Sin build step. */

const state = {
  clients: [],
  activeClient: null,
  scheduleBaseId: null,
  eventSource: null,
  notifFilter: "all",
  notifSelection: new Set(),
  charts: {},
};

/* ---------- helpers ---------- */
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];
const el = (tag, cls, html) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html !== undefined) n.innerHTML = html;
  return n;
};
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let msg = `Error ${res.status}`;
    try { const j = await res.json(); msg = j.detail || msg; } catch (_) {}
    throw new Error(msg);
  }
  return res.status === 204 ? null : res.json();
}
function form(obj) {
  const fd = new FormData();
  Object.entries(obj).forEach(([k, v]) => { if (v !== null && v !== undefined) fd.append(k, v); });
  return fd;
}
let toastTimer = null;
function toast(msg, ok = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (ok ? " is-ok" : "");
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), 4200);
}

/* ---------- manejo "inteligente" de errores ----------
   Nunca se muestra al usuario el texto crudo de una excepcion (rutas de archivo,
   WinError/Errno, "Failed to fetch", stacks, JSON de FastAPI, etc.): se traduce a un
   mensaje entendible segun el tipo de falla, o a uno generico si no se reconoce. El
   error real SI queda en la consola del navegador (F12) para depurar. */
const _FRIENDLY_ERROR_RULES = [
  [/winerror|no such file|enoent|no se pudo encontrar el archivo|not recognized as an internal|no inicio el worker/i,
    "No se pudo iniciar el motor de consultas en este equipo. Contacta a soporte técnico."],
  [/failed to fetch|networkerror|econnrefused|connection refused|no se pudo contactar|err_connection|err_internet/i,
    "No hay conexión con el servicio de consultas. Verifica tu conexión o que el servicio esté encendido, e intenta de nuevo."],
  [/timed? ?out|timeout|tiempo de espera/i,
    "La consulta tardó demasiado y se canceló. Intenta de nuevo en unos minutos."],
  [/radware|captcha|hcaptcha/i,
    "El portal judicial bloqueó la consulta temporalmente. Intenta de nuevo más tarde."],
  [/50[0-9]\b|bad gateway|gateway timeout|internal server error/i,
    "El servicio no respondió correctamente. Intenta de nuevo; si persiste, contacta soporte."],
  [/^error \d+$/i,
    "No se pudo completar la solicitud. Intenta de nuevo."],
  [/^(not found|forbidden|unauthorized|bad request|method not allowed|unprocessable entity|internal server error)$/i,
    "No se pudo completar la solicitud. Intenta de nuevo."],
];
const _GENERIC_ERROR_MSG = "Ocurrió un problema. Intenta de nuevo; si persiste, contacta soporte.";
function friendlyError(raw) {
  let msg;
  try { msg = typeof raw === "string" ? raw : JSON.stringify(raw); } catch (_) { msg = String(raw); }
  msg = (msg || "").trim();
  if (!msg) return _GENERIC_ERROR_MSG;
  for (const [re, friendly] of _FRIENDLY_ERROR_RULES) {
    if (re.test(msg)) return friendly;
  }
  // Mensajes propios del backend (HTTPException de nuestra API): cortos, en español,
  // sin tecnicismos -> se muestran tal cual. Cualquier otra cosa se asume tecnica.
  const looksTechnical = /[{}[\]]|traceback|exception|stack|0x[0-9a-f]{4,}|\.py"?,? line|\.(js|cjs):\d+|errno|\berr_|^\[object/i.test(msg) || msg.length > 180;
  return looksTechnical ? _GENERIC_ERROR_MSG : msg;
}
function showErrorPopup(raw) {
  console.error("[Consola CJ]", raw);
  $("#errorPopupMsg").textContent = friendlyError(raw);
  $("#errorPopup").hidden = false;
}
function closeErrorPopup() { $("#errorPopup").hidden = true; }
$("#errorPopupClose").addEventListener("click", closeErrorPopup);
$("#errorPopupOk").addEventListener("click", closeErrorPopup);
$("#errorPopup").addEventListener("click", (e) => { if (e.target === $("#errorPopup")) closeErrorPopup(); });

let _confirmPopupCallback = null;
let _confirmPopupCancelCallback = null;
function showConfirmPopup(message, onConfirm, opts = {}) {
  $("#confirmPopupTitle").textContent = opts.title || "⚠ Confirmar acción";
  $("#confirmPopupMsg").textContent = message;
  $("#confirmPopupOk").textContent = opts.okLabel || "Sí, eliminar";
  _confirmPopupCallback = onConfirm;
  _confirmPopupCancelCallback = opts.onCancel || null;
  $("#confirmPopup").hidden = false;
}
function closeConfirmPopup() {
  $("#confirmPopup").hidden = true;
  const cb = _confirmPopupCancelCallback;
  _confirmPopupCallback = null;
  _confirmPopupCancelCallback = null;
  if (cb) cb();
}
$("#confirmPopupClose").addEventListener("click", closeConfirmPopup);
$("#confirmPopupCancel").addEventListener("click", closeConfirmPopup);
$("#confirmPopupOk").addEventListener("click", () => {
  const cb = _confirmPopupCallback;
  $("#confirmPopup").hidden = true;
  _confirmPopupCallback = null;
  _confirmPopupCancelCallback = null;
  if (cb) cb();
});
$("#confirmPopup").addEventListener("click", (e) => { if (e.target === $("#confirmPopup")) closeConfirmPopup(); });

/* Paleta azul judicial para los gráficos (sede Ecuador). */
const CHART_PALETTE = ["#13398a", "#1e56c4", "#5f8ee0", "#0f2f73", "#a8c4f0", "#8a8a8a"];
const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#666";

/* ---------- tema claro/oscuro ---------- */
function paintThemeToggle(theme) {
  const btn = $("#themeToggle");
  if (!btn) return;
  const label = $("#themeToggleLabel");
  if (label) label.textContent = theme === "dark" ? "Modo claro" : "Modo oscuro";
  const moon = btn.querySelector(".icon-moon");
  const sun = btn.querySelector(".icon-sun");
  // El atributo/propiedad `hidden` no se refleja de forma fiable en <svg> en todos los
  // navegadores; se alterna con `display` directamente para garantizar el cambio visual.
  if (moon) moon.style.display = theme === "dark" ? "none" : "";
  if (sun) sun.style.display = theme === "dark" ? "" : "none";
}
function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  localStorage.setItem("cej-theme", theme);
  paintThemeToggle(theme);
}
(function initTheme() {
  // Modo claro por defecto siempre (a menos que el usuario haya elegido oscuro antes).
  const saved = localStorage.getItem("cej-theme") || "light";
  document.documentElement.setAttribute("data-theme", saved);
})();
// app.js va al final del body: los elementos ya existen, se cablea directo (sin esperar
// DOMContentLoaded, que ya pudo haber disparado).
(function wireThemeToggle() {
  const btn = $("#themeToggle");
  if (!btn) return;
  paintThemeToggle(document.documentElement.getAttribute("data-theme"));
  btn.addEventListener("click", () => {
    const next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
    applyTheme(next);
    if ($("#module-dashboard").classList.contains("is-active")) loadDashboard();
  });
})();

/* ---------- segmented controls (delegado) ---------- */
document.addEventListener("click", (e) => {
  const btn = e.target.closest(".segmented button[data-val]");
  if (!btn) return;
  const seg = btn.closest(".segmented");
  seg.querySelectorAll("button").forEach((b) => b.classList.remove("is-active"));
  btn.classList.add("is-active");
  seg.dataset.value = btn.dataset.val;
});

/* ---------- navegación de módulos ---------- */
$("#sidebarNav").addEventListener("click", (e) => {
  const item = e.target.closest(".navitem");
  if (!item) return;
  showModule(item.dataset.module);
});
function showModule(name) {
  $$(".navitem").forEach((n) => n.classList.toggle("is-active", n.dataset.module === name));
  $$(".module").forEach((m) => m.classList.toggle("is-active", m.id === `module-${name}`));
  if (name === "dashboard") loadDashboard();
  if (name === "clientes") loadClients();
  if (name === "procesos") loadProcesses();
  if (name === "notificaciones") loadNotifications();
  if (name === "inclusiones") fillClientSelect("#incClient");
  if (name === "gestion") loadGestion();
}

/* ============================================================
   DASHBOARD
   ============================================================ */
async function loadDashboard() {
  let d;
  try { d = await api("/api/ecuador/dashboard"); } catch (err) { return showErrorPopup(err.message); }
  const stats = [
    { label: "Clientes", value: d.total_clients, accent: "forest" },
    { label: "Procesos", value: d.total_processes, accent: "green" },
    { label: "Actuaciones", value: d.total_actuaciones, accent: "olive" },
    { label: "Requieren manual", value: d.notifications.manual_pendiente, accent: "danger" },
    { label: "Notif. sin leer", value: d.notifications.no_leidas, accent: "rock" },
    { label: "Con movimiento", value: d.notifications.con_movimiento, accent: "green" },
  ];
  $("#statGrid").innerHTML = stats.map((s) => `
    <div class="statcard statcard--${s.accent}">
      <span class="statcard__value">${s.value}</span>
      <span class="statcard__label">${s.label}</span>
    </div>`).join("");
  drawDoughnut("chartPortfolio", { "Al día": d.procesos_al_dia, "Requieren manual": d.procesos_manual });
  drawDoughnut("chartNotif", d.notifications_breakdown);
  drawDoughnut("chartType", d.clients_by_type);
  drawDoughnut("chartImportance", d.clients_by_importance);
  drawLine("chartActivity", d.activity_by_day);
  drawBarH("chartTopActive", d.top_active_processes.map((x) => ({ name: x.radicado, count: x.count })));
  drawBar("chartByClient", d.processes_by_client);
}
function drawLine(id, arr) {
  destroyChart(id);
  const ctx = document.getElementById(id);
  if (!ctx) return;
  const tick = cssVar("--rock");
  state.charts[id] = new Chart(ctx, {
    type: "line",
    data: {
      labels: arr.map((x) => (x.day || "").slice(5)),
      datasets: [
        { label: "Consultas", data: arr.map((x) => x.total), borderColor: cssVar("--forest"), backgroundColor: "rgba(19,57,138,.12)", fill: true, tension: .35, pointRadius: 3 },
        { label: "Con movimiento", data: arr.map((x) => x.con_mov), borderColor: cssVar("--olive"), backgroundColor: "transparent", tension: .35, pointRadius: 3 },
      ],
    },
    options: { plugins: { legend: { position: "bottom", labels: { color: tick, font: { family: "Manrope" } } } }, scales: { y: { beginAtZero: true, ticks: { precision: 0, color: tick } }, x: { ticks: { color: tick } } } },
  });
}
function drawBarH(id, arr) {
  destroyChart(id);
  const ctx = document.getElementById(id);
  if (!ctx) return;
  const tick = cssVar("--rock");
  state.charts[id] = new Chart(ctx, {
    type: "bar",
    data: { labels: arr.map((x) => x.name), datasets: [{ data: arr.map((x) => x.count), backgroundColor: cssVar("--olive"), borderRadius: 5 }] },
    options: { indexAxis: "y", plugins: { legend: { display: false } }, scales: { x: { beginAtZero: true, ticks: { precision: 0, color: tick } }, y: { ticks: { color: tick, font: { size: 10 } } } } },
  });
}

/* ============================================================
   GESTIÓN (calendario)
   ============================================================ */
const MONTHS_ES = ["Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"];
const WEEKDAYS_ES = ["Lun", "Mar", "Mie", "Jue", "Vie", "Sab", "Dom"];
state.gestion = { year: null, month: null, selectedDate: null }; // month 1-12
function parseLocalDate(dateStr) {
  const [y, m, d] = String(dateStr).split("-").map(Number);
  return new Date(y, (m || 1) - 1, d || 1);
}
function isoLocal(date) {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
}
function addDays(date, days) {
  const d = new Date(date);
  d.setDate(d.getDate() + days);
  return d;
}
function weekStart(date) {
  const d = new Date(date);
  d.setHours(0, 0, 0, 0);
  d.setDate(d.getDate() - ((d.getDay() + 6) % 7));
  return d;
}
async function loadGestion() {
  if (state.gestion.year === null) {
    const now = new Date();
    state.gestion.year = now.getFullYear();
    state.gestion.month = now.getMonth() + 1;
  }
  await renderCalendar();
}
async function renderCalendar() {
  const { year, month } = state.gestion;
  $("#calMonthLabel").textContent = `${MONTHS_ES[month - 1]} ${year}`;
  let data;
  try { data = await api(`/api/ecuador/gestion/month?year=${year}&month=${month}`); } catch (err) { return showErrorPopup(err.message); }
  const days = data.days || {};
  const grid = $("#calGrid");
  grid.innerHTML = "";
  const first = new Date(year, month - 1, 1);
  const startWeekday = (first.getDay() + 6) % 7; // lunes=0
  const daysInMonth = new Date(year, month, 0).getDate();
  const todayStr = new Date().toISOString().slice(0, 10);
  for (let i = 0; i < startWeekday; i++) grid.appendChild(el("div", "calcell calcell--empty"));
  for (let d = 1; d <= daysInMonth; d++) {
    const dateStr = `${year}-${String(month).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
    const act = days[dateStr];
    const cell = el("div", "calcell" + (dateStr === todayStr ? " is-today" : "") + (act ? " has-activity" : ""));
    const dots = act ? `<div class="calcell__dots">
      ${act.consultas ? '<i class="dot dot--consulta"></i>' : ""}
      ${act.movimiento ? '<i class="dot dot--mov"></i>' : ""}
      ${act.manual ? '<i class="dot dot--manual"></i>' : ""}
      ${act.recordatorios ? '<i class="dot dot--rem"></i>' : ""}</div>` : "";
    cell.innerHTML = `<span class="calcell__num">${d}</span>${dots}`;
    cell.addEventListener("click", () => selectDay(dateStr, cell));
    grid.appendChild(cell);
  }
  // auto-selecciona hoy si está en el mes visible
  const todayCell = [...grid.children].find((c) => c.querySelector(".calcell__num")?.textContent == new Date().getDate() && `${year}-${String(month).padStart(2,"0")}` === todayStr.slice(0,7));
  if (todayCell) selectDay(todayStr, todayCell);
  else {
    const firstVisible = `${year}-${String(month).padStart(2, "0")}-01`;
    state.gestion.selectedDate = firstVisible;
    $("#dayPanel").innerHTML = `<p class="muted">Selecciona un día en el calendario para ver su detalle.</p>`;
    renderWeekAgenda(firstVisible);
  }
}
async function selectDay(dateStr, cell) {
  state.gestion.selectedDate = dateStr;
  $$(".calcell.is-selected").forEach((c) => c.classList.remove("is-selected"));
  if (cell) cell.classList.add("is-selected");
  renderWeekAgenda(dateStr);
  const panel = $("#dayPanel");
  panel.innerHTML = `<p class="muted">Cargando…</p>`;
  let d;
  try { d = await api(`/api/ecuador/gestion/day?date=${dateStr}`); } catch (err) { showErrorPopup(err.message); panel.innerHTML = `<p class="muted">No se pudo cargar el día.</p>`; return; }
  const k = d.notif_by_kind || {};
  const [yy, mm, dd] = dateStr.split("-");
  const consultasHtml = d.consultas.length
    ? d.consultas.map((c) => `<div class="dayrow"><span class="dayrow__scope">${esc((c.scope || "").toUpperCase())}</span><span>${c.veces} consulta(s) · ${c.total} proc · <b>${c.con_mov}</b> con mov · ${c.sin_mov} sin · ${c.errores} err</span></div>`).join("")
    : `<p class="muted">Sin consultas este día.</p>`;
  const notifsHtml = d.notifications.length
    ? d.notifications.map((n) => `<div class="daynotif daynotif--${esc(n.kind)}" data-pid="${n.process_id || ""}"><b>${esc(n.title)}</b><span>${esc(n.body || "")}</span></div>`).join("")
    : `<p class="muted">Sin notificaciones este día.</p>`;
  panel.innerHTML = `
    <div class="daypanel__head">
      <h3>${dd} de ${MONTHS_ES[Number(mm) - 1]} ${yy}</h3>
      <div class="daykpis">
        <div class="daykpi"><b>${d.total_consultas}</b><span>Consultas</span></div>
        <div class="daykpi"><b>${k.movimiento || 0}</b><span>Movimientos</span></div>
        <div class="daykpi"><b>${k.sin_movimiento || 0}</b><span>Sin movimientos</span></div>
        <div class="daykpi daykpi--ok"><b>${d.manual_resueltas}</b><span>Manuales resueltas</span></div>
        <div class="daykpi daykpi--pend"><b>${d.manual_pendientes}</b><span>Manuales pendientes</span></div>
      </div>
      <div class="daystatus ${d.manual_pendientes === 0 ? "daystatus--ok" : "daystatus--pend"}">
        ${d.manual_pendientes === 0 ? "✓ Al día en cargas manuales" : `⚠ ${d.manual_pendientes} carga(s) manual(es) pendiente(s)`}
      </div>
    </div>
    <h4 class="ledger">Tareas y recordatorios</h4>
    <div class="reminders" id="dayReminders"></div>
    <form class="reminder-form" id="reminderForm">
      <div class="reminder-form__row">
        <input class="field" id="remTitle" placeholder="Nueva tarea / recordatorio / anotación" autocomplete="off" />
        <input class="field field--time" id="remTime" type="time" />
      </div>
      <div class="reminder-form__row">
        <div class="segmented" id="remKind" data-value="tarea">
          <button type="button" data-val="tarea" class="is-active">Tarea</button>
          <button type="button" data-val="recordatorio">Recordatorio</button>
          <button type="button" data-val="anotacion">Anotación</button>
        </div>
        <select class="field field--select" id="remClient"><option value="">Sin cliente</option></select>
      </div>
      <textarea class="field" id="remNotes" rows="2" placeholder="Notas (opcional)"></textarea>
      <button type="submit" class="btn btn--solid btn--sm">Agregar para el ${dd}/${mm}</button>
    </form>
    <h4 class="ledger">Consultas realizadas</h4>
    <div class="dayconsultas">${consultasHtml}</div>
    <h4 class="ledger">Notificaciones del día</h4>
    <div class="daynotifs">${notifsHtml}</div>`;
  panel.querySelectorAll(".daynotif[data-pid]").forEach((n) => {
    const pid = n.dataset.pid;
    if (pid) { n.classList.add("is-clickable"); n.addEventListener("click", () => openProcessFromNotif(Number(pid))); }
  });
  fillClientSelect("#remClient");
  renderReminders(dateStr, d.reminders || []);
  $("#reminderForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const title = $("#remTitle").value.trim();
    if (!title) return toast("Escribe el título de la tarea");
    try {
      await api("/api/ecuador/reminders", { method: "POST", body: form({
        title, due_date: dateStr, due_time: $("#remTime").value || "",
        kind: $("#remKind").dataset.value, notes: $("#remNotes").value,
        client_id: $("#remClient").value || null,
      }) });
      toast("Recordatorio agregado", true);
      selectDay(dateStr, $(".calcell.is-selected")); // recarga el panel
      renderCalendar(); // refresca los puntos del calendario
    } catch (err) { showErrorPopup(err.message); }
  });
}
async function renderWeekAgenda(anchorDateStr = state.gestion.selectedDate) {
  const list = $("#weekAgendaList");
  const range = $("#weekAgendaRange");
  if (!list || !range) return;
  const anchor = anchorDateStr ? parseLocalDate(anchorDateStr) : new Date();
  const start = weekStart(anchor);
  const dates = Array.from({ length: 7 }, (_, idx) => addDays(start, idx));
  const dateKeys = dates.map(isoLocal);
  range.textContent = `${dateKeys[0].slice(8, 10)}/${dateKeys[0].slice(5, 7)} - ${dateKeys[6].slice(8, 10)}/${dateKeys[6].slice(5, 7)}`;
  list.innerHTML = `<p class="muted">Cargando semana...</p>`;
  let data;
  try {
    data = await Promise.all(dateKeys.map((date) => api(`/api/ecuador/reminders?date=${date}`)));
  } catch (err) {
    showErrorPopup(err.message);
    list.innerHTML = `<p class="muted">No se pudo cargar la semana.</p>`;
    return;
  }
  const total = data.reduce((sum, items) => sum + items.length, 0);
  if (!total) {
    list.innerHTML = `<p class="muted">Sin audiencias ni recordatorios esta semana.</p>`;
    return;
  }
  list.innerHTML = "";
  data.forEach((items, idx) => {
    const date = dateKeys[idx];
    const day = el("div", "weekagenda__day");
    day.innerHTML = `<div class="weekagenda__date"><b>${WEEKDAYS_ES[idx]}</b><span>${date.slice(8, 10)}/${date.slice(5, 7)}</span></div>`;
    const body = el("div", "weekagenda__items");
    if (!items.length) {
      body.innerHTML = `<span class="weekagenda__empty">Sin agenda</span>`;
    } else {
      items.forEach((r) => {
        const item = el("button", "weekagenda__item weekagenda__item--" + esc(r.kind));
        item.type = "button";
        item.innerHTML = `
          <span>${r.due_time ? esc(r.due_time) + " · " : ""}${esc(r.title)}</span>
          <small>${esc([r.client_name, r.process_radicado, r.kind === "audiencia" ? countdownLabel(r.due_date) : r.kind].filter(Boolean).join(" · "))}</small>`;
        item.addEventListener("click", () => {
          const isVisibleMonth = date.slice(0, 7) === `${state.gestion.year}-${String(state.gestion.month).padStart(2, "0")}`;
          const cell = isVisibleMonth
            ? [...$("#calGrid").children].find((c) => c.querySelector(".calcell__num")?.textContent == Number(date.slice(8, 10)))
            : null;
          selectDay(date, cell);
        });
        body.appendChild(item);
      });
    }
    day.appendChild(body);
    list.appendChild(day);
  });
}
function renderReminders(dateStr, reminders) {
  const wrap = $("#dayReminders");
  if (!wrap) return;
  if (!reminders.length) { wrap.innerHTML = `<p class="muted">Sin tareas para este día. Agrega una abajo.</p>`; return; }
  wrap.innerHTML = "";
  reminders.forEach((r) => {
    const row = el("div", "reminder reminder--" + esc(r.kind) + (r.done ? " is-done" : ""));
    row.innerHTML = `
      <input type="checkbox" class="reminder__check" ${r.done ? "checked" : ""} />
      <div class="reminder__body">
        <div class="reminder__title">${r.due_time ? `<span class="reminder__time">${esc(r.due_time)}</span>` : ""}${esc(r.title)}${r.kind === "audiencia" ? `<span class="reminder__time">${esc(countdownLabel(r.due_date))}</span>` : ""}<span class="reminder__kind">${esc(r.kind)}</span></div>
        ${r.notes ? `<div class="reminder__notes">${esc(r.notes)}</div>` : ""}
        ${r.client_name || r.process_radicado ? `<div class="reminder__client">${esc([r.client_name, r.process_radicado].filter(Boolean).join(" · "))}</div>` : ""}
      </div>
      <button class="reminder__del" title="Eliminar">✕</button>`;
    row.querySelector(".reminder__check").addEventListener("change", async (e) => {
      await api(`/api/ecuador/reminders/${r.id}/done`, { method: "PATCH", body: form({ done: e.target.checked }) });
      selectDay(dateStr, $(".calcell.is-selected"));
    });
    row.querySelector(".reminder__del").addEventListener("click", async () => {
      await api(`/api/ecuador/reminders/${r.id}`, { method: "DELETE" });
      selectDay(dateStr, $(".calcell.is-selected"));
      renderCalendar();
    });
    wrap.appendChild(row);
  });
}
function countdownLabel(dateStr) {
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const due = new Date(`${dateStr}T00:00:00`);
  const diff = Math.round((due - today) / 86400000);
  if (diff === 0) return "Audiencia hoy";
  if (diff === 1) return "Audiencia en 1 dia";
  if (diff > 1) return `Audiencia en ${diff} dias`;
  return `Audiencia hace ${Math.abs(diff)} dia(s)`;
}
$("#calPrev").addEventListener("click", () => { state.gestion.month--; if (state.gestion.month < 1) { state.gestion.month = 12; state.gestion.year--; } renderCalendar(); });
$("#calNext").addEventListener("click", () => { state.gestion.month++; if (state.gestion.month > 12) { state.gestion.month = 1; state.gestion.year++; } renderCalendar(); });
$("#backupImport").addEventListener("click", () => $("#backupImportFile").click());
$("#backupImportFile").addEventListener("change", () => {
  const input = $("#backupImportFile");
  if (!input.files.length) return;
  showConfirmPopup(
    "Importar un respaldo reemplaza la memoria actual del sistema. Se guardara una copia de seguridad antes de importar. ¿Continuar?",
    async () => {
      try {
        await api("/api/ecuador/backup/import", { method: "POST", body: form({ file: input.files[0] }) });
        input.value = "";
        state.activeClient = null;
        await loadClients();
        loadDashboard();
        toast("Respaldo importado", true);
      } catch (err) { showErrorPopup(err.message); }
    },
    { okLabel: "Sí, importar", onCancel: () => { input.value = ""; } },
  );
});
function destroyChart(id) { if (state.charts[id]) { state.charts[id].destroy(); delete state.charts[id]; } }
function drawDoughnut(id, obj) {
  destroyChart(id);
  const labels = Object.keys(obj), data = Object.values(obj);
  const ctx = document.getElementById(id);
  if (!ctx) return;
  if (!labels.length) { ctx.parentElement.querySelector(".chart-empty")?.remove(); return; }
  const tick = cssVar("--rock");
  state.charts[id] = new Chart(ctx, {
    type: "doughnut",
    data: { labels, datasets: [{ data, backgroundColor: CHART_PALETTE, borderWidth: 2, borderColor: cssVar("--white") }] },
    options: { plugins: { legend: { position: "bottom", labels: { font: { family: "Manrope" }, color: tick } } }, cutout: "62%" },
  });
}
function drawBar(id, arr) {
  destroyChart(id);
  const ctx = document.getElementById(id);
  if (!ctx) return;
  const tick = cssVar("--rock");
  state.charts[id] = new Chart(ctx, {
    type: "bar",
    data: { labels: arr.map((x) => x.name), datasets: [{ data: arr.map((x) => x.count), backgroundColor: cssVar("--forest"), borderRadius: 6 }] },
    options: { plugins: { legend: { display: false } }, scales: { y: { beginAtZero: true, ticks: { precision: 0, color: tick } }, x: { ticks: { color: tick, font: { family: "Manrope" } } } } },
  });
}
$("#refreshDashboard").addEventListener("click", loadDashboard);

/* ============================================================
   CLIENTES
   ============================================================ */
async function loadClients() {
  state.clients = await api("/api/ecuador/clients");
  renderClients();
  fillClientSelect("#procClientFilter", true);
  fillClientSelect("#incClient");
}
function renderClients() {
  const list = $("#clientList");
  list.innerHTML = "";
  state.clients.forEach((c, i) => {
    const card = el("li", "clientcard" + (state.activeClient?.id === c.id ? " is-active" : ""));
    card.style.animationDelay = `${i * 40}ms`;
    card.innerHTML = `
      <div class="clientcard__row">
        <div class="clientcard__name">${esc(c.name)}</div>
        <span class="chip" data-imp="${esc(c.importance)}">${esc(c.importance)}</span>
      </div>
      <div class="clientcard__meta">${c.client_type ? esc(c.client_type) + " · " : ""}${c.process_count} proceso${c.process_count === 1 ? "" : "s"}</div>`;
    card.addEventListener("click", () => selectClient(c.id));
    list.appendChild(card);
  });
}
function fillClientSelect(sel, withAll) {
  const node = $(sel);
  if (!node) return;
  const current = node.value;
  const opts = (withAll ? '<option value="">Todos los clientes</option>' : '<option value="">Sin asignar</option>')
    + state.clients.map((c) => `<option value="${c.id}">${esc(c.name)}</option>`).join("");
  node.innerHTML = opts;
  node.value = current;
}

$("#newClientBtn").addEventListener("click", () => { $("#clientForm").hidden = !$("#clientForm").hidden; });
$("#clientCancel").addEventListener("click", () => ($("#clientForm").hidden = true));
$("#clientForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const name = $("#clientName").value.trim();
  if (!name) return toast("El nombre es obligatorio");
  try {
    await api("/api/ecuador/clients", { method: "POST", body: form({
      name, description: $("#clientDesc").value,
      importance: $("#clientImportance").dataset.value,
      client_type: $("#clientType").dataset.value,
    }) });
    $("#clientName").value = ""; $("#clientDesc").value = "";
    $("#clientForm").hidden = true;
    await loadClients();
    toast("Cliente creado", true);
  } catch (err) { showErrorPopup(err.message); }
});

async function selectClient(id) {
  state.activeClient = state.clients.find((c) => c.id === id) || null;
  if (!state.activeClient) return;
  renderClients();
  $("#clientEmpty").hidden = true;
  $("#clientWorkspace").hidden = false;
  await renderClientWorkspace();
}

async function renderClientWorkspace() {
  const c = state.activeClient;
  const ws = $("#clientWorkspace");
  ws.innerHTML = `
    <div class="client-header">
      <div>
        <span class="chip" data-imp="${esc(c.importance)}">${esc(c.importance)}</span>
        ${c.client_type ? `<span class="chip chip--type">${esc(c.client_type)}</span>` : ""}
        <h2 class="client-header__name">${esc(c.name)}</h2>
        <p class="client-header__desc">${esc(c.description || "")}</p>
      </div>
      <div class="module__head-actions">
        <a class="btn btn--gold btn--sm" href="/api/ecuador/clients/${c.id}/report.pdf">Informe PDF</a>
        <button class="btn btn--danger-ghost btn--sm" id="deleteClientBtn">Eliminar cliente</button>
      </div>
    </div>
    <div class="dropzone" id="dropzone">
      <input type="file" id="fileInput" accept=".xlsx,.xlsm" hidden />
      <div class="dropzone__inner">
        <span class="dropzone__icon">
          <svg viewBox="0 0 24 24" fill="none"><path d="M12 16V4M12 4L7 9M12 4L17 9" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/><path d="M4 16v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>
        </span>
        <p><strong>Carga masiva:</strong> arrastra el Excel con todos los procesos</p>
        <small>Detecta radicado automáticamente</small>
      </div>
    </div>
    <h3 class="ledger ledger--section">Bases del cliente</h3>
    <div class="baselist" id="baseList"></div>
    <h3 class="ledger ledger--section">Historial de consultas</h3>
    <div class="historylist" id="historyList"></div>`;
  wireDropzone();
  $("#deleteClientBtn").addEventListener("click", deleteActiveClient);
  await loadBases();
  await loadHistory();
}

function deleteActiveClient() {
  showConfirmPopup(`¿Eliminar el cliente "${state.activeClient.name}" y todas sus bases? Esta acción no se puede deshacer.`, async () => {
    try {
      await api(`/api/ecuador/clients/${state.activeClient.id}`, { method: "DELETE" });
      state.activeClient = null;
      $("#clientWorkspace").hidden = true;
      $("#clientEmpty").hidden = false;
      await loadClients();
      toast("Cliente eliminado", true);
    } catch (err) { showErrorPopup(err.message); }
  });
}

async function loadHistory() {
  const list = await api(`/api/ecuador/clients/${state.activeClient.id}/history`);
  const wrap = $("#historyList");
  if (!list.length) { wrap.innerHTML = `<p class="muted">Aún no hay consultas registradas.</p>`; return; }
  wrap.innerHTML = list.map((h) => `
    <div class="historyrow">
      <span class="historyrow__scope">${esc((h.scope || "").toUpperCase())}${h.mode ? " · " + esc(h.mode.toUpperCase()) : ""}</span>
      <span class="historyrow__counts">${h.total} proc · <b>${h.con_mov}</b> con mov · ${h.sin_mov} sin · ${h.errores} err</span>
      <span class="historyrow__date">${esc((h.created_at || "").replace("T", " "))}</span>
    </div>`).join("");
}

/* ---------- bases (dentro de cliente) ---------- */
async function loadBases() {
  const bases = await api(`/api/ecuador/clients/${state.activeClient.id}/bases`);
  renderBases(bases);
}
function mapTag(m) {
  const parts = [`<span class="tag">RADICADO · <b>col ${m.radicado_col + 1}</b></span>`];
  parts.push(m.demandante_col != null ? `<span class="tag">DEMANDANTE · <b>col ${m.demandante_col + 1}</b></span>` : `<span class="tag tag--warn">sin DEMANDANTE</span>`);
  parts.push(m.demandado_col != null ? `<span class="tag">DEMANDADO · <b>col ${m.demandado_col + 1}</b></span>` : `<span class="tag tag--warn">sin DEMANDADO</span>`);
  return parts.join("");
}
function renderBases(bases) {
  const wrap = $("#baseList");
  wrap.innerHTML = "";
  if (!bases.length) { wrap.innerHTML = `<p class="muted">Aún no hay bases. Carga un Excel arriba.</p>`; return; }
  bases.forEach((b) => {
    const card = el("div", "basecard");
    let lastRun = "";
    if (b.last_run) {
      const cls = { done: "", error: "err", cancelled: "cancelled" }[b.last_run.status] || "";
      lastRun = `<span class="basecard__lastrun">última: <b class="${cls}">${b.last_run.status === "done" ? "OK" : b.last_run.status.toUpperCase()}</b></span>`;
    }
    card.innerHTML = `
      <div class="basecard__top">
        <div><h4 class="basecard__name">${esc(b.name)}</h4><span class="basecard__count">${b.row_count} radicados</span></div>
        <button class="btn btn--danger-ghost btn--sm" data-del="${b.id}">Eliminar</button>
      </div>
      <div class="basecard__map">${mapTag(b.mapping)}</div>
      <div class="basecard__actions">
        <button class="btn btn--solid btn--sm" data-run="total" data-base="${b.id}">Consultar TOTAL</button>
        <button class="btn btn--ghost btn--sm" data-run="daily" data-base="${b.id}">Consultar DIARIA</button>
        <button class="btn btn--ghost btn--sm" data-schedule="${b.id}"><svg viewBox="0 0 24 24" fill="none" width="15" height="15"><circle cx="12" cy="12" r="8.5" stroke="currentColor" stroke-width="1.6"/><path d="M12 7.5V12L15 14" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg> Programar</button>
        ${lastRun}
      </div>`;
    card.querySelector("[data-del]").addEventListener("click", () => deleteBase(b.id, b.name));
    card.querySelectorAll("[data-run]").forEach((btn) => btn.addEventListener("click", () => startRun(b, btn.dataset.run)));
    card.querySelector("[data-schedule]").addEventListener("click", () => openSchedule(b));
    wrap.appendChild(card);
  });
}
function deleteBase(id, name) {
  showConfirmPopup(`¿Eliminar la base "${name}"? Esta acción no se puede deshacer.`, async () => {
    try {
      await api(`/api/ecuador/bases/${id}`, { method: "DELETE" });
      await loadBases(); await loadClients();
      toast("Base eliminada", true);
    } catch (err) { showErrorPopup(err.message); }
  });
}

function wireDropzone() {
  const dropzone = $("#dropzone"), fileInput = $("#fileInput");
  dropzone.addEventListener("click", () => fileInput.click());
  dropzone.addEventListener("dragover", (e) => { e.preventDefault(); dropzone.classList.add("is-drag"); });
  dropzone.addEventListener("dragleave", () => dropzone.classList.remove("is-drag"));
  dropzone.addEventListener("drop", (e) => { e.preventDefault(); dropzone.classList.remove("is-drag"); if (e.dataTransfer.files.length) uploadFile(e.dataTransfer.files[0]); });
  fileInput.addEventListener("change", () => { if (fileInput.files.length) uploadFile(fileInput.files[0]); });
}
async function uploadFile(file) {
  if (!/\.xlsx?$/i.test(file.name)) return toast("Debe ser un archivo Excel (.xlsx)");
  toast("Analizando base y detectando columnas…", true);
  try {
    const base = await api(`/api/ecuador/clients/${state.activeClient.id}/bases`, { method: "POST", body: form({ name: file.name.replace(/\.[^.]+$/, ""), file }) });
    await loadBases(); await loadClients();
    toast(`Base cargada: ${base.row_count} radicados`, true);
  } catch (err) { showErrorPopup(err.message); }
}

/* ============================================================
   MIS PROCESOS
   ============================================================ */
let procSearchTimer = null;
function showProcGrid() { $("#procDetail").hidden = true; $("#procList").hidden = false; $("#procToolbar").hidden = false; }
async function loadProcesses() {
  showProcGrid();
  const search = $("#procSearch").value.trim();
  const clientId = $("#procClientFilter").value;
  const qs = new URLSearchParams();
  if (search) qs.set("search", search);
  if (clientId) qs.set("client_id", clientId);
  let procs;
  try { procs = await api(`/api/ecuador/processes?${qs}`); } catch (err) { return showErrorPopup(err.message); }
  $("#procesosCount").textContent = `${procs.length} proceso${procs.length === 1 ? "" : "s"} en el repositorio`;
  const wrap = $("#procList");
  wrap.innerHTML = "";
  if (!procs.length) { wrap.innerHTML = `<p class="muted">No hay procesos. Carga una base o usa Inclusiones.</p>`; return; }
  procs.forEach((p, i) => wrap.appendChild(buildProcessCard(p, i)));
}
function buildProcessCard(p, i) {
  const card = el("div", "proccard proccard--clickable");
  card.style.animationDelay = `${i * 30}ms`;
  const manual = p.needs_manual ? `<span class="alert-manual">⚠ EXTRAER DOCUMENTO MANUALMENTE</span>` : "";
  card.innerHTML = `
    <div class="proccard__head">
      <div class="proccard__id">
        <span class="proccard__radicado">${esc(p.radicado)}</span>
        ${p.client_name ? `<span class="proccard__client">${esc(p.client_name)}</span>` : ""}
      </div>
      <div class="proccard__actions">
        <span class="proccard__enter">Entrar al proceso →</span>
      </div>
    </div>
    <div class="proccard__parties">
      <span><b>Materia:</b> ${esc(p.materia || "—")}</span>
      <span><b>Tipo:</b> ${esc(p.estado || "—")}</span>
    </div>
    ${manual}`;
  card.addEventListener("click", () => enterProcess(p.id));
  return card;
}
async function enterProcess(processId) {
  $("#procList").hidden = true; $("#procToolbar").hidden = true;
  const detail = $("#procDetail");
  detail.hidden = false;
  detail.innerHTML = `<p class="muted">Entrando al proceso…</p>`;
  let p;
  try { p = await api(`/api/ecuador/processes/${processId}`); } catch (err) { showErrorPopup(err.message); detail.innerHTML = `<p class="muted">No se pudo cargar el proceso.</p>`; return; }
  await api(`/api/ecuador/processes/${processId}/seen`, { method: "POST" }); // limpia marcas "nuevo"
  renderProcessDetail(p);
}
function renderProcessDetail(p) {
  const detail = $("#procDetail");
  const manual = p.needs_manual ? `<div class="alert-manual alert-manual--big">⚠ EXTRAER DOCUMENTO MANUALMENTE</div>` : "";
  const reporte = (p.detail && p.detail.reporte) || {};
  const extra = ["Delito/Asunto", "Ciudad", "Fecha de Ingreso"]
    .filter((k) => reporte[k])
    .map((k) => `<span><b>${esc(k)}:</b> ${esc(reporte[k])}</span>`)
    .join("");
  const expedientes = (p.detail && p.detail.expedientes) || [];
  const expedientesHtml = expedientes.length ? `
    <h3 class="ledger ledger--section">Expedientes / Judicaturas (${expedientes.length})</h3>
    <div class="expgrid">
      ${expedientes.map((e) => `
        <div class="expcard">
          <div class="expcard__id">ID Judicatura: ${esc(e.idJudicatura || "—")}</div>
          ${e.nombreJudicatura ? `<div class="expcard__row"><b>Judicatura</b>${esc(e.nombreJudicatura)}</div>` : ""}
          ${e.ciudad ? `<div class="expcard__row"><b>Ciudad</b>${esc(e.ciudad)}</div>` : ""}
          <div class="expcard__row"><b>Actores</b>${(e.actores && e.actores.length) ? esc(e.actores.join(", ")) : "—"}</div>
          <div class="expcard__row"><b>Demandados</b>${(e.demandados && e.demandados.length) ? esc(e.demandados.join(", ")) : "—"}</div>
        </div>`).join("")}
    </div>` : "";
  detail.innerHTML = `
    <button class="btn btn--ghost btn--sm" id="procBack">← Volver a la lista</button>
    <div class="procdetail__head">
      <div>
        <h2 class="procdetail__radicado">${esc(p.radicado)}</h2>
        <div class="procdetail__meta">
          ${p.materia ? `<span><b>Materia:</b> ${esc(p.materia)}</span>` : ""}
          ${p.estado ? `<span><b>Tipo de acción:</b> ${esc(p.estado)}</span>` : ""}
          ${p.organo ? `<span><b>Judicatura:</b> ${esc(p.organo)}</span>` : ""}
          ${extra}
          ${p.client_name ? `<span><b>Cliente:</b> ${esc(p.client_name)}</span>` : ""}
        </div>
      </div>
      <div class="procdetail__actions">
        <a class="btn btn--gold" href="/api/ecuador/processes/${p.id}/download">Descargar Excel</a>
        <button class="btn btn--danger-ghost" id="procDelete">Eliminar</button>
      </div>
    </div>
    ${manual}
    ${expedientesHtml}
    <h3 class="ledger ledger--section">Actuaciones del proceso (${p.actuaciones.length})</h3>
    <div class="acttable-wrap">
      ${p.actuaciones.length ? `<table class="acttable">
        <thead><tr><th>N</th><th>Fecha</th><th>Resolución</th><th>Acto</th><th>Sumilla</th><th>Adjunto</th></tr></thead>
        <tbody>${p.actuaciones.map((a) => `
          <tr class="${a.is_new ? "is-new" : ""}">
            <td>${esc(a.n)}</td><td>${esc(a.fecha)}</td><td>${esc(a.resolucion)}</td>
            <td>${esc(a.acto)}</td><td>${esc(a.sumilla)}</td><td>${actAttachmentHtml(a)}</td>
          </tr>`).join("")}</tbody>
      </table>` : `<p class="muted">Sin actuaciones registradas.</p>`}
    </div>`;
  $("#procBack").addEventListener("click", loadProcesses);
  $("#procDelete").addEventListener("click", () => {
    showConfirmPopup(`¿Eliminar el proceso ${p.radicado}? Esta acción no se puede deshacer.`, async () => {
      try {
        await api(`/api/ecuador/processes/${p.id}`, { method: "DELETE" });
        toast("Proceso eliminado", true);
        loadProcesses();
      } catch (err) { showErrorPopup(err.message); }
    });
  });
  detail.querySelectorAll("[data-attach-act]").forEach((btn) => {
    btn.addEventListener("click", () => detail.querySelector(`[data-attach-input="${btn.dataset.attachAct}"]`).click());
  });
  detail.querySelectorAll("[data-attach-input]").forEach((input) => {
    input.addEventListener("change", async () => {
      if (!input.files.length) return;
      try {
        await api(`/api/ecuador/actuaciones/${input.dataset.attachInput}/attachment`, { method: "POST", body: form({ file: input.files[0] }) });
        toast("Documento adjuntado", true);
        enterProcess(p.id);
      } catch (err) { showErrorPopup(err.message); }
    });
  });
}
function actAttachmentHtml(a) {
  const link = a.attachment_path
    ? `<a class="act-attach__link" href="/api/ecuador/actuaciones/${a.id}/attachment">Ver PDF</a>`
    : "";
  return `<div class="act-attach">${link}<button class="act-attach__upload" type="button" data-attach-act="${a.id}">${a.attachment_path ? "Cambiar" : "Adjuntar"}</button><input type="file" accept=".pdf" data-attach-input="${a.id}" /></div>`;
}
// Nota: no hay "Consulta Única" en Ecuador — esa función re-consulta vía el scraper de
// navegador de Perú (MANAGER/browser), que no aplica aquí (los procesos de Ecuador vienen
// del bot externo). Para refrescar un radicado hay que volver a incluirlo desde Inclusiones.
$("#procSearch").addEventListener("input", () => { clearTimeout(procSearchTimer); procSearchTimer = setTimeout(loadProcesses, 280); });
$("#procClientFilter").addEventListener("change", loadProcesses);
$("#downloadAllProc").addEventListener("click", async () => {
  const clientId = $("#procClientFilter").value;
  const url = `/api/ecuador/processes-download${clientId ? "?client_id=" + clientId : ""}`;
  try {
    const res = await fetch(url);
    if (!res.ok) {
      let msg = "No hay procesos para exportar.";
      try { const data = await res.json(); if (data.detail) msg = data.detail; } catch {}
      showErrorPopup(msg);
      return;
    }
    const blob = await res.blob();
    const match = (res.headers.get("Content-Disposition") || "").match(/filename="?([^"]+)"?/);
    const a = document.createElement("a");
    const blobUrl = URL.createObjectURL(blob);
    a.href = blobUrl;
    a.download = match ? match[1] : "procesos.xlsx";
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(blobUrl);
  } catch (err) { showErrorPopup(err.message); }
});
$("#deleteAllProc").addEventListener("click", () => {
  const filterEl = $("#procClientFilter");
  const clientId = filterEl.value;
  const clientLabel = clientId ? filterEl.selectedOptions[0].textContent : null;
  const msg = clientLabel
    ? `¿Eliminar TODOS los procesos de "${clientLabel}"? Esta acción no se puede deshacer.`
    : `¿Eliminar TODOS los procesos? (${$("#procesosCount").textContent}) Esta acción no se puede deshacer.`;
  showConfirmPopup(msg, async () => {
    try {
      const result = await api(`/api/ecuador/processes${clientId ? "?client_id=" + clientId : ""}`, { method: "DELETE" });
      toast(`${result.deleted} proceso(s) eliminado(s)`, true);
      loadProcesses();
    } catch (err) { showErrorPopup(err.message); }
  });
});

/* ============================================================
   INCLUSIONES (bot externo de Ecuador — llamada sincrona, sin SSE)
   ============================================================ */
/* Variante simple del overlay de consola: no hay progreso caso-por-caso (es una sola
   llamada HTTP al bot), asi que solo se muestra un spinner indeterminado mientras se
   espera la respuesta. */
function openBotWaitConsole(label) {
  $("#console").hidden = false;
  $("#consoleMode").textContent = "INCLUSIÓN (BOT ECUADOR)";
  $("#consoleBaseName").textContent = label;
  $("#liveRadicado").textContent = "—";
  $("#liveParte").textContent = "Esperando respuesta del bot…";
  $("#liveDone").textContent = "0";
  $("#liveTotal").textContent = "—";
  $("#progressBar").style.width = "100%";
  $("#progressBar").classList.add("is-indeterminate");
  $("#consoleLog").innerHTML = "";
  $("#consoleFooter").hidden = true;
  $("#consoleCancel").hidden = true; // una llamada sincrona al bot no se puede cancelar
  $("#consoleClose").hidden = true;
  $("#retryErrorsBtn").hidden = true;
  setLamp(true);
}
function finishBotConsole({ ok, total, error, downloadHref }) {
  setLamp(false);
  $("#progressBar").classList.remove("is-indeterminate");
  $("#progressBar").style.width = "100%";
  $("#consoleClose").hidden = false;
  $("#consoleFooter").hidden = false;
  if (ok) {
    $("#liveParte").textContent = "Respuesta recibida";
    $("#liveDone").textContent = total;
    $("#liveTotal").textContent = total;
    $("#consoleSummary").innerHTML = `${total} proceso${total === 1 ? "" : "s"} creado(s)/actualizado(s) · <b>revisa Mis Procesos</b>`;
    if (downloadHref) { $("#downloadBtn").href = downloadHref; $("#downloadBtn").hidden = false; }
    else { $("#downloadBtn").hidden = true; }
  } else {
    $("#liveParte").textContent = "Error";
    // Sin popup a proposito: la consola ya es su propio dialogo abierto con este mismo
    // mensaje adentro; abrir el popup encima solo apila dos fondos oscuros.
    console.error("[Consola CJ]", error);
    $("#consoleSummary").innerHTML = `<span style="color:var(--danger)">${esc(friendlyError(error))}</span>`;
    $("#downloadBtn").hidden = true;
  }
  loadClients();
}
$("#inclusionForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const radicado = $("#incRadicado").value.trim();
  if (!radicado) return toast("El radicado es obligatorio");
  const clientId = $("#incClient").value || "";
  openBotWaitConsole(radicado);
  try {
    const result = await api("/api/ecuador/inclusiones/bot", { method: "POST", body: form({ radicado, client_id: clientId || null }) });
    finishBotConsole({
      ok: true, total: result.total,
      downloadHref: result.process_ids.length ? `/api/ecuador/processes-download-batch?ids=${result.process_ids.join(",")}` : null,
    });
    $("#incRadicado").value = "";
  } catch (err) {
    finishBotConsole({ ok: false, error: err.message });
  }
});

/* pestañas Individual / Masivo */
$("#inclusionTabs").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-val]");
  if (!b) return;
  const masivo = b.dataset.val === "masivo";
  $("#inclusionForm").hidden = masivo;
  $("#inclusionBulkForm").hidden = !masivo;
  if (masivo) fillClientSelect("#incBulkClient");
});
/* carga masiva por Excel dentro de Inclusiones: el Excel se manda tal cual al bot,
   que hace su propia lectura (radicados en columna A desde la fila 2). */
(function wireIncDropzone() {
  const dz = $("#incDropzone"), fi = $("#incFileInput");
  if (!dz) return;
  dz.addEventListener("click", () => fi.click());
  dz.addEventListener("dragover", (e) => { e.preventDefault(); dz.classList.add("is-drag"); });
  dz.addEventListener("dragleave", () => dz.classList.remove("is-drag"));
  dz.addEventListener("drop", (e) => { e.preventDefault(); dz.classList.remove("is-drag"); if (e.dataTransfer.files.length) bulkInclude(e.dataTransfer.files[0]); });
  fi.addEventListener("change", () => { if (fi.files.length) bulkInclude(fi.files[0]); });
})();
async function bulkInclude(file) {
  const clientId = $("#incBulkClient").value;
  if (!clientId) return toast("Selecciona el cliente destino primero");
  if (!/\.xlsx?$/i.test(file.name)) return toast("Debe ser un archivo Excel (.xlsx)");
  $("#incFileInput").value = "";
  openBotWaitConsole(file.name);
  try {
    const result = await api("/api/ecuador/inclusiones/bot/bulk", { method: "POST", body: form({ file, client_id: clientId }) });
    finishBotConsole({
      ok: true, total: result.total,
      downloadHref: result.process_ids.length ? `/api/ecuador/processes-download-batch?ids=${result.process_ids.join(",")}` : null,
    });
  } catch (err) {
    finishBotConsole({ ok: false, error: err.message });
  }
}

/* ============================================================
   NOTIFICACIONES
   ============================================================ */
async function loadNotifications() {
  let data;
  try { data = await api(`/api/ecuador/notifications?notif_filter=${state.notifFilter}`); } catch (err) { return showErrorPopup(err.message); }
  updateNotifBadge(data.counts.no_leidas);
  $("#notifSummary").textContent = `${data.counts.total} en total · ${data.counts.no_leidas} sin leer`;
  state.notifSelection.clear();
  syncNotifSelUI();
  const wrap = $("#notifList");
  if (!data.items.length) { wrap.innerHTML = `<p class="muted">No hay notificaciones en este filtro.</p>`; return; }
  wrap.innerHTML = "";
  data.items.forEach((n) => wrap.appendChild(buildNotif(n)));
}
function buildNotif(n) {
  const row = el("div", "notif notif--" + n.kind + (n.read ? "" : " is-unread"));
  const resolveBtn = (n.kind === "manual" && !n.resolved)
    ? `<button class="btn btn--gold btn--sm notif__resolve">Auto cargado en Plataforma</button>` : "";
  const attachBtn = (n.kind === "manual" && !n.resolved)
    ? `<button class="btn btn--ghost btn--sm notif__attach">Adjuntar PDF</button><input type="file" class="notif__file" accept=".pdf" hidden />` : "";
  row.innerHTML = `
    <input type="checkbox" class="notif__check" data-id="${n.id}" />
    <div class="notif__body">
      <div class="notif__title">${esc(n.title)}</div>
      <div class="notif__meta">${esc(n.body || "")}</div>
      <div class="notif__timestamp">Consultado: ${esc((n.created_at || "").replace("T", " "))}</div>
    </div>
    ${attachBtn}
    ${resolveBtn}
    ${n.process_id ? `<span class="notif__arrow">→</span>` : ""}`;
  row.querySelector(".notif__check").addEventListener("change", (e) => {
    if (e.target.checked) state.notifSelection.add(n.id); else state.notifSelection.delete(n.id);
    syncNotifSelUI();
  });
  const rb = row.querySelector(".notif__resolve");
  if (rb) rb.addEventListener("click", async (e) => {
    e.stopPropagation();
    await api(`/api/ecuador/notifications/${n.id}/resolve`, { method: "POST" });
    toast("Marcado como cargado en plataforma", true);
    loadNotifications();
    refreshNotifBadge();
  });
  const ab = row.querySelector(".notif__attach");
  if (ab) ab.addEventListener("click", (e) => {
    e.stopPropagation();
    row.querySelector(".notif__file").click();
  });
  const fileInput = row.querySelector(".notif__file");
  if (fileInput) fileInput.addEventListener("change", async (e) => {
    e.stopPropagation();
    if (!fileInput.files.length) return;
    try {
      await api(`/api/ecuador/notifications/${n.id}/attachment`, { method: "POST", body: form({ file: fileInput.files[0] }) });
      toast("PDF adjuntado y alerta resuelta", true);
      loadNotifications();
      refreshNotifBadge();
    } catch (err) { showErrorPopup(err.message); }
  });
  row.querySelector(".notif__body").addEventListener("click", async () => {
    if (!n.read) await api(`/api/ecuador/notifications/${n.id}/read`, { method: "PATCH", body: form({ read: true }) });
    if (n.process_id) openProcessFromNotif(n.process_id);
    else loadNotifications();
  });
  return row;
}
function syncNotifSelUI() { $("#notifDeleteSel").disabled = state.notifSelection.size === 0; }
function updateNotifBadge(count) {
  const badge = $("#navNotifBadge");
  badge.textContent = count;
  badge.hidden = !count;
}
async function openProcessFromNotif(processId) {
  showModule("procesos");
  // Entra directamente al detalle del proceso correspondiente ("dentro de la card").
  await enterProcess(processId);
}
let globalSearchTimer = null;
function openGlobalSearch() {
  $("#globalSearch").hidden = false;
  $("#globalSearchInput").value = "";
  $("#globalSearchResults").innerHTML = `<p class="muted">Escribe para buscar.</p>`;
  $("#globalSearchInput").focus();
}
function closeGlobalSearch() {
  $("#globalSearch").hidden = true;
}
$("#globalSearchBtn").addEventListener("click", openGlobalSearch);
$("#globalSearch").addEventListener("click", (e) => { if (e.target === $("#globalSearch")) closeGlobalSearch(); });
$("#globalSearchInput").addEventListener("input", () => {
  clearTimeout(globalSearchTimer);
  globalSearchTimer = setTimeout(runGlobalSearch, 180);
});
async function runGlobalSearch() {
  const q = $("#globalSearchInput").value.trim();
  const wrap = $("#globalSearchResults");
  if (q.length < 2) { wrap.innerHTML = `<p class="muted">Escribe al menos 2 caracteres.</p>`; return; }
  let data;
  try { data = await api(`/api/ecuador/search?q=${encodeURIComponent(q)}`); } catch (err) { showErrorPopup(err.message); wrap.innerHTML = `<p class="muted">No se pudo buscar.</p>`; return; }
  const items = [];
  (data.clients || []).forEach((c) => items.push({ kind: "Cliente", title: c.name, meta: `${c.process_count || 0} procesos`, action: () => { closeGlobalSearch(); showModule("clientes"); selectClient(c.id); } }));
  (data.processes || []).forEach((p) => items.push({ kind: "Proceso", title: p.radicado, meta: [p.client_name, p.demandante, p.demandado].filter(Boolean).join(" · "), action: () => { closeGlobalSearch(); openProcessFromNotif(p.id); } }));
  if (!items.length) { wrap.innerHTML = `<p class="muted">Sin resultados.</p>`; return; }
  wrap.innerHTML = "";
  items.forEach((item) => {
    const btn = el("button", "command__item");
    btn.innerHTML = `<div><div><strong>${esc(item.title)}</strong></div><div class="command__meta">${esc(item.meta || "")}</div></div><span class="command__kind">${esc(item.kind)}</span>`;
    btn.addEventListener("click", item.action);
    wrap.appendChild(btn);
  });
}
$("#notifFilters").addEventListener("click", (e) => {
  const b = e.target.closest("[data-nf]");
  if (!b) return;
  $$("#notifFilters .badge").forEach((x) => x.classList.remove("is-active"));
  b.classList.add("is-active");
  state.notifFilter = b.dataset.nf;
  loadNotifications();
});
$("#notifReadAll").addEventListener("click", async () => { await api("/api/ecuador/notifications/read-all", { method: "POST" }); loadNotifications(); });
$("#notifDeleteSel").addEventListener("click", async () => {
  if (!state.notifSelection.size) return;
  await api("/api/ecuador/notifications/delete", { method: "POST", body: form({ ids: [...state.notifSelection].join(",") }) });
  loadNotifications();
  toast("Notificaciones eliminadas", true);
});
$("#notifDeleteAll").addEventListener("click", () => {
  showConfirmPopup("¿Eliminar TODAS las notificaciones? Esta acción no se puede deshacer.", async () => {
    try {
      await api("/api/ecuador/notifications", { method: "DELETE" });
      loadNotifications();
      toast("Notificaciones eliminadas", true);
    } catch (err) { showErrorPopup(err.message); }
  });
});

/* ============================================================
   CORRIDA EN VIVO (SSE) — compartida por bases e inclusiones
   ============================================================ */
function startRun(base, mode) {
  const label = mode === "total" ? "TOTAL (todas las actuaciones)" : "DIARIA (solo cambios)";
  // Segundo paso de confirmacion antes de arrancar el navegador y el recorrido.
  showConfirmPopup(
    `¿Iniciar la consulta ${label} de la base "${base.name}"?\n\nSe abrirá el navegador y comenzará el recorrido de los expedientes.`,
    async () => {
      try {
        const run = await api(`/api/ecuador/bases/${base.id}/run`, { method: "POST", body: form({ mode }) });
        openConsole(base, mode, run.id);
        streamRun(run.id);
      } catch (err) { showErrorPopup(err.message); }
    },
    { okLabel: "Sí, iniciar" },
  );
}
function openConsole(base, mode, runId) {
  $("#console").hidden = false;
  $("#consoleMode").textContent = mode === "total" ? "CONSULTA TOTAL" : (mode === "daily" ? "CONSULTA DIARIA" : (mode === "retry" ? "REINTENTO DE ERRORES" : "INCLUSIÓN"));
  $("#consoleBaseName").textContent = base.name;
  $("#liveRadicado").textContent = "—";
  $("#liveParte").textContent = "Iniciando navegador…";
  $("#liveDone").textContent = "0";
  $("#liveTotal").textContent = base.row_count;
  $("#progressBar").style.width = "0%";
  $("#consoleLog").innerHTML = "";
  $("#consoleFooter").hidden = true;
  $("#consoleClose").hidden = true;
  $("#retryErrorsBtn").hidden = true;
  $("#retryErrorsBtn").dataset.runId = "";
  $("#downloadBtn").dataset.runId = runId;
  const cancelBtn = $("#consoleCancel");
  cancelBtn.hidden = mode === "inclusion"; // una inclusión de 1 caso no se cancela
  cancelBtn.disabled = false;
  cancelBtn.textContent = "Cancelar";
  setLamp(true);
}
$("#consoleCancel").addEventListener("click", () => {
  const runId = $("#downloadBtn").dataset.runId;
  if (!runId || Number(runId) < 0) return;
  showConfirmPopup("¿Detener esta consulta? El expediente en curso terminará y los demás se cancelan.", async () => {
    const btn = $("#consoleCancel");
    btn.disabled = true; btn.textContent = "Cancelando…";
    $("#liveParte").textContent = "Cancelando… terminando el expediente en curso";
    try { await api(`/api/ecuador/runs/${runId}/cancel`, { method: "POST" }); }
    catch (err) { showErrorPopup(err.message); btn.disabled = false; btn.textContent = "Cancelar"; }
  }, { okLabel: "Sí, detener" });
});
function setLamp(busy) {
  $("#statuslamp").classList.toggle("is-busy", busy);
  $("#statuslabel").textContent = busy ? "Consultando" : "En reposo";
}
function streamRun(runId) {
  if (state.eventSource) state.eventSource.close();
  const es = new EventSource(`/api/ecuador/runs/${runId}/stream`);
  state.eventSource = es;
  es.onmessage = (msg) => { let ev; try { ev = JSON.parse(msg.data); } catch (_) { return; } handleEvent(ev, runId); };
  es.onerror = () => {};
}
function handleEvent(ev, runId) {
  switch (ev.type) {
    case "case_started":
      $("#liveRadicado").textContent = ev.radicado;
      $("#liveParte").textContent = ev.parte || "—";
      $("#liveTotal").textContent = ev.total;
      break;
    case "case_done": {
      $("#liveDone").textContent = ev.done;
      const pct = ev.total ? Math.round((ev.done / ev.total) * 100) : 0;
      $("#progressBar").style.width = pct + "%";
      addLog(ev);
      break;
    }
    case "generating":
      // La extracción terminó; ahora se arma el Excel. Barra indeterminada para que no
      // parezca colgado mientras se genera el archivo.
      $("#liveRadicado").textContent = "Consolidando resultados";
      $("#liveParte").textContent = "Generando Excel… un momento";
      $("#progressBar").style.width = "100%";
      $("#progressBar").classList.add("is-indeterminate");
      $("#consoleCancel").hidden = true;
      break;
    case "run_finished": finishConsole(ev, runId); break;
    case "cancel_requested": { const b = $("#consoleCancel"); b.disabled = true; b.textContent = "Cancelando…"; break; }
    case "snapshot": if (ev.status === "done") $("#liveTotal").textContent = ev.total; break;
  }
}
function addLog(ev) {
  const log = $("#consoleLog");
  const ok = ev.status === "OK";
  const line = el("div", "logline");
  line.innerHTML = `<span class="logline__idx">${ev.index}/${ev.total}</span><span class="logline__rad">${esc(ev.radicado)}</span><span class="logline__status ${ok ? "ok" : "err"}">${ok ? "✓ " + esc(ev.movimientos || "OK") : "✕ error"}</span>`;
  log.appendChild(line);
  log.scrollTop = log.scrollHeight;
}
function finishConsole(ev, runId) {
  if (state.eventSource) { state.eventSource.close(); state.eventSource = null; }
  setLamp(false);
  $("#consoleCancel").hidden = true;
  $("#consoleClose").hidden = false;
  $("#consoleFooter").hidden = false;
  $("#progressBar").classList.remove("is-indeterminate");
  $("#progressBar").style.width = "100%";
  const isInclusion = Number(runId) < 0;
  if (ev.status === "done") {
    if (isInclusion) {
      $("#consoleSummary").innerHTML = `Proceso creado · <b>revisa Mis Procesos</b>`;
      $("#downloadBtn").hidden = true;
    } else {
      $("#consoleSummary").innerHTML = `Consulta finalizada · <b>Excel listo</b>`;
      $("#downloadBtn").href = `/api/ecuador/runs/${runId}/download`;
      $("#downloadBtn").hidden = false;
    }
  } else if (ev.status === "cancelled") {
    $("#consoleSummary").innerHTML = `<span style="color:var(--olive)">Cancelada</span> · Excel parcial disponible`;
    $("#downloadBtn").href = `/api/ecuador/runs/${runId}/download`;
    $("#downloadBtn").hidden = false;
  } else {
    // Sin popup a proposito: la consola ya es su propio dialogo abierto con este mismo
    // mensaje adentro; abrir el popup encima solo apila dos fondos oscuros.
    console.error("[Consola CJ]", ev.error);
    $("#consoleSummary").innerHTML = `<span style="color:var(--danger)">${esc(friendlyError(ev.error))}</span>`;
    $("#downloadBtn").hidden = true;
  }
  if (state.activeClient) loadBases();
  loadClients();
  updateRetryButton(runId);
}
async function updateRetryButton(runId) {
  const btn = $("#retryErrorsBtn");
  btn.hidden = true;
  btn.dataset.runId = "";
  if (Number(runId) < 0) return;
  try {
    const data = await api(`/api/ecuador/runs/${runId}/cases`);
    if ((data.counts?.error || 0) > 0) {
      btn.hidden = false;
      btn.dataset.runId = runId;
      btn.textContent = `Reintentar errores (${data.counts.error})`;
    }
  } catch (_) {}
}
$("#retryErrorsBtn").addEventListener("click", () => {
  const sourceRunId = $("#retryErrorsBtn").dataset.runId;
  if (!sourceRunId) return;
  showConfirmPopup("¿Reconsultar solo los procesos que fallaron en esta corrida?", async () => {
    try {
      const run = await api(`/api/ecuador/runs/${sourceRunId}/retry-errors`, { method: "POST" });
      openConsole({ name: "Reintento de errores", row_count: run.total }, "retry", run.id);
      streamRun(run.id);
    } catch (err) { showErrorPopup(err.message); }
  }, { okLabel: "Sí, reintentar" });
});
$("#consoleClose").addEventListener("click", () => {
  $("#console").hidden = true;
  if (state.eventSource) { state.eventSource.close(); state.eventSource = null; }
});

/* ============================================================
   PROGRAMAR (schedule modal)
   ============================================================ */
async function openSchedule(base) {
  state.scheduleBaseId = base.id;
  $("#scheduleBaseName").textContent = base.name;
  $("#scheduleModal").hidden = false;
  await loadSchedules(base.id);
}
async function loadSchedules(baseId) {
  const list = await api(`/api/ecuador/bases/${baseId}/schedules`);
  const ul = $("#scheduleList");
  ul.innerHTML = "";
  if (!list.length) ul.innerHTML = `<li><span class="muted">Sin programaciones.</span></li>`;
  list.forEach((s) => {
    const li = el("li", "schedrow" + (s.enabled ? "" : " is-off"));
    li.innerHTML = `
      <span class="schedrow__time">${String(s.hour).padStart(2, "0")}:${String(s.minute).padStart(2, "0")}</span>
      <span class="schedrow__mode">${s.mode === "daily" ? "Diaria" : "Total"}</span>
      <span class="schedrow__spacer"></span>
      <button class="btn btn--ghost btn--sm" data-toggle="${s.id}">${s.enabled ? "Pausar" : "Activar"}</button>
      <button class="btn btn--danger-ghost btn--sm" data-delsched="${s.id}">✕</button>`;
    li.querySelector("[data-toggle]").addEventListener("click", async () => { await api(`/api/ecuador/schedules/${s.id}`, { method: "PATCH", body: form({ enabled: !s.enabled }) }); loadSchedules(baseId); });
    li.querySelector("[data-delsched]").addEventListener("click", async () => { await api(`/api/ecuador/schedules/${s.id}`, { method: "DELETE" }); loadSchedules(baseId); });
    ul.appendChild(li);
  });
}
$("#scheduleForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api(`/api/ecuador/bases/${state.scheduleBaseId}/schedules`, { method: "POST", body: form({ mode: $("#schedMode").dataset.value, hour: $("#schedHour").value, minute: $("#schedMinute").value }) });
    await loadSchedules(state.scheduleBaseId);
    toast("Programación agregada", true);
  } catch (err) { showErrorPopup(err.message); }
});
function closeScheduleModal() { $("#scheduleModal").hidden = true; }
$("#scheduleModalClose").addEventListener("click", closeScheduleModal);
$("#scheduleModal").addEventListener("click", (e) => { if (e.target === $("#scheduleModal")) closeScheduleModal(); });
document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
    e.preventDefault();
    openGlobalSearch();
    return;
  }
  if (e.key !== "Escape") return;
  if (!$("#confirmPopup").hidden) { closeConfirmPopup(); return; }
  if (!$("#errorPopup").hidden) { closeErrorPopup(); return; }
  if (!$("#globalSearch").hidden) { closeGlobalSearch(); return; }
  if (!$("#scheduleModal").hidden) { closeScheduleModal(); return; }
  if (!$("#console").hidden && !$("#consoleClose").hidden) $("#consoleClose").click();
});

/* ---------- boot ---------- */
async function refreshNotifBadge() {
  try { const d = await api("/api/ecuador/dashboard"); updateNotifBadge(d.notifications.no_leidas); } catch (_) {}
}
loadClients()
  .then(() => { loadDashboard(); refreshNotifBadge(); })
  .catch((e) => toast("No se pudo conectar con el servidor: " + e.message));
