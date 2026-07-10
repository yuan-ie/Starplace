// ---------------------------------------------------------------------
// PLACE — frontend
//
// Identity comes from the server-side Discord session (httpOnly cookie),
// never from a client-supplied header — a header can be spoofed to spend
// someone else's charges, a session cookie signed by the server can't be.
// ---------------------------------------------------------------------

const state = {
  size: 200,
  version: 0,
  pixels: new Uint8Array(0),
  palette: {},          // { "0": {name, hex}, ... }
  selectedColor: 0,
  pending: null,        // { x, y } awaiting confirmation, or null
  scale: 1,
  offsetX: 0,
  offsetY: 0,
  minScale: 0.5,
  maxScale: 40,
  loggedIn: false,
  user: null,           // { id, username, avatar_url } when logged in
  charges: 0,
  maxCharges: 3,
  nextRefillAt: null,
  chargeInterval: 1200,
};

const el = {
  viewport: document.getElementById("viewport"),
  stage: document.getElementById("stage"),
  pixelCanvas: document.getElementById("pixel-canvas"),
  fxCanvas: document.getElementById("fx-canvas"),
  cursorCell: document.getElementById("cursor-cell"),
  readout: document.getElementById("readout"),
  readoutXY: document.getElementById("readout-xy"),
  readoutColor: document.getElementById("readout-color"),
  paletteGrid: document.getElementById("palette-grid"),
  statusPanel: document.getElementById("status-panel"),
  chargeDots: document.getElementById("charge-dots"),
  chargeReadout: document.getElementById("charge-readout"),
  cooldownTrack: document.getElementById("cooldown-bar-track"),
  cooldownFill: document.getElementById("cooldown-bar-fill"),
  statusLockNote: document.getElementById("status-lock-note"),
  signinBtn: document.getElementById("signin-btn"),
  accountIdentity: document.getElementById("account-identity"),
  accountAvatar: document.getElementById("account-avatar"),
  accountUsername: document.getElementById("account-username"),
  signoutBtn: document.getElementById("signout-btn"),
  placeBtn: document.getElementById("place-btn"),
  placeBtnCoords: document.getElementById("place-btn-coords"),
  zoomIn: document.getElementById("zoom-in"),
  zoomOut: document.getElementById("zoom-out"),
  zoomReset: document.getElementById("zoom-reset"),
  tickerTrack: document.getElementById("ticker-track"),
  toast: document.getElementById("toast"),
};

const pixelCtx = el.pixelCanvas.getContext("2d");
const fxCtx = el.fxCanvas.getContext("2d");

// -----------------------------------------------------------------------
// API helpers
// -----------------------------------------------------------------------

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: {
      "Content-Type": "application/json",
      ...(opts.headers || {}),
    },
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok && res.status !== 429) {
    const err = new Error(body.error || `request failed (${res.status})`);
    err.status = res.status;
    throw err;
  }
  return body;
}

// -----------------------------------------------------------------------
// Discord session
// -----------------------------------------------------------------------

async function checkSession() {
  try {
    const res = await api("/api/session");
    state.loggedIn = res.logged_in;
    state.user = res.user;
  } catch (_) {
    state.loggedIn = false;
    state.user = null;
  }
  renderAccountPanel();
}

function renderAccountPanel() {
  if (state.loggedIn && state.user) {
    el.signinBtn.hidden = true;
    el.accountIdentity.hidden = false;
    el.accountAvatar.src = state.user.avatar_url || "";
    el.accountUsername.textContent = state.user.username;
    el.statusPanel.classList.remove("locked");
    el.statusLockNote.hidden = true;
  } else {
    el.signinBtn.hidden = false;
    el.accountIdentity.hidden = true;
    el.statusPanel.classList.add("locked");
    el.statusLockNote.hidden = false;
    state.charges = 0;
    renderChargeDots();
  }
  updatePlaceButton();
}

el.signinBtn.addEventListener("click", () => {
  const returnTo = encodeURIComponent(location.pathname + location.search);
  location.href = `/auth/login?return_to=${returnTo}`;
});

el.signoutBtn.addEventListener("click", async () => {
  await api("/auth/logout", { method: "POST" });
  state.loggedIn = false;
  state.user = null;
  clearPending();
  renderAccountPanel();
});

function promptSignIn() {
  showToast("Sign in with Discord to place a pixel.");
}

// -----------------------------------------------------------------------
// Rendering: pixel grid
// -----------------------------------------------------------------------

function colorHex(colorId) {
  const entry = state.palette[String(colorId)];
  return entry ? entry.hex : "#000000";
}

function drawFullCanvas() {
  const { size, pixels } = state;
  const img = pixelCtx.createImageData(size, size);
  for (let i = 0; i < size * size; i++) {
    const hex = colorHex(pixels[i]);
    const r = parseInt(hex.slice(1, 3), 16);
    const g = parseInt(hex.slice(3, 5), 16);
    const b = parseInt(hex.slice(5, 7), 16);
    img.data[i * 4] = r;
    img.data[i * 4 + 1] = g;
    img.data[i * 4 + 2] = b;
    img.data[i * 4 + 3] = 255;
  }
  pixelCtx.putImageData(img, 0, 0);
}

function drawPixel(x, y, colorId) {
  const hex = colorHex(colorId);
  pixelCtx.fillStyle = hex;
  pixelCtx.fillRect(x, y, 1, 1);
}

// -----------------------------------------------------------------------
// "Wet paint" glow effect — the page's one signature flourish. Freshly
// placed pixels get a brief warm glow that fades, like ink settling.
// -----------------------------------------------------------------------

let glows = []; // { x, y, start, color }

function addGlow(x, y, colorId) {
  glows.push({ x, y, start: performance.now(), hex: colorHex(colorId) });
}

function tickGlows(now) {
  fxCtx.clearRect(0, 0, el.fxCanvas.width, el.fxCanvas.height);
  const DURATION = 1200;
  glows = glows.filter((g) => now - g.start < DURATION);
  for (const g of glows) {
    const t = (now - g.start) / DURATION; // 0 -> 1
    const alpha = 1 - t;
    const radius = 0.6 + t * 1.8;
    const grad = fxCtx.createRadialGradient(
      g.x + 0.5, g.y + 0.5, 0,
      g.x + 0.5, g.y + 0.5, radius
    );
    grad.addColorStop(0, hexToRgba(g.hex, alpha * 0.9));
    grad.addColorStop(1, hexToRgba(g.hex, 0));
    fxCtx.fillStyle = grad;
    fxCtx.fillRect(g.x - radius, g.y - radius, radius * 2, radius * 2);
  }
  requestAnimationFrame(tickGlows);
}
requestAnimationFrame(tickGlows);

function hexToRgba(hex, alpha) {
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  return `rgba(${r},${g},${b},${alpha})`;
}

// -----------------------------------------------------------------------
// Pan + zoom
// -----------------------------------------------------------------------

function applyTransform() {
  el.stage.style.transform = `translate(${state.offsetX}px, ${state.offsetY}px) scale(${state.scale})`;
}

function fitToScreen() {
  const vw = el.viewport.clientWidth;
  const vh = el.viewport.clientHeight;
  const margin = 0.86;
  const scale = Math.min(vw / state.size, vh / state.size) * margin;
  state.scale = Math.max(state.minScale, Math.min(scale, state.maxScale));
  state.offsetX = (vw - state.size * state.scale) / 2;
  state.offsetY = (vh - state.size * state.scale) / 2;
  applyTransform();
}

function zoomAt(clientX, clientY, factor) {
  const rect = el.viewport.getBoundingClientRect();
  const px = clientX - rect.left;
  const py = clientY - rect.top;
  const worldX = (px - state.offsetX) / state.scale;
  const worldY = (py - state.offsetY) / state.scale;

  const newScale = Math.max(state.minScale, Math.min(state.scale * factor, state.maxScale));
  state.offsetX = px - worldX * newScale;
  state.offsetY = py - worldY * newScale;
  state.scale = newScale;
  applyTransform();
}

el.viewport.addEventListener(
  "wheel",
  (e) => {
    e.preventDefault();
    const factor = Math.exp(-e.deltaY * 0.0016);
    zoomAt(e.clientX, e.clientY, factor);
  },
  { passive: false }
);

el.zoomIn.addEventListener("click", () => {
  const r = el.viewport.getBoundingClientRect();
  zoomAt(r.left + r.width / 2, r.top + r.height / 2, 1.5);
});
el.zoomOut.addEventListener("click", () => {
  const r = el.viewport.getBoundingClientRect();
  zoomAt(r.left + r.width / 2, r.top + r.height / 2, 1 / 1.5);
});
el.zoomReset.addEventListener("click", fitToScreen);
window.addEventListener("resize", fitToScreen);

// -----------------------------------------------------------------------
// Pointer handling: pan on drag, click (no drag) selects a cell,
// pinch-to-zoom with two touch pointers.
// -----------------------------------------------------------------------

const pointers = new Map(); // pointerId -> {x, y}
let dragMoved = 0;
let panStart = null;       // {x, y, offsetX, offsetY}
let pinchStartDist = null;
let pinchStartScale = null;

function pointerDist(a, b) {
  return Math.hypot(a.x - b.x, a.y - b.y);
}

el.viewport.addEventListener("pointerdown", (e) => {
  el.viewport.setPointerCapture(e.pointerId);
  pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
  dragMoved = 0;

  if (pointers.size === 1) {
    panStart = { x: e.clientX, y: e.clientY, offsetX: state.offsetX, offsetY: state.offsetY };
    el.viewport.classList.add("grabbing");
  } else if (pointers.size === 2) {
    const [a, b] = [...pointers.values()];
    pinchStartDist = pointerDist(a, b);
    pinchStartScale = state.scale;
  }
});

el.viewport.addEventListener("pointermove", (e) => {
  if (!pointers.has(e.pointerId)) {
    updateHover(e);
    return;
  }
  pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });

  if (pointers.size === 2) {
    const [a, b] = [...pointers.values()];
    const dist = pointerDist(a, b);
    const midX = (a.x + b.x) / 2;
    const midY = (a.y + b.y) / 2;
    const factor = (dist / pinchStartDist) * pinchStartScale / state.scale;
    zoomAt(midX, midY, factor);
    dragMoved = 99;
  } else if (panStart) {
    const dx = e.clientX - panStart.x;
    const dy = e.clientY - panStart.y;
    dragMoved = Math.max(dragMoved, Math.hypot(dx, dy));
    state.offsetX = panStart.offsetX + dx;
    state.offsetY = panStart.offsetY + dy;
    applyTransform();
  }
  updateHover(e);
});

function endPointer(e) {
  const wasClick = dragMoved < 6 && pointers.size === 1;
  pointers.delete(e.pointerId);
  if (pointers.size === 0) {
    panStart = null;
    el.viewport.classList.remove("grabbing");
    if (wasClick) handleCanvasClick(e);
  } else if (pointers.size === 1) {
    // dropped from pinch back to single-finger pan
    const [only] = [...pointers.values()];
    panStart = { x: only.x, y: only.y, offsetX: state.offsetX, offsetY: state.offsetY };
  }
}

el.viewport.addEventListener("pointerup", endPointer);
el.viewport.addEventListener("pointercancel", endPointer);
el.viewport.addEventListener("pointerleave", () => {
  el.cursorCell.hidden = true;
  el.readout.hidden = true;
});

function clientToCell(clientX, clientY) {
  const rect = el.viewport.getBoundingClientRect();
  const px = clientX - rect.left;
  const py = clientY - rect.top;
  const worldX = (px - state.offsetX) / state.scale;
  const worldY = (py - state.offsetY) / state.scale;
  return { x: Math.floor(worldX), y: Math.floor(worldY) };
}

function updateHover(e) {
  const { x, y } = clientToCell(e.clientX, e.clientY);
  if (x < 0 || y < 0 || x >= state.size || y >= state.size) {
    el.cursorCell.hidden = true;
    el.readout.hidden = true;
    return;
  }
  el.cursorCell.hidden = false;
  el.cursorCell.classList.toggle("is-pending", !!state.pending);
  positionCursorCell(state.pending || { x, y });

  el.readout.hidden = false;
  el.readout.style.left = "0px";
  el.readout.style.top = "0px";
  el.readout.style.transform = `translate(${e.clientX + 16}px, ${e.clientY + 16}px)`;
  el.readoutXY.textContent = `${x}, ${y}`;
  el.readoutColor.style.background = colorHex(state.selectedColor);
}

function positionCursorCell(cell) {
  const size = state.scale;
  el.cursorCell.style.width = `${size}px`;
  el.cursorCell.style.height = `${size}px`;
  el.cursorCell.style.left = `${state.offsetX + cell.x * state.scale}px`;
  el.cursorCell.style.top = `${state.offsetY + cell.y * state.scale}px`;
}

function handleCanvasClick(e) {
  const { x, y } = clientToCell(e.clientX, e.clientY);
  if (x < 0 || y < 0 || x >= state.size || y >= state.size) return;
  if (!state.loggedIn) {
    promptSignIn();
    return;
  }
  state.pending = { x, y };
  positionCursorCell(state.pending);
  el.cursorCell.classList.add("is-pending");
  updatePlaceButton();
}

// -----------------------------------------------------------------------
// Palette
// -----------------------------------------------------------------------

function renderPalette() {
  el.paletteGrid.innerHTML = "";
  const ids = Object.keys(state.palette).sort((a, b) => Number(a) - Number(b));
  for (const id of ids) {
    const btn = document.createElement("button");
    btn.className = "swatch" + (Number(id) === state.selectedColor ? " selected" : "");
    btn.style.background = state.palette[id].hex;
    btn.title = state.palette[id].name;
    btn.addEventListener("click", () => {
      state.selectedColor = Number(id);
      renderPalette();
      updatePlaceButton();
    });
    el.paletteGrid.appendChild(btn);
  }
}

// -----------------------------------------------------------------------
// Place button + charges
// -----------------------------------------------------------------------

function updatePlaceButton() {
  if (state.pending) {
    el.placeBtn.classList.add("visible");
    el.placeBtnCoords.textContent = `(${state.pending.x}, ${state.pending.y})`;
  } else {
    el.placeBtn.classList.remove("visible");
  }
  el.placeBtn.disabled = !state.loggedIn || state.charges <= 0 || !state.pending;
}

el.placeBtn.addEventListener("click", async () => {
  if (!state.loggedIn) {
    promptSignIn();
    return;
  }
  if (!state.pending || state.charges <= 0) return;
  const { x, y } = state.pending;
  const colorId = state.selectedColor;

  // optimistic paint
  drawPixel(x, y, colorId);
  addGlow(x, y, colorId);
  clearPending();

  try {
    const result = await api("/api/paint", {
      method: "POST",
      body: JSON.stringify({ x, y, color_id: colorId }),
    });
    if (result.ok) {
      state.version = result.version;
      refreshMe();
    } else if (result.error === "no_charges") {
      showToast("Out of ink — wait for a refill.");
      refreshMe();
    } else {
      showToast("That placement didn't go through.");
    }
  } catch (err) {
    if (err.status === 401) {
      state.loggedIn = false;
      state.user = null;
      renderAccountPanel();
      promptSignIn();
    } else {
      showToast(err.message || "Connection hiccup — try again.");
    }
  }
});

function clearPending() {
  state.pending = null;
  el.cursorCell.classList.remove("is-pending");
  updatePlaceButton();
}

window.addEventListener("keydown", (e) => {
  if (e.key === "Escape") clearPending();
  if (e.key === "Enter" && state.pending) el.placeBtn.click();
});

function renderChargeDots() {
  el.chargeDots.innerHTML = "";
  for (let i = 0; i < state.maxCharges; i++) {
    const dot = document.createElement("div");
    dot.className = "charge-dots__dot" + (i < state.charges ? "" : " empty");
    el.chargeDots.appendChild(dot);
  }
  el.chargeReadout.textContent = `${state.charges} / ${state.maxCharges} loaded`;

  if (state.charges < state.maxCharges && state.nextRefillAt) {
    el.cooldownTrack.hidden = false;
    tickCooldown();
  } else {
    el.cooldownTrack.hidden = true;
  }
  updatePlaceButton();
}

let cooldownTimer = null;
function tickCooldown() {
  clearInterval(cooldownTimer);
  const update = () => {
    if (!state.nextRefillAt) return;
    const remaining = state.nextRefillAt - Date.now() / 1000;
    const pct = Math.max(0, Math.min(1, 1 - remaining / state.chargeInterval));
    el.cooldownFill.style.width = `${pct * 100}%`;
    if (remaining <= 0) {
      clearInterval(cooldownTimer);
      refreshMe();
    }
  };
  update();
  cooldownTimer = setInterval(update, 1000);
}

async function refreshMe() {
  if (!state.loggedIn) return;
  try {
    const me = await api("/api/me");
    state.charges = me.charges;
    state.maxCharges = me.max_charges;
    state.nextRefillAt = me.next_refill_at;
    state.chargeInterval = me.charge_interval;
    renderChargeDots();
  } catch (err) {
    if (err.status === 401) {
      state.loggedIn = false;
      state.user = null;
      renderAccountPanel();
    }
    // otherwise silent — will retry on next poll
  }
}

// -----------------------------------------------------------------------
// Live updates (short polling — no extra backend deps required)
// -----------------------------------------------------------------------

async function pollUpdates() {
  try {
    const { changes } = await api(`/api/updates?since=${state.version}`);
    for (const c of changes) {
      state.pixels[c.y * state.size + c.x] = c.color_id;
      drawPixel(c.x, c.y, c.color_id);
      addGlow(c.x, c.y, c.color_id);
      state.version = Math.max(state.version, c.version);
    }
  } catch (_) { /* silent — will retry */ }
}

async function pollActivity() {
  try {
    const entries = await api("/api/activity?limit=12");
    renderTicker(entries);
  } catch (_) { /* silent */ }
}

function renderTicker(entries) {
  if (!entries.length) {
    el.tickerTrack.textContent = "waiting for the first pixel…";
    return;
  }
  el.tickerTrack.innerHTML = entries
    .map((e, i) => {
      const name = escapeHtml(e.display_name || "anon");
      const cls = i === 0 ? "fresh" : "";
      return `<span class="${cls}">${name} → (${e.x},${e.y})</span>`;
    })
    .join("  ·  ");
}

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// -----------------------------------------------------------------------
// Toast
// -----------------------------------------------------------------------

let toastTimer = null;
function showToast(message) {
  el.toast.textContent = message;
  el.toast.hidden = false;
  requestAnimationFrame(() => el.toast.classList.add("visible"));
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    el.toast.classList.remove("visible");
    setTimeout(() => { el.toast.hidden = true; }, 220);
  }, 2600);
}

// -----------------------------------------------------------------------
// Boot
// -----------------------------------------------------------------------

async function boot() {
  const [snapshot, palette] = await Promise.all([
    api("/api/canvas"),
    api("/api/palette"),
  ]);

  state.size = snapshot.size;
  state.version = snapshot.version;
  state.pixels = Uint8Array.from(snapshot.pixels);
  state.palette = palette;

  el.pixelCanvas.width = state.size;
  el.pixelCanvas.height = state.size;
  el.pixelCanvas.style.width = `${state.size}px`;
  el.pixelCanvas.style.height = `${state.size}px`;
  el.fxCanvas.width = state.size;
  el.fxCanvas.height = state.size;
  el.fxCanvas.style.width = `${state.size}px`;
  el.fxCanvas.style.height = `${state.size}px`;

  drawFullCanvas();
  renderPalette();
  fitToScreen();

  await checkSession();
  await refreshMe();
  await pollActivity();

  const authError = new URLSearchParams(location.search).get("auth_error");
  if (authError) {
    showToast("Discord sign-in didn't go through — please try again.");
    history.replaceState(null, "", location.pathname);
  }

  setInterval(pollUpdates, 1500);
  setInterval(refreshMe, 5000);
  setInterval(pollActivity, 4000);
}

boot().catch((err) => showToast(`Couldn't load the canvas: ${err.message}`));
