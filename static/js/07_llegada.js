// OnRoute · ¿Cuándo llega el bus? Ubicación del usuario y tiempo de llegada (/api/eta)

// ---------- Semana 2: ¿Cuándo llega el bus? (ubicación del usuario + tráfico) ----------
// La página pide permiso de ubicación al abrir. Con la ubicación, el servidor calcula
// cuánto tarda el bus en llegar hasta el usuario con el tráfico actual (/api/eta).
const ETA_REFRESH_MS = 10000; // igual que el bus, que envía su posición cada 10 s
const isMobile = /Mobi|Android|iPhone|iPad/i.test(navigator.userAgent);
let clientId = null; // identificador anónimo del navegador, solo para contar visitantes
try {
  clientId = localStorage.getItem("onrouteClient");
  if (!clientId) {
    clientId = window.crypto && crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + String(Math.random()).slice(2, 10);
    localStorage.setItem("onrouteClient", clientId);
  }
} catch (e) { clientId = null; }

function logUsage(event) {
  fetch("/api/usage", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ event, client_id: clientId, device: isMobile ? "mobile" : "desktop" }),
  }).catch(() => {});
}

const userIcon = L.divIcon({ className: "", html: '<div class="user-pin"></div>', iconSize: [22, 22], iconAnchor: [11, 11] });
const userMarker = L.marker([0, 0], { icon: userIcon, zIndexOffset: 900, title: "Tu ubicación" });
const etaLine = L.polyline([], { color: "#ffd166", weight: 4, opacity: 0.85, dashArray: "8 8" });
let userPos = null, etaTimer = null, etaBusy = false, lastEtaAt = 0, etaFitted = false, geoWatch = null;
const geoLogged = {};

function logGeoOnce(event) { if (!geoLogged[event]) { geoLogged[event] = true; logUsage(event); } }

const ICO = {
  road: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M7 21L10 3M17 21L14 3M12 7v2M12 13v2M12 19v1"/></svg>',
  traffic: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="8" y="2" width="8" height="20" rx="3"/><circle cx="12" cy="7" r="1.4"/><circle cx="12" cy="12" r="1.4"/><circle cx="12" cy="17" r="1.4"/></svg>',
  arrow: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12h14M13 6l6 6-6 6"/></svg>',
  pause: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M9 6v12M15 6v12"/></svg>',
  refresh: '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12a9 9 0 1 1-3-6.7L21 8M21 3v5h-5"/></svg>',
};

// state: ok | here | leaving | wait | off
function setEta(state, o) {
  $("etaBox").dataset.state = state;
  $("etaChip").textContent = o.chip || "";
  $("etaLabel").textContent = o.label || "";
  $("etaNum").textContent = o.num || "";
  $("etaNum").classList.toggle("text", Boolean(o.textMain));
  $("etaUnit").textContent = o.unit || "";
  $("etaClock").innerHTML = o.clock || "";
  $("etaFacts").innerHTML = (o.facts || []).map((f) => `<span class="eta-fact${f.warn ? " warn" : ""}">${ICO[f.icon] || ""}${f.text}</span>`).join("");
  $("etaSub").innerHTML = o.sub || "";
  $("etaNote").innerHTML = o.note ? `${ICO.refresh}${o.note}` : "";
}
const fmtClock = (ms) => new Date(ms).toLocaleTimeString("es-CO", { hour: "numeric", minute: "2-digit" });

function etaSetMapLayers(show) {
  [userMarker, etaLine].forEach((layer) => {
    if (show && userPos) { if (!map.hasLayer(layer)) layer.addTo(map); }
    else if (map.hasLayer(layer)) map.removeLayer(layer);
  });
}

function showEta(d) {
  if (d.error) { setEta("off", { chip: "Error", label: "No pudimos calcular", num: "Inténtalo de nuevo", textMain: true, sub: d.error }); return; }
  if (d.status !== "ok") etaLine.setLatLngs([]);
  if (d.status === "no_data") {
    setEta("off", { chip: "Sin datos", label: BUS_NAME, num: "Aún no sale", textMain: true, sub: "El bus todavía no ha enviado su ubicación." });
    return;
  }
  if (d.status === "offline") {
    setEta("off", { chip: "Sin señal", label: BUS_NAME, num: "Sin señal", textMain: true,
      sub: `El bus no envía datos desde ${fmtAgo(d.age_s * 1000)}. El tiempo aparece apenas vuelva a conectarse.` });
    return;
  }
  if (d.status === "here") {
    setEta("here", { chip: "Llegando", label: "Prepárate", num: "¡Ya llega!", textMain: true,
      sub: `El bus está a <b>${fmtDistance(d.straight_m)}</b> de ti.` });
    return;
  }

  const leaving = d.trend === "leaving";
  const facts = [{ icon: "road", text: `${fmtDistance(d.distance_m)} por la vía` }];
  if (d.source === "tomtom") {
    facts.push(d.traffic_delay_s >= 60
      ? { icon: "traffic", text: `+${Math.round(d.traffic_delay_s / 60)} min por tráfico`, warn: true }
      : { icon: "traffic", text: "Tráfico fluido" });
  }
  if (d.trend === "approaching") facts.push({ icon: "arrow", text: "Se acerca" });
  if (d.trend === "stopped") facts.push({ icon: "pause", text: "Detenido" });
  setEta(leaving ? "leaving" : "ok", {
    chip: leaving ? "Se aleja" : d.eta_min <= 3 ? "Muy cerca" : "En camino",
    label: `${BUS_NAME} llega en`,
    num: String(d.eta_min), unit: "min",
    clock: `Hacia las <b>${fmtClock(Date.now() + d.eta_s * 1000)}</b>`,
    facts,
    sub: leaving ? "El bus va en sentido contrario a ti; el tiempo incluye la vuelta." : "",
    note: d.source === "tomtom" ? "Tráfico en tiempo real · se actualiza cada 10 s"
      : d.source === "modelo" ? "Modelo propio (recorridos reales) · se actualiza cada 10 s"
      : "Estimado sin tráfico · se actualiza cada 10 s",
  });
  etaLine.setLatLngs(d.path || []);
  etaSetMapLayers(view === "live");
  if (!etaFitted && !historyMode && d.path && d.path.length) {
    etaFitted = true;
    setFollow(false);
    map.fitBounds(L.latLngBounds(d.path).extend(marker.getLatLng()), { padding: [40, 40], maxZoom: 16 });
  }
}

function requestEta() {
  if (!userPos || etaBusy) return;
  etaBusy = true;
  lastEtaAt = Date.now();
  fetch("/api/eta", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ lat: userPos.lat, lon: userPos.lon, client_id: clientId }),
  })
    .then((r) => r.json())
    .then(showEta)
    .catch(() => setEta("off", { chip: "Sin conexión", label: BUS_NAME, num: "Reintentando…", textMain: true, sub: "No pudimos calcular el tiempo de llegada. Volvemos a intentar en 10 s." }))
    .finally(() => { etaBusy = false; });
}

function onUserPosition(pos) {
  const first = !userPos;
  userPos = { lat: pos.coords.latitude, lon: pos.coords.longitude };
  userMarker.setLatLng([userPos.lat, userPos.lon]);
  etaSetMapLayers(view === "live");
  const meBtn = document.getElementById("meBtn");
  if (meBtn) meBtn.classList.remove("d-none");
  logGeoOnce("geo_granted");
  $("etaBtn").classList.add("d-none");
  if (first) {
    setEta("wait", { chip: "Calculando", label: `${BUS_NAME} llega en`, num: "Calculando…", textMain: true, sub: "Ya tenemos tu ubicación." });
    // Muestra al usuario en el mapa junto con el bus (si el bus no tiene datos, centra en el usuario)
    if (!historyMode) {
      setFollow(false);
      const busLL = marker.getLatLng();
      const hasBus = busLL && (busLL.lat !== 4.6 || busLL.lng !== -74.08);
      if (hasBus) map.fitBounds(L.latLngBounds([[userPos.lat, userPos.lon], [busLL.lat, busLL.lng]]), { padding: [60, 60], maxZoom: 16 });
      else map.setView([userPos.lat, userPos.lon], 16);
    }
  }
  if (first || Date.now() - lastEtaAt > 5000) requestEta();
  if (!etaTimer) etaTimer = setInterval(requestEta, ETA_REFRESH_MS);
}

function onUserError(err) {
  if (userPos) return; // ya teníamos ubicación: un error pasajero no borra el cálculo
  if (err.code === 1) {
    logGeoOnce("geo_denied");
    setEta("off", { chip: "Sin permiso", label: "¿Dónde estás?", num: "Activa tu ubicación", textMain: true,
      sub: "Así calculamos cuánto tarda el bus en llegar a ti. Permítela desde el candado de la barra de direcciones y toca el botón." });
  } else {
    logGeoOnce("geo_unavailable");
    setEta("off", { chip: "Sin ubicación", label: "¿Dónde estás?", num: "No te encontramos", textMain: true,
      sub: "Revisa que la ubicación del celular esté encendida y vuelve a intentar." });
  }
  $("etaBtn").classList.remove("d-none");
}

function startGeolocation() {
  if (!window.isSecureContext || !navigator.geolocation) {
    setEta("off", { chip: "No disponible", label: "¿Dónde estás?", num: "Ubicación no disponible", textMain: true,
      sub: "Este navegador no permite usar tu ubicación en esta página (necesita https)." });
    return;
  }
  setEta("wait", { chip: "Buscando", label: `${BUS_NAME} llega en`, num: "Ubicándote…", textMain: true,
    sub: "Si el navegador pregunta, toca <b>Permitir</b> para calcular cuánto tarda el bus en llegar a ti." });
  $("etaBtn").classList.add("d-none");
  if (geoWatch !== null) navigator.geolocation.clearWatch(geoWatch);
  geoWatch = navigator.geolocation.watchPosition(onUserPosition, onUserError,
    { enableHighAccuracy: true, maximumAge: 15000, timeout: 20000 });
}

// Cuando llega una posición nueva del bus se recalcula (máximo cada 5 s)
function etaOnBusUpdate() {
  if (userPos && Date.now() - lastEtaAt > 5000) requestEta();
}

$("etaBtn").addEventListener("click", startGeolocation);
