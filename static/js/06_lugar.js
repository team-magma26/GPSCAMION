// OnRoute · Buscar por lugar: cuándo pasó el bus por un punto

// ---------- Buscar por lugar (mapa propio) ----------
let placeMap = null;
let placeMarker = null;
let placeCircle = null;
let passageLayer = null;
let passageMarkers = [];
const placeIcon = L.divIcon({ className: "", html: '<div class="place-pin"></div>', iconSize: [20, 20], iconAnchor: [10, 10] });

function initPlaceMap() {
  if (placeMap) return;
  placeMap = L.map("placeMap").setView(map.getCenter(), 14);
  addTiles(placeMap);
  passageLayer = L.layerGroup().addTo(placeMap);
  placeMap.on("click", (e) => setPlacePoint(e.latlng.lat, e.latlng.lng)); // un clic basta para marcar el punto
}

function setPlacePoint(lat, lng) {
  if (placeMarker) placeMap.removeLayer(placeMarker);
  if (placeCircle) placeMap.removeLayer(placeCircle);
  placeMarker = L.marker([lat, lng], { icon: placeIcon }).addTo(placeMap);
  placeCircle = L.circle([lat, lng], { radius: PASSAGE_RADIUS_M, color: "#8ecae6", weight: 2, fillOpacity: 0.12 }).addTo(placeMap);
  $("pickPrompt").classList.add("d-none");
  $("pointChip").classList.remove("d-none");
  $("pointChip").textContent = `${lat.toFixed(5)}, ${lng.toFixed(5)} · radio ${PASSAGE_RADIUS_M} m`;
  $("placeExtras").classList.remove("d-none");
}

// Revisa si el bus tiene historial en la zona del resultado (usa el tamaño del lugar como radio).
function checkPlaceHistory(it, badge) {
  const lat = parseFloat(it.lat), lon = parseFloat(it.lon);
  let radius = PASSAGE_RADIUS_M;
  if (Array.isArray(it.boundingbox) && it.boundingbox.length === 4) {
    const [south, north, west, east] = it.boundingbox.map(Number);
    radius = Math.min(Math.max(distM([south, west], [north, east]) / 2, PASSAGE_RADIUS_M), 50000);
  }
  fetch(`/api/has_history?lat=${lat}&lon=${lon}&radius=${Math.round(radius)}`)
    .then((r) => r.json())
    .then((data) => {
      if (data.error || typeof data.has_history !== "boolean") throw new Error("sin datos");
      badge.textContent = data.has_history ? "Con historial" : "Sin historial";
      badge.classList.add(data.has_history ? "yes" : "no");
    })
    .catch(() => { badge.textContent = "No se pudo revisar el historial"; });
}

// 1) Buscar un lugar por nombre (Nominatim / OpenStreetMap) y llevar el mapa hasta allá.
function searchPlace() {
  const q = $("placeQuery").value.trim();
  const results = $("placeResults");
  if (!q) { showAlert("Escribe el nombre de un lugar, por ejemplo: Cartagena."); return; }

  results.innerHTML = '<div class="muted">Buscando…</div>';
  fetch("https://nominatim.openstreetmap.org/search?format=jsonv2&limit=5&accept-language=es&countrycodes=co&q=" + encodeURIComponent(q))
    .then((r) => r.json())
    .then((items) => {
      results.innerHTML = "";
      if (items.length === 0) {
        results.innerHTML = '<div class="muted">No encontramos ese lugar. Prueba con otro nombre.</div>';
        return;
      }
      items.forEach((it) => {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "list-group-item list-group-item-action place-result";
        const nameEl = document.createElement("span");
        nameEl.textContent = it.display_name;
        const badge = document.createElement("span");
        badge.className = "hist-badge";
        badge.textContent = "Revisando historial…";
        btn.append(nameEl, badge);
        checkPlaceHistory(it, badge);
        btn.addEventListener("click", () => {
          const lat = parseFloat(it.lat), lon = parseFloat(it.lon);
          placeMap.flyTo([lat, lon], 15);
          setPlacePoint(lat, lon);
          results.innerHTML = "";
        });
        results.appendChild(btn);
      });
    })
    .catch(() => {
      results.innerHTML = "";
      showAlert("No se pudo buscar el lugar. Revisa tu conexión e intenta de nuevo.");
    });
}
$("placeSearchBtn").addEventListener("click", searchPlace);
$("placeQuery").addEventListener("keydown", (e) => { if (e.key === "Enter") searchPlace(); });

// 3) Buscar pasadas (fechas opcionales).
$("searchPassages").addEventListener("click", () => {
  if (!placeMarker) { showAlert("Primero marca un punto en el mapa."); return; }

  const start = $("passStart").value;
  const end = $("passEnd").value;
  if (!validateRange(start, end)) return; // las fechas son obligatorias
  const dateParams = `&start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}`;

  const { lat, lng } = placeMarker.getLatLng();
  const banner = $("passagesBanner");

  fetch(`/api/passages?lat=${lat}&lon=${lng}&radius=${PASSAGE_RADIUS_M}${dateParams}`)
    .then((r) => r.json())
    .then((data) => {
      if (data.error) { showAlert(data.error); return; }

      passageLayer.clearLayers();
      passageMarkers = [];

      if (data.passages.length === 0) {
        banner.textContent = "El bus no pasó por ese punto en las fechas elegidas.";
      } else {
        const items = data.passages.map((p, i) => {
          passageMarkers.push(
            L.circleMarker([p.lat, p.lon], { radius: 7, color: "#f4a261", fillColor: "#f4a261", fillOpacity: 0.9 })
              .bindPopup(`${p.start_gps_time} — ${p.end_gps_time}`)
              .addTo(passageLayer)
          );
          const label = p.start_received_at === p.end_received_at
            ? `${p.start_received_at} (${p.points_count} lectura${p.points_count > 1 ? "s" : ""})`
            : `${p.start_received_at} → ${p.end_received_at} (${p.points_count} lecturas)`;
          return `<li><button type="button" class="pass-item" data-i="${i}">${label}</button></li>`;
        }).join("");
        banner.innerHTML = `<div class="fw-bold mb-2">${data.passages.length} pasada(s) encontrada(s)</div><ul class="list-unstyled m-0">${items}</ul>`;
        banner.querySelectorAll(".pass-item").forEach((b) => b.addEventListener("click", () => {
          const m = passageMarkers[Number(b.dataset.i)];
          placeMap.flyTo(m.getLatLng(), 17);
          m.openPopup();
        }));
      }

      banner.classList.remove("d-none");
      placeMap.fitBounds(placeCircle.getBounds(), { maxZoom: 16, padding: [20, 20] });
    })
    .catch(() => showAlert("Error consultando las pasadas por ese punto"));
});

$("clearPassages").addEventListener("click", () => {
  if (passageLayer) passageLayer.clearLayers();
  passageMarkers = [];
  if (placeMarker) { placeMap.removeLayer(placeMarker); placeMarker = null; }
  if (placeCircle) { placeMap.removeLayer(placeCircle); placeCircle = null; }
  $("passagesBanner").classList.add("d-none");
  $("placeExtras").classList.add("d-none");
  $("pointChip").classList.add("d-none");
  $("pickPrompt").classList.remove("d-none");
  $("passStart").value = "";
  $("passEnd").value = "";
  $("placeResults").innerHTML = "";
});
