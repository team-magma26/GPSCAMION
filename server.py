import argparse
import json
import os
import queue
import socket
import threading
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo
from zoneinfo import ZoneInfo

import psycopg2
from psycopg2 import extensions
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template, request
from flask import Flask, Response, jsonify, render_template, request

load_dotenv()
#esto es una prueba para ver si se actualiza solo 

DB_HOST = os.environ["DB_HOST"]
DB_PORT = os.environ.get("DB_PORT", "5432")
DB_NAME = os.environ["DB_NAME"]
DB_USER = os.environ["DB_USER"]
DB_PASSWORD = os.environ["DB_PASSWORD"]
DB_SSLMODE = os.environ.get("DB_SSLMODE", "require")

# Nombre visible de la página, configurable desde .env (ej: APP_NAME="Flota Norte - GPS")
APP_NAME = os.environ.get("APP_NAME", "GPS Truck Tracker")

# Cuenta que esta instancia escribe (si es writer) y muestra (writer o reader).
# Se configura en el .env de cada instancia (ej: ACCOUNT_ID=alejandra).
# Si no se define, usa "main" y todo se comporta como antes de este cambio.
ACCOUNT_ID = os.environ.get("ACCOUNT_ID", "main")

# Zona horaria con la que se guardan received_at y started_at. Es
# independiente de la zona horaria del sistema operativo de la instancia.
APP_TZ = ZoneInfo(os.environ.get("APP_TZ", "America/Bogota"))


def now_local() -> datetime:
    """Hora actual en APP_TZ, sin tzinfo (naive), para que siga siendo
    comparable con los datetime que se leen de la BD como texto."""
    return datetime.now(APP_TZ).replace(tzinfo=None)


NOTIFY_CHANNEL = "new_location"

db_lock = threading.Lock()

# Umbral de silencio del GPS para considerar que empieza un recorrido nuevo.
# Si pasan más de este tiempo sin recibir un paquete UDP, el próximo paquete
# que llegue abre una sesión nueva (y el mapa borra el trazo anterior).
SESSION_GAP_SECONDS = 60

# Umbral para agrupar lecturas consecutivas cerca de un mismo punto (búsqueda
# "por lugar") en una sola "pasada". Si el camión estuvo ahí varias lecturas
# seguidas con menos de este tiempo entre una y otra, se reporta como una
# sola visita (con hora de entrada y salida) en vez de listarlas todas.
PASSAGE_GAP_SECONDS = 300

session_lock = threading.Lock()
# Estado en memoria del proceso writer: cuándo llegó el último paquete UDP
# y cuál es la sesión (recorrido) activa en este momento. No depende de que
# el servidor Flask se reinicie — depende de que el camión deje de transmitir.
_last_packet_dt = None
_active_session_id = None

# Límite de puntos que devuelve una consulta de historial por rango, para
# no traer de una sola vez rangos enormes (ej: varios meses) a la página.
MAX_HISTORY_RANGE_POINTS = 3000

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
    # Migración: si la tabla ya existía de antes (sin session_id), la agrega
    # sin tocar los datos que ya había.
    cur.execute("ALTER TABLE locations ADD COLUMN IF NOT EXISTS session_id TEXT")
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_locations_account_id ON locations (account_id, id DESC)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_locations_session_id ON locations (session_id, id ASC)"
    )
    # Índice sobre received_at: la búsqueda por rango de fecha/hora filtra
    # justo por esta columna, así que sin este índice cada búsqueda
    # recorrería la tabla completa.
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_locations_received_at ON locations (received_at)"
    )
    # Índice compuesto: la búsqueda por rango filtra por cuenta y por fecha.
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_locations_acc_received ON locations (account_id, received_at)"
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
    # Migración: cada sesión pertenece a una cuenta. Las sesiones que ya
    # existían quedan asignadas a 'main'.
    cur.execute(
        "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS account_id TEXT NOT NULL DEFAULT 'main'"
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


def save_location(lat: float, lon: float, gps_time: str):
    global _last_packet_dt, _active_session_id

    received_at_dt = now_local()
    received_at = received_at_dt.strftime("%Y-%m-%d %H:%M:%S")

    # Decide si este paquete pertenece al recorrido actual o si abre uno
    # nuevo, según cuánto tiempo pasó desde el último paquete recibido.
    with session_lock:
        if _last_packet_dt is None or (received_at_dt - _last_packet_dt).total_seconds() > SESSION_GAP_SECONDS:
            _active_session_id = start_new_session()
            notify_session_start(_active_session_id)
        _last_packet_dt = received_at_dt
        session_id = _active_session_id

    payload = {**location_to_payload(lat, lon, gps_time), "account_id": ACCOUNT_ID}

    with db_lock:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO locations (account_id, session_id, lat, lon, gps_time, received_at) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (ACCOUNT_ID, session_id, lat, lon, gps_time, received_at),
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
            # Compatibilidad: si aún no hay ninguna sesión registrada (ej.
            # datos viejos previos a este cambio), no filtra por sesión,
            # pero sí por cuenta.
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
    Usa 'received_at' porque siempre tiene el formato fijo
    'YYYY-MM-DD HH:MM:SS' que guarda el propio servidor al insertar.
    Devuelve también el texto de la sentencia SQL ejecutada, para poder
    mostrarla o registrarla."""
    query = (
        "SELECT lat, lon, gps_time, received_at FROM locations "
        "WHERE account_id = %s AND received_at BETWEEN %s AND %s "
        "ORDER BY id ASC LIMIT %s"
    )
    params = (ACCOUNT_ID, start_dt, end_dt, max_points)

    with db_lock:
        conn = get_connection()
        cur = conn.cursor()
        sql_text = cur.mogrify(query, params).decode("utf-8")
        print(f"[SQL] {sql_text}")  # queda visible en journalctl / logs del servicio
        cur.execute(query, params)
        rows = cur.fetchall()
        cur.close()
        conn.close()
    return rows, sql_text


def get_passages_near(target_lat: float, target_lon: float, radius_m: float,
                       start_dt: str = None, end_dt: str = None,
                       max_points: int = MAX_HISTORY_RANGE_POINTS):
    """Puntos de ACCOUNT_ID dentro de radius_m metros del punto
    (target_lat, target_lon), calculando la distancia con la fórmula de
    Haversine directo en SQL (sin necesidad de extensiones tipo PostGIS).
    Si se pasan start_dt/end_dt (formato 'YYYY-MM-DD HH:MM:SS'), acota
    además por fecha/hora, igual que get_history_range().
    Devuelve también el texto de la sentencia SQL ejecutada."""
    date_filter = ""
    params = [target_lat, target_lon, target_lat, ACCOUNT_ID]
    if start_dt and end_dt:
        date_filter = "AND received_at BETWEEN %s AND %s"
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
        ORDER BY received_at ASC
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


def group_passages(rows, gap_seconds: int = PASSAGE_GAP_SECONDS):
    """Agrupa filas (lat, lon, gps_time, received_at, distance_m) ordenadas
    por received_at en "pasadas": si el camión estuvo cerca del punto en
    varias lecturas seguidas (menos de gap_seconds entre una y otra), se
    reporta como una sola pasada con su hora de entrada y de salida, en vez
    de listar cada lectura individual."""
    passages = []
    current = None
    prev_dt = None

    for lat, lon, gps_time, received_at, distance_m in rows:
        received_dt = datetime.strptime(received_at, "%Y-%m-%d %H:%M:%S")

        if current is None or (received_dt - prev_dt).total_seconds() > gap_seconds:
            if current is not None:
                passages.append(current)
            current = {
                "start_received_at": received_at,
                "end_received_at": received_at,
                "start_gps_time": gps_time,
                "end_gps_time": gps_time,
                "lat": lat,
                "lon": lon,
                "points_count": 1,
                "min_distance_m": round(distance_m, 1),
            }
        else:
            current["end_received_at"] = received_at
            current["end_gps_time"] = gps_time
            current["points_count"] += 1
            if distance_m < current["min_distance_m"]:
                current["min_distance_m"] = round(distance_m, 1)
                current["lat"] = lat
                current["lon"] = lon

        prev_dt = received_dt

    if current is not None:
        passages.append(current)

    return passages


subscribers = []
subscribers_lock = threading.Lock()


def broadcast(payload: dict):
    with subscribers_lock:
        for q in subscribers:
            q.put(payload)


def location_to_payload(lat, lon, gps_time):
    date_part, _, time_part = gps_time.partition(" ")
    return {"type": "location", "lat": lat, "lon": lon, "date": date_part, "time": time_part}


def parse_message(raw: str):
    fields = {}
    for part in raw.strip().split(";"):
        if "=" in part:
            key, value = part.split("=", 1)
            fields[key.strip()] = value.strip()
    lat = float(fields["Lat"])
    lon = float(fields["Lon"])
    gps_time = fields["Time"]
    return lat, lon, gps_time


def udp_listener():

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", UDP_PORT))
    print(f"[UDP] (writer) Sniffer listening on 0.0.0.0:{UDP_PORT}")
    while True:
        try:
            data, addr = sock.recvfrom(1024)
            raw = data.decode("utf-8", errors="ignore")
            lat, lon, gps_time = parse_message(raw)
            save_location(lat, lon, gps_time)
            print(f"[UDP] (writer) {addr[0]} -> lat={lat} lon={lon} time={gps_time}")
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
            # Todas las instancias reciben todos los NOTIFY de la BD; cada una
            # solo reenvía a su página los de su propia cuenta.
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
    desde el frontend. Parámetros esperados (query string):
      start = 'YYYY-MM-DDTHH:MM'  (formato nativo de <input type="datetime-local">)
      end   = 'YYYY-MM-DDTHH:MM'
      debug = '1' (opcional) -> además de los puntos, devuelve la sentencia SQL ejecutada
    """
    start = request.args.get("start")
    end = request.args.get("end")
    debug = request.args.get("debug") == "1"

    if not start or not end:
        return jsonify({"error": "Se requieren los parámetros 'start' y 'end'"}), 400

    # El input datetime-local llega como 'YYYY-MM-DDTHH:MM'. Se concatena
    # con segundos y se reemplaza la 'T' por espacio para que calce
    # exactamente con el formato 'YYYY-MM-DD HH:MM:SS' que guarda la BD.
    start_norm = start.replace("T", " ") + ":00"
    end_norm = end.replace("T", " ") + ":59"

    rows, sql_text = get_history_range(start_norm, end_norm)
    points = [
        {"lat": lat, "lon": lon, "time": gps_time, "received_at": received_at}
        for lat, lon, gps_time, received_at in rows
    ]

    response = {"points": points, "count": len(points)}
    if debug:
        response["sql"] = sql_text
    return jsonify(response)


@app.route("/api/passages")
def api_passages():
    """Busca en qué momentos el camión pasó cerca de un punto elegido en el
    mapa. Parámetros (query string):
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

    debug = request.args.get("debug") == "1"

    rows, sql_text = get_passages_near(target_lat, target_lon, radius_m, start_norm, end_norm)
    passages = group_passages(rows)

    response = {"passages": passages, "count": len(passages), "points_matched": len(rows)}
    if debug:
        response["sql"] = sql_text
    return jsonify(response)


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