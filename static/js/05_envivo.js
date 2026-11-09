// OnRoute · En vivo: botones del mapa, suavizado del trazo y estado del bus

// ---------- En vivo ----------
// Botón "Seguir bus" dentro del mapa (antes estaba en el panel y en celular quedaba lejos)
const FollowControl = L.Control.extend({
  options: { position: "topright" },
  onAdd() {
    const b = L.DomUtil.create("button", "map-follow-btn active");
    b.type = "button";
    b.title = "Mantiene el mapa centrado en el bus. Se apaga solo si mueves el mapa.";
    b.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><circle cx="12" cy="12" r="7"/><circle cx="12" cy="12" r="2.5" fill="currentColor"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3"/></svg><span>Seguir bus</span>';
    L.DomEvent.disableClickPropagation(b);
    L.DomEvent.on(b, "click", () => setFollow(!followTruck));
    return b;
  },
});
const followBtn = new FollowControl().addTo(map).getContainer();
const MeControl = L.Control.extend({
  options: { position: "topright" },
  onAdd() {
    const b = L.DomUtil.create("button", "map-follow-btn d-none");
    b.type = "button";
    b.id = "meBtn";
    b.title = "Centrar el mapa en tu ubicación";
    b.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="4" fill="currentColor"/><circle cx="12" cy="12" r="8"/></svg><span>Mi ubicación</span>';
    L.DomEvent.disableClickPropagation(b);
    L.DomEvent.on(b, "click", () => {
      if (!userPos) return;
      setFollow(false);
      map.setView([userPos.lat, userPos.lon], Math.max(map.getZoom(), 16));
    });
    return b;
  },
});
new MeControl().addTo(map);
function setFollow(on) {
  followTruck = on;
  followBtn.classList.toggle("active", on);
  followBtn.setAttribute("aria-pressed", String(on));
  if (on && !historyMode) map.panTo(marker.getLatLng());
}
setFollow(true);
map.on("dragstart", () => setFollow(false));
$("clearTrail").addEventListener("click", () => trail.setLatLngs([]));

function updateFields(data) {
  $("lat").textContent = data.lat !== null ? data.lat : "--";
  $("lon").textContent = data.lon !== null ? data.lon : "--";
  $("date").textContent = data.date !== null ? data.date : "--";
  $("time").textContent = data.time !== null ? data.time : "--";
}

function updateMap(lat, lon, addToTrail = true) {
  const point = [lat, lon];
  marker.setLatLng(point);
  if (addToTrail) trail.addLatLng(point);
  if (!mapCentered) {
    map.setView(point, 15);
    mapCentered = true;
  } else if (followTruck) {
    map.panTo(point, { animate: true, duration: 0.5 });
  }
}

// Suavizado Catmull-Rom para que el GPS "en zigzag" se vea como curva natural.
function catmullRomSpline(points, segments = 8) {
  if (points.length < 3) return points;
  const result = [];
  for (let i = 0; i < points.length - 1; i++) {
    const p0 = points[i === 0 ? i : i - 1];
    const p1 = points[i];
    const p2 = points[i + 1];
    const p3 = points[i + 2 < points.length ? i + 2 : i + 1];
    for (let t = 0; t < segments; t++) {
      const st = t / segments, st2 = st * st, st3 = st2 * st;
      const lat = 0.5 * (2 * p1[0] + (-p0[0] + p2[0]) * st +
        (2 * p0[0] - 5 * p1[0] + 4 * p2[0] - p3[0]) * st2 +
        (-p0[0] + 3 * p1[0] - 3 * p2[0] + p3[0]) * st3);
      const lon = 0.5 * (2 * p1[1] + (-p0[1] + p2[1]) * st +
        (2 * p0[1] - 5 * p1[1] + 4 * p2[1] - p3[1]) * st2 +
        (-p0[1] + 3 * p1[1] - 3 * p2[1] + p3[1]) * st3);
      result.push([lat, lon]);
    }
  }
  result.push(points[points.length - 1]);
  return result;
}


// ---------- Estado del bus ----------
const recentPoints = []; // últimas lecturas recibidas en vivo
let lastMsgAt = 0;
const lastRoute = { start: null, end: null }; // inicio y fin (gps_time) de la ruta más reciente

function setLastRouteFromPoints(points) {
  lastRoute.start = points.length ? points[0].time : null;
  lastRoute.end = points.length ? points[points.length - 1].time : null;
}

function showLastRoute() {
  if (!lastRoute.end) { setTruckStatus("off", "Sin recorridos aún", "Esperando datos del bus"); return; }
  const parts = [];
  const t0 = parseTime(lastRoute.start), t1 = parseTime(lastRoute.end);
  if (t0 !== null && t1 !== null && t1 >= t0) parts.push(fmtDuration(t1 - t0));
  const pts = trail.getLatLngs();
  if (pts.length > 1) {
    let d = 0;
    for (let i = 1; i < pts.length; i++) d += distM([pts[i - 1].lat, pts[i - 1].lng], [pts[i].lat, pts[i].lng]);
    parts.push(fmtDistance(d));
  }
  parts.push("última lectura " + fmtLast(lastRoute.end));
  setTruckStatus("last", "Última ruta del bus", parts.join(" · "));
}

function setTruckStatus(state, title, sub) {
  $("truckStatus").dataset.state = state;
  $("truckStatusTitle").textContent = title;
  $("truckStatusSub").textContent = sub;
}

// "hace 5 min", "hace 3 días"...
function fmtAgo(ms) {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return `hace ${s} s`;
  const m = Math.round(s / 60);
  if (m < 60) return `hace ${m} min`;
  const h = Math.round(m / 60);
  if (h < 24) return `hace ${h} h`;
  const d = Math.round(h / 24);
  return `hace ${d} día${d > 1 ? "s" : ""}`;
}

let sseDown = false; // true si se cayó la conexión con el servidor
function lastSeenAt() { return lastMsgAt || parseTime(lastRoute.end); }

// Indicador superior, "Recibida hace X" y aviso sobre el mapa cuando el bus deja de enviar datos
function updateSignalUI(fresh) {
  const seen = lastSeenAt();
  const ago = seen ? fmtAgo(Date.now() - seen) : null;
  $("agoLine").innerHTML = seen ? `Recibida <b>${ago}</b>` : "Aún no hay datos del bus";
  if (!sseDown) {
    $("dot").classList.toggle("live", fresh);
    $("status").textContent = fresh ? `En vivo — último dato ${ago}`
      : seen ? `Sin datos del bus · último ${ago}` : "Esperando datos del bus";
  }
  const show = !fresh && view === "live";
  $("noDataAlert").classList.toggle("d-none", !show);
  if (show) {
    $("noDataText").textContent = seen
      ? `Último dato ${ago}. Te mostramos su última ruta; el mapa se actualiza solo cuando vuelva.`
      : "Todavía no ha enviado ninguna ubicación.";
  }
}

function refreshTruckStatus() {
  if (!lastMsgAt || Date.now() - lastMsgAt > SIGNAL_TIMEOUT_MS) {
    recentPoints.length = 0;
    showLastRoute();
    updateSignalUI(false);
    return;
  }
  updateSignalUI(true);
  if (recentPoints.length < 2) { setTruckStatus("on", "En línea", "Recibiendo datos"); return; }
  const a = recentPoints[0], b = recentPoints[recentPoints.length - 1];
  const hours = (b.t - a.t) / 3600000;
  const kmh = hours > 0 ? distM([a.lat, a.lon], [b.lat, b.lon]) / 1000 / hours : 0;
  if (kmh >= MOVING_KMH) setTruckStatus("move", "En movimiento", `${Math.round(kmh)} km/h`);
  else setTruckStatus("stop", "Detenido", "Sin desplazamiento");
}

function trackTruck(lat, lon, data) {
  const now = Date.now();
  lastMsgAt = now;
  if (data && data.date) {
    lastRoute.end = `${data.date} ${data.time}`;
    if (!lastRoute.start) lastRoute.start = lastRoute.end;
  }
  recentPoints.push({ t: now, lat: Number(lat), lon: Number(lon) });
  while (recentPoints.length > 1 && now - recentPoints[0].t > 60000) recentPoints.shift();
  refreshTruckStatus();
}
