// OnRoute · Utilidades: constantes, distancias, formatos de fecha y capas del mapa

const $ = (id) => document.getElementById(id);

// Nombre de la ruta y del bus (vienen del servidor: ROUTE_NAME y BUS_NAME en el .env)
const CFG = window.ONROUTE || {};
const ROUTE_NAME = CFG.routeName || "Ruta";
const BUS_NAME = CFG.busName || "Bus";   // "Bus de Luis": así la gente sabe a qué bus se monta

const PASSAGE_RADIUS_M = 200;
const PAGE_SIZE = 5;
const SIM_POINTS_PER_SEC = 4; // a velocidad 1×
const ROUTE_COLORS = ["#f4a261", "#2ecc71", "#8ecae6", "#e63946", "#ffb703", "#9b5de5", "#00b4d8", "#ff7aa2"];
const SIGNAL_TIMEOUT_MS = 60000; // 1 minuto sin datos = sin señal
const MOVING_KMH = 3;            // por debajo de esta velocidad se considera detenido

// Distancia en metros entre dos puntos [lat, lon]
function distM(a, b) {
  const R = 6371000, rad = Math.PI / 180;
  const dLat = (b[0] - a[0]) * rad, dLon = (b[1] - a[1]) * rad;
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(a[0] * rad) * Math.cos(b[0] * rad) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(h));
}
function fmtDistance(m) { return m < 1000 ? `${Math.round(m)} m` : `${(m / 1000).toFixed(1).replace(".", ",")} km`; }
function fmtDuration(ms) {
  const min = Math.round(ms / 60000);
  if (min < 1) return "menos de 1 min";
  if (min < 60) return `${min} min`;
  const h = Math.floor(min / 60), r = min % 60;
  return r ? `${h} h ${r} min` : `${h} h`;
}
function hour24(h, str) {
  if (/p\.?\s?m/i.test(str) && h < 12) return h + 12;
  if (/a\.?\s?m/i.test(str) && h === 12) return 0;
  return h;
}
function fmtLast(str) {
  const t = parseTime(str);
  if (t === null) return str;
  const d = new Date(t), now = new Date();
  const hh = String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
  if (d.toDateString() === now.toDateString()) return `hoy ${hh}`;
  return `${String(d.getDate()).padStart(2, "0")}/${String(d.getMonth() + 1).padStart(2, "0")} ${hh}`;
}
// Intenta leer fechas como "2026-10-06 14:30:00" o "06/10/2026 14:30:00". Si no puede, devuelve null.
function parseTime(str) {
  if (!str) return null;
  str = String(str);
  let m = str.match(/(\d{4})-(\d{2})-(\d{2})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?/);
  if (m) return new Date(+m[1], m[2] - 1, +m[3], hour24(+m[4], str), +m[5], +(m[6] || 0)).getTime();
  m = str.match(/(\d{1,2})[\/-](\d{1,2})[\/-](\d{4}),?\s+(\d{1,2}):(\d{2})(?::(\d{2}))?/);
  if (m) return new Date(+m[3], m[2] - 1, +m[1], hour24(+m[4], str), +m[5], +(m[6] || 0)).getTime();
  return null;
}

// Tiles normales de OpenStreetMap: gratis y sin API key.
function addTiles(m) {
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: "&copy; <a href='https://www.openstreetmap.org/copyright'>OpenStreetMap</a> contributors",
  }).addTo(m);
}
