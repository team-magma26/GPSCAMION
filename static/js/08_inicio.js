// OnRoute · Arranque: carga inicial, conexión en vivo (/events), bienvenida y vista inicial

// Se ejecuta al final, cuando todos los demás archivos ya están cargados.
updateDateLimits();
setInterval(updateDateLimits, 30000);

refreshTruckStatus();
setInterval(refreshTruckStatus, 5000);

// ---------- Carga inicial en vivo ----------
loadLiveHistory(true);   // recorrido actual de cada bus
loadLatest();            // última ubicación de cada bus
// Al volver a la pestaña (el celular pausa la conexión en vivo) se actualiza todo de una vez
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") loadLatest(); });

const source = new EventSource("/events");

source.onmessage = (event) => {
  const data = JSON.parse(event.data);

  if (data.type === "session_start") {
    // Un bus empezó un recorrido nuevo: se borra solo SU línea
    const b = getBus(data.bus);
    b.lastRoute.start = null;
    if (!historyMode) b.trail.setLatLngs([]);
    $("status").textContent = `Nuevo recorrido de ${b.name}`;
    return;
  }

  if (isShown(getBus(data.bus))) updateFields(data);
  if (data.lat !== null && data.lon !== null) trackTruck(data.lat, data.lon, data);
  if (data.lat !== null && data.lon !== null && !historyMode) updateMap(data.lat, data.lon, true, data.bus);
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
  setEta("wait", { chip: "Listo", label: NEXT_LABEL, num: "Toca Empezar", textMain: true,
    sub: MULTI_BUS ? "Con tu ubicación te decimos en cuántos minutos llega cada bus." : "Con tu ubicación te decimos en cuántos minutos llega el bus." });
}
$("wlStart").addEventListener("click", () => { closeWelcome(); startGeolocation(); });
$("wlSkip").addEventListener("click", () => {
  closeWelcome();
  setEta("off", { chip: "Sin ubicación", label: "¿Dónde estás?", num: "Cuando quieras", textMain: true,
    sub: "Toca el botón para saber cuánto tarda el bus en llegar a ti." });
  $("etaBtn").classList.remove("d-none");
});

setView("live");
