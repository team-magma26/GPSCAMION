import argparse
import json
import math
import os
import queue
import socket
import threading
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg2
from psycopg2 import extensions
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template, request

load_dotenv()


DB_HOST = os.environ["DB_HOST"]
DB_PORT = os.environ.get("DB_PORT", "5432")
DB_NAME = os.environ["DB_NAME"]
DB_USER = os.environ["DB_USER"]
DB_PASSWORD = os.environ["DB_PASSWORD"]
DB_SSLMODE = os.environ.get("DB_SSLMODE", "require")


APP_NAME = os.environ.get("APP_NAME", "GPS Truck Tracker")


ACCOUNT_ID = os.environ.get("ACCOUNT_ID", "main")


APP_TZ = ZoneInfo(os.environ.get("APP_TZ", "America/Bogota"))


def now_local() -> datetime:
    """Hora actual en APP_TZ, sin tzinfo (naive), para que siga siendo
    comparable con los datetime que se leen de la BD como texto."""
    return datetime.now(APP_TZ).replace(tzinfo=None)


NOTIFY_CHANNEL = "new_location"

db_lock = threading.Lock()


SESSION_GAP_SECONDS = 60


PASSAGE_GAP_SECONDS = 300

session_lock = threading.Lock()

_last_packet_dt = None
_active_session_id = None


MAX_HISTORY_RANGE_POINTS = 3000

# ---------- Semana 2: tiempo de llegada (ETA) con tráfico ----------
# Llave de la API de TomTom (https://developer.tomtom.com). Sin llave se usa un
# cálculo aproximado (distancia / velocidad) y la página lo indica.
TOMTOM_API_KEY = os.environ.get("TOMTOM_API_KEY", "").strip()
ETA_FALLBACK_KMH = float(os.environ.get("ETA_FALLBACK_KMH", "22"))  # velocidad promedio del bus sin datos de tráfico
ETA_BUS_MAX_AGE_S = 180      # si el último dato del bus es más viejo que esto, se considera sin señal
ETA_HERE_M = 80              # a menos de esto, "el bus está donde estás"
ETA_CACHE_S = 30             # misma consulta dentro de 30 s no vuelve a llamar a la API (cuida la cuota gratis)
_eta_cache = {}
_eta_cache_lock = threading.Lock()

# ---------- Semana 3: métricas de uso (página oculta /metricas) ----------
# Si METRICS_KEY tiene valor, /metricas solo abre con ?key=<valor>; si no, cualquiera con el enlace.
METRICS_KEY = os.environ.get("METRICS_KEY", "").strip()
USAGE_EVENTS = {"visit", "geo_granted", "geo_denied", "geo_unavailable"}

parser = argparse.ArgumentParser(description="GPS tracking: 1 writer (UDP+insert) + N readers (solo lectura)")
parser.add_argument("--role", choices=["writer", "reader"], required=True,
                     help="writer = recibe UDP e inserta a la RDS. reader = solo lee/muestra.")
parser.add_argument("--udp-port", type=int, default=5000)
parser.add_argument("--http-port", type=int, default=8000)
args = parser.parse_args()

ROLE = args.role
UDP_PORT = args.udp_port
HTTP_PORT = args.http_port
IS_WRITER = ROLE == "writer"


def get_connection():
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
        user=DB_USER, password=DB_PASSWORD, sslmode=DB_SSLMODE,
    )


def init_db():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS locations (
            id SERIAL PRIMARY KEY,
            account_id TEXT NOT NULL,
            lat DOUBLE PRECISION NOT NULL,
            lon DOUBLE PRECISION NOT NULL,
            gps_time TEXT NOT NULL,
            received_at TEXT NOT NULL
        )
        """
    )

    cur.execute("ALTER TABLE locations ADD COLUMN IF NOT EXISTS session_id TEXT")
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_locations_account_id ON locations (account_id, id DESC)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_locations_session_id ON locations (session_id, id ASC)"
    )

    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_locations_received_at ON locations (received_at)"
    )

    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_locations_acc_received ON locations (account_id, received_at)"
    )

    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_locations_acc_gpstime ON locations (account_id, gps_time)"
    )

    # Acelera la búsqueda "¿tiene historial cerca de este lugar?" (/api/has_history)
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_locations_acc_latlon ON locations (account_id, lat, lon)"
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id SERIAL PRIMARY KEY,
            session_id TEXT NOT NULL UNIQUE,
            started_at TEXT NOT NULL
        )
        """
    )

    cur.execute(
        "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS account_id TEXT NOT NULL DEFAULT 'main'"
    )

    # Datos extra que manda el ESP32 (opcionales: la app Android no los manda)
    cur.execute("ALTER TABLE locations ADD COLUMN IF NOT EXISTS battery_v DOUBLE PRECISION")
    cur.execute("ALTER TABLE locations ADD COLUMN IF NOT EXISTS signal_csq INTEGER")
    cur.execute("ALTER TABLE locations ADD COLUMN IF NOT EXISTS seq INTEGER")

    # Eventos de uso de la página (visitas, permisos de ubicación, consultas de ETA)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS usage_events (
            id SERIAL PRIMARY KEY,
            account_id TEXT NOT NULL,
            ts TEXT NOT NULL,
            event TEXT NOT NULL,
            client_id TEXT,
            device TEXT,
            value DOUBLE PRECISION,
            detail TEXT
        )
        """
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_usage_acc_ts ON usage_events (account_id, ts)"
    )
    conn.commit()
    cur.close()
    conn.close()


def start_new_session() -> str:
    """Registra un nuevo recorrido de ACCOUNT_ID y devuelve su session_id.
    Todo lo que se inserte después de esto queda "marcado" con esta sesión,
    así el mapa puede trazar solo el recorrido actual."""
    now = now_local()
    session_id = now.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO sessions (session_id, account_id, started_at) VALUES (%s, %s, %s)",
        (session_id, ACCOUNT_ID, now.strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.commit()
    cur.close()
    conn.close()
    return session_id


def get_current_session_id():
    """Sesión más reciente registrada para ACCOUNT_ID. La consulta cualquier
    proceso (writer o reader) directamente contra la BD — no depende de que
    sea el mismo proceso que la creó."""
    with db_lock:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT session_id FROM sessions WHERE account_id = %s ORDER BY id DESC LIMIT 1",
            (ACCOUNT_ID,),
        )
        row = cur.fetchone()
        cur.close()
        conn.close()
    return row[0] if row else None


def get_last_received_at():
    """received_at del último punto guardado de ACCOUNT_ID, para saber
    cuánto tiempo llevaba el GPS callado la última vez que corrió el writer."""
    with db_lock:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT received_at FROM locations WHERE account_id = %s ORDER BY id DESC LIMIT 1",
            (ACCOUNT_ID,),
        )
        row = cur.fetchone()
        cur.close()
        conn.close()
    return row[0] if row else None


def notify_session_start(session_id: str):
    """Avisa en vivo (vía LISTEN/NOTIFY) a todas las páginas ya abiertas de
    esta cuenta que empezó una sesión nueva, para que borren el trazo sin
    necesidad de recargar la página."""
    conn = get_connection()
    conn.set_isolation_level(extensions.ISOLATION_LEVEL_AUTOCOMMIT)
    cur = conn.cursor()
    cur.execute(
        f"NOTIFY {NOTIFY_CHANNEL}, %s",
        (json.dumps({
            "type": "session_start",
            "session_id": session_id,
            "account_id": ACCOUNT_ID,
        }),),
    )
    cur.close()
    conn.close()


def init_session_tracking():
    """Se llama una vez al arrancar el writer. Retoma la sesión activa si el
    último paquete llegó hace poco, o abre una sesión nueva si no hay
    ninguna todavía o si el silencio ya superó SESSION_GAP_SECONDS."""
    global _last_packet_dt, _active_session_id

    existing_session_id = get_current_session_id()
    last_received_at = get_last_received_at()

    if existing_session_id is None:
        _active_session_id = start_new_session()
        notify_session_start(_active_session_id)
        _last_packet_dt = None
        return

    _active_session_id = existing_session_id
    _last_packet_dt = (
        datetime.strptime(last_received_at, "%Y-%m-%d %H:%M:%S")
        if last_received_at else None
    )


def save_location(lat: float, lon: float, gps_time: str, extras: dict = None):
    global _last_packet_dt, _active_session_id
    extras = extras or {}

    received_at_dt = now_local()
    received_at = received_at_dt.strftime("%Y-%m-%d %H:%M:%S")


    with session_lock:
        if _last_packet_dt is None or (received_at_dt - _last_packet_dt).total_seconds() > SESSION_GAP_SECONDS:
            _active_session_id = start_new_session()
            notify_session_start(_active_session_id)
        _last_packet_dt = received_at_dt
        session_id = _active_session_id

    payload = {**location_to_payload(lat, lon, gps_time), "account_id": ACCOUNT_ID}
    if extras.get("battery_v") is not None:
        payload["battery_pct"] = battery_pct(extras["battery_v"])

    with db_lock:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO locations (account_id, session_id, lat, lon, gps_time, received_at, "
            "battery_v, signal_csq, seq) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (ACCOUNT_ID, session_id, lat, lon, gps_time, received_at,
             extras.get("battery_v"), extras.get("signal_csq"), extras.get("seq")),
        )
        cur.execute(
            f"NOTIFY {NOTIFY_CHANNEL}, %s",
            (json.dumps(payload),)
        )
        conn.commit()
        cur.close()
        conn.close()


def get_latest_location():

    with db_lock:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT lat, lon, gps_time FROM locations WHERE account_id = %s ORDER BY id DESC LIMIT 1",
            (ACCOUNT_ID,),
        )
        row = cur.fetchone()
        cur.close()
        conn.close()
    return row


def get_history(limit: int = 1000):
    """Recorrido de la sesión actual únicamente (el historial completo sigue
    intacto en la tabla, solo no se muestra en el mapa)."""
    session_id = get_current_session_id()
    with db_lock:
        conn = get_connection()
        cur = conn.cursor()
        if session_id:
            cur.execute(
                "SELECT lat, lon, gps_time FROM locations WHERE session_id = %s ORDER BY id DESC LIMIT %s",
                (session_id, limit),
            )
        else:

            cur.execute(
                "SELECT lat, lon, gps_time FROM locations WHERE account_id = %s ORDER BY id DESC LIMIT %s",
                (ACCOUNT_ID, limit),
            )
        rows = cur.fetchall()
        cur.close()
        conn.close()
    return list(reversed(rows))


def get_history_range(start_dt: str, end_dt: str, max_points: int = MAX_HISTORY_RANGE_POINTS):
    """Historial de ACCOUNT_ID dentro de una ventana de fecha/hora arbitraria
    elegida por el usuario (no se limita a la sesión activa como get_history()).
    Filtra y ordena por 'gps_time': es la hora que reporta el propio GPS, o
    sea el momento real en que el camión estuvo ahí (a diferencia de
    'received_at', que es cuándo el servidor recibió el paquete y puede
    traer algo de retraso de red). Asume que gps_time viene en formato
    'YYYY-MM-DD HH:MM:SS'; si el dispositivo llegara a mandar otro formato,
    esta comparación de texto dejaría de ser confiable para ese punto.
    Devuelve también el texto de la sentencia SQL ejecutada, para poder
    mostrarla o registrarla."""
    query = (
        "SELECT lat, lon, gps_time, received_at FROM locations "
        "WHERE account_id = %s AND gps_time BETWEEN %s AND %s "
        "ORDER BY gps_time ASC LIMIT %s"
    )
    params = (ACCOUNT_ID, start_dt, end_dt, max_points)

    with db_lock:
        conn = get_connection()
        cur = conn.cursor()
        sql_text = cur.mogrify(query, params).decode("utf-8")
        print(f"[SQL] {sql_text}")
        cur.execute(query, params)
        rows = cur.fetchall()
        cur.close()
        conn.close()
    return rows, sql_text


def get_sessions(limit: int = 50, start_dt: str = None, end_dt: str = None):
    """Lista de recorridos (sesiones) ya registrados para ACCOUNT_ID, con el
    rango de gps_time que cubre cada uno y cuántos puntos tiene, para que el
    usuario los vea en la tabla de recorridos del frontend.
    Si se pasan start_dt/end_dt (formato 'YYYY-MM-DD HH:MM:SS'), solo se
    devuelven los recorridos cuyo rango de gps_time se solapa con esa
    ventana (el recorrido tuvo al menos un punto dentro de ese período)."""
    having_clause = ""
    params = [ACCOUNT_ID]
    if start_dt and end_dt:
        having_clause = "HAVING MIN(gps_time) <= %s AND MAX(gps_time) >= %s"
        params += [end_dt, start_dt]
    params.append(limit)

    query = (
        "SELECT session_id, MIN(gps_time), MAX(gps_time), COUNT(*) "
        "FROM locations WHERE account_id = %s AND session_id IS NOT NULL "
        "GROUP BY session_id " + having_clause +
        " ORDER BY MIN(gps_time) DESC LIMIT %s"
    )

    with db_lock:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(query, params)
        rows = cur.fetchall()
        cur.close()
        conn.close()
    return rows


def get_session_route(session_id: str, max_points: int = MAX_HISTORY_RANGE_POINTS):
    """Puntos de un recorrido (sesión) específico ya registrado, ordenados
    por gps_time, para dibujarlo en el mapa igual que get_history_range()."""
    with db_lock:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT lat, lon, gps_time, received_at FROM locations "
            "WHERE account_id = %s AND session_id = %s ORDER BY gps_time ASC LIMIT %s",
            (ACCOUNT_ID, session_id, max_points),
        )
        rows = cur.fetchall()
        cur.close()
        conn.close()
    return rows


def get_passages_near(target_lat: float, target_lon: float, radius_m: float,
                       start_dt: str = None, end_dt: str = None,
                       max_points: int = MAX_HISTORY_RANGE_POINTS):
    """Puntos de ACCOUNT_ID dentro de radius_m metros del punto
    (target_lat, target_lon), calculando la distancia con la fórmula de
    Haversine directo en SQL (sin necesidad de extensiones tipo PostGIS).
    Si se pasan start_dt/end_dt (formato 'YYYY-MM-DD HH:MM:SS'), acota
    además por 'gps_time' (la hora real del GPS, no la de llegada al
    servidor), igual que get_history_range().
    Devuelve también el texto de la sentencia SQL ejecutada."""
    date_filter = ""
    params = [target_lat, target_lon, target_lat, ACCOUNT_ID]
    if start_dt and end_dt:
        date_filter = "AND gps_time BETWEEN %s AND %s"
        params += [start_dt, end_dt]
    params += [radius_m, max_points]

    query = f"""
        SELECT lat, lon, gps_time, received_at, distance_m FROM (
            SELECT lat, lon, gps_time, received_at,
                6371000 * acos(
                    LEAST(1.0, GREATEST(-1.0,
                        cos(radians(%s)) * cos(radians(lat)) * cos(radians(lon) - radians(%s)) +
                        sin(radians(%s)) * sin(radians(lat))
                    ))
                ) AS distance_m
            FROM locations
            WHERE account_id = %s
            {date_filter}
        ) sub
        WHERE distance_m <= %s
        ORDER BY gps_time ASC
        LIMIT %s
    """

    with db_lock:
        conn = get_connection()
        cur = conn.cursor()
        sql_text = cur.mogrify(query, params).decode("utf-8")
        print(f"[SQL] {sql_text}")
        cur.execute(query, params)
        rows = cur.fetchall()
        cur.close()
        conn.close()
    return rows, sql_text


def has_history_near(target_lat: float, target_lon: float, radius_m: float) -> bool:
    """True si ACCOUNT_ID tiene al menos un punto dentro de radius_m metros.
    Primero filtra por un rectángulo aproximado de lat/lon (barato) y solo
    entonces calcula Haversine; se detiene en el primer punto que encuentra."""
    dlat = radius_m / 111320.0
    cos_lat = max(math.cos(math.radians(target_lat)), 0.01)
    dlon = radius_m / (111320.0 * cos_lat)

    query = """
        SELECT 1 FROM locations
        WHERE account_id = %s
          AND lat BETWEEN %s AND %s
          AND lon BETWEEN %s AND %s
          AND 6371000 * acos(
                LEAST(1.0, GREATEST(-1.0,
                    cos(radians(%s)) * cos(radians(lat)) * cos(radians(lon) - radians(%s)) +
                    sin(radians(%s)) * sin(radians(lat))
                ))
              ) <= %s
        LIMIT 1
    """
    params = (
        ACCOUNT_ID,
        target_lat - dlat, target_lat + dlat,
        target_lon - dlon, target_lon + dlon,
        target_lat, target_lon, target_lat,
        radius_m,
    )

    with db_lock:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(query, params)
        row = cur.fetchone()
        cur.close()
        conn.close()
    return row is not None


def group_passages(rows, gap_seconds: int = PASSAGE_GAP_SECONDS):
    """Agrupa filas (lat, lon, gps_time, received_at, distance_m) ordenadas
    por gps_time en "pasadas": si el camión estuvo cerca del punto en varias
    lecturas seguidas (menos de gap_seconds entre una y otra, medido con la
    hora real del GPS), se reporta como una sola pasada con su hora de
    entrada y de salida, en vez de listar cada lectura individual.
    received_at se conserva en cada pasada solo como referencia de cuándo
    llegó ese dato al servidor."""
    passages = []
    current = None
    prev_dt = None

    for lat, lon, gps_time, received_at, distance_m in rows:
        try:
            gps_dt = datetime.strptime(gps_time, "%Y-%m-%d %H:%M:%S")
        except ValueError:

            print(f"[WARN] gps_time con formato inesperado, se omite: {gps_time!r}")
            continue

        if current is None or (gps_dt - prev_dt).total_seconds() > gap_seconds:
            if current is not None:
                passages.append(current)
            current = {
                "start_gps_time": gps_time,
                "end_gps_time": gps_time,
                "start_received_at": received_at,
                "end_received_at": received_at,
                "lat": lat,
                "lon": lon,
                "points_count": 1,
                "min_distance_m": round(distance_m, 1),
            }
        else:
            current["end_gps_time"] = gps_time
            current["end_received_at"] = received_at
            current["points_count"] += 1
            if distance_m < current["min_distance_m"]:
                current["min_distance_m"] = round(distance_m, 1)
                current["lat"] = lat
                current["lon"] = lon

        prev_dt = gps_dt

    if current is not None:
        passages.append(current)

    return passages


# ================= Semana 2 y 3: batería, ETA y métricas =================

# Curva aproximada de una celda Li-ion 18650 (2 celdas en paralelo = misma curva)
_LIION_CURVE = [(3.00, 0), (3.30, 5), (3.50, 15), (3.60, 25), (3.70, 45),
                (3.80, 60), (3.90, 72), (4.00, 83), (4.10, 93), (4.20, 100)]


def battery_pct(volts):
    """Convierte el voltaje de la batería en un porcentaje aproximado."""
    if volts is None:
        return None
    if volts <= _LIION_CURVE[0][0]:
        return 0
    for (v0, p0), (v1, p1) in zip(_LIION_CURVE, _LIION_CURVE[1:]):
        if volts <= v1:
            return round(p0 + (p1 - p0) * (volts - v0) / (v1 - v0))
    return 100


def haversine_m(lat1, lon1, lat2, lon2):
    r = math.radians
    h = (math.sin(r(lat2 - lat1) / 2) ** 2 +
         math.cos(r(lat1)) * math.cos(r(lat2)) * math.sin(r(lon2 - lon1) / 2) ** 2)
    return 2 * 6371000 * math.asin(math.sqrt(h))


def parse_dt(text):
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def get_recent_locations(limit: int = 8):
    """Últimas lecturas del bus, la más reciente primero."""
    with db_lock:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT lat, lon, gps_time, received_at FROM locations "
            "WHERE account_id = %s ORDER BY id DESC LIMIT %s",
            (ACCOUNT_ID, limit),
        )
        rows = cur.fetchall()
        cur.close()
        conn.close()
    return rows


def recent_speed_kmh(rows):
    """Velocidad del bus en el último minuto aprox. (rows: más reciente primero)."""
    if len(rows) < 2:
        return None
    t_new = parse_dt(rows[0][2])
    dist = 0.0
    t_old = None
    for (la1, lo1, g1, _), (la0, lo0, g0, _) in zip(rows, rows[1:]):
        t0 = parse_dt(g0)
        if t_new is None or t0 is None or (t_new - t0).total_seconds() > 90:
            break
        dist += haversine_m(la0, lo0, la1, lo1)
        t_old = t0
    if t_old is None:
        return None
    secs = (t_new - t_old).total_seconds()
    return dist / secs * 3.6 if secs > 0 else None


def tomtom_route(blat, blon, ulat, ulon):
    """Ruta del bus hasta el usuario con el tráfico actual (TomTom Routing API).
    Devuelve (segundos, metros, retraso_por_tráfico_s, puntos_de_la_ruta)."""
    query = urllib.parse.urlencode({
        "key": TOMTOM_API_KEY, "traffic": "true", "travelMode": "bus",
        "routeType": "fastest", "computeTravelTimeFor": "all",
    })
    url = (f"https://api.tomtom.com/routing/1/calculateRoute/"
           f"{blat:.6f},{blon:.6f}:{ulat:.6f},{ulon:.6f}/json?{query}")
    with urllib.request.urlopen(url, timeout=8) as resp:
        data = json.load(resp)
    route = data["routes"][0]
    summary = route["summary"]
    pts = [(p["latitude"], p["longitude"]) for leg in route.get("legs", []) for p in leg.get("points", [])]
    step = max(1, len(pts) // 250)
    pts = pts[::step] + ([pts[-1]] if pts and (len(pts) - 1) % step else [])
    return (summary["travelTimeInSeconds"], summary["lengthInMeters"],
            summary.get("trafficDelayInSeconds", 0), pts)


def estimate_eta(blat, blon, ulat, ulon, speed_kmh):
    """Cálculo de respaldo, sin API: distancia en línea recta x 1.35 (las calles
    no son rectas) dividida por la velocidad del bus (la actual si se está
    moviendo, o una velocidad promedio)."""
    meters = haversine_m(blat, blon, ulat, ulon) * 1.35
    kmh = speed_kmh if speed_kmh and speed_kmh >= 8 else ETA_FALLBACK_KMH
    return meters / (kmh / 3.6), meters, 0, [(blat, blon), (ulat, ulon)]


def compute_eta(blat, blon, ulat, ulon, speed_kmh):
    key = (round(blat, 4), round(blon, 4), round(ulat, 3), round(ulon, 3))
    now = time.time()
    with _eta_cache_lock:
        hit = _eta_cache.get(key)
        if hit and now - hit[0] < ETA_CACHE_S:
            return hit[1]
    result = None
    if TOMTOM_API_KEY:
        try:
            result = (*tomtom_route(blat, blon, ulat, ulon), "tomtom")
        except Exception as exc:
            print(f"[ETA] TomTom falló, se usa el estimado: {exc}")
    if result is None:
        result = (*estimate_eta(blat, blon, ulat, ulon, speed_kmh), "estimado")
    with _eta_cache_lock:
        if len(_eta_cache) > 500:
            _eta_cache.clear()
        _eta_cache[key] = (now, result)
    return result


def record_usage(event, client_id=None, device=None, value=None, detail=None):
    """Guarda un evento de uso. Nunca rompe la página: si la tabla todavía no
    existe (el writer no se ha reiniciado con este código) simplemente no guarda."""
    try:
        with db_lock:
            conn = get_connection()
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO usage_events (account_id, ts, event, client_id, device, value, detail) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (ACCOUNT_ID, now_local().strftime("%Y-%m-%d %H:%M:%S"), event,
                 (client_id or "")[:40] or None, (device or "")[:10] or None, value,
                 (detail or "")[:300] or None),
            )
            conn.commit()
            cur.close()
            conn.close()
    except Exception as exc:
        print(f"[USAGE] No se guardó el evento {event}: {exc}")


def _query(sql, params):
    with db_lock:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall()
        cur.close()
        conn.close()
    return rows


def _percentile(values, p):
    if not values:
        return None
    values = sorted(values)
    k = (len(values) - 1) * p / 100
    f = math.floor(k)
    c = min(f + 1, len(values) - 1)
    return values[f] + (values[c] - values[f]) * (k - f)


def _downsample(items, max_items=300):
    step = max(1, math.ceil(len(items) / max_items))
    return items[::step]


def build_metrics(days: int):
    now = now_local()
    since = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0)
    since_s = since.strftime("%Y-%m-%d %H:%M:%S")
    day_keys = [(since + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]

    # ---------- Bus: operación, calidad de los datos, batería y señal ----------
    try:
        rows = _query(
            "SELECT lat, lon, gps_time, received_at, session_id, battery_v, signal_csq, seq "
            "FROM locations WHERE account_id = %s AND received_at >= %s ORDER BY id ASC",
            (ACCOUNT_ID, since_s),
        )
        extras_ok = True
    except Exception:
        rows = [(*r, None, None, None) for r in _query(
            "SELECT lat, lon, gps_time, received_at, session_id "
            "FROM locations WHERE account_id = %s AND received_at >= %s ORDER BY id ASC",
            (ACCOUNT_ID, since_s),
        )]
        extras_ok = False

    per_day = {d: {"date": d, "points": 0, "km": 0.0, "active_s": 0.0, "max_kmh": 0.0,
                   "trips": set(), "lost": 0} for d in day_keys}
    latencies = []
    hourly = {}
    battery_series, signal_series = [], []
    prev = None
    total_lost = 0
    for lat, lon, gps_time, received_at, session_id, bat, csq, seq in rows:
        rdt = parse_dt(received_at)
        gdt = parse_dt(gps_time)
        day = received_at[:10]
        d = per_day.get(day)
        if d is None:
            prev = None
            continue
        d["points"] += 1
        if session_id:
            d["trips"].add(session_id)
        if rdt and gdt:
            lag = (rdt - gdt).total_seconds()
            if -5 <= lag <= 3600:
                latencies.append(max(lag, 0))
        if rdt and (now - rdt).total_seconds() <= 24 * 3600:
            hk = rdt.strftime("%Y-%m-%d %H:00")
            hourly[hk] = hourly.get(hk, 0) + 1
            if bat is not None:
                battery_series.append({"t": received_at, "v": round(bat, 3), "pct": battery_pct(bat)})
            if csq is not None:
                signal_series.append({"t": received_at, "csq": csq, "dbm": -113 + 2 * csq})
        if prev is not None and prev["session"] == session_id and gdt and prev["gdt"]:
            dt = (gdt - prev["gdt"]).total_seconds()
            if 0 < dt <= 120:
                dist = haversine_m(prev["lat"], prev["lon"], lat, lon)
                kmh = dist / dt * 3.6
                if kmh <= 120:            # descarta saltos del GPS
                    d["km"] += dist / 1000
                    d["active_s"] += dt
                    if dist >= 20:
                        d["max_kmh"] = max(d["max_kmh"], kmh)
            if seq is not None and prev["seq"] is not None and seq > prev["seq"]:
                gap = seq - prev["seq"] - 1
                if gap < 1000:
                    d["lost"] += gap
                    total_lost += gap
        prev = {"lat": lat, "lon": lon, "gdt": gdt, "session": session_id, "seq": seq}

    days_out = []
    for d in per_day.values():
        hours = d["active_s"] / 3600
        days_out.append({
            "date": d["date"], "points": d["points"], "trips": len(d["trips"]),
            "km": round(d["km"], 2), "active_h": round(hours, 2),
            "avg_kmh": round(d["km"] / hours, 1) if hours > 0.01 else None,
            "max_kmh": round(d["max_kmh"], 1) if d["max_kmh"] else None,
            "lost": d["lost"],
        })

    hourly_out = []
    for i in range(23, -1, -1):
        hk = (now - timedelta(hours=i)).strftime("%Y-%m-%d %H:00")
        hourly_out.append({"hour": hk, "packets": hourly.get(hk, 0)})

    last = rows[-1] if rows else None
    if last is None:
        lr = _query(
            "SELECT lat, lon, gps_time, received_at FROM locations WHERE account_id = %s ORDER BY id DESC LIMIT 1",
            (ACCOUNT_ID,),
        )
        last = (*lr[0], None, None, None, None) if lr else None
    last_seen = last[3] if last else None
    last_dt = parse_dt(last_seen) if last_seen else None
    last_bat = next((r[5] for r in reversed(rows) if r[5] is not None), None)
    last_csq = next((r[6] for r in reversed(rows) if r[6] is not None), None)

    # Autonomía: cuánto baja la batería por hora en las últimas 2 h
    drain = None
    recent_bat = [b for b in battery_series if parse_dt(b["t"]) and (now - parse_dt(b["t"])).total_seconds() <= 7200]
    if len(recent_bat) >= 10:
        t0, t1 = parse_dt(recent_bat[0]["t"]), parse_dt(recent_bat[-1]["t"])
        hrs = (t1 - t0).total_seconds() / 3600
        dp = recent_bat[0]["pct"] - recent_bat[-1]["pct"]
        if hrs >= 0.25 and dp > 0:
            drain = dp / hrs
    received = len(rows)
    device = {
        "last_seen": last_seen,
        "online": bool(last_dt and (now - last_dt).total_seconds() <= 60),
        "battery_v": round(last_bat, 2) if last_bat is not None else None,
        "battery_pct": battery_pct(last_bat),
        "battery_drain_pct_h": round(drain, 1) if drain else None,
        "battery_hours_left": round(battery_pct(last_bat) / drain, 1) if drain and last_bat is not None else None,
        "signal_csq": last_csq,
        "signal_dbm": -113 + 2 * last_csq if last_csq is not None else None,
        "extras_enabled": extras_ok,
    }
    quality = {
        "received": received,
        "lost": total_lost,
        "loss_pct": round(total_lost / (received + total_lost) * 100, 2) if received + total_lost else None,
        "latency_median_s": round(_percentile(latencies, 50), 1) if latencies else None,
        "latency_p95_s": round(_percentile(latencies, 95), 1) if latencies else None,
    }

    # ---------- Usuarios de la página ----------
    try:
        ev = _query(
            "SELECT ts, event, client_id, device, value FROM usage_events "
            "WHERE account_id = %s AND ts >= %s ORDER BY id ASC",
            (ACCOUNT_ID, since_s),
        )
        usage_ok = True
    except Exception:
        ev, usage_ok = [], False
    u_day = {d: {"date": d, "visits": 0, "visitors": set(), "eta": 0} for d in day_keys}
    by_hour = [0] * 24
    counts = {"geo_granted": 0, "geo_denied": 0, "geo_unavailable": 0}
    devices = {"mobile": 0, "desktop": 0}
    seen_dev = set()
    eta_values = []
    all_visitors = set()
    for ts, event, client_id, dev, value in ev:
        d = u_day.get(ts[:10])
        if d is None:
            continue
        if event == "visit":
            d["visits"] += 1
            if client_id:
                d["visitors"].add(client_id)
                all_visitors.add(client_id)
            try:
                by_hour[int(ts[11:13])] += 1
            except ValueError:
                pass
            if dev in devices and client_id not in seen_dev:
                devices[dev] += 1
                seen_dev.add(client_id)
        elif event == "eta":
            d["eta"] += 1
            if value is not None:
                eta_values.append(value)
        elif event in counts:
            counts[event] += 1
    asked = counts["geo_granted"] + counts["geo_denied"]
    usage = {
        "enabled": usage_ok,
        "days": [{"date": d["date"], "visits": d["visits"], "visitors": len(d["visitors"]), "eta": d["eta"]}
                 for d in u_day.values()],
        "total_visits": sum(d["visits"] for d in u_day.values()),
        "unique_visitors": len(all_visitors),
        "eta_queries": sum(d["eta"] for d in u_day.values()),
        "eta_avg_min": round(sum(eta_values) / len(eta_values), 1) if eta_values else None,
        "geo": counts,
        "geo_accept_pct": round(counts["geo_granted"] / asked * 100) if asked else None,
        "devices": devices,
        "visits_by_hour": by_hour,
    }

    return {
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "days": days,
        "device": device,
        "quality": quality,
        "operation": {
            "days": days_out,
            "total_km": round(sum(d["km"] for d in days_out), 2),
            "total_active_h": round(sum(d["active_h"] for d in days_out), 2),
            "total_trips": sum(d["trips"] for d in days_out),
        },
        "hourly_packets": hourly_out,
        "battery_series": _downsample(battery_series),
        "signal_series": _downsample(signal_series),
        "usage": usage,
    }


subscribers = []
subscribers_lock = threading.Lock()


def broadcast(payload: dict):
    with subscribers_lock:
        for q in subscribers:
            q.put(payload)


def location_to_payload(lat, lon, gps_time):
    date_part, _, time_part = gps_time.partition(" ")
    return {"type": "location", "lat": lat, "lon": lon, "date": date_part, "time": time_part}


def _opt_float(value, lo, hi):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if lo <= v <= hi else None


def parse_message(raw: str):
    """Lat, Lon y Time son obligatorios. Bat (voltaje de la batería), Sig
    (CSQ del módem, 0-31) y Seq (contador de tramas) son opcionales: los manda
    el ESP32 y sirven para las métricas; si no vienen se guardan vacíos."""
    fields = {}
    for part in raw.strip().split(";"):
        if "=" in part:
            key, value = part.split("=", 1)
            fields[key.strip()] = value.strip()
    lat = float(fields["Lat"])
    lon = float(fields["Lon"])
    gps_time = fields["Time"]

    datetime.strptime(gps_time, "%Y-%m-%d %H:%M:%S")

    extras = {"battery_v": _opt_float(fields.get("Bat"), 2.0, 5.0)}
    sig = _opt_float(fields.get("Sig"), 0, 31)
    extras["signal_csq"] = int(sig) if sig is not None else None
    seq = _opt_float(fields.get("Seq"), 0, 2_000_000_000)
    extras["seq"] = int(seq) if seq is not None else None
    return lat, lon, gps_time, extras


def udp_listener():

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", UDP_PORT))
    print(f"[UDP] (writer) Sniffer listening on 0.0.0.0:{UDP_PORT}")
    while True:
        try:
            data, addr = sock.recvfrom(1024)
            raw = data.decode("utf-8", errors="ignore")
            lat, lon, gps_time, extras = parse_message(raw)
            save_location(lat, lon, gps_time, extras)
            print(f"[UDP] (writer) {addr[0]} -> lat={lat} lon={lon} time={gps_time} "
                  f"bat={extras['battery_v']} csq={extras['signal_csq']} seq={extras['seq']}")
        except Exception as exc:
            print(f"[UDP] (writer) Ignored malformed packet: {exc}")


def listen_notifications():

    conn = get_connection()
    conn.set_isolation_level(extensions.ISOLATION_LEVEL_AUTOCOMMIT)
    cur = conn.cursor()
    cur.execute(f"LISTEN {NOTIFY_CHANNEL};")
    print(f"[DB] ({ROLE}) Listening on '{NOTIFY_CHANNEL}' (account: {ACCOUNT_ID})")
    while True:
        conn.poll()
        while conn.notifies:
            notify = conn.notifies.pop(0)
            payload = json.loads(notify.payload)

            if payload.get("account_id", "main") == ACCOUNT_ID:
                broadcast(payload)


app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html", role=ROLE, app_name=APP_NAME)


@app.route("/api/latest")
def api_latest():
    row = get_latest_location()
    if row is None:
        return jsonify({"lat": None, "lon": None, "date": None, "time": None})
    lat, lon, gps_time = row
    return jsonify(location_to_payload(lat, lon, gps_time))


@app.route("/api/session")
def api_session():
    return jsonify({"session_id": get_current_session_id()})


@app.route("/api/history")
def api_history():
    rows = get_history()
    points = [{"lat": lat, "lon": lon, "time": gps_time} for lat, lon, gps_time in rows]
    return jsonify(points)


@app.route("/api/history_range")
def api_history_range():
    """Historial acotado a una ventana de fecha/hora que elige el usuario
    desde el frontend, filtrando por la hora real del GPS (gps_time).
    Parámetros esperados (query string):
      start = 'YYYY-MM-DDTHH:MM'  (formato nativo de <input type="datetime-local">)
      end   = 'YYYY-MM-DDTHH:MM'
      debug = '1' (opcional) -> además de los puntos, devuelve la sentencia SQL ejecutada
    """
    start = request.args.get("start")
    end = request.args.get("end")
    debug = request.args.get("debug") == "1"

    if not start or not end:
        return jsonify({"error": "Se requieren los parámetros 'start' y 'end'"}), 400


    start_norm = start.replace("T", " ") + ":00"
    end_norm = end.replace("T", " ") + ":59"

    now_str = now_local().strftime("%Y-%m-%d %H:%M:%S")
    if start_norm > now_str:
        return jsonify({"error": "La fecha/hora 'desde' no puede ser futura"}), 400

    rows, sql_text = get_history_range(start_norm, end_norm)
    points = [
        {"lat": lat, "lon": lon, "time": gps_time, "received_at": received_at}
        for lat, lon, gps_time, received_at in rows
    ]

    response = {"points": points, "count": len(points)}
    if debug:
        response["sql"] = sql_text
    return jsonify(response)


@app.route("/api/sessions")
def api_sessions():
    """Lista de recorridos (sesiones) ya registrados para ACCOUNT_ID, para
    poblar la tabla de recorridos del frontend. Parámetros (query string):
      limit = cantidad máxima de recorridos a devolver (opcional, default 50)
      start, end = 'YYYY-MM-DDTHH:MM' (opcionales, ambos juntos) -> si se
                   pasan, solo devuelve los recorridos cuyo rango de
                   gps_time se solapa con esa ventana de fecha/hora.
    """
    limit = request.args.get("limit", default=50, type=int)
    start = request.args.get("start")
    end = request.args.get("end")

    if (start and not end) or (end and not start):
        return jsonify({"error": "Si filtras por fecha, se requieren 'start' y 'end'"}), 400

    start_norm = (start.replace("T", " ") + ":00") if start else None
    end_norm = (end.replace("T", " ") + ":59") if end else None

    if start_norm and start_norm > now_local().strftime("%Y-%m-%d %H:%M:%S"):
        return jsonify({"error": "La fecha/hora 'desde' no puede ser futura"}), 400

    rows = get_sessions(limit, start_norm, end_norm)
    sessions = [
        {"session_id": sid, "start_time": start, "end_time": end, "points": points}
        for sid, start, end, points in rows
    ]
    return jsonify({"sessions": sessions})


@app.route("/api/session_route")
def api_session_route():
    """Puntos de un recorrido (sesión) específico ya registrado, para
    dibujarlo en el mapa. Parámetros (query string):
      session_id = identificador del recorrido (obligatorio, viene de /api/sessions)
    """
    session_id = request.args.get("session_id")
    if not session_id:
        return jsonify({"error": "Se requiere el parámetro 'session_id'"}), 400

    rows = get_session_route(session_id)
    points = [
        {"lat": lat, "lon": lon, "time": gps_time, "received_at": received_at}
        for lat, lon, gps_time, received_at in rows
    ]
    return jsonify({"points": points, "count": len(points)})


@app.route("/api/passages")
def api_passages():
    """Busca en qué momentos el camión pasó cerca de un punto elegido en el
    mapa, según la hora real del GPS (gps_time). Parámetros (query string):
      lat, lon   = coordenadas del punto seleccionado (obligatorios)
      radius     = radio de búsqueda en metros (opcional, default 100)
      start, end = 'YYYY-MM-DDTHH:MM' (opcionales, mismo formato que
                   /api/history_range; si se omiten, busca en todo el historial)
      debug      = '1' (opcional) -> además de las pasadas, devuelve la sentencia SQL ejecutada
    """
    lat_param = request.args.get("lat")
    lon_param = request.args.get("lon")
    if not lat_param or not lon_param:
        return jsonify({"error": "Se requieren los parámetros 'lat' y 'lon'"}), 400

    try:
        target_lat = float(lat_param)
        target_lon = float(lon_param)
    except ValueError:
        return jsonify({"error": "'lat' y 'lon' deben ser numéricos"}), 400

    radius_m = request.args.get("radius", default=100, type=float)

    start = request.args.get("start")
    end = request.args.get("end")
    start_norm = (start.replace("T", " ") + ":00") if start else None
    end_norm = (end.replace("T", " ") + ":59") if end else None

    if start_norm and start_norm > now_local().strftime("%Y-%m-%d %H:%M:%S"):
        return jsonify({"error": "La fecha/hora 'desde' no puede ser futura"}), 400

    debug = request.args.get("debug") == "1"

    rows, sql_text = get_passages_near(target_lat, target_lon, radius_m, start_norm, end_norm)
    passages = group_passages(rows)

    response = {"passages": passages, "count": len(passages), "points_matched": len(rows)}
    if debug:
        response["sql"] = sql_text
    return jsonify(response)


@app.route("/api/has_history")
def api_has_history():
    """Responde si el camión tiene historial cerca de un punto (para mostrar
    "Con historial" / "Sin historial" en la búsqueda por lugar).
    Parámetros (query string):
      lat, lon = coordenadas del punto (obligatorios)
      radius   = radio en metros (opcional, default 200, máximo 50000)
    """
    try:
        target_lat = float(request.args.get("lat", ""))
        target_lon = float(request.args.get("lon", ""))
    except ValueError:
        return jsonify({"error": "'lat' y 'lon' son obligatorios y deben ser numéricos"}), 400

    radius_m = request.args.get("radius", default=200, type=float)
    radius_m = min(max(radius_m, 1), 50000)

    return jsonify({"has_history": has_history_near(target_lat, target_lon, radius_m)})


@app.route("/api/eta", methods=["POST"])
def api_eta():
    """Tiempo de llegada del bus hasta donde está el usuario.
    Cuerpo JSON: {"lat": .., "lon": .., "client_id": .. (opcional)}.
    La ubicación del usuario NO se guarda: solo se usa para el cálculo."""
    data = request.get_json(silent=True) or {}
    try:
        ulat = float(data["lat"])
        ulon = float(data["lon"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "Se requieren 'lat' y 'lon' numéricos"}), 400
    if not (-90 <= ulat <= 90 and -180 <= ulon <= 180):
        return jsonify({"error": "Coordenadas fuera de rango"}), 400

    rows = get_recent_locations()
    if not rows:
        return jsonify({"status": "no_data"})
    blat, blon, gps_time, received_at = rows[0]
    last_dt = parse_dt(received_at)
    age = (now_local() - last_dt).total_seconds() if last_dt else None
    base = {"bus": {"lat": blat, "lon": blon, "time": gps_time}, "last_seen": received_at,
            "age_s": round(age) if age is not None else None}
    if age is None or age > ETA_BUS_MAX_AGE_S:
        return jsonify({**base, "status": "offline"})

    straight = haversine_m(blat, blon, ulat, ulon)
    speed = recent_speed_kmh(rows)

    # ¿Se acerca o se aleja? Compara la distancia de ahora con la de hace ~30-90 s
    trend = "unknown"
    t_new = parse_dt(gps_time)
    for la, lo, g, _ in rows[1:]:
        t_old = parse_dt(g)
        if t_new and t_old and (t_new - t_old).total_seconds() >= 25:
            if (t_new - t_old).total_seconds() <= 120:
                diff = haversine_m(la, lo, ulat, ulon) - straight
                trend = "approaching" if diff > 15 else "leaving" if diff < -15 else "stopped"
            break

    if straight <= ETA_HERE_M:
        record_usage("eta", data.get("client_id"), None, 0, "here")
        return jsonify({**base, "status": "here", "straight_m": round(straight), "trend": trend})

    seconds, meters, delay, path, source = compute_eta(blat, blon, ulat, ulon, speed)
    eta_min = seconds / 60
    record_usage("eta", data.get("client_id"), None, round(eta_min, 1), source)
    return jsonify({
        **base, "status": "ok", "source": source,
        "eta_s": round(seconds), "eta_min": max(1, round(eta_min)),
        "distance_m": round(meters), "straight_m": round(straight),
        "traffic_delay_s": round(delay or 0),
        "bus_speed_kmh": round(speed, 1) if speed is not None else None,
        "trend": trend,
        "path": [[round(a, 6), round(b, 6)] for a, b in path],
    })


@app.route("/api/usage", methods=["POST"])
def api_usage():
    """Registra eventos de uso de la página (para las métricas de la semana 3)."""
    data = request.get_json(silent=True) or {}
    event = data.get("event")
    if event not in USAGE_EVENTS:
        return jsonify({"error": "evento no válido"}), 400
    device = data.get("device") if data.get("device") in ("mobile", "desktop") else None
    record_usage(event, str(data.get("client_id") or ""), device)
    return jsonify({"ok": True})


def _metrics_allowed():
    return not METRICS_KEY or request.args.get("key") == METRICS_KEY


@app.route("/metricas")
def metricas():
    """Página oculta (no tiene enlace en la página principal)."""
    if not _metrics_allowed():
        return "Not Found", 404
    return render_template("metricas.html", app_name=APP_NAME, key=request.args.get("key", ""))


@app.route("/api/metrics")
def api_metrics():
    if not _metrics_allowed():
        return jsonify({"error": "Not Found"}), 404
    days = min(max(request.args.get("days", default=7, type=int), 1), 31)
    return jsonify(build_metrics(days))


@app.route("/events")
def events():
    def stream():
        q = queue.Queue()
        with subscribers_lock:
            subscribers.append(q)
        try:
            while True:
                try:
                    payload = q.get(timeout=15)
                    yield f"data: {json.dumps(payload)}\n\n"
                except queue.Empty:
                    yield ": keep-alive\n\n"
        finally:
            with subscribers_lock:
                subscribers.remove(q)
    return Response(stream(), mimetype="text/event-stream")


if __name__ == "__main__":
    if IS_WRITER:
        init_db()
        init_session_tracking()
        print(f"[DB] (writer) Cuenta: {ACCOUNT_ID} | Sesión activa: {_active_session_id}")
        threading.Thread(target=udp_listener, daemon=True).start()
    threading.Thread(target=listen_notifications, daemon=True).start()
    print(f"[HTTP] ({ROLE}) '{APP_NAME}' en http://0.0.0.0:{HTTP_PORT} (DB: {DB_NAME}@{DB_HOST}, cuenta: {ACCOUNT_ID}, tz: {APP_TZ.key})")
    app.run(host="0.0.0.0", port=HTTP_PORT, threaded=True)