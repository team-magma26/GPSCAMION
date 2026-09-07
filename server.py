import argparse
import json
import os
import queue
import socket
import threading
from datetime import datetime

import psycopg2
from psycopg2 import extensions
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template

load_dotenv()

DB_HOST = os.environ["DB_HOST"]
DB_PORT = os.environ.get("DB_PORT", "5432")
DB_NAME = os.environ["DB_NAME"]
DB_USER = os.environ["DB_USER"]
DB_PASSWORD = os.environ["DB_PASSWORD"]
DB_SSLMODE = os.environ.get("DB_SSLMODE", "require")


ACCOUNT_ID = os.environ["ACCOUNT_ID"]  


NOTIFY_CHANNEL = "new_location"

db_lock = threading.Lock()

parser = argparse.ArgumentParser(description="GPS tracking UDP sniffer + web server (Postgres compartida + SSE, multi-cuenta)")
parser.add_argument("--udp-port", type=int, default=5000)
parser.add_argument("--http-port", type=int, default=8000)
args = parser.parse_args()

UDP_PORT = args.udp_port
HTTP_PORT = args.http_port


def get_connection():
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
        user=DB_USER, password=DB_PASSWORD, sslmode=DB_SSLMODE,
    )


def init_db():
    conn = get_connection()
    cur = conn.cursor()
    # Tabla compartida entre las 3 cuentas, con account_id para separar los datos
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
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_locations_account_id ON locations (account_id, id DESC)"
    )
    conn.commit()
    cur.close()
    conn.close()


def save_location(lat: float, lon: float, gps_time: str):
    received_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with db_lock:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO locations (account_id, lat, lon, gps_time, received_at) VALUES (%s, %s, %s, %s, %s)",
            (ACCOUNT_ID, lat, lon, gps_time, received_at),
        )
        cur.execute(
            f"NOTIFY {NOTIFY_CHANNEL}, %s",
            (json.dumps(location_to_payload(ACCOUNT_ID, lat, lon, gps_time)),)
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
    with db_lock:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT lat, lon, gps_time FROM locations WHERE account_id = %s ORDER BY id DESC LIMIT %s",
            (ACCOUNT_ID, limit),
        )
        rows = cur.fetchall()
        cur.close()
        conn.close()
    return list(reversed(rows))


subscribers = []
subscribers_lock = threading.Lock()


def broadcast(payload: dict):
    with subscribers_lock:
        for q in subscribers:
            q.put(payload)


def location_to_payload(account_id, lat, lon, gps_time):
    date_part, _, time_part = gps_time.partition(" ")
    return {"account_id": account_id, "lat": lat, "lon": lon, "date": date_part, "time": time_part}


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
    print(f"[UDP] ({ACCOUNT_ID}) Sniffer listening on 0.0.0.0:{UDP_PORT}")
    while True:
        try:
            data, addr = sock.recvfrom(1024)
            raw = data.decode("utf-8", errors="ignore")
            lat, lon, gps_time = parse_message(raw)
            save_location(lat, lon, gps_time)
            print(f"[UDP] ({ACCOUNT_ID}) {addr[0]} -> lat={lat} lon={lon} time={gps_time}")
        except Exception as exc:
            print(f"[UDP] ({ACCOUNT_ID}) Ignored malformed packet: {exc}")


def listen_notifications():
    conn = get_connection()
    conn.set_isolation_level(extensions.ISOLATION_LEVEL_AUTOCOMMIT)
    cur = conn.cursor()
    cur.execute(f"LISTEN {NOTIFY_CHANNEL};")
    print(f"[DB] ({ACCOUNT_ID}) Listening on shared channel '{NOTIFY_CHANNEL}', filtering for this account")
    while True:
        conn.poll()
        while conn.notifies:
            notify = conn.notifies.pop(0)
            payload = json.loads(notify.payload)
            # La RDS es compartida por las 3 cuentas: solo re-emitimos lo nuestro
            if payload.get("account_id") == ACCOUNT_ID:
                broadcast(payload)


app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html", account_id=ACCOUNT_ID)


@app.route("/api/latest")
def api_latest():
    row = get_latest_location()
    if row is None:
        return jsonify({"lat": None, "lon": None, "date": None, "time": None})
    lat, lon, gps_time = row
    return jsonify(location_to_payload(ACCOUNT_ID, lat, lon, gps_time))


@app.route("/api/history")
def api_history():
    rows = get_history()
    points = [{"lat": lat, "lon": lon, "time": gps_time} for lat, lon, gps_time in rows]
    return jsonify(points)


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
    init_db()
    threading.Thread(target=udp_listener, daemon=True).start()
    threading.Thread(target=listen_notifications, daemon=True).start()
    print(f"[HTTP] ({ACCOUNT_ID}) Web page at http://0.0.0.0:{HTTP_PORT} (UDP port: {UDP_PORT}, DB: {DB_NAME}@{DB_HOST})")
    app.run(host="0.0.0.0", port=HTTP_PORT, threaded=True)