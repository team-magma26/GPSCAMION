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
// "Seguir bus" sigue al bus elegido (focusBus): se elige tocándolo en el mapa o en la lista de estado
function setFollow(on) {
  followTruck = on;
  followBtn.classList.toggle("active", on);
  followBtn.setAttribute("aria-pressed", String(on));
  const b = getBus(focusBus);
  if (on && !historyMode && b.hasPos) map.panTo(b.marker.getLatLng());
}
function focusOn(id) {
  focusBus = getBus(id).id;
  setFollow(true);
  refreshTruckStatus();
}
setFollow(true);
map.on("dragstart", () => setFollow(false));
$("clearTrail").addEventListener("click", () => busList().forEach((b) => b.trail.setLatLngs([])));

// "Última ubicación recibida": la del bus que mandó más reciente
function updateFields(data) {
  $("lat").textContent = data.lat !== null ? data.lat : "--";
  $("lon").textContent = data.lon !== null ? data.lon : "--";
  $("date").textContent = data.date !== null ? data.date : "--";
  $("time").textContent = data.time !== null ? data.time : "--";
  if (MULTI_BUS && data.bus) $("lastBusName").textContent = `· ${getBus(data.bus).name}`;
}

function updateMap(lat, lon, addToTrail = true, busId = null) {
  const b = getBus(busId);
  const point = [lat, lon];
  b.marker.setLatLng(point);
  b.hasPos = true;
  if (view === "live" && !historyMode && !map.hasLayer(b.marker)) b.marker.addTo(map);
  if (addToTrail) b.trail.addLatLng(point);
  if (!mapCentered) {
    map.setView(point, 15);
    mapCentered = true;
  } else if (followTruck && b.id === focusBus) {
    map.panTo(point, { animate: true, duration: 0.5 });
  }
}

// Recorrido actual de cada bus (al abrir la página y al volver a "En vivo")
function loadLiveHistory(fit) {
  return Promise.all(busList().map((b) =>
    fetch(`/api/history?bus=${encodeURIComponent(b.id)}`)
      .then((r) => r.json())
      .then((points) => {
        b.lastRoute.start = points.length ? points[0].time : null;
        b.lastRoute.end = points.length ? points[points.length - 1].time : null;
        if (historyMode) return;
        b.trail.setLatLngs(points.map((p) => [p.lat, p.lon]));
        if (points.length) {
          const last = points[points.length - 1];
          b.marker.setLatLng([last.lat, last.lon]);
          b.hasPos = true;
        }
      })
      .catch(() => {})
  )).then(() => {
    if (historyMode) return;
    setLiveBusesVisible(true);
    refreshTruckStatus();
    if (fit) fitLive();
  });
}

// Última ubicación de cada bus. Si es reciente (menos de 1 min), cuenta como "en vivo":
// así no aparece el aviso "sin señal" al abrir la página o al volver a ella.
function loadLatest() {
  return fetch("/api/latest")
    .then((r) => r.json())
    .then((data) => {
      (data.buses || []).forEach((it) => {
        if (it.lat === null) return;
        const b = getBus(it.bus);
        if (!historyMode) updateMap(it.lat, it.lon, false, it.bus);
        if (!b.lastRoute.end && it.date) b.lastRoute.end = `${it.date} ${it.time}`;
        if (it.age_s !== null && it.age_s !== undefined && it.age_s * 1000 <= SIGNAL_TIMEOUT_MS) {
          b.lastMsgAt = Math.max(b.lastMsgAt, Date.now() - it.age_s * 1000);
        }
      });
      if (data.lat !== null && data.lat !== undefined) updateFields(data);
      refreshTruckStatus();
    })
    .catch(() => {});
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


// ---------- Estado de cada bus ----------
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

const isFresh = (b) => b.lastMsgAt && Date.now() - b.lastMsgAt <= SIGNAL_TIMEOUT_MS;
function lastSeenAt(b) { return b.lastMsgAt || parseTime(b.lastRoute.end); }

// Estado de un bus: en movimiento / detenido / en línea / sin señal / sin datos
function busStatus(b) {
  if (!isFresh(b)) {
    b.recentPoints.length = 0;
    if (!b.lastRoute.end) return { state: "off", title: "Sin datos aún", sub: "Todavía no ha enviado su ubicación" };
    const parts = [];
    const t0 = parseTime(b.lastRoute.start), t1 = parseTime(b.lastRoute.end);
    if (t0 !== null && t1 !== null && t1 > t0) parts.push(`última ruta de ${fmtDuration(t1 - t0)}`);
    parts.push("última lectura " + fmtLast(b.lastRoute.end));
    return { state: "last", title: "Sin señal", sub: parts.join(" · ") };
  }
  if (b.recentPoints.length < 2) return { state: "on", title: "En línea", sub: "Recibiendo datos" };
  const a = b.recentPoints[0], z = b.recentPoints[b.recentPoints.length - 1];
  const hours = (z.t - a.t) / 3600000;
  const kmh = hours > 0 ? distM([a.lat, a.lon], [z.lat, z.lon]) / 1000 / hours : 0;
  return kmh >= MOVING_KMH
    ? { state: "move", title: "En movimiento", sub: `${Math.round(kmh)} km/h` }
    : { state: "stop", title: "Detenido", sub: "Sin desplazamiento" };
}

let sseDown = false; // true si se cayó la conexión con el servidor

// Lista de estado (un renglón por bus; tocarlo centra el mapa en ese bus) + indicador superior + aviso del mapa
function refreshTruckStatus() {
  const list = $("busStatusList");
  list.innerHTML = busList().map((b) => {
    const st = busStatus(b);
    const name = MULTI_BUS ? `<span class="bus-swatch" style="background:${b.color}"></span>${b.name}` : st.title;
    const sub = MULTI_BUS ? `<b>${st.title}</b> · ${st.sub}` : st.sub;
    const sel = MULTI_BUS && b.id === focusBus ? " selected" : "";
    return `<button type="button" class="truck-status${sel}" data-state="${st.state}" data-bus="${b.id}"
      title="Centrar el mapa en ${b.name}"><span class="ts-dot"></span>
      <div><div class="ts-title">${name}</div><div class="ts-sub">${sub}</div></div></button>`;
  }).join("");
  list.querySelectorAll("[data-bus]").forEach((el) => el.addEventListener("click", () => focusOn(el.dataset.bus)));

  const fresh = busList().some(isFresh);
  const seen = Math.max(0, ...busList().map((b) => lastSeenAt(b) || 0)) || null;
  const ago = seen ? fmtAgo(Date.now() - seen) : null;
  $("agoLine").innerHTML = seen ? `Recibida <b>${ago}</b>` : "Aún no hay datos";
  if (!sseDown) {
    $("dot").classList.toggle("live", fresh);
    $("status").textContent = fresh ? `En vivo — último dato ${ago}`
      : seen ? `Sin datos de los buses · último ${ago}` : "Esperando datos";
  }
  // Aviso sobre el mapa: solo si NINGÚN bus está mandando
  const show = !fresh && view === "live";
  $("noDataAlert").classList.toggle("d-none", !show);
  if (show) {
    $("noDataTitle").textContent = MULTI_BUS ? "Los buses están sin señal" : "El bus está sin señal";
    $("noDataText").textContent = seen
      ? `Último dato ${ago}. Te mostramos su última ruta; el mapa se actualiza solo cuando vuelvan.`
      : "Todavía no han enviado ninguna ubicación.";
  }
}

function trackTruck(lat, lon, data) {
  const b = getBus(data && data.bus);
  const now = Date.now();
  b.lastMsgAt = now;
  if (data && data.date) {
    b.lastRoute.end = `${data.date} ${data.time}`;
    if (!b.lastRoute.start) b.lastRoute.start = b.lastRoute.end;
  }
  b.recentPoints.push({ t: now, lat: Number(lat), lon: Number(lon) });
  while (b.recentPoints.length > 1 && now - b.recentPoints[0].t > 60000) b.recentPoints.shift();
  refreshTruckStatus();
}
