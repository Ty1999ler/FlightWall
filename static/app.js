/* FlightWall front-end: polls /api/aircraft and renders the wall. */
"use strict";

const board = document.getElementById("board");
const emptyState = document.getElementById("empty-state");
const banner = document.getElementById("status-banner");
const countBadge = document.getElementById("count-badge");
const locationLine = document.getElementById("location-line");

let config = null;
let latest = [];            // last aircraft list from the server
let cards = new Map();      // hex -> {el, refs}
let pollTimer = null;

/* Location source is per-device (localStorage), never stored on the server. */
const LOC_KEY = "flightwall_location_source";
let locSource = localStorage.getItem(LOC_KEY) || "home"; // home | device
let devicePos = null;
let geoWatchId = null;
let geoError = null;

function geoAvailable() {
  if (!("geolocation" in navigator)) return "no browser support";
  if (!window.isSecureContext) return "needs HTTPS";
  return null;
}

function startDeviceLocation() {
  const unavailable = geoAvailable();
  if (unavailable) { geoError = unavailable; return; }
  if (geoWatchId != null) return;
  geoWatchId = navigator.geolocation.watchPosition(
    p => {
      devicePos = { lat: p.coords.latitude, lon: p.coords.longitude };
      geoError = null;
      updateLocationLine();
    },
    e => {
      geoError = e.message || "location permission denied";
      if (e.code === e.PERMISSION_DENIED && geoWatchId != null) {
        // a denied watch is dead forever; clear it so the next poll can
        // re-prompt (dismissed) or pick up a re-grant from site settings
        navigator.geolocation.clearWatch(geoWatchId);
        geoWatchId = null;
      }
      updateLocationLine();
    },
    { enableHighAccuracy: false, maximumAge: 30000, timeout: 15000 }
  );
}

function stopDeviceLocation() {
  if (geoWatchId != null) { navigator.geolocation.clearWatch(geoWatchId); geoWatchId = null; }
  devicePos = null;
  geoError = null;
}

const PLANE_SVG = `<svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor">
  <path d="M12 1.5 13.6 9l8.4 4.4v1.8l-8.3-1.6-.7 5.6 2.6 1.9v1.4L12 21.4l-3.6 1.1v-1.4l2.6-1.9-.7-5.6L2 15.2v-1.8L10.4 9 12 1.5z"/>
</svg>`;

/* ---------------- formatting helpers ---------------- */

const COMPASS = ["N","NNE","NE","ENE","E","ESE","SE","SSE","S","SSW","SW","WSW","W","WNW","NW","NNW"];
const compass = deg => (deg == null) ? "" : COMPASS[Math.round(((deg % 360) + 360) % 360 / 22.5) % 16];

const metric = () => config && config.units === "metric";

function fmtAlt(ft, onGround) {
  if (onGround) return "GROUND";
  if (ft == null) return "—";
  return metric()
    ? `${Math.round(ft * 0.3048).toLocaleString()} m`
    : `${Math.round(ft).toLocaleString()} ft`;
}
function fmtSpeed(kt) {
  if (kt == null) return "—";
  return metric() ? `${Math.round(kt * 1.852)} km/h` : `${Math.round(kt)} kt`;
}
function fmtDist(nm) {
  if (nm == null) return "—";
  const v = metric() ? nm * 1.852 : nm * 1.15078;
  return `${v < 10 ? v.toFixed(1) : Math.round(v)} ${metric() ? "km" : "mi"}`;
}
function trendArrow(rate) {
  if (rate == null || Math.abs(rate) < 320) return "";
  return rate > 0 ? ` <span class="trend-up">▲</span>` : ` <span class="trend-down">▼</span>`;
}

function setText(node, text) { if (node.textContent !== text) node.textContent = text; }
function setHTML(node, html) { if (node.innerHTML !== html) node.innerHTML = html; }
function esc(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* ---------------- card rendering ---------------- */

function buildCard(hex) {
  const el = document.createElement("article");
  el.className = "card";
  el.innerHTML = `
    <span class="overhead-badge hidden">OVERHEAD</span>
    <span class="emergency-badge hidden">EMERGENCY</span>
    <div class="card-top">
      <div>
        <div class="airline"></div>
        <div class="flightno"></div>
        <div class="plane-meta"></div>
      </div>
      <img class="photo hidden" alt="" loading="eager">
    </div>
    <div class="unverified-tag hidden">⚠ ROUTE UNVERIFIED</div>
    <div class="route hidden">
      <div class="endpoint"><div class="code"></div><div class="city"></div></div>
      <div class="route-line"><span class="jet">${PLANE_SVG}</span></div>
      <div class="endpoint dest"><div class="code"></div><div class="city"></div></div>
    </div>
    <div class="no-route hidden"></div>
    <div class="stats">
      <div class="stat"><div class="label">ALTITUDE</div><div class="value alt"></div></div>
      <div class="stat"><div class="label">SPEED</div><div class="value spd"></div></div>
      <div class="stat"><div class="label">DISTANCE</div><div class="value dist"></div></div>
      <div class="stat"><div class="label">LOOK ANGLE</div><div class="value look"></div></div>
    </div>`;
  const q = s => el.querySelector(s);
  const refs = {
    badge: q(".overhead-badge"), emerg: q(".emergency-badge"),
    airline: q(".airline"), flightno: q(".flightno"),
    meta: q(".plane-meta"), photo: q(".photo"), unverified: q(".unverified-tag"),
    route: q(".route"), jet: q(".jet"), noRoute: q(".no-route"),
    oCode: q(".endpoint:not(.dest) .code"), oCity: q(".endpoint:not(.dest) .city"),
    dCode: q(".endpoint.dest .code"), dCity: q(".endpoint.dest .city"),
    alt: q(".alt"), spd: q(".spd"), dist: q(".dist"), look: q(".look"),
  };
  refs.photo.addEventListener("error", () => {
    refs.photo.classList.add("hidden");
    delete refs.photo.dataset.src; // let the next poll retry a transient failure
  });
  el.addEventListener("click", () =>
    window.open(`https://globe.adsbexchange.com/?icao=${hex}`, "_blank", "noopener"));
  return { el, refs };
}

function updateCard(card, a) {
  const { refs, el } = card;
  const ident = a.flight_iata || a.callsign || a.registration || a.hex.toUpperCase();
  const showCs = a.flight_iata && a.callsign && a.flight_iata !== a.callsign;
  setHTML(refs.flightno, `${esc(ident)}${showCs ? `<span class="cs">${esc(a.callsign)}</span>` : ""}`);
  setText(refs.airline, a.airline || (a.is_ga ? "Private / General aviation" : "Unknown operator"));

  const type = a.type_name || a.type_code || "";
  setText(refs.meta, [type, a.registration].filter(Boolean).join("  ·  "));

  if (a.photo_thumb) {
    if (refs.photo.dataset.src !== a.photo_thumb) {
      refs.photo.dataset.src = a.photo_thumb;
      refs.photo.src = a.photo_thumb;
      refs.photo.classList.remove("hidden");
    }
  } else {
    refs.photo.classList.add("hidden");
  }

  const hasRoute = a.origin && a.destination;
  refs.route.classList.toggle("hidden", !hasRoute);
  refs.noRoute.classList.toggle("hidden", hasRoute);
  if (hasRoute) {
    setText(refs.oCode, a.origin.iata || a.origin.icao || "?");
    setText(refs.oCity, a.origin.city || a.origin.name || "");
    setText(refs.dCode, a.destination.iata || a.destination.icao || "?");
    setText(refs.dCity, a.destination.city || a.destination.name || "");
    refs.route.classList.toggle("unverified", !a.route_plausible);
    refs.unverified.classList.toggle("hidden", a.route_plausible);
  } else {
    refs.unverified.classList.add("hidden");
    setText(refs.noRoute, a.is_ga ? "Local / VFR flight — no filed route" : "Route not available");
  }

  if (a.track != null) refs.jet.style.transform = `rotate(${Math.round(a.track)}deg)`;
  // place the jet at its real position along the route (clamped off the codes)
  const pct = a.route_progress == null ? 50 : Math.min(90, Math.max(10, a.route_progress * 100));
  refs.jet.style.left = `${pct}%`;

  setHTML(refs.alt, fmtAlt(a.alt_ft, a.on_ground) + trendArrow(a.vert_rate));
  setText(refs.spd, fmtSpeed(a.gs_kt));
  const distText = a.dst_nm == null ? "—" : `${fmtDist(a.dst_nm)} ${compass(a.dir_deg)}`;
  setHTML(refs.dist, a.approaching
    ? `${esc(distText)} <span class="inbound">▾ inbound</span>` : esc(distText));
  setText(refs.look, a.on_ground ? "on ground"
    : a.elev_deg == null ? "—"
    : a.elev_deg < 1 ? "on horizon" : `${Math.round(a.elev_deg)}° up`);

  // 60°+ above the horizon means it is genuinely above your head
  const overhead = !a.on_ground && (a.elev_deg != null
    ? a.elev_deg >= 60
    : a.dst_nm != null && a.dst_nm < 2);
  el.classList.toggle("overhead", overhead);
  refs.badge.classList.toggle("hidden", !overhead || a.emergency);

  el.classList.toggle("emergency", !!a.emergency);
  refs.emerg.classList.toggle("hidden", !a.emergency);
  if (a.emergency) setText(refs.emerg, `EMERGENCY ${a.squawk || ""}`.trim());
}

/* "Next up" strip under the hero card in closest mode. */
const nextUp = document.createElement("div");
nextUp.id = "next-up";

function renderNextUp(list, closestOnly) {
  if (!closestOnly || list.length < 2) { nextUp.remove(); return; }
  const rows = list.slice(1, 4).map(a => {
    const ident = a.flight_iata || a.callsign || a.registration || a.hex.toUpperCase();
    const route = a.origin && a.destination
      ? `${a.origin.iata || a.origin.icao || "?"} → ${a.destination.iata || a.destination.icao || "?"}`
      : (a.airline || "");
    const dist = a.dst_nm == null ? "" : `${fmtDist(a.dst_nm)} ${compass(a.dir_deg)}`;
    return `<div class="next-row"><span class="n-id">${esc(ident)}</span>` +
      `<span class="n-route">${esc(route)}</span><span class="n-dist">${esc(dist)}</span></div>`;
  }).join("");
  setHTML(nextUp, `<div class="next-label">NEXT UP</div>${rows}`);
  if (board.lastElementChild !== nextUp) board.appendChild(nextUp);
}

function render(list) {
  latest = list; // radar keeps showing everything in range
  updateTrails(list);
  const closestOnly = !config || config.display_mode !== "all";
  document.body.classList.toggle("closest-mode", closestOnly);
  const shown = closestOnly ? list.slice(0, 1) : list;
  const seen = new Set();
  let anchor = null; // re-inserting an in-place node kills CSS transitions,
  for (const a of shown) { // so only move cards whose order actually changed
    seen.add(a.hex);
    let card = cards.get(a.hex);
    if (!card) {
      card = buildCard(a.hex);
      cards.set(a.hex, card);
    }
    updateCard(card, a);
    const expected = anchor ? anchor.nextElementSibling : board.firstElementChild;
    if (card.el !== expected) board.insertBefore(card.el, expected);
    anchor = card.el;
  }
  for (const [hex, card] of cards) {
    if (!seen.has(hex)) { card.el.remove(); cards.delete(hex); }
  }
  renderNextUp(list, closestOnly);
  emptyState.classList.toggle("hidden", list.length > 0);
  if (!list.length) {
    emptyState.innerHTML = `<div class="empty-icon">🌌</div>
      <div>Quiet skies — no aircraft within ${fmtDist(config ? config.radius_nm : 30)}</div>`;
    board.appendChild(emptyState);
  }
  setText(countBadge, `${list.length} in range`);
}

/* ---------------- polling ---------------- */

let polling = false; // guard: a slow response must not overlap (or outrace) a newer one

async function poll() {
  if (polling) return;
  polling = true;
  try {
    const usingDevice = locSource === "device";
    if (usingDevice) startDeviceLocation();
    const body = usingDevice && devicePos ? { lat: devicePos.lat, lon: devicePos.lon } : {};
    const r = await fetch("/api/aircraft", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      redirect: "manual",
    });
    if (r.type === "opaqueredirect") { // Cloudflare Access session expired
      showBanner("Sign-in expired — reload this page to sign in again.");
      return;
    }
    const data = await r.json();
    if (data.error === "not configured") {
      showBanner("Set your location to start tracking — open ⚙ settings.");
      if (!poll.autoOpened) { poll.autoOpened = true; openSettings(); }
      return;
    }
    if (data.error) {
      showBanner(`Live feed problem: ${data.error} — showing last known data.`);
      return;
    }
    if (usingDevice && !devicePos && geoError) {
      showBanner(`Device location unavailable (${geoError}) — showing home instead.`);
      render(data.aircraft || []);
      return;
    }
    hideBanner();
    render(data.aircraft);
  } catch (e) {
    showBanner("Cannot reach the FlightWall server — is server.py still running?");
  } finally {
    polling = false;
  }
}

function startPolling() {
  if (pollTimer) clearInterval(pollTimer);
  const secs = config && config.refresh_seconds ? config.refresh_seconds : 8;
  pollTimer = setInterval(poll, secs * 1000);
  poll();
}

function showBanner(msg) { banner.textContent = msg; banner.classList.remove("hidden"); }
function hideBanner() { banner.classList.add("hidden"); }

/* ---------------- header: clock + location ---------------- */

function tickClock() {
  const now = new Date();
  document.getElementById("clock").textContent = now.toLocaleTimeString([], { hour12: false });
  document.getElementById("date-line").textContent =
    now.toLocaleDateString([], { weekday: "long", month: "long", day: "numeric" });
}
setInterval(tickClock, 1000);
tickClock();

function updateLocationLine() {
  if (locSource === "device") {
    const radius = config ? ` · ${fmtDist(config.radius_nm)} radius` : "";
    setText(locationLine, devicePos
      ? `your location (GPS)${radius}`
      : `waiting for GPS… ${geoError ? `(${geoError})` : ""}`);
    return;
  }
  if (!config || config.lat == null) { setText(locationLine, "location not set"); return; }
  const name = config.location_name || `${config.lat.toFixed(3)}, ${config.lon.toFixed(3)}`;
  setText(locationLine, `${name} · ${fmtDist(config.radius_nm)} radius`);
}

/* ---------------- radar (map scope) ---------------- */

const radar = document.getElementById("radar");
const rctx = radar.getContext("2d");
let sweep = 0;

// The scope shows at most this range so the map stays usefully zoomed in;
// aircraft further out (but inside the search radius) clamp to the rim.
const RADAR_RANGE_NM = 20;

const tileCache = new Map(); // "z/x/y" -> Image (dark CARTO basemap tiles)

function tileFor(z, x, y) {
  const key = `${z}/${x}/${y}`;
  let img = tileCache.get(key);
  if (!img) {
    if (tileCache.size > 160) tileCache.clear();
    img = new Image();
    img.crossOrigin = "anonymous";
    img.onerror = () => tileCache.delete(key); // retry on a later frame
    img.src = `https://${"abcd"[(x + y) % 4]}.basemaps.cartocdn.com/dark_all/${z}/${x}/${y}.png`;
    tileCache.set(key, img);
  }
  return img;
}

function radarCenter() {
  if (locSource === "device" && devicePos) return devicePos;
  if (config && config.lat != null) return { lat: config.lat, lon: config.lon };
  return null;
}

/* Trails: the last few minutes of each aircraft's positions, drawn as a
   fading tail so approach paths sweep visibly across the map. */
const TRAIL_MS = 4 * 60 * 1000;
const trails = new Map(); // hex -> [{lat, lon, t}]

function updateTrails(list) {
  const now = Date.now();
  for (const a of list) {
    if (a.lat == null || a.lon == null) continue;
    let tr = trails.get(a.hex);
    if (!tr) { tr = []; trails.set(a.hex, tr); }
    const last = tr[tr.length - 1];
    if (!last || last.lat !== a.lat || last.lon !== a.lon) {
      tr.push({ lat: a.lat, lon: a.lon, t: now });
    }
    while (tr.length > 48 || (tr.length && now - tr[0].t > TRAIL_MS)) tr.shift();
  }
  for (const [hex, tr] of trails) { // forget aircraft that left the feed
    if (!tr.length || now - tr[tr.length - 1].t > TRAIL_MS) trails.delete(hex);
  }
}

function drawTrails(center, rangeNm, cx, cy, R) {
  const now = Date.now();
  const nmPerDegLon = 60 * Math.cos(center.lat * Math.PI / 180);
  const closestHex = latest.length ? latest[0].hex : null;
  rctx.save();
  rctx.beginPath(); rctx.arc(cx, cy, R, 0, Math.PI * 2); rctx.clip();
  rctx.lineWidth = 2.5;
  rctx.lineCap = "round";
  for (const [hex, tr] of trails) {
    if (tr.length < 2) continue;
    const rgb = hex === closestHex ? "255,200,87" : "90,209,255";
    let prev = null;
    for (const p of tr) {
      const x = cx + ((p.lon - center.lon) * nmPerDegLon / rangeNm) * R;
      const y = cy - ((p.lat - center.lat) * 60 / rangeNm) * R;
      if (prev) {
        const age = Math.min(1, (now - p.t) / TRAIL_MS);
        rctx.strokeStyle = `rgba(${rgb},${(0.55 * (1 - age)).toFixed(3)})`;
        rctx.beginPath(); rctx.moveTo(prev.x, prev.y); rctx.lineTo(x, y); rctx.stroke();
      }
      prev = { x, y };
    }
  }
  rctx.restore();
}

function drawMap(center, rangeNm, w, cx, cy, R) {
  // meters per canvas pixel so the map scale matches the blip scale exactly
  const mpp = (rangeNm * 1852) / R;
  const latRad = center.lat * Math.PI / 180;
  const z = Math.max(3, Math.min(17, Math.round(
    Math.log2(156543.03392 * Math.cos(latRad) / mpp))));
  const scale = (156543.03392 * Math.cos(latRad) / Math.pow(2, z)) / mpp;
  const n = Math.pow(2, z);
  const wx = (center.lon + 180) / 360 * 256 * n;
  const wy = (0.5 - Math.log(Math.tan(Math.PI / 4 + latRad / 2)) / (2 * Math.PI)) * 256 * n;
  rctx.save();
  rctx.beginPath(); rctx.arc(cx, cy, R, 0, Math.PI * 2); rctx.clip();
  const tx0 = Math.floor((wx - cx / scale) / 256), tx1 = Math.floor((wx + cx / scale) / 256);
  const ty0 = Math.floor((wy - cy / scale) / 256), ty1 = Math.floor((wy + cy / scale) / 256);
  for (let tx = tx0; tx <= tx1; tx++) {
    for (let ty = Math.max(0, ty0); ty <= Math.min(n - 1, ty1); ty++) {
      const img = tileFor(z, ((tx % n) + n) % n, ty);
      if (img.complete && img.naturalWidth) {
        rctx.drawImage(img,
          cx + (tx * 256 - wx) * scale, cy + (ty * 256 - wy) * scale,
          256 * scale + 0.5, 256 * scale + 0.5);
      }
    }
  }
  rctx.fillStyle = "rgba(7, 11, 22, 0.38)"; // dim so blips stay readable
  rctx.fillRect(0, 0, w, w);
  rctx.restore();
}

function drawRadar() {
  const w = radar.width, cx = w / 2, cy = w / 2, R = w / 2 - 8;
  rctx.clearRect(0, 0, w, w);
  const center = radarCenter();
  const rangeNm = Math.min(config ? config.radius_nm : 30, RADAR_RANGE_NM);
  if (center) {
    drawMap(center, rangeNm, w, cx, cy, R);
    drawTrails(center, rangeNm, cx, cy, R);
  }

  rctx.strokeStyle = "#1f2b4daa";
  rctx.lineWidth = 2;
  for (const f of [0.5, 1]) {
    rctx.beginPath(); rctx.arc(cx, cy, R * f, 0, Math.PI * 2); rctx.stroke();
  }
  rctx.fillStyle = "#8b98b8aa";
  rctx.font = "20px Consolas, monospace";
  rctx.textAlign = "center";
  rctx.fillText("N", cx, cy - R + 24);
  rctx.fillText(`${rangeNm} nm`, cx, cy + R - 12);

  // sweep
  sweep = (sweep + 0.02) % (Math.PI * 2);
  const grad = rctx.createConicGradient
    ? rctx.createConicGradient(sweep, cx, cy)
    : null;
  if (grad) {
    grad.addColorStop(0, "rgba(90,209,255,0.28)");
    grad.addColorStop(0.12, "rgba(90,209,255,0)");
    grad.addColorStop(1, "rgba(90,209,255,0)");
    rctx.fillStyle = grad;
    rctx.beginPath(); rctx.moveTo(cx, cy); rctx.arc(cx, cy, R, 0, Math.PI * 2); rctx.fill();
  }

  // home dot
  rctx.fillStyle = "#ffc857";
  rctx.beginPath(); rctx.arc(cx, cy, 5, 0, Math.PI * 2); rctx.fill();

  // blips (same scale as the map, so they sit over the real streets below)
  latest.forEach((a, i) => {
    if (a.dst_nm == null || a.dir_deg == null) return;
    const r = Math.min(a.dst_nm / rangeNm, 1) * R;
    const ang = (a.dir_deg - 90) * Math.PI / 180;
    const x = cx + Math.cos(ang) * r, y = cy + Math.sin(ang) * r;
    rctx.fillStyle = i === 0 ? "#ffc857" : "#5ad1ff";
    rctx.beginPath(); rctx.arc(x, y, i === 0 ? 6 : 4, 0, Math.PI * 2); rctx.fill();
  });

  requestAnimationFrame(drawRadar);
}
requestAnimationFrame(drawRadar);

/* ---------------- settings modal ---------------- */

const modal = document.getElementById("settings-modal");
const modalError = document.getElementById("modal-error");

function openSettings() {
  if (!config) return;
  document.getElementById("set-lat").value = config.lat ?? "";
  document.getElementById("set-lon").value = config.lon ?? "";
  document.getElementById("set-name").value = config.location_name || "";
  document.getElementById("set-radius").value = config.radius_nm;
  document.getElementById("set-refresh").value = config.refresh_seconds;
  document.getElementById("set-units").value = config.units;
  document.getElementById("set-display").value = config.display_mode || "closest";
  const locSel = document.getElementById("set-locsource");
  locSel.value = locSource;
  const unavailable = geoAvailable();
  locSel.options[1].disabled = !!unavailable;
  locSel.options[1].textContent = unavailable
    ? `This device — GPS (${unavailable})` : "This device — GPS";
  modalError.classList.add("hidden");
  modal.classList.remove("hidden");
}
function closeSettings() { modal.classList.add("hidden"); }

document.getElementById("gear-btn").addEventListener("click", openSettings);
document.getElementById("btn-cancel").addEventListener("click", closeSettings);
modal.addEventListener("click", e => { if (e.target === modal) closeSettings(); });

document.getElementById("btn-save").addEventListener("click", async () => {
  const body = {
    lat: parseFloat(document.getElementById("set-lat").value),
    lon: parseFloat(document.getElementById("set-lon").value),
    location_name: document.getElementById("set-name").value,
    radius_nm: parseFloat(document.getElementById("set-radius").value),
    refresh_seconds: parseInt(document.getElementById("set-refresh").value, 10),
    units: document.getElementById("set-units").value,
    display_mode: document.getElementById("set-display").value,
  };
  // blank fields mean "keep the current value" (NaN would reach the server
  // as null and fail the whole save); both-blank lat/lon is allowed so GPS
  // mode can be enabled even when no home location was ever saved
  const latRaw = document.getElementById("set-lat").value.trim();
  const lonRaw = document.getElementById("set-lon").value.trim();
  if (latRaw === "" && lonRaw === "") {
    delete body.lat;
    delete body.lon;
  } else if (Number.isNaN(body.lat) || Number.isNaN(body.lon)) {
    modalError.textContent = "Latitude and longitude are required.";
    modalError.classList.remove("hidden");
    return;
  }
  if (!Number.isFinite(body.radius_nm)) delete body.radius_nm;
  if (!Number.isFinite(body.refresh_seconds)) delete body.refresh_seconds;
  const r = await fetch("/api/config", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    modalError.textContent = "Server rejected those values.";
    modalError.classList.remove("hidden");
    return;
  }
  config = await r.json();
  if (config.persisted === false) {
    showBanner("Settings applied, but the server could not write config.json — "
      + "they will reset on restart. Check that the data folder is writable.");
  }
  const newSource = document.getElementById("set-locsource").value;
  if (newSource !== locSource) {
    locSource = newSource;
    localStorage.setItem(LOC_KEY, locSource);
    if (locSource === "device") startDeviceLocation(); else stopDeviceLocation();
  }
  closeSettings();
  updateLocationLine();
  startPolling();
});

document.getElementById("btn-uselocation").addEventListener("click", async () => {
  modalError.classList.add("hidden");
  const r = await fetch("/api/locate", { method: "POST" });
  if (!r.ok) {
    modalError.textContent = "IP geolocation failed — enter coordinates manually.";
    modalError.classList.remove("hidden");
    return;
  }
  config = await r.json();
  if (config.persisted === false) {
    showBanner("Settings applied, but the server could not write config.json — "
      + "they will reset on restart. Check that the data folder is writable.");
  }
  // the server already saved the detected location, so reflect it everywhere
  updateLocationLine();
  startPolling();
  openSettings(); // repopulate fields with detected values
});

/* ---------------- kiosk mode (?kiosk=1) ---------------- */

if (new URLSearchParams(location.search).has("kiosk")) {
  document.body.classList.add("kiosk");

  // burn-in protection: drift the whole page a couple of px on a slow cycle
  const drift = [[0, 0], [2, 1], [1, 2], [-1, 1], [-2, -1], [0, -2], [1, -1]];
  let di = 0;
  setInterval(() => {
    di = (di + 1) % drift.length;
    document.body.style.transform = `translate(${drift[di][0]}px, ${drift[di][1]}px)`;
  }, 60000);

  // hide the cursor after a few idle seconds
  let cursorTimer = setTimeout(() => { document.body.style.cursor = "none"; }, 5000);
  document.addEventListener("mousemove", () => {
    document.body.style.cursor = "";
    clearTimeout(cursorTimer);
    cursorTimer = setTimeout(() => { document.body.style.cursor = "none"; }, 5000);
  });

  // keep the display awake where the browser allows it (needs HTTPS/localhost)
  if ("wakeLock" in navigator) {
    const wake = () => navigator.wakeLock.request("screen").catch(() => {});
    wake();
    document.addEventListener("visibilitychange", () => { if (!document.hidden) wake(); });
  }

  // dim the wall overnight (23:00-06:00)
  const night = () => {
    const h = new Date().getHours();
    document.body.classList.toggle("night", h >= 23 || h < 6);
  };
  night();
  setInterval(night, 60000);
}

/* ---------------- boot ---------------- */

(async function boot() {
  try {
    const r = await fetch("/api/config");
    config = await r.json();
  } catch (e) {
    showBanner("Cannot reach the FlightWall server — retrying…");
    setTimeout(boot, 5000); // page opened before server.py: keep retrying
    return;
  }
  if (locSource === "device") startDeviceLocation();
  updateLocationLine();
  if (!config.configured && locSource !== "device") {
    showBanner("Set your location to start tracking.");
    openSettings();
  }
  startPolling();
})();
