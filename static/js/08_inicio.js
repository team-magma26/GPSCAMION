// OnRoute · Arranque: carga inicial, conexión en vivo (/events), bienvenida y vista inicial

// Se ejecuta al final, cuando todos los demás archivos ya están cargados.
updateDateLimits();
setInterval(updateDateLimits, 30000);

refreshTruckStatus();
setInterval(refreshTruckStatus, 5000);

// ---------- Carga inicial en vivo ----------
fetch("/api/history")
  .then((r) => r.json())
  .then((points) => {
    setLastRouteFromPoints(points);
    if (historyMode) return;
    const latlngs = points.map((p) => [p.lat, p.lon]);
    trail.setLatLngs(latlngs);
    refreshTruckStatus();
    if (latlngs.length > 0) {
      marker.setLatLng(latlngs[latlngs.length - 1]);
      map.fitBounds(trail.getBounds(), { maxZoom: 15, padding: [20, 20] });
      mapCentered = true;
    }
  })
  .catch(() => {});

fetch("/api/latest")
  .then((r) => r.json())
  .then((data) => {
    updateFields(data);
    if (data.lat !== null && data.lon !== null) updateMap(data.lat, data.lon, false);
  })
  .catch(() => {});

const source = new EventSource("/events");

source.onmessage = (event) => {
  const data = JSON.parse(event.data);

  if (data.type === "session_start") {
    lastRoute.start = null;
    if (!historyMode) {
      trail.setLatLngs([]);
      mapCentered = false;
    }
    $("status").textContent = "Nueva sesión de recorrido iniciada";
    return;
  }

  updateFields(data);
  if (data.lat !== null && data.lon !== null) trackTruck(data.lat, data.lon, data);
  if (data.lat !== null && data.lon !== null && !historyMode) updateMap(data.lat, data.lon, true);
  if (data.lat !== null && data.lon !== null) etaOnBusUpdate();
};

source.onopen = () => { sseDown = false; refreshTruckStatus(); };
source.onerror = () => {
  sseDown = true;
  $("dot").classList.remove("live");
  $("status").textContent = "Conexión perdida — reintentando…";
};


// Cuenta la visita (anónima) para las métricas de uso
logUsage("visit");

// Primera visita: bienvenida y el permiso se pide al tocar "Empezar" (con contexto).
// Visitas siguientes: el permiso se pide apenas abre la página.
let welcomed = false;
try { welcomed = localStorage.getItem("onrouteWelcomed") === "1"; } catch (e) {}
function closeWelcome() {
  $("welcome").classList.add("d-none");
  try { localStorage.setItem("onrouteWelcomed", "1"); } catch (e) {}
}
if (welcomed) {
  startGeolocation();
} else {
  $("welcome").classList.remove("d-none");
  setEta("wait", { chip: "Listo", label: `${BUS_NAME} llega en`, num: "Toca Empezar", textMain: true,
    sub: "Con tu ubicación te decimos en cuántos minutos llega el bus." });
}
$("wlStart").addEventListener("click", () => { closeWelcome(); startGeolocation(); });
$("wlSkip").addEventListener("click", () => {
  closeWelcome();
  setEta("off", { chip: "Sin ubicación", label: "¿Dónde estás?", num: "Cuando quieras", textMain: true,
    sub: "Toca el botón para saber cuánto tarda el bus en llegar a ti." });
  $("etaBtn").classList.remove("d-none");
});

setView("live");
