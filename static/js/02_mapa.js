// OnRoute · Mapa principal, marcador del bus y estado compartido de la página

// ---------- Mapa principal (en vivo + recorridos) ----------
const map = L.map("map").setView([4.6, -74.08], 13);
addTiles(map);

// Ícono del vehículo en el mapa (independiente del logo de la app)
const truckSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M5.2 17.5H4a1 1 0 0 1-1-1V6.5A2.5 2.5 0 0 1 5.5 4h11A3.5 3.5 0 0 1 20 7.5l1 5.5v3.5a1 1 0 0 1-1 1h-1.2"/><path d="M9.2 17.5h5.6"/><path d="M3 12h18"/><path d="M8 4v8M13 4v8"/><circle cx="7.2" cy="17.6" r="2"/><circle cx="16.8" cy="17.6" r="2"/></svg>';
// Bus de frente, relleno (se ve bien sobre el mapa claro y el oscuro). La punta del pin marca la posición exacta.
const busSolidSvg = '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3.5" y="1.5" width="17" height="17" rx="3.5" fill="#fff"/><rect x="5.5" y="17" width="3.5" height="4.5" rx="1.2" fill="#fff"/><rect x="15" y="17" width="3.5" height="4.5" rx="1.2" fill="#fff"/><rect x="8" y="2.8" width="8" height="1.6" rx=".8" fill="currentColor"/><rect x="5.5" y="5.5" width="13" height="6.5" rx="1.4" fill="currentColor"/><circle cx="7.6" cy="15" r="1.3" fill="currentColor"/><circle cx="16.4" cy="15" r="1.3" fill="currentColor"/></svg>';
const mkIcon = (cls, label) => L.divIcon({
  className: "",
  html: `<div class="bus-marker ${cls}" role="img" aria-label="${cls === "sim" ? "Bus (simulación)" : label || BUS_NAME}"><span class="bm-pulse"></span><span class="bm-ground"></span><span class="bm-dir"><svg viewBox="0 0 24 24"><path d="M12 2.5 20.5 20 12 15.2 3.5 20z"/></svg></span><span class="bm-tip"></span><span class="bm-pin">${busSolidSvg}</span></div>`,
  iconSize: [56, 62], iconAnchor: [28, 59], tooltipAnchor: [0, -58],
});
// ---------- Un marcador y una línea por cada bus de la ruta ----------
const BUS_TRAIL_COLORS = ["#4c8dff", "#e2322d", "#2ecc71", "#f4a261"];
const buses = {}; // id -> { id, name, color, index, marker, trail, hasPos, lastMsgAt, recentPoints, lastRoute }
BUSES.forEach((b, i) => {
  const color = BUS_TRAIL_COLORS[i % BUS_TRAIL_COLORS.length];
  const marker = L.marker([0, 0], { icon: mkIcon("", b.name), title: b.name, zIndexOffset: 500 - i });
  // Etiqueta fija con el nombre del bus ("Bus de Luis") encima del marcador
  marker.bindTooltip(b.name, { permanent: true, direction: "top", className: `bus-tag bus-tag-${i}` });
  marker.on("click", () => focusOn(b.id));
  marker._dirColor = color; // la flecha de sentido usa el color de la línea de este bus
  marker.on("add", () => applyHeading(marker));
  const trail = L.polyline([], { color, weight: 5, opacity: 0.9 }).addTo(map);
  const arrows = L.layerGroup().addTo(map); // flechas de sentido sobre la línea del recorrido
  buses[b.id] = { id: b.id, name: b.name, color, index: i, marker, trail, arrows, arrowTimer: null, hasPos: false,
                  lastMsgAt: 0, recentPoints: [], lastRoute: { start: null, end: null } };
});
const busList = () => BUSES.map((b) => buses[b.id]);
const getBus = (id) => buses[id] || buses[BUSES[0].id]; // tramas sin "bus" = el primero
let focusBus = BUSES[0].id; // el bus que sigue "Seguir bus"
// Qué buses se ven en vivo: "all" (por defecto) o el id de uno. Se recuerda en este navegador.
let busFilter = "all";
try { const f = localStorage.getItem("onrouteBusFilter"); if (f && buses[f]) busFilter = f; } catch (e) {}
if (busFilter !== "all") focusBus = busFilter;
const isShown = (b) => busFilter === "all" || b.id === busFilter;

// Muestra u oculta los buses del mapa en vivo (en las vistas históricas se ocultan)
function setLiveBusesVisible(show) {
  busList().forEach((b) => {
    const on = show && isShown(b);
    if (on && b.hasPos) { if (!map.hasLayer(b.marker)) b.marker.addTo(map); }
    else if (map.hasLayer(b.marker)) map.removeLayer(b.marker);
    if (on) { if (!map.hasLayer(b.trail)) b.trail.addTo(map); if (!map.hasLayer(b.arrows)) b.arrows.addTo(map); }
    else {
      if (map.hasLayer(b.trail)) map.removeLayer(b.trail);   // se oculta sin perder la línea
      if (map.hasLayer(b.arrows)) map.removeLayer(b.arrows);
    }
    if (!show) { b.trail.setLatLngs([]); b.arrows.clearLayers(); }
  });
}
// ---------- Sentido de movimiento ----------
// Flecha alrededor del marcador (hacia dónde va el bus ahora) y flechas sobre la línea del recorrido.
// El mapa (Web Mercator) conserva los ángulos, así que el rumbo geográfico sirve directo para rotar.
const DIR_MIN_M = 30;        // distancia mínima para calcular el rumbo (evita el ruido del GPS quieto)
const DIR_LOOKBACK = 30;     // máximo de lecturas hacia atrás (~5 min a una lectura cada 10 s)
const ARROW_SPACING_M = 400; // una flecha cada 400 m sobre la línea
const ARROW_MAX = 120;       // tope de flechas por línea (en recorridos largos se separan más)
const asLatLon = (p) => (Array.isArray(p) ? p : [p.lat, p.lng !== undefined ? p.lng : p.lon]);

// Rumbo de a hacia b en grados (0 = norte, 90 = este)
function bearingDeg(a, b) {
  const r = Math.PI / 180;
  const dLon = (b[1] - a[1]) * r;
  const y = Math.sin(dLon) * Math.cos(b[0] * r);
  const x = Math.cos(a[0] * r) * Math.sin(b[0] * r) - Math.sin(a[0] * r) * Math.cos(b[0] * r) * Math.cos(dLon);
  return (Math.atan2(y, x) / r + 360) % 360;
}

// Rumbo actual: desde la última lectura que esté a más de DIR_MIN_M hasta la más reciente.
// null si el bus no se ha movido lo suficiente (detenido o sin datos).
function headingOf(points) {
  const n = points.length;
  if (n < 2) return null;
  const z = asLatLon(points[n - 1]);
  for (let i = n - 2; i >= 0 && n - 1 - i <= DIR_LOOKBACK; i--) {
    const a = asLatLon(points[i]);
    if (distM(a, z) >= DIR_MIN_M) return bearingDeg(a, z);
  }
  return null;
}

// Gira la flecha del marcador. Si apunta hacia arriba, sube un poco la etiqueta del nombre para no taparla.
function setHeading(marker, deg) {
  marker._heading = deg;
  applyHeading(marker);
}
function applyHeading(marker) {
  const el = marker.getElement();
  const bm = el && el.querySelector(".bus-marker");
  if (!bm) return;
  const deg = marker._heading;
  const has = deg !== null && deg !== undefined;
  bm.classList.toggle("has-dir", has);
  if (has) bm.style.setProperty("--hd", `${Math.round(deg)}deg`);
  if (marker._dirColor) bm.style.setProperty("--dc", marker._dirColor);
  const up = has && (deg > 300 || deg < 60);
  const tt = marker.getTooltip();
  if (tt) { tt.options.offset = L.point(0, up ? -34 : 0); if (tt.isOpen()) tt.update(); }
}

// Flechas pequeñas a lo largo de una línea, cada ARROW_SPACING_M, apuntando en el sentido del recorrido
function arrowIcon(deg, color) {
  return L.divIcon({
    className: "",
    html: `<div class="trail-arrow" style="--hd:${Math.round(deg)}deg;--c:${color}"><svg viewBox="0 0 24 24"><path d="M12 3 21 21 12 16 3 21z"/></svg></div>`,
    iconSize: [18, 18], iconAnchor: [9, 9],
  });
}
function arrowsAlong(points, color) {
  const pts = points.map(asLatLon);
  const out = [];
  if (pts.length < 2) return out;
  let total = 0;
  for (let i = 1; i < pts.length; i++) total += distM(pts[i - 1], pts[i]);
  const step = Math.max(ARROW_SPACING_M, total / ARROW_MAX);
  let next = step / 2, acc = 0;
  for (let i = 1; i < pts.length; i++) {
    const a = pts[i - 1], b = pts[i], d = distM(a, b);
    if (d < 1) continue;
    while (acc + d >= next) {
      const f = (next - acc) / d;
      const p = [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f];
      out.push(L.marker(p, { icon: arrowIcon(bearingDeg(a, b), color), interactive: false, keyboard: false }));
      next += step;
    }
    acc += d;
  }
  return out;
}

// Actualiza la flecha del marcador y (con un pequeño retraso, para no redibujar en cada trama) las de la línea
function syncDirection(b, now = false) {
  const pts = b.trail.getLatLngs();
  setHeading(b.marker, headingOf(pts));
  clearTimeout(b.arrowTimer);
  b.arrowTimer = setTimeout(() => {
    b.arrows.clearLayers();
    arrowsAlong(b.trail.getLatLngs(), b.color).forEach((m) => b.arrows.addLayer(m));
  }, now ? 0 : 1500);
}

// Encuadra el mapa para que se vean todos los buses con su recorrido
function fitLive() {
  const layers = [];
  busList().filter(isShown).forEach((b) => {
    if (b.hasPos) layers.push(b.marker);
    if (b.trail.getLatLngs().length > 1) layers.push(b.trail);
  });
  if (!layers.length) return;
  map.fitBounds(L.featureGroup(layers).getBounds(), { maxZoom: 15, padding: [40, 40] });
  mapCentered = true;
}

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
