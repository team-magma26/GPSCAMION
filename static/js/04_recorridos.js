// OnRoute · Histórico: rutas en el mapa, simulador de recorrido y tabla de recorridos

// ---------- Rutas históricas en el mapa ----------
function endpointIcon(letter, color) {
  return L.divIcon({
    className: "",
    html: `<div class="route-endpoint" style="background:${color}">${letter}</div>`,
    iconSize: [24, 24], iconAnchor: [12, 12],
  });
}

function drawRoute(route) {
  const latlngs = route.points.map((p) => [p.lat, p.lon]);
  route.polyline = L.polyline(catmullRomSpline(latlngs, 8), {
    color: route.color, weight: 4, opacity: 0.9, smoothFactor: 1.5,
  }).addTo(routesLayer);

  if (route.points.length > 0) {
    const first = route.points[0];
    const last = route.points[route.points.length - 1];
    route.startMarker = L.marker([first.lat, first.lon], { icon: endpointIcon("A", route.color) })
      .bindPopup(`<strong>Inicio</strong><br>${first.time}<br>Lat: ${first.lat.toFixed(5)}, Lon: ${first.lon.toFixed(5)}`)
      .addTo(routesLayer);
    route.endMarker = L.marker([last.lat, last.lon], { icon: endpointIcon("B", route.color) })
      .bindPopup(`<strong>Fin</strong><br>${last.time}<br>Lat: ${last.lat.toFixed(5)}, Lon: ${last.lon.toFixed(5)}`)
      .addTo(routesLayer);
  }
}

function undrawRoute(route) {
  if (route.polyline) routesLayer.removeLayer(route.polyline);
  if (route.startMarker) routesLayer.removeLayer(route.startMarker);
  if (route.endMarker) routesLayer.removeLayer(route.endMarker);
  route.polyline = route.startMarker = route.endMarker = null;
}

function setRouteVisible(sessionId, desiredVisible) {
  const route = routesBySession.get(sessionId);
  if (!route) return;
  route.visible = desiredVisible;

  if (!desiredVisible) { undrawRoute(route); updateHistoryViewState(); return; }
  if (route.loaded) {
    if (!route.polyline) drawRoute(route);
    updateHistoryViewState();
    return;
  }
  route.failed = false;
  loadRoutePoints(sessionId);
}

function loadRoutePoints(sessionId) {
  const route = routesBySession.get(sessionId);
  if (!route || route.loaded || route.loading || route.failed) return;
  route.loading = true;
  fetch(`/api/session_route?session_id=${encodeURIComponent(sessionId)}`)
    .then((r) => r.json())
    .then((data) => {
      route.points = data.points || [];
      route.loaded = true;
      route.loading = false;
      if (route.visible) { drawRoute(route); updateHistoryViewState(); }
      updateRouteStats(sessionId);
    })
    .catch(() => { route.loading = false; route.failed = true; updateRouteStats(sessionId); });
}

function routeMetaText(route, s) {
  if (route.failed) return "";
  if (!route.loaded) return "Calculando…";
  const pts = route.points;
  let d = 0;
  for (let i = 1; i < pts.length; i++) d += distM([pts[i - 1].lat, pts[i - 1].lon], [pts[i].lat, pts[i].lon]);
  let t0 = parseTime(s.start_time), t1 = parseTime(s.end_time);
  if ((t0 === null || t1 === null || t1 < t0) && pts.length > 1) {
    t0 = parseTime(pts[0].time); t1 = parseTime(pts[pts.length - 1].time);
  }
  const parts = [];
  if (t0 !== null && t1 !== null && t1 >= t0) parts.push(fmtDuration(t1 - t0));
  parts.push(fmtDistance(d));
  return parts.join(" · ");
}

function updateRouteStats(sessionId) {
  const route = routesBySession.get(sessionId);
  const s = sessionsList.find((x) => String(x.session_id) === String(sessionId));
  if (!route || !s) return;
  routesTableBody.querySelectorAll("tr[data-session-id]").forEach((tr) => {
    if (tr.dataset.sessionId === String(sessionId)) {
      const el = tr.querySelector(".route-meta");
      if (el) el.textContent = routeMetaText(route, s);
    }
  });
}

function anyRouteVisible() {
  for (const route of routesBySession.values()) if (route.visible) return true;
  return false;
}

function updateHistoryViewState() {
  const banner = $("historyBanner");
  if (view === "routes" && routesBySession.size === 0 && currentFilterLabel) {
    banner.classList.remove("d-none");
    banner.textContent = `No hay recorridos guardados entre ${currentFilterLabel}`;
  } else {
    banner.classList.add("d-none");
  }

  if (anyRouteVisible()) {
    busList().forEach((b) => b.trail.setLatLngs([]));
    const visiblePolylines = [...routesBySession.values()].filter((r) => r.visible && r.polyline).map((r) => r.polyline);
    if (visiblePolylines.length > 0) {
      map.fitBounds(L.featureGroup(visiblePolylines).getBounds(), { maxZoom: 15, padding: [20, 20] });
    }
  }
  refreshSimPanel();
}

// ---------- Simulador de recorrido ----------
const simMarker = L.marker([0, 0], { icon: mkIcon("sim"), zIndexOffset: 1000 });
const simTrail = L.polyline([], { color: "#ffffff", weight: 5, opacity: 0.9 }).addTo(map);
const sim = { route: null, id: null, pos: 0, playing: false, last: 0, raf: null };

function getRouteByKey(key) {
  for (const [k, r] of routesBySession) if (String(k) === String(key)) return r;
  return null;
}

function refreshSimPanel() {
  const list = [...routesBySession.entries()].filter(([, r]) => r.visible && r.loaded && r.points.length > 0);
  const panel = $("simPanel");

  if (list.length === 0) {
    panel.classList.add("d-none");
    stopSim();
    simTrail.setLatLngs([]);
    if (map.hasLayer(simMarker)) map.removeLayer(simMarker);
    sim.route = sim.id = null;
    return;
  }

  panel.classList.remove("d-none");
  const select = $("simRoute");
  select.innerHTML = list.map(([k]) => {
    const s = sessionsList.find((x) => String(x.session_id) === String(k));
    return `<option value="${k}">${s ? s.start_time + " → " + s.end_time : k}</option>`;
  }).join("");

  const keep = list.some(([k]) => String(k) === String(sim.id)) ? String(sim.id) : String(list[0][0]);
  select.value = keep;
  if (keep !== String(sim.id)) setSimRoute(keep);
}

function setSimRoute(key) {
  stopSim();
  sim.id = String(key);
  sim.route = getRouteByKey(key);
  sim.pos = 0;
  if (!map.hasLayer(simMarker)) simMarker.addTo(map);
  renderSim();
}

function renderSim() {
  if (!sim.route) return;
  const pts = sim.route.points;
  const n = pts.length;
  // El slider avanza punto por punto (lecturas reales del GPS), sin posiciones intermedias inventadas
  const i = Math.min(Math.max(Math.floor(sim.pos), 0), n - 1);
  const a = pts[i];
  const lat = a.lat, lon = a.lon;

  simMarker.setLatLng([lat, lon]);
  simTrail.setLatLngs(pts.slice(0, i + 1).map((p) => [p.lat, p.lon]));
  $("simSlider").max = Math.max(n - 1, 0);
  $("simSlider").value = i;
  $("simSlider").setAttribute("aria-valuetext", `Punto ${i + 1} de ${n}, ${a.time}`);
  $("simInfo").textContent = `${a.time} · punto ${i + 1} de ${n}`;
  if (!map.getBounds().contains([lat, lon])) map.panTo([lat, lon]);
}

function simTick(ts) {
  if (!sim.playing) return;
  const dt = (ts - sim.last) / 1000;
  sim.last = ts;
  sim.pos += dt * SIM_POINTS_PER_SEC * Number($("simSpeed").value);
  const end = sim.route.points.length - 1;
  if (sim.pos >= end) { sim.pos = end; renderSim(); stopSim(); return; }
  renderSim();
  sim.raf = requestAnimationFrame(simTick);
}

function startSim() {
  if (!sim.route) return;
  if (sim.pos >= sim.route.points.length - 1) sim.pos = 0;
  sim.playing = true;
  sim.last = performance.now();
  $("simPlay").textContent = "⏸ Pausar";
  sim.raf = requestAnimationFrame(simTick);
}

function stopSim() {
  sim.playing = false;
  if (sim.raf) cancelAnimationFrame(sim.raf);
  sim.raf = null;
  $("simPlay").textContent = "▶ Reproducir";
}

$("simPlay").addEventListener("click", () => (sim.playing ? stopSim() : startSim()));
$("simReset").addEventListener("click", () => { stopSim(); sim.pos = 0; renderSim(); });
$("simRoute").addEventListener("change", (e) => setSimRoute(e.target.value));
$("simSlider").addEventListener("input", (e) => {
  if (!sim.route) return;
  sim.pos = Number(e.target.value); // índice del punto
  renderSim();
});


// ---------- Tabla de recorridos ----------
const todosCheckbox = $("routesTodosCheckbox");
const routesTableBody = $("routesTableBody");

function setAllRoutesVisible(visible) {
  routesBySession.forEach((_, sessionId) => setRouteVisible(sessionId, visible));
  renderRoutesPage();
}

function syncTodosCheckbox() {
  const routes = [...routesBySession.values()];
  todosCheckbox.checked = routes.length > 0 && routes.every((r) => r.visible);
}

function renderRoutesPage() {
  routesTableBody.querySelectorAll("tr[data-session-id]").forEach((tr) => tr.remove());

  const totalPages = Math.max(1, Math.ceil(sessionsList.length / PAGE_SIZE));
  currentPage = Math.min(currentPage, totalPages - 1);
  const pageItems = sessionsList.slice(currentPage * PAGE_SIZE, (currentPage + 1) * PAGE_SIZE);

  pageItems.forEach((s) => {
    const route = routesBySession.get(s.session_id);
    const row = document.createElement("tr");
    row.dataset.sessionId = s.session_id;
    row.innerHTML = `
      <td><input type="checkbox" class="form-check-input"${route.visible ? " checked" : ""}></td>
      <td><div><span class="route-swatch" style="background:${route.color}"></span>${MULTI_BUS && s.bus_name ? `<b>${s.bus_name}</b> · ` : ""}${s.start_time} → ${s.end_time}${s.kind === "incompleta" ? ' <span class="route-tag">incompleta</span>' : ""}</div><div class="route-meta">${routeMetaText(route, s)}</div></td>
      <td>${s.points}</td>`;
    row.querySelector("input").addEventListener("change", (e) => {
      setRouteVisible(s.session_id, e.target.checked);
      syncTodosCheckbox();
    });
    routesTableBody.appendChild(row);
  });
  pageItems.forEach((s) => loadRoutePoints(s.session_id)); // calcula duración y distancia de la página visible

  $("pageInfo").textContent = sessionsList.length === 0 ? "Sin recorridos" : `Página ${currentPage + 1} de ${totalPages}`;
  $("prevPage").disabled = currentPage === 0;
  $("nextPage").disabled = currentPage >= totalPages - 1;
  syncTodosCheckbox();
}

$("prevPage").addEventListener("click", () => { currentPage--; renderRoutesPage(); });
$("nextPage").addEventListener("click", () => { currentPage++; renderRoutesPage(); });
todosCheckbox.addEventListener("change", () => setAllRoutesVisible(todosCheckbox.checked));

function clearRoutesTable() {
  routesBySession.forEach((route) => undrawRoute(route));
  routesBySession.clear();
  sessionsList = [];
  currentPage = 0;
  todosCheckbox.checked = false;
}

// Al entrar a Recorridos solo se llena la tabla; al filtrar por fecha se muestran las rutas encontradas.
function loadRoutesTable(start, end) {
  clearRoutesTable();
  const autoShow = Boolean(start && end);
  let url = "/api/sessions";
  if (autoShow) url += `?start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}`;

  fetch(url)
    .then((r) => r.json())
    .then((data) => {
      if (data.error) { showAlert(data.error); return; }
      sessionsList = data.sessions || [];
      sessionsList.forEach((s, i) => {
        routesBySession.set(s.session_id, {
          color: ROUTE_COLORS[i % ROUTE_COLORS.length],
          points: [], polyline: null, startMarker: null, endMarker: null,
          visible: false, loaded: false, loading: false,
        });
      });
      if (autoShow) sessionsList.forEach((s) => setRouteVisible(s.session_id, true));
      renderRoutesPage();
      updateHistoryViewState();
    })
    .catch(() => showAlert("Error cargando la tabla de recorridos"));
}

$("filterRoutesByDate").addEventListener("click", () => {
  const start = $("historyStart").value;
  const end = $("historyEnd").value;
  if (!validateRange(start, end)) return;
  currentFilterLabel = `${start.replace("T", " ")} → ${end.replace("T", " ")}`;
  loadRoutesTable(start, end);
});

$("clearFilter").addEventListener("click", () => {
  $("historyStart").value = "";
  $("historyEnd").value = "";
  currentFilterLabel = null;
  loadRoutesTable();
});
