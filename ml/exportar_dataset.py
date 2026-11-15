"""
Exporta el dataset de tiempos de llegada a partir de las lecturas REALES de los buses.

Cada ejemplo responde: estando el bus en la lectura i, ¿cuántos segundos tardó realmente en
llegar a la lectura j (más adelante en la ruta)? Solo se usan lecturas reales: los tramos que la
página dibuja como estimados NO entran al dataset. La posición en la ruta (avance) se calcula con
vueltas.py, igual que en el servidor.

Dos formas de leer los datos:

  1) De la base de datos (en la instancia, con el .env de main). SOLO LEE: la conexión se abre en
     modo de solo lectura.
        python ml/exportar_dataset.py --desde "2026-11-09 06:00"
        python ml/exportar_dataset.py --desde "2026-11-09 06:00" --hasta "2026-11-20 22:00"

  2) De archivos JSON descargados de la página (para entrenar en el PC sin base de datos).
     Archivos con nombre bus1_*.json, bus2_*.json con {"points": [{"lat","lon","time"}, ...]}:
        python ml/exportar_dataset.py --json C:\\Users\\atuir\\Downloads\\OnRoute_datos

Genera ml/dataset_eta.csv (una fila por ejemplo) con la columna "vuelta" para separar
entrenamiento y prueba por vueltas.
"""
import argparse
import csv
import glob
import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
import vueltas  # noqa: E402
from ml.eta_ml import FEATURES, build_features, haversine_m, parse_time, tiempo_ref, velocidad_kmh  # noqa: E402

MAX_KMH = 120              # saltos del GPS más rápidos que esto se descartan
PASO_ORIGEN = 3            # se toma 1 de cada 3 lecturas como "posición actual" del bus
PASO_DESTINO = 6           # y 1 de cada 6 lecturas siguientes como destino
MAX_ADELANTE_S = 60 * 60   # destinos hasta 60 min adelante
MIN_DIST_M = 150           # destinos a menos de 150 m se omiten (ya "llegó")
TERMINAL_M = 300           # el bus esperando en el punto de inicio no sirve de origen
# La vuelta con la que se hizo ruta_referencia.json no entra al dataset: para ella "tiempo_ref_s"
# sería exactamente la respuesta y el error de validación saldría mejor de lo que es.
VUELTA_REFERENCIA = ("bus1", "2026-11-10 07:52:15", "2026-11-10 09:46:06")


def connect():
    import psycopg2
    conn = psycopg2.connect(
        host=os.environ["DB_HOST"], port=os.environ.get("DB_PORT", "5432"),
        dbname=os.environ["DB_NAME"], user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"], sslmode=os.environ.get("DB_SSLMODE", "require"),
    )
    conn.set_session(readonly=True)   # garantía extra: esta conexión no puede escribir
    return conn


def leer_db(desde, hasta):
    from dotenv import load_dotenv
    load_dotenv(os.path.join(BASE, ".env"))     # mismo .env que usa server.py
    account_id = os.environ.get("ACCOUNT_ID", "main")
    sql = ("SELECT COALESCE(vehicle_id, 'bus1'), lat, lon, gps_time FROM locations "
           "WHERE account_id = %s AND gps_time >= %s")
    params = [account_id, desde]
    if hasta:
        sql += " AND gps_time <= %s"
        params.append(hasta)
    sql += " ORDER BY 1, gps_time ASC"
    conn = connect()
    cur = conn.cursor()
    cur.execute(sql, params)
    data = defaultdict(list)
    for bus, lat, lon, t in cur.fetchall():
        data[bus].append((float(lat), float(lon), str(t)[:19]))
    cur.close()
    conn.close()
    return data


def leer_json(carpeta):
    data = defaultdict(list)
    for f in sorted(glob.glob(os.path.join(carpeta, "*.json"))):
        bus = os.path.basename(f).split("_")[0]
        try:
            with open(f, encoding="utf-8") as fh:
                pts = json.load(fh).get("points", [])
        except (ValueError, AttributeError):
            continue
        for p in pts:
            t = p.get("time") or p.get("gps_time")
            if t and "lat" in p and "lon" in p:
                data[bus].append((float(p["lat"]), float(p["lon"]), str(t)[:19]))
    return data


def limpiar(rows):
    """Ordena, quita horas repetidas y saltos imposibles del GPS."""
    out = []
    for lat, lon, t in sorted(set(rows), key=lambda r: r[2]):
        if out:
            dt = (parse_time(t) - parse_time(out[-1][2])).total_seconds()
            if dt <= 0:
                continue
            if haversine_m(out[-1][0], out[-1][1], lat, lon) / dt * 3.6 > MAX_KMH:
                continue
        out.append((lat, lon, t))
    return out


def ejemplos_dia(bus, rows, R):
    """Ejemplos (origen -> destino) de un bus en un día."""
    on, P, links = vueltas.avance(rows, R)
    if len(on) < 10:
        return []
    L = R.L
    times = [parse_time(rows[i][2]) for i in on]
    prog = [P[i] for i in on]
    # tramos continuos: se cortan donde no se sabe qué pasó (cut); "skip" (vuelta corta) también
    # corta, porque ahí el avance salta km que el bus no recorrió
    tramo, t_id = [], 0
    for q, i in enumerate(on):
        if q > 0 and links[i][1] in ("cut", "skip"):
            t_id += 1
        tramo.append(t_id)
    out = []
    for q in range(0, len(on) - 1, PASO_ORIGEN):
        i = on[q]
        av = prog[q] % L
        v1 = velocidad_kmh(prog, times, q, 60)
        v5 = velocidad_kmh(prog, times, q, 300)
        if (av < TERMINAL_M or av > L - TERMINAL_M) and v1 < 3:
            continue                           # esperando en el inicio: no se sabe cuándo sale
        if bus == VUELTA_REFERENCIA[0] and VUELTA_REFERENCIA[1] <= rows[i][2] <= VUELTA_REFERENCIA[2]:
            continue
        vuelta = f"{bus}|{rows[i][2][:10]}|{int(prog[q] // L)}"
        for r in range(q + PASO_DESTINO, len(on), PASO_DESTINO):
            if tramo[r] != tramo[q]:
                break
            seg = (times[r] - times[q]).total_seconds()
            if seg > MAX_ADELANTE_S:
                break
            dist = prog[r] - prog[q]
            if dist < MIN_DIST_M:
                continue
            j = on[r]
            f = build_features(
                dist_ruta_m=dist,
                dist_recta_m=haversine_m(rows[i][0], rows[i][1], rows[j][0], rows[j][1]),
                tiempo_ref_s=tiempo_ref(R, prog[q], prog[r]),
                avance_m=av, destino_m=prog[r] % L,
                vel_actual_kmh=v1, vel_5min_kmh=v5, dt=times[q],
            )
            out.append([vuelta, bus, rows[i][2], *[f[k] for k in FEATURES], round(seg, 1)])
    return out


def main():
    ap = argparse.ArgumentParser(description="Exporta el dataset de tiempos de llegada (solo lectura).")
    ap.add_argument("--desde", help='Leer de la base de datos desde esta fecha, ej. "2026-11-09 06:00"')
    ap.add_argument("--hasta", help="Fecha y hora final (opcional, con --desde)")
    ap.add_argument("--json", help="Leer de una carpeta con archivos bus1_*.json, bus2_*.json")
    ap.add_argument("--salida", default=os.path.join(BASE, "ml", "dataset_eta.csv"))
    ap.add_argument("--ruta", default=os.path.join(BASE, "ruta_referencia.json"))
    args = ap.parse_args()

    def norm(s):
        s = s.strip().replace("T", " ")
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(s, fmt).strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                pass
        sys.exit(f"Fecha no válida: {s!r}. Usa el formato AAAA-MM-DD HH:MM")

    if args.json:
        data = leer_json(args.json)
        fuente = f"archivos de {args.json}"
    elif args.desde:
        data = leer_db(norm(args.desde), norm(args.hasta) if args.hasta else None)
        fuente = f"base de datos desde {args.desde}"
    else:
        sys.exit("Indica --desde (base de datos) o --json (carpeta con archivos).")

    R = vueltas.Ruta.from_file(args.ruta)
    filas = []
    for bus, rows in sorted(data.items()):
        por_dia = defaultdict(list)
        for r in limpiar(rows):
            por_dia[r[2][:10]].append(r)
        n0 = len(filas)
        for dia, rr in sorted(por_dia.items()):
            filas += ejemplos_dia(bus, rr, R)
        print(f"  {bus}: {len(rows)} lecturas en {len(por_dia)} días -> {len(filas) - n0} ejemplos")
    if not filas:
        sys.exit("No salieron ejemplos: revisa las fechas o los archivos.")

    with open(args.salida, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["vuelta", "bus", "hora_origen", *FEATURES, "segundos"])
        w.writerows(filas)
    n_v = len({r[0] for r in filas})
    print(f"Dataset ({fuente}): {len(filas)} ejemplos de {n_v} vueltas -> {args.salida}")


if __name__ == "__main__":
    main()
