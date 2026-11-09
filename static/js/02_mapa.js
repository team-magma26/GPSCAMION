// OnRoute · Mapa principal, marcador del bus y estado compartido de la página

// ---------- Mapa principal (en vivo + recorridos) ----------
const map = L.map("map").setView([4.6, -74.08], 13);
addTiles(map);

// Ícono del vehículo en el mapa (independiente del logo de la app)
const truckSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M5.2 17.5H4a1 1 0 0 1-1-1V6.5A2.5 2.5 0 0 1 5.5 4h11A3.5 3.5 0 0 1 20 7.5l1 5.5v3.5a1 1 0 0 1-1 1h-1.2"/><path d="M9.2 17.5h5.6"/><path d="M3 12h18"/><path d="M8 4v8M13 4v8"/><circle cx="7.2" cy="17.6" r="2"/><circle cx="16.8" cy="17.6" r="2"/></svg>';
// Bus de frente, relleno (se ve bien sobre el mapa claro y el oscuro). La punta del pin marca la posición exacta.
const busSolidSvg = '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3.5" y="1.5" width="17" height="17" rx="3.5" fill="#fff"/><rect x="5.5" y="17" width="3.5" height="4.5" rx="1.2" fill="#fff"/><rect x="15" y="17" width="3.5" height="4.5" rx="1.2" fill="#fff"/><rect x="8" y="2.8" width="8" height="1.6" rx=".8" fill="currentColor"/><rect x="5.5" y="5.5" width="13" height="6.5" rx="1.4" fill="currentColor"/><circle cx="7.6" cy="15" r="1.3" fill="currentColor"/><circle cx="16.4" cy="15" r="1.3" fill="currentColor"/></svg>';
const mkIcon = (cls) => L.divIcon({
  className: "",
  html: `<div class="bus-marker ${cls}" role="img" aria-label="${cls === "sim" ? "Bus (simulación)" : BUS_NAME}"><span class="bm-pulse"></span><span class="bm-ground"></span><span class="bm-tip"></span><span class="bm-pin">${busSolidSvg}</span></div>`,
  iconSize: [56, 62], iconAnchor: [28, 59], tooltipAnchor: [0, -58],
});
const marker = L.marker([4.6, -74.08], { icon: mkIcon(""), title: BUS_NAME }).addTo(map);
// Etiqueta fija con el nombre del bus encima del marcador
marker.bindTooltip(BUS_NAME, { permanent: true, direction: "top", className: "bus-tag" });
const trail = L.polyline([], { color: "#4c8dff", weight: 5, opacity: 0.9 }).addTo(map);

const routesLayer = L.layerGroup().addTo(map);
const routesBySession = new Map(); // session_id -> { color, points, polyline, startMarker, endMarker, visible, loaded, loading }

let view = "live";
let lastHistoryView = "routes";
let routesLoaded = false;
let mapCentered = false;
let followTruck = true;
let historyMode = false; // true en cualquier vista histórica: ignora el mapa en vivo
let sessionsList = [];
let currentPage = 0;
let currentFilterLabel = null;
