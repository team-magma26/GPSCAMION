// OnRoute · Interfaz: avisos, botones de información, fechas, vistas, menú Avanzado y tema del mapa

// ---------- Avisos ----------
function showAlert(message) {
  $("modalMessage").textContent = message;
  bootstrap.Modal.getOrCreateInstance($("appModal")).show();
}

// ---------- Botones "i" ----------
document.querySelectorAll(".info-btn").forEach((el) => {
  el.textContent = "i";
  new bootstrap.Popover(el, { trigger: "click", placement: "auto" });
});
document.addEventListener("click", (e) => {
  if (e.target.closest(".info-btn") || e.target.closest(".popover")) return;
  document.querySelectorAll(".info-btn").forEach((el) => {
    const p = bootstrap.Popover.getInstance(el);
    if (p) p.hide();
  });
});

// ---------- Fechas ----------
function localDatetimeString(date) {
  const offsetMs = date.getTimezoneOffset() * 60000;
  return new Date(date.getTime() - offsetMs).toISOString().slice(0, 16);
}
function updateDateLimits() {
  const nowStr = localDatetimeString(new Date());
  ["historyStart", "historyEnd", "passStart", "passEnd"].forEach((id) => { $(id).max = nowStr; });
}

function validateRange(start, end) {
  if (!start || !end) { showAlert("Selecciona fecha y hora de inicio y fin."); return false; }
  const nowStr = localDatetimeString(new Date());
  if (start > nowStr) { showAlert("La fecha/hora 'Desde' no puede ser futura."); return false; }
  if (end > nowStr) { showAlert("La fecha/hora 'Hasta' no puede ser futura."); return false; }
  if (start > end) { showAlert("La fecha/hora 'Desde' no puede ser posterior a 'Hasta'."); return false; }
  return true;
}

// ---------- Vistas: En vivo / Recorridos / Por lugar ----------
function setView(v) {
  const prev = view;
  view = v;
  historyMode = v !== "live";
  if (historyMode) lastHistoryView = v;

  document.body.dataset.mode = v;
  $("modeLive").checked = v === "live";
  $("modeHistory").checked = v !== "live";
  $("subRoutes").checked = v === "routes";
  $("subPlace").checked = v === "place";
  $("subNav").classList.toggle("d-none", v === "live");
  $("liveChip").classList.toggle("d-none", v !== "live");
  $("backLive").classList.toggle("d-none", v === "live");
  $("advBtn").classList.toggle("on", v !== "live");
  document.querySelectorAll(".adv-item[data-go]").forEach((b) => b.setAttribute("aria-current", String(b.dataset.go === v)));
  $("livePanel").classList.toggle("d-none", v !== "live");
  $("etaPanel").classList.toggle("d-none", v !== "live");
  etaSetMapLayers(v === "live");
  $("routesPanel").classList.toggle("d-none", v !== "routes");
  $("placePanel").classList.toggle("d-none", v !== "place");
  $("mapWrap").classList.toggle("d-none", v === "place");
  $("statsCard").classList.toggle("d-none", v !== "live");
  $("placeWrap").classList.toggle("d-none", v !== "place");
  followBtn.classList.toggle("d-none", v !== "live");
  refreshTruckStatus();
  if (v !== "routes") stopSim();

  if (prev === "live" && v !== "live") {
    trail.setLatLngs([]);
    if (map.hasLayer(marker)) map.removeLayer(marker);
  }
  if (v === "live" && prev !== "live") leaveHistory();

  if (v === "routes") {
    if (!routesLoaded) { routesLoaded = true; loadRoutesTable(); }
    setTimeout(() => map.invalidateSize(), 0);
  } else if (v === "place") {
    initPlaceMap();
    setTimeout(() => placeMap.invalidateSize(), 0);
  } else {
    setTimeout(() => map.invalidateSize(), 0);
  }
}

function leaveHistory() {
  $("historyStart").value = "";
  $("historyEnd").value = "";
  currentFilterLabel = null;
  routesLoaded = false;
  clearRoutesTable();
  updateHistoryViewState();
  if (!map.hasLayer(marker)) marker.addTo(map);
  fetch("/api/history")
    .then((r) => r.json())
    .then((points) => {
      setLastRouteFromPoints(points);
      trail.setLatLngs(points.map((p) => [p.lat, p.lon]));
      refreshTruckStatus();
      if (points.length > 0) {
        const last = points[points.length - 1];
        marker.setLatLng([last.lat, last.lon]);
        map.fitBounds(trail.getBounds(), { maxZoom: 15, padding: [20, 20] });
      }
    })
    .catch(() => {});
}

$("modeLive").addEventListener("change", () => setView("live"));
$("modeHistory").addEventListener("change", () => setView(lastHistoryView));
$("backLive").addEventListener("click", () => setView("live"));

// ---------- Menú "Avanzado" (histórico y métricas) ----------
// Es un dropdown de Bootstrap: abrir, cerrar, Esc, clic afuera y flechas los maneja Bootstrap.
const advBtn = $("advBtn"), advMenu = $("advMenu");
const advDropdown = bootstrap.Dropdown.getOrCreateInstance(advBtn);
function setAdvOpen(open) { if (open) advDropdown.show(); else advDropdown.hide(); }
advBtn.addEventListener("hidden.bs.dropdown", () => {
  $("advKeyForm").classList.add("d-none");
  $("advKeyMsg").textContent = "";
});
function openMetrics(key) {
  window.open(`/metricas${key ? "?key=" + encodeURIComponent(key) : ""}`, "_blank", "noopener");
}
function forgetMetricsKey() { try { localStorage.removeItem("onrouteMetricsKey"); } catch (e) {} }
// Revisa la clave con el servidor antes de guardarla (si es incorrecta, no se guarda)
function checkMetricsKey(key) {
  return fetch(`/api/metrics?days=1&key=${encodeURIComponent(key)}`, { method: "GET" }).then((r) => r.ok);
}
function askMetricsKey(msg) {
  $("advKeyForm").classList.remove("d-none");
  $("advKeyMsg").textContent = msg || "";
  $("advKey").focus();
}
advMenu.querySelectorAll(".adv-item").forEach((b) => b.addEventListener("click", () => {
  const go = b.dataset.go;
  if (go === "metrics") {
    let saved = null;
    try { saved = localStorage.getItem("onrouteMetricsKey"); } catch (e) {}
    if (!saved) { askMetricsKey(""); return; }
    checkMetricsKey(saved).then((ok) => {
      if (ok) { openMetrics(saved); setAdvOpen(false); }
      else { forgetMetricsKey(); askMetricsKey("La clave guardada ya no sirve. Escríbela de nuevo."); }
    }).catch(() => askMetricsKey("No se pudo revisar la clave. Intenta de nuevo."));
    return;
  }
  setAdvOpen(false);
  setView(go);
}));
$("advKeyForm").addEventListener("submit", (e) => {
  e.preventDefault();
  const key = $("advKey").value.trim();
  if (!key) return;
  checkMetricsKey(key).then((ok) => {
    if (!ok) { $("advKeyMsg").textContent = "Clave incorrecta. Revisa e intenta de nuevo."; $("advKey").select(); return; }
    try { localStorage.setItem("onrouteMetricsKey", key); } catch (err) {}
    openMetrics(key);
    $("advKey").value = "";
    $("advKeyMsg").textContent = "";
    setAdvOpen(false);
  }).catch(() => { $("advKeyMsg").textContent = "No se pudo revisar la clave. Intenta de nuevo."; });
});
$("subRoutes").addEventListener("change", () => setView("routes"));
$("subPlace").addEventListener("change", () => setView("place"));


// ---------- Barra superior retraíble (celular) ----------
$("topToggle").addEventListener("click", () => {
  const open = document.querySelector(".topbar").classList.toggle("open");
  $("topToggle").setAttribute("aria-expanded", open);
});

// ---------- Tema del mapa (claro / oscuro) ----------
function applyMapTheme(dark) {
  document.body.classList.toggle("map-dark", dark);
  $("mapThemeBtn").textContent = dark ? "☀ Mapa claro" : "☾ Mapa oscuro";
  try { localStorage.setItem("mapTheme", dark ? "dark" : "light"); } catch (e) {}
}
let savedDark = false;
try { savedDark = localStorage.getItem("mapTheme") === "dark"; } catch (e) {}
applyMapTheme(savedDark);
$("mapThemeBtn").addEventListener("click", () => applyMapTheme(!document.body.classList.contains("map-dark")));
