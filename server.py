import argparse
import json
import math
import os
import queue
import re
import select
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

import vueltas

load_dotenv()


DB_HOST = os.environ["DB_HOST"]
DB_PORT = os.environ.get("DB_PORT", "5432")
DB_NAME = os.environ["DB_NAME"]
DB_USER = os.environ["DB_USER"]
DB_PASSWORD = os.environ["DB_PASSWORD"]
DB_SSLMODE = os.environ.get("DB_SSLMODE", "require")


APP_NAME = os.environ.get("APP_NAME", "GPS Truck Tracker")
# Lo que ve la gente en la página: nombre de la ruta y del bus (para saber a qué bus se monta)
ROUTE_NAME = os.environ.get("ROUTE_NAME", "Ruta Uninorte")
BUS_NAME = os.environ.get("BUS_NAME", "Bus de Luis")


def _parse_buses(text):
    """BUSES="bus1:Bus de Luis,bus2:Bus de Andrés" -> {"bus1": "Bus de Luis", "bus2": "Bus de Andrés"}"""
    out = {}
    for part in (text or "").split(","):
        if ":" in part:
            bid, name = (x.strip() for x in part.split(":", 1))
            if re.fullmatch(r"[A-Za-z0-9_-]{1,20}", bid) and name:
                out[bid] = name
    return out


# Varios buses en la misma ruta. Cada trama trae "Id=bus1" (o bus2...) y así se sabe de cuál es.
BUSES = _parse_buses(os.environ.get("BUSES", "")) or {"bus1": BUS_NAME, "bus2": os.environ.get("BUS2_NAME", "Bus de Andrés")}
DEFAULT_BUS = next(iter(BUSES))           # las filas viejas (sin vehicle_id) son de este bus
VEH_SQL = f"COALESCE(vehicle_id, '{DEFAULT_BUS}')"   # seguro: DEFAULT_BUS ya pasó por la validación de arriba
# La app de Android no manda "Id": cada bus que use la app manda a su propio puerto UDP.
# UDP_BUS_PORTS="5002:bus2" -> lo que llegue al puerto 5002 es del bus2 (el puerto principal es del primer bus).
UDP_BUS_PORTS = {}
for _part in os.environ.get("UDP_BUS_PORTS", "").split(","):
    if ":" in _part:
        _port, _bid = (x.strip() for x in _part.split(":", 1))
        if _port.isdigit() and _bid in BUSES:
            UDP_BUS_PORTS[int(_port)] = _bid


def _parse_point(text, default_radius=None):
    """"11.0206,-74.868067" o "11.0206,-74.868067,300" -> (lat, lon, radio_m)"""
    try:
        parts = [float(x) for x in text.split(",")]
        return (parts[0], parts[1], parts[2] if len(parts) > 2 else default_radius)
    except (ValueError, IndexError):
        return None


# ---------- Vueltas de la ruta (cómo se separan los recorridos en la tabla) ----------
# La ruta va de oeste (Puerto Colombia) a este (extremo cerca de Uninorte). Una vuelta:
# sale del punto de inicio, va al extremo este, se devuelve, va al oeste (Puerto Colombia
# o una vuelta más corta) y regresa al punto de inicio. Medido con los datos del 9 al 13 de nov.
ROUTE_START = _parse_point(os.environ.get("ROUTE_START", "11.0206,-74.868067,150"), 150)
ROUTE_EAST_LON = float(os.environ.get("ROUTE_EAST_LON", "-74.856"))      # más al este = llegó al extremo este
ROUTE_WEST_LON = float(os.environ.get("ROUTE_WEST_LON", "-74.900"))      # más al oeste = se fue hacia Puerto Colombia
ROUTE_PTO_COL_LON = float(os.environ.get("ROUTE_PTO_COL_LON", "-74.945"))  # más al oeste = llegó a Puerto Colombia
ROUTE_LOOPS = bool(ROUTE_START) and os.environ.get("ROUTE_LOOPS", "1") != "0"
LOOP_MAX_GAP_MIN = 60            # (método anterior) un hueco más largo corta la vuelta
LOOPS_DEFAULT_DAYS = 7           # sin filtro de fechas, la tabla busca vueltas de los últimos 7 días
# Ruta de referencia (una vuelta completa sin huecos). Con ella las vueltas se separan por el avance
# sobre la ruta y los tramos sin datos se rellenan como estimados (ver vueltas.py).
ROUTE_REF_FILE = os.environ.get("ROUTE_REF_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "ruta_referencia.json"))
try:
    ROUTE_REF = vueltas.Ruta.from_file(ROUTE_REF_FILE)
except (OSError, ValueError, KeyError) as _exc:
    ROUTE_REF = None
    print(f"[VUELTAS] Sin ruta de referencia ({_exc}); se usa el método anterior")
_loops_cache = {}                # (bus, día, n_puntos, última_hora) -> vueltas de ese día
LOOP_MIN_POINTS = 30             # un tramo incompleto con menos puntos no se muestra


def bus_name(bid):
    return BUSES.get(bid, bid)


def clean_bus(bid):
    """Identificador de bus válido, o el bus por defecto."""
    bid = (bid or "").strip()
    return bid if re.fullmatch(r"[A-Za-z0-9_-]{1,20}", bid) else DEFAULT_BUS


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

_last_packet_dt = {}       # bus -> hora del último paquete
_active_session_id = {}    # bus -> recorrido (sesión) activo


MAX_HISTORY_RANGE_POINTS = 3000

# ---------- Semana 2: tiempo de llegada (ETA) con tráfico ----------
# Llave de la API de TomTom (https://developer.tomtom.com). Sin llave se usa un
# cálculo aproximado (distancia / velocidad) y la página lo indica.
TOMTOM_API_KEY = os.environ.get("TOMTOM_API_KEY", "").strip()
ETA_FALLBACK_KMH = float(os.environ.get("ETA_FALLBACK_KMH", "22"))  # velocidad promedio del bus sin datos de tráfico
ETA_BUS_MAX_AGE_S = 180      # si el último dato del bus es más viejo que esto, se considera sin señal
ETA_HERE_M = 80              # a menos de esto, "el bus está donde estás"
# TomTom se consulta máximo cada ETA_CACHE_S segundos por zona del usuario (el tráfico no cambia tanto en
# 1-2 min y así se cuida la cuota gratis). Entre consultas el tiempo se ajusta según cuánto se acercó el bus,
# así que la página sigue actualizándose cada 10 s.
ETA_CACHE_S = int(os.environ.get("ETA_CACHE_S", "120"))
TOMTOM_MONTHLY_QUOTA = int(os.environ.get("TOMTOM_MONTHLY_QUOTA", "20000"))  # para la barra de la página de métricas
ETA_LOG_EVERY_S = 60         # cada usuario guarda como máximo 1 registro "eta" por minuto (la página pide cada 10 s)
_eta_log_last = {}           # client_id -> (momento, fuente) del último registro guardado
_eta_cache = {}
_eta_cache_lock = threading.Lock()

# ---------- Semana 3: modelo propio de tiempo de llegada (ML) ----------
# ETA_METHOD (en el .env):
#   auto     -> modelo propio si existe; si no, TomTom; si no, el estimado (por defecto)
#   ml       -> modelo propio; si no se puede, el estimado (sin TomTom)
#   tomtom   -> como antes: TomTom y, si falla, el estimado
#   estimado -> solo la fórmula (distancia / velocidad)
# El modelo y la ruta los generan ml/exportar_dataset.py y ml/entrenar.py.
# Si no existen (o falta scikit-learn), el servidor funciona igual que antes.
ETA_METHOD = os.environ.get("ETA_METHOD", "auto").strip().lower()
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ML_MODEL_PATH = os.environ.get("ML_MODEL_PATH", os.path.join(_BASE_DIR, "ml", "modelo_eta.joblib"))
ML_ROUTE_PATH = os.environ.get("ML_ROUTE_PATH", os.path.join(_BASE_DIR, "ml", "ruta_referencia.json"))
ML_MAX_OFF_ROUTE_M = 300     # si el bus está a más de esto de la ruta conocida, no se usa el modelo
_ML_MODEL = None
_ML_ROUTE = None
if ETA_METHOD in ("auto", "ml") and os.path.exists(ML_MODEL_PATH) and os.path.exists(ML_ROUTE_PATH):
    try:
        from ml.eta_ml import RefRoute, build_features as ml_build_features, load_model, predict_seconds
        _ML_MODEL = load_model(ML_MODEL_PATH)
        _ML_ROUTE = RefRoute.load(ML_ROUTE_PATH) if _ML_MODEL else None
        if _ML_MODEL:
            print(f"[ML] Modelo de llegada cargado: {_ML_MODEL.get('nombre')} "
                  f"({_ML_MODEL.get('recorridos')} recorridos, entrenado {_ML_MODEL.get('entrenado')})")
    except Exception as exc:
        print(f"[ML] No se pudo cargar el modelo, se sigue sin ML: {exc}")
        _ML_MODEL = _ML_ROUTE = None

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
    ensure_bus_columns()


def ensure_bus_columns():
    """Columna vehicle_id (qué bus) en locations y sessions. La corren writer y readers
    al arrancar, así ningún proceso consulta la columna antes de que exista."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("ALTER TABLE locations ADD COLUMN IF NOT EXISTS vehicle_id TEXT")
    cur.execute("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS vehicle_id TEXT")
    # La app de Android manda la batería del celular en % (Bat=87); el ESP32 la manda en voltios (Bat=3.95)
    cur.execute("ALTER TABLE locations ADD COLUMN IF NOT EXISTS battery_pct INTEGER")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_locations_acc_vehicle ON locations (account_id, vehicle_id, id DESC)")
    conn.commit()
    cur.close()
    conn.close()


def start_new_session(bus: str = None) -> str:
    """Registra un nuevo recorrido de un bus de ACCOUNT_ID y devuelve su session_id.
    Todo lo que se inserte después de esto queda "marcado" con esta sesión,
    así el mapa puede trazar solo el recorrido actual."""
    bus = bus or DEFAULT_BUS
    now = now_local()
    session_id = now.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO sessions (session_id, account_id, started_at, vehicle_id) VALUES (%s, %s, %s, %s)",
        (session_id, ACCOUNT_ID, now.strftime("%Y-%m-%d %H:%M:%S"), bus),
    )
    conn.commit()
    cur.close()
    conn.close()
    return session_id


def get_current_session_id(bus: str = None):
    """Sesión más reciente de un bus de ACCOUNT_ID. La consulta cualquier
    proceso (writer o reader) directamente contra la BD — no depende de que
    sea el mismo proceso que la creó."""
    bus = bus or DEFAULT_BUS
    with db_lock:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            f"SELECT session_id FROM sessions WHERE account_id = %s AND {VEH_SQL} = %s ORDER BY id DESC LIMIT 1",
            (ACCOUNT_ID, bus),
        )
        row = cur.fetchone()
        cur.close()
        conn.close()
    return row[0] if row else None


def get_last_received_at(bus: str = None):
    """received_at del último punto guardado de un bus, para saber
    cuánto tiempo llevaba el GPS callado la última vez que corrió el writer."""
    bus = bus or DEFAULT_BUS
    with db_lock:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            f"SELECT received_at FROM locations WHERE account_id = %s AND {VEH_SQL} = %s ORDER BY id DESC LIMIT 1",
            (ACCOUNT_ID, bus),
        )
        row = cur.fetchone()
        cur.close()
        conn.close()
    return row[0] if row else None


def notify_session_start(session_id: str, bus: str = None):
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
            "bus": bus or DEFAULT_BUS,
        }),),
    )
    cur.close()
    conn.close()


def init_session_tracking():
    """Se llama una vez al arrancar el writer. Retoma la sesión activa si el
    último paquete llegó hace poco, o abre una sesión nueva si no hay
    ninguna todavía o si el silencio ya superó SESSION_GAP_SECONDS."""
    for bus in BUSES:
        existing_session_id = get_current_session_id(bus)
        last_received_at = get_last_received_at(bus)
        _active_session_id[bus] = existing_session_id
        # Sin recorrido previo: el primer paquete de ese bus abre uno nuevo
        _last_packet_dt[bus] = (
            datetime.strptime(last_received_at, "%Y-%m-%d %H:%M:%S")
            if existing_session_id and last_received_at else None
        )


def save_location(lat: float, lon: float, gps_time: str, extras: dict = None, bus: str = None):
    extras = extras or {}
    bus = clean_bus(bus)

    received_at_dt = now_local()
    received_at = received_at_dt.strftime("%Y-%m-%d %H:%M:%S")


    with session_lock:
        last_dt = _last_packet_dt.get(bus)
        if last_dt is None or not _active_session_id.get(bus) or \
                (received_at_dt - last_dt).total_seconds() > SESSION_GAP_SECONDS:
            _active_session_id[bus] = start_new_session(bus)
            notify_session_start(_active_session_id[bus], bus)
        _last_packet_dt[bus] = received_at_dt
        session_id = _active_session_id[bus]

    payload = {**location_to_payload(lat, lon, gps_time), "account_id": ACCOUNT_ID,
               "bus": bus, "received_at": received_at}
    if extras.get("battery_v") is not None:
        payload["battery_pct"] = battery_pct(extras["battery_v"])
    elif extras.get("battery_pct") is not None:
        payload["battery_pct"] = extras["battery_pct"]

    with db_lock:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO locations (account_id, session_id, lat, lon, gps_time, received_at, "
            "battery_v, signal_csq, seq, vehicle_id, battery_pct) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (ACCOUNT_ID, session_id, lat, lon, gps_time, received_at,
             extras.get("battery_v"), extras.get("signal_csq"), extras.get("seq"), bus,
             extras.get("battery_pct")),
        )
        cur.execute(
            f"NOTIFY {NOTIFY_CHANNEL}, %s",
            (json.dumps(payload),)
        )
        conn.commit()
        cur.close()
        conn.close()


def get_latest_location(bus: str = None):
    """Última ubicación de un bus: (lat, lon, gps_time, received_at)."""
    bus = bus or DEFAULT_BUS
    with db_lock:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            f"SELECT lat, lon, gps_time, received_at FROM locations WHERE account_id = %s AND {VEH_SQL} = %s "
            "ORDER BY id DESC LIMIT 1",
            (ACCOUNT_ID, bus),
        )
        row = cur.fetchone()
        cur.close()
        conn.close()
    return row


def get_history(limit: int = 1000, bus: str = None):
    """Recorrido actual de un bus únicamente (el historial completo sigue
    intacto en la tabla, solo no se muestra en el mapa)."""
    bus = bus or DEFAULT_BUS
    session_id = get_current_session_id(bus)
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
                f"SELECT lat, lon, gps_time FROM locations WHERE account_id = %s AND {VEH_SQL} = %s "
                "ORDER BY id DESC LIMIT %s",
                (ACCOUNT_ID, bus, limit),
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
        f"SELECT session_id, MIN(gps_time), MAX(gps_time), COUNT(*), MAX({VEH_SQL}) "
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


def _bus_points(bus, start_dt, end_dt):
    """Puntos de un bus entre dos fechas, ordenados por la hora del GPS."""
    with db_lock:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT lat, lon, gps_time, received_at FROM locations "
            f"WHERE account_id = %s AND {VEH_SQL} = %s AND gps_time BETWEEN %s AND %s "
            "ORDER BY gps_time ASC",
            (ACCOUNT_ID, bus, start_dt, end_dt),
        )
        rows = cur.fetchall()
        cur.close()
        conn.close()
    return rows


def find_loops(rows):
    """Separa los puntos de un bus (ordenados por hora) en vueltas.

    Una vuelta TERMINA cuando el bus cruza la longitud del punto de inicio yendo hacia el este
    después de haber ido al oeste (vuelve de Puerto Colombia o de una vuelta corta). El cruce se
    mira entre dos puntos seguidos, así que se detecta aunque justo ahí no haya datos (el celular
    sin señal); en ese caso la hora de llegada se calcula interpolando.
    La vuelta EMPIEZA en la última lectura en el punto de inicio (no cuenta la espera).
    Un hueco de más de LOOP_MAX_GAP_MIN minutos (celular apagado) deja lo que iba como incompleto.

    Devuelve [(inicio, fin, n_puntos, tipo, hueco_max_min)], tipo = vuelta | vuelta corta | incompleta."""
    slat, slon, srad = ROUTE_START
    loops = []
    st = {"start": 0, "ok": False, "west": False, "east": False, "pc": False, "gap": 0.0}

    def near_start(k, radius=srad):
        return haversine_m(slat, slon, rows[k][0], rows[k][1]) <= radius

    def reset(i, ok):
        st.update(start=i, ok=ok, west=False, east=False, pc=False, gap=0.0)

    def close(i_end, complete, end_time=None):
        i0 = st["start"]
        if st["ok"]:                      # salida = última lectura en el inicio antes de alejarse
            for k in range(st["start"], i_end):
                if near_start(k):
                    i0 = k
                elif not near_start(k, 1000):
                    break
        n = i_end - i0 + 1
        if complete and n >= 10:
            loops.append((rows[i0][2], end_time or rows[i_end][2], n,
                          "vuelta" if st["pc"] else "vuelta corta", round(st["gap"])))
        elif not complete and n >= LOOP_MIN_POINTS:
            loops.append((rows[i0][2], rows[i_end][2], n, "incompleta", round(st["gap"])))

    if not rows:
        return loops
    reset(0, near_start(0, 300))
    for i in range(1, len(rows)):
        la0, lo0, g0 = rows[i - 1][0], rows[i - 1][1], parse_dt(rows[i - 1][2])
        la1, lo1, g1 = rows[i][0], rows[i][1], parse_dt(rows[i][2])
        gap = (g1 - g0).total_seconds() / 60 if g0 and g1 else 0
        if gap > LOOP_MAX_GAP_MIN:
            if st["east"] or st["west"]:
                close(i - 1, False)
            reset(i, near_start(i, 300))
            continue
        st["gap"] = max(st["gap"], gap)
        st["east"] |= lo1 > ROUTE_EAST_LON
        st["west"] |= lo1 < ROUTE_WEST_LON
        st["pc"] |= lo1 < ROUTE_PTO_COL_LON
        crossed = lo0 < slon <= lo1 and abs((la0 + la1) / 2 - slat) < 0.02
        if crossed and st["west"]:
            end_time = None
            if gap > 3 and g0 and g1:     # cruzó durante un hueco: hora estimada de llegada
                frac = (slon - lo0) / (lo1 - lo0)
                end_time = (g0 + (g1 - g0) * frac).strftime("%Y-%m-%d %H:%M:%S")
            close(i, st["ok"] and st["east"], end_time)
            reset(i, True)
    if st["east"] or st["west"]:
        close(len(rows) - 1, False)
    return loops


def _day_loops(bus, day, rows):
    """Vueltas de un bus en un día (con caché: un día pasado no cambia)."""
    key = (bus, day, len(rows), str(rows[-1][2]) if rows else "")
    if key not in _loops_cache:
        if len(_loops_cache) > 400:
            _loops_cache.clear()
        _loops_cache[key] = vueltas.day_loops([(r[0], r[1], r[2]) for r in rows], ROUTE_REF)
    return _loops_cache[key]


def _bus_day_rows(bus, day):
    return _bus_points(bus, f"{day} 00:00:00", f"{day} 23:59:59")


def get_loops(start_dt=None, end_dt=None, limit=50):
    """Vueltas de todos los buses en un rango (por defecto, los últimos LOOPS_DEFAULT_DAYS días)."""
    end_dt = end_dt or now_local().strftime("%Y-%m-%d %H:%M:%S")
    start_dt = start_dt or (now_local() - timedelta(days=LOOPS_DEFAULT_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    out = []
    for bus in BUSES:
        if ROUTE_REF:
            rows = _bus_points(bus, start_dt[:10] + " 00:00:00", end_dt[:10] + " 23:59:59")
            by_day = {}
            for r in rows:
                by_day.setdefault(str(r[2])[:10], []).append(r)
            for day, day_rows in by_day.items():
                for lp in _day_loops(bus, day, day_rows):
                    if lp["end"] < start_dt or lp["start"] > end_dt:
                        continue
                    t0, t1 = lp["start"], lp["end"]
                    out.append({"session_id": f"v|{bus}|{t0}|{t1}", "start_time": t0, "end_time": t1,
                                "points": len(lp["points"]), "bus": bus, "bus_name": bus_name(bus),
                                "kind": lp["kind"], "observed_km": lp["observed_km"], "est_km": lp["est_km"],
                                "max_gap_min": max([round((parse_dt(g[1]) - parse_dt(g[0])).total_seconds() / 60)
                                                    for g in lp["gaps"]] or [0])})
            continue
        for t0, t1, n, kind, gap in find_loops(_bus_points(bus, start_dt, end_dt)):
            out.append({"session_id": f"v|{bus}|{t0}|{t1}", "start_time": t0, "end_time": t1,
                        "points": n, "bus": bus, "bus_name": bus_name(bus),
                        "kind": kind, "max_gap_min": gap})
    out.sort(key=lambda x: x["start_time"], reverse=True)
    return out[:limit]


def get_session_route(session_id: str, max_points: int = MAX_HISTORY_RANGE_POINTS):
    """Puntos de un recorrido (sesión) específico ya registrado, ordenados
    por gps_time, para dibujarlo en el mapa igual que get_history_range().
    Una vuelta tiene id "v|bus|inicio|fin": se devuelven los puntos de ese bus entre esas horas."""
    if session_id.startswith("v|"):
        try:
            _, bus, t0, t1 = session_id.split("|")
        except ValueError:
            return []
        bus = clean_bus(bus)
        if ROUTE_REF:
            for lp in _day_loops(bus, t0[:10], _bus_day_rows(bus, t0[:10])):
                if lp["start"] == t0:
                    # (lat, lon, hora, recibido, estimado): los estimados no vienen de la base de datos
                    return [(la, lo, t, None if est else t, est) for la, lo, t, est in lp["points"]][:max_points]
        return _bus_points(bus, t0, t1)[:max_points]
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


def get_recent_locations(limit: int = 8, bus: str = None):
    """Últimas lecturas de un bus, la más reciente primero."""
    bus = bus or DEFAULT_BUS
    with db_lock:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT lat, lon, gps_time, received_at FROM locations "
            f"WHERE account_id = %s AND {VEH_SQL} = %s ORDER BY id DESC LIMIT %s",
            (ACCOUNT_ID, bus, limit),
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


def compute_eta(blat, blon, ulat, ulon, speed_kmh, bus: str = None):
    # La llave es solo la zona del usuario (~100 m): mientras el bus avanza no se
    # vuelve a llamar a TomTom; el último resultado se escala con la distancia
    # en línea recta que le queda al bus (si se acercó un 10 %, el tiempo baja un 10 %).
    key = (bus, round(ulat, 3), round(ulon, 3))   # cada bus tiene su propia caché
    now = time.time()
    straight_now = haversine_m(blat, blon, ulat, ulon)
    with _eta_cache_lock:
        hit = _eta_cache.get(key)
    if hit and now - hit[0] < ETA_CACHE_S and hit[2] > 0:
        seconds, meters, delay, path, source = hit[1]
        ratio = min(max(straight_now / hit[2], 0.0), 1.5)
        if path and len(path) > 1:
            path = [(blat, blon)] + list(path[1:])
        return (seconds * ratio, meters * ratio, delay * ratio, path, source)
    result = None
    if TOMTOM_API_KEY:
        t0 = time.time()
        try:
            result = (*tomtom_route(blat, blon, ulat, ulon), "tomtom")
            record_usage("tomtom", None, None, round((time.time() - t0) * 1000), "ok")
        except Exception as exc:
            print(f"[ETA] TomTom falló, se usa el estimado: {exc}")
            record_usage("tomtom", None, None, round((time.time() - t0) * 1000), "error")
    if result is None:
        result = (*estimate_eta(blat, blon, ulat, ulon, speed_kmh), "estimado")
    with _eta_cache_lock:
        if len(_eta_cache) > 500:
            _eta_cache.clear()
        _eta_cache[key] = (now, result, straight_now)
    return result


def ml_eta(rows, blat, blon, ulat, ulon, speed_kmh):
    """Tiempo de llegada con el modelo propio (entrenado con recorridos reales).
    El destino es el punto de la ruta de referencia más cercano al usuario.
    Devuelve None si no se puede usar (sin modelo, bus fuera de la ruta o yendo
    en sentido contrario al usuario); entonces se usa TomTom o el estimado."""
    if not (_ML_MODEL and _ML_ROUTE):
        return None
    b_cum, b_i, b_off = _ML_ROUTE.project(blat, blon)
    u_cum, u_i, _ = _ML_ROUTE.project(ulat, ulon)
    if b_off > ML_MAX_OFF_ROUTE_M:
        return None
    sentido = 1 if u_cum >= b_cum else 0

    # ¿Hacia dónde va el bus sobre la ruta? (posición de hace 25-120 s)
    t_new = parse_dt(rows[0][2])
    for la, lo, g, _ in rows[1:]:
        t_old = parse_dt(g)
        if t_new and t_old and 25 <= (t_new - t_old).total_seconds() <= 120:
            avance = b_cum - _ML_ROUTE.project(la, lo)[0]
            if abs(avance) > 20 and (avance > 0) != (sentido == 1):
                return None   # el bus va en sentido contrario: tiene que dar la vuelta
            break

    dist_ruta = abs(u_cum - b_cum)
    dest_lat, dest_lon = _ML_ROUTE.pts[u_i]
    feats = ml_build_features(
        dist_ruta_m=dist_ruta,
        dist_recta_m=haversine_m(blat, blon, dest_lat, dest_lon),
        avance_m=b_cum, sentido=sentido, vel_actual_kmh=speed_kmh or 0.0,
        dt=t_new or now_local(),
    )
    try:
        seconds = predict_seconds(_ML_MODEL, feats)
    except Exception as exc:
        print(f"[ML] Falló la predicción, se usa TomTom/estimado: {exc}")
        return None
    path = _ML_ROUTE.between(b_i, u_i)
    path = [(blat, blon)] + list(path[1:]) if path else [(blat, blon), (ulat, ulon)]
    return seconds, dist_ruta, 0, path, "modelo"


def record_eta_usage(client_id, eta_min, source):
    """Guarda la consulta de llegada, pero máximo 1 vez por minuto por usuario
    (o cuando cambia la fuente, por ejemplo cuando el bus llega: "here")."""
    now = time.time()
    if client_id:
        last = _eta_log_last.get(client_id)
        if last and now - last[0] < ETA_LOG_EVERY_S and last[1] == source:
            return
        if len(_eta_log_last) > 5000:
            _eta_log_last.clear()
        _eta_log_last[client_id] = (now, source)
    record_usage("eta", client_id, None, eta_min, source)


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
            "SELECT lat, lon, gps_time, received_at, session_id, battery_v, signal_csq, seq, "
            f"{VEH_SQL}, battery_pct FROM locations WHERE account_id = %s AND received_at >= %s ORDER BY id ASC",
            (ACCOUNT_ID, since_s),
        )
        extras_ok = True
    except Exception:
        rows = [(*r, None, None, None, DEFAULT_BUS, None) for r in _query(
            "SELECT lat, lon, gps_time, received_at, session_id "
            "FROM locations WHERE account_id = %s AND received_at >= %s ORDER BY id ASC",
            (ACCOUNT_ID, since_s),
        )]
        extras_ok = False

    per_day = {d: {"date": d, "points": 0, "km": 0.0, "active_s": 0.0, "max_kmh": 0.0,
                   "trips": set(), "lost": 0, "first": None, "last": None} for d in day_keys}
    intervals = []   # segundos entre una ubicación y la siguiente (el ESP32 envía cada 10 s)
    latencies = []
    hourly = {}
    battery_series, signal_series = [], []
    prev_by_bus = {}     # cada bus se compara con SU punto anterior (las tramas de los dos se mezclan)
    total_lost = 0
    bus_last = {}        # bus -> (received_at, batería, señal) más recientes
    for lat, lon, gps_time, received_at, session_id, bat, csq, seq, vbus, bpct in rows:
        prev = prev_by_bus.get(vbus)
        pct_now = battery_pct(bat) if bat is not None else bpct   # voltios (ESP32) o % (celular)
        bl = bus_last.setdefault(vbus, {"last_seen": None, "battery_v": None, "battery_pct": None,
                                        "signal_csq": None, "points": 0})
        bl["last_seen"] = received_at
        bl["points"] += 1
        if bat is not None:
            bl["battery_v"] = bat
        if pct_now is not None:
            bl["battery_pct"] = pct_now
        if csq is not None:
            bl["signal_csq"] = csq
        rdt = parse_dt(received_at)
        gdt = parse_dt(gps_time)
        day = received_at[:10]
        d = per_day.get(day)
        if d is None:
            prev_by_bus[vbus] = None
            continue
        d["points"] += 1
        d["first"] = d["first"] or received_at[11:16]
        d["last"] = received_at[11:16]
        if session_id:
            d["trips"].add(session_id)
        if rdt and gdt:
            lag = (rdt - gdt).total_seconds()
            if -5 <= lag <= 3600:
                latencies.append(max(lag, 0))
        if rdt and (now - rdt).total_seconds() <= 24 * 3600:
            hk = rdt.strftime("%Y-%m-%d %H:00")
            hourly[hk] = hourly.get(hk, 0) + 1
            if pct_now is not None:
                battery_series.append({"t": received_at, "v": round(bat, 3) if bat is not None else None, "pct": pct_now})
            if csq is not None:
                signal_series.append({"t": received_at, "csq": csq, "dbm": -113 + 2 * csq})
        if prev is not None and prev["session"] == session_id and gdt and prev["gdt"]:
            dt = (gdt - prev["gdt"]).total_seconds()
            if 0 < dt <= 120:
                intervals.append(dt)
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
        prev_by_bus[vbus] = {"lat": lat, "lon": lon, "gdt": gdt, "session": session_id, "seq": seq}

    days_out = []
    for d in per_day.values():
        hours = d["active_s"] / 3600
        days_out.append({
            "date": d["date"], "points": d["points"], "trips": len(d["trips"]),
            "km": round(d["km"], 2), "active_h": round(hours, 2),
            "avg_kmh": round(d["km"] / hours, 1) if hours > 0.01 else None,
            "max_kmh": round(d["max_kmh"], 1) if d["max_kmh"] else None,
            "lost": d["lost"], "first": d["first"], "last": d["last"],
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
    last_pct = next((battery_pct(r[5]) if r[5] is not None else r[9]
                     for r in reversed(rows) if r[5] is not None or r[9] is not None), None)
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
        "battery_pct": last_pct,
        "battery_drain_pct_h": round(drain, 1) if drain else None,
        "battery_hours_left": round(last_pct / drain, 1) if drain and last_pct is not None else None,
        "signal_csq": last_csq,
        "signal_dbm": -113 + 2 * last_csq if last_csq is not None else None,
        "extras_enabled": extras_ok,
        # Estado de cada bus (la tarjeta grande muestra el que mandó más reciente)
        "buses": [{
            "id": b, "name": bus_name(b), "last_seen": bus_last.get(b, {}).get("last_seen"),
            "online": bool(bus_last.get(b, {}).get("last_seen") and parse_dt(bus_last[b]["last_seen"])
                           and (now - parse_dt(bus_last[b]["last_seen"])).total_seconds() <= 60),
            "battery_v": round(bus_last[b]["battery_v"], 2) if bus_last.get(b, {}).get("battery_v") is not None else None,
            "battery_pct": bus_last.get(b, {}).get("battery_pct"),
            "signal_csq": bus_last.get(b, {}).get("signal_csq"),
            "points": bus_last.get(b, {}).get("points", 0),
        } for b in list(BUSES) + [x for x in bus_last if x not in BUSES]],
    }
    quality = {
        "received": received,
        "lost": total_lost,
        "loss_pct": round(total_lost / (received + total_lost) * 100, 2) if received + total_lost else None,
        "latency_median_s": round(_percentile(latencies, 50), 1) if latencies else None,
        "latency_p95_s": round(_percentile(latencies, 95), 1) if latencies else None,
        "interval_median_s": round(_percentile(intervals, 50), 1) if intervals else None,
    }

    # ---------- Usuarios de la página ----------
    try:
        ev = _query(
            "SELECT ts, event, client_id, device, value, detail FROM usage_events "
            "WHERE account_id = %s AND ts >= %s ORDER BY id ASC",
            (ACCOUNT_ID, since_s),
        )
        usage_ok = True
    except Exception:
        ev, usage_ok = [], False
    u_day = {d: {"date": d, "visits": 0, "visitors": set(), "eta_users": set(), "sessions": 0,
                 "tomtom": 0} for d in day_keys}
    by_hour = [0] * 24
    counts = {"geo_granted": 0, "geo_denied": 0, "geo_unavailable": 0}
    devices = {"mobile": 0, "desktop": 0}
    seen_dev = set()
    all_visitors, eta_users = set(), set()
    visit_days = {}                      # client_id -> días en que abrió la página
    eta_by_client = {}                   # client_id -> [(fecha, minutos, fuente)]
    sources = {"tomtom": 0, "estimado": 0, "modelo": 0, "here": 0}
    eta_updates = 0
    tomtom = {"ok": 0, "error": 0, "ms": []}
    for ts, event, client_id, dev, value, detail in ev:
        d = u_day.get(ts[:10])
        if d is None:
            continue
        if event == "visit":
            d["visits"] += 1
            if client_id:
                d["visitors"].add(client_id)
                all_visitors.add(client_id)
                visit_days.setdefault(client_id, set()).add(ts[:10])
            try:
                by_hour[int(ts[11:13])] += 1
            except ValueError:
                pass
            if dev in devices and client_id not in seen_dev:
                devices[dev] += 1
                seen_dev.add(client_id)
        elif event == "eta":
            eta_updates += 1
            if detail in sources:
                sources[detail] += 1
            if client_id:
                d["eta_users"].add(client_id)
                eta_users.add(client_id)
                eta_by_client.setdefault(client_id, []).append((parse_dt(ts), value, detail))
        elif event == "tomtom":
            d["tomtom"] += 1
            tomtom["ok" if detail == "ok" else "error"] += 1
            if value is not None and detail == "ok":
                tomtom["ms"].append(value)
        elif event in counts:
            counts[event] += 1

    # Una "consulta" = una persona siguiendo el bus. Si pasa más de 10 min sin pedir
    # el tiempo de llegada, la siguiente vez cuenta como otra consulta.
    sessions = []
    for client_id, items in eta_by_client.items():
        items = [it for it in items if it[0]]
        cur_s = None
        for t, value, detail in items:
            if cur_s is None or (t - cur_s["end"]).total_seconds() > 600:
                cur_s = {"start": t, "end": t, "first_min": value, "arrived": False}
                sessions.append(cur_s)
                day = u_day.get(t.strftime("%Y-%m-%d"))
                if day:
                    day["sessions"] += 1
            cur_s["end"] = t
            if detail == "here":
                cur_s["arrived"] = True
    durations = [(x["end"] - x["start"]).total_seconds() / 60 for x in sessions]
    first_eta = [x["first_min"] for x in sessions if x["first_min"]]
    arrived = sum(1 for x in sessions if x["arrived"])

    # TomTom en el mes (la cuota es mensual), aunque el periodo elegido sea más corto
    try:
        month_start = now.replace(day=1, hour=0, minute=0, second=0).strftime("%Y-%m-%d %H:%M:%S")
        tomtom_month = _query("SELECT COUNT(*) FROM usage_events WHERE event = 'tomtom' AND ts >= %s",
                              (month_start,))[0][0]
    except Exception:
        tomtom_month = None

    asked = counts["geo_granted"] + counts["geo_denied"]
    usage = {
        "enabled": usage_ok,
        "days": [{"date": d["date"], "visits": d["visits"], "visitors": len(d["visitors"]),
                  "eta_users": len(d["eta_users"]), "sessions": d["sessions"], "tomtom": d["tomtom"]}
                 for d in u_day.values()],
        "total_visits": sum(d["visits"] for d in u_day.values()),
        "unique_visitors": len(all_visitors),
        "returning_visitors": sum(1 for v in visit_days.values() if len(v) >= 2),
        "eta_users": len(eta_users),
        "eta_sessions": len(sessions),
        "eta_updates": eta_updates,
        "eta_session_median_min": round(_percentile(durations, 50), 1) if durations else None,
        "eta_first_avg_min": round(sum(first_eta) / len(first_eta), 1) if first_eta else None,
        "eta_arrived": arrived,
        "eta_sources": sources,
        "geo": counts,
        "geo_accept_pct": round(counts["geo_granted"] / asked * 100) if asked else None,
        "devices": devices,
        "visits_by_hour": by_hour,
        "funnel": {"visitors": len(all_visitors), "geo": counts["geo_granted"],
                   "eta_users": len(eta_users), "arrived": arrived},
    }
    traffic_api = {
        "configured": bool(TOMTOM_API_KEY),
        "every_s": ETA_CACHE_S,
        "calls": tomtom["ok"] + tomtom["error"],
        "errors": tomtom["error"],
        "avg_ms": round(sum(tomtom["ms"]) / len(tomtom["ms"])) if tomtom["ms"] else None,
        "month_calls": tomtom_month,
        "month_quota": TOMTOM_MONTHLY_QUOTA,
        "eta_method": ETA_METHOD,
        "ml_loaded": _ML_MODEL is not None,
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
        "traffic_api": traffic_api,
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

    # Bat: entre 2 y 5 = voltios (ESP32); entre 5 y 100 = porcentaje (app de Android)
    bat = _opt_float(fields.get("Bat"), 0, 100)
    extras = {"battery_v": bat if bat is not None and 2.0 <= bat <= 5.0 else None,
              "battery_pct": int(round(bat)) if bat is not None and bat > 5.0 else None}
    sig = _opt_float(fields.get("Sig"), 0, 31)
    extras["signal_csq"] = int(sig) if sig is not None else None
    seq = _opt_float(fields.get("Seq"), 0, 2_000_000_000)
    extras["seq"] = int(seq) if seq is not None else None
    bid = fields.get("Id", "").strip()
    extras["bus"] = bid if re.fullmatch(r"[A-Za-z0-9_-]{1,20}", bid) else None
    return lat, lon, gps_time, extras


def udp_listener(port: int = None, port_bus: str = None):
    """Escucha tramas UDP. Si la trama trae "Id=", ese es el bus; si no (app de
    Android), el bus es el del puerto (port_bus, o el primero en el puerto principal)."""
    port = port or UDP_PORT
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", port))
    print(f"[UDP] (writer) Sniffer listening on 0.0.0.0:{port} (bus por defecto: {port_bus or DEFAULT_BUS})")
    while True:
        try:
            data, addr = sock.recvfrom(1024)
            raw = data.decode("utf-8", errors="ignore")
            lat, lon, gps_time, extras = parse_message(raw)
            bus = extras.pop("bus", None) or port_bus or DEFAULT_BUS
            save_location(lat, lon, gps_time, extras, bus)
            print(f"[UDP] (writer) {addr[0]} -> {bus} lat={lat} lon={lon} time={gps_time} "
                  f"bat={extras['battery_v'] if extras['battery_v'] is not None else extras.get('battery_pct')} "
                  f"csq={extras['signal_csq']} seq={extras['seq']}")
        except Exception as exc:
            print(f"[UDP] (writer) Ignored malformed packet: {exc}")


def listen_notifications():
    """Recibe los avisos (NOTIFY) de ubicaciones nuevas y los reparte a las páginas abiertas.
    select() deja el hilo DORMIDO hasta que llega un aviso (o pasan 30 s): antes se usaba
    conn.poll() en un ciclo sin espera, que gastaba el 100 % de un núcleo de CPU todo el tiempo
    y agotó los créditos de CPU de la instancia."""
    while True:
        try:
            conn = get_connection()
            conn.set_isolation_level(extensions.ISOLATION_LEVEL_AUTOCOMMIT)
            cur = conn.cursor()
            cur.execute(f"LISTEN {NOTIFY_CHANNEL};")
            print(f"[DB] ({ROLE}) Listening on '{NOTIFY_CHANNEL}' (account: {ACCOUNT_ID})")
            while True:
                if select.select([conn], [], [], 30) == ([], [], []):
                    continue                      # 30 s sin avisos: sigue esperando
                conn.poll()
                while conn.notifies:
                    notify = conn.notifies.pop(0)
                    try:
                        payload = json.loads(notify.payload)
                    except ValueError:
                        continue
                    if payload.get("account_id", "main") == ACCOUNT_ID:
                        broadcast(payload)
        except Exception as exc:
            # Si se cae la conexión con la base de datos, espera y se vuelve a conectar
            print(f"[DB] ({ROLE}) LISTEN se desconectó ({exc}); reintentando en 5 s")
            time.sleep(5)


app = Flask(__name__)


# Versión de los archivos de static/ (cambia en cada reinicio): obliga al navegador a bajar el CSS/JS nuevo tras un deploy
ASSET_V = str(int(time.time()))


@app.route("/")
def index():
    return render_template(
        "index.html", role=ROLE, app_name=APP_NAME, asset_v=ASSET_V, buses=BUSES,
        route_name=ROUTE_NAME, bus_name=BUS_NAME, bus_name_lc=BUS_NAME[:1].lower() + BUS_NAME[1:],
        page_config={"routeName": ROUTE_NAME, "busName": BUS_NAME,
                     "buses": [{"id": b, "name": n} for b, n in BUSES.items()]},
    )


@app.route("/api/latest")
def api_latest():
    """Última ubicación de cada bus. Los campos de arriba (lat, lon...) son los del
    bus que mandó más reciente, para que siga funcionando lo que ya los usaba."""
    now = now_local()
    buses = []
    for bid in BUSES:
        row = get_latest_location(bid)
        if row is None:
            continue
        lat, lon, gps_time, received_at = row
        rdt = parse_dt(received_at)
        buses.append({**location_to_payload(lat, lon, gps_time), "bus": bid, "name": bus_name(bid),
                      "received_at": received_at,
                      "age_s": round((now - rdt).total_seconds()) if rdt else None})
    if not buses:
        return jsonify({"lat": None, "lon": None, "date": None, "time": None, "buses": []})
    newest = max(buses, key=lambda b: b["received_at"] or "")
    return jsonify({**newest, "buses": buses})


@app.route("/api/session")
def api_session():
    bus = clean_bus(request.args.get("bus"))
    return jsonify({"session_id": get_current_session_id(bus), "bus": bus})


@app.route("/api/history")
def api_history():
    rows = get_history(bus=clean_bus(request.args.get("bus")))
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

    if ROUTE_LOOPS and request.args.get("modo") != "sesiones":
        # Recorridos = vueltas completas de la ruta (inicio -> extremo este -> Puerto Colombia -> inicio)
        return jsonify({"sessions": get_loops(start_norm, end_norm, limit), "modo": "vueltas"})

    rows = get_sessions(limit, start_norm, end_norm)
    sessions = [
        {"session_id": sid, "start_time": start, "end_time": end, "points": points,
         "bus": bid, "bus_name": bus_name(bid)}
        for sid, start, end, points, bid in rows
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
    points = []
    for lat, lon, gps_time, received_at, *rest in rows:
        p = {"lat": lat, "lon": lon, "time": gps_time, "received_at": received_at}
        if rest and rest[0]:
            p["est"] = True               # tramo sin datos reconstruido con la ruta de referencia
        points.append(p)
    return jsonify({"points": points, "count": len(points),
                    "estimated": sum(1 for p in points if p.get("est"))})


@app.route("/api/passages")
def api_passages():
    """Busca en qué momentos el camión pasó cerca de un punto elegido en el
    mapa, según la hora real del GPS (gps_time). Parámetros (query string):
      lat, lon   = coordenadas del punto seleccionado (obligatorios)
      radius     = radio de búsqueda en metros (opcional, default 100)
      start, end = 'YYYY-MM-DDTHH:MM' (obligatorios, mismo formato que
                   /api/history_range)
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
    if not start or not end:  # la consulta por lugar siempre va acotada por fechas
        return jsonify({"error": "Selecciona fecha y hora de inicio y fin"}), 400
    start_norm = start.replace("T", " ") + ":00"
    end_norm = end.replace("T", " ") + ":59"

    if start_norm > now_local().strftime("%Y-%m-%d %H:%M:%S"):
        return jsonify({"error": "La fecha/hora 'desde' no puede ser futura"}), 400
    if start_norm > end_norm:
        return jsonify({"error": "La fecha/hora 'desde' no puede ser posterior a 'hasta'"}), 400

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

    results = [eta_for_bus(bid, ulat, ulon) for bid in BUSES]
    # El "principal" es el que llega primero: ya llegó > en camino > sin señal > sin datos
    rank = {"here": 0, "ok": 1, "offline": 2, "no_data": 3}
    best = min(results, key=lambda r: (rank.get(r["status"], 9), r.get("eta_s") or 0))
    if best["status"] == "here":
        record_eta_usage(data.get("client_id"), 0, "here")
    elif best["status"] == "ok":
        record_eta_usage(data.get("client_id"), round(best["eta_s"] / 60, 1), best["source"])
    return jsonify({**best, "buses": results})


def eta_for_bus(bid, ulat, ulon):
    """Tiempo de llegada de un bus hasta el usuario (mismo cálculo de antes, por bus)."""
    out = {"bus_id": bid, "name": bus_name(bid)}
    rows = get_recent_locations(bus=bid)
    if not rows:
        return {**out, "status": "no_data"}
    blat, blon, gps_time, received_at = rows[0]
    last_dt = parse_dt(received_at)
    age = (now_local() - last_dt).total_seconds() if last_dt else None
    base = {**out, "bus": {"lat": blat, "lon": blon, "time": gps_time}, "last_seen": received_at,
            "age_s": round(age) if age is not None else None}
    if age is None or age > ETA_BUS_MAX_AGE_S:
        return {**base, "status": "offline"}

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
        return {**base, "status": "here", "straight_m": round(straight), "trend": trend}

    # Método de cálculo según ETA_METHOD: modelo propio -> TomTom -> estimado
    result = ml_eta(rows, blat, blon, ulat, ulon, speed) if ETA_METHOD in ("auto", "ml") else None
    if result is None and ETA_METHOD in ("ml", "estimado"):
        result = (*estimate_eta(blat, blon, ulat, ulon, speed), "estimado")
    if result is None:
        result = compute_eta(blat, blon, ulat, ulon, speed, bid)
    seconds, meters, delay, path, source = result
    return {
        **base, "status": "ok", "source": source,
        "eta_s": round(seconds), "eta_min": max(1, round(seconds / 60)),
        "distance_m": round(meters), "straight_m": round(straight),
        "traffic_delay_s": round(delay or 0),
        "bus_speed_kmh": round(speed, 1) if speed is not None else None,
        "trend": trend,
        "path": [[round(a, 6), round(b, 6)] for a, b in path],
    }


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


# ---------- Respaldo: un celular dentro del bus envía su GPS por internet ----------
# Se usa si el módem del equipo falla. Solo funciona en el WRITER (es el que lleva los recorridos)
# y pide una llave (INGEST_KEY en el .env; si no existe, se usa la misma METRICS_KEY).
INGEST_KEY = os.environ.get("INGEST_KEY", "").strip() or METRICS_KEY
INGEST_MAX_ACC_M = 60        # ubicaciones del celular con más error que esto se descartan


def _ingest_allowed(key):
    return bool(INGEST_KEY) and key == INGEST_KEY


@app.route("/enviar")
def enviar_page():
    """Página para el celular que va en el bus (oculta, con llave)."""
    if not _ingest_allowed(request.args.get("key", "")):
        return "Not Found", 404
    bus = clean_bus(request.args.get("bus"))
    return render_template("enviar.html", key=request.args.get("key", ""), bus_name=bus_name(bus),
                           bus_id=bus, writer=IS_WRITER)


@app.route("/api/ingest", methods=["POST"])
def api_ingest():
    """Recibe una ubicación del celular de respaldo y la guarda igual que una trama del ESP32."""
    data = request.get_json(silent=True) or {}
    if not _ingest_allowed(str(data.get("key", ""))):
        return jsonify({"error": "Not Found"}), 404
    if not IS_WRITER:
        return jsonify({"error": "Esta instancia no es el writer: abre la página en el dominio del writer"}), 409
    try:
        lat, lon = float(data["lat"]), float(data["lon"])
        acc = float(data.get("acc") or 0)
        gps_time = datetime.strptime(str(data["time"]), "%Y-%m-%d %H:%M:%S").strftime("%Y-%m-%d %H:%M:%S")
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "datos no válidos"}), 400
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return jsonify({"error": "coordenadas fuera de rango"}), 400
    if acc > INGEST_MAX_ACC_M:
        return jsonify({"ok": False, "skipped": f"precisión {acc:.0f} m (máx {INGEST_MAX_ACC_M} m)"})
    save_location(lat, lon, gps_time, {}, clean_bus(str(data.get("bus") or "")))
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
        print(f"[DB] (writer) Cuenta: {ACCOUNT_ID} | Buses: {BUSES} | Sesiones activas: {_active_session_id}")
        threading.Thread(target=udp_listener, daemon=True).start()
        for _p, _b in UDP_BUS_PORTS.items():
            if _p != UDP_PORT:
                threading.Thread(target=udp_listener, args=(_p, _b), daemon=True).start()
    else:
        try:
            ensure_bus_columns()
        except Exception as exc:
            print(f"[DB] ({ROLE}) No se pudo revisar la columna vehicle_id: {exc}")
    threading.Thread(target=listen_notifications, daemon=True).start()
    print(f"[HTTP] ({ROLE}) '{APP_NAME}' en http://0.0.0.0:{HTTP_PORT} (DB: {DB_NAME}@{DB_HOST}, cuenta: {ACCOUNT_ID}, tz: {APP_TZ.key})")
    app.run(host="0.0.0.0", port=HTTP_PORT, threaded=True)