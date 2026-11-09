"""
Exporta el dataset de tiempos de llegada a partir de los recorridos reales.

SOLO LEE la base de datos (la conexión se abre en modo de solo lectura): no
borra ni modifica nada. Toma los recorridos desde la fecha que se indique, así
los recorridos de prueba o simulados anteriores quedan fuera sin borrarlos.

Uso (en la instancia, desde la carpeta del proyecto, con el .env de main):
    python ml/exportar_dataset.py --desde "2026-11-10 06:00"
    python ml/exportar_dataset.py --desde "2026-11-10 06:00" --hasta "2026-11-20 22:00"

Genera:
    ml/dataset_eta.csv         una fila por ejemplo (punto del bus -> punto futuro de la ruta)
    ml/ruta_referencia.json    la ruta de la línea (el recorrido real más largo)

Cada ejemplo responde: estando el bus en el punto i de un recorrido, ¿cuántos
segundos tardó realmente en llegar al punto j del mismo recorrido?
"""
import argparse
import csv
import os
import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ml.eta_ml import FEATURES, RefRoute, build_features, haversine_m, parse_time  # noqa: E402

MAX_KMH = 120          # saltos del GPS más rápidos que esto se descartan
MIN_PUNTOS = 30        # recorridos con menos puntos (~5 min) no se usan
PASO_ORIGEN = 3        # se toma 1 de cada 3 puntos como "posición actual" del bus
PASO_DESTINO = 6       # y 1 de cada 6 puntos siguientes como destino
MAX_ADELANTE_S = 45 * 60   # destinos hasta 45 min adelante
MIN_DIST_M = 150           # destinos a menos de 150 m se omiten (ya "llegó")


def connect():
    import psycopg2
    conn = psycopg2.connect(
        host=os.environ["DB_HOST"], port=os.environ.get("DB_PORT", "5432"),
        dbname=os.environ["DB_NAME"], user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"], sslmode=os.environ.get("DB_SSLMODE", "require"),
    )
    conn.set_session(readonly=True)   # garantía extra: esta conexión no puede escribir
    return conn


def leer_recorridos(conn, account_id, desde, hasta):
    sql = ("SELECT session_id, lat, lon, gps_time FROM locations "
           "WHERE account_id = %s AND session_id IS NOT NULL AND gps_time >= %s")
    params = [account_id, desde]
    if hasta:
        sql += " AND gps_time <= %s"
        params.append(hasta)
    sql += " ORDER BY session_id, gps_time ASC"
    cur = conn.cursor()
    cur.execute(sql, params)
    trips = defaultdict(list)
    for sid, lat, lon, gps_time in cur.fetchall():
        t = parse_time(gps_time)
        if t is not None:
            trips[sid].append((t, float(lat), float(lon)))
    cur.close()
    return trips


def limpiar(points):
    """Quita horas repetidas y saltos imposibles del GPS."""
    out = []
    for t, lat, lon in points:
        if out:
            t0, a0, b0 = out[-1]
            dt = (t - t0).total_seconds()
            if dt <= 0:
                continue
            if haversine_m(a0, b0, lat, lon) / dt * 3.6 > MAX_KMH:
                continue
        out.append((t, lat, lon))
    return out


def velocidad_reciente(pts, cum, i, ventana_s=60):
    t_i = pts[i][0]
    k = i
    while k > 0 and (t_i - pts[k - 1][0]).total_seconds() <= ventana_s:
        k -= 1
    dt = (t_i - pts[k][0]).total_seconds()
    return (cum[i] - cum[k]) / dt * 3.6 if dt > 0 else 0.0


def main():
    ap = argparse.ArgumentParser(description="Exporta el dataset de tiempos de llegada (solo lectura).")
    ap.add_argument("--desde", required=True, help='Fecha y hora inicial, ej. "2026-11-10 06:00"')
    ap.add_argument("--hasta", help="Fecha y hora final (opcional)")
    ap.add_argument("--salida", default=os.path.join("ml", "dataset_eta.csv"))
    ap.add_argument("--ruta", default=os.path.join("ml", "ruta_referencia.json"))
    args = ap.parse_args()

    def norm(s):
        s = s.strip().replace("T", " ")
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(s, fmt).strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                pass
        sys.exit(f"Fecha no válida: {s!r}. Usa el formato AAAA-MM-DD HH:MM")

    from dotenv import load_dotenv
    load_dotenv()  # mismo .env que usa server.py (base de datos y ACCOUNT_ID)

    desde = norm(args.desde)
    hasta = norm(args.hasta) if args.hasta else None
    account_id = os.environ.get("ACCOUNT_ID", "main")

    conn = connect()
    trips_raw = leer_recorridos(conn, account_id, desde, hasta)
    conn.close()

    trips = {}
    for sid, pts in trips_raw.items():
        pts = limpiar(pts)
        if len(pts) >= MIN_PUNTOS:
            trips[sid] = pts
    print(f"Recorridos desde {desde}{' hasta ' + hasta if hasta else ''}: "
          f"{len(trips_raw)} encontrados, {len(trips)} útiles (≥ {MIN_PUNTOS} puntos)")
    if not trips:
        sys.exit("No hay recorridos útiles en ese rango de fechas.")

    # Distancia acumulada de cada recorrido
    cums = {}
    for sid, pts in trips.items():
        c = [0.0]
        for (_, a1, b1), (_, a2, b2) in zip(pts, pts[1:]):
            c.append(c[-1] + haversine_m(a1, b1, a2, b2))
        cums[sid] = c

    # Ruta de referencia = el recorrido más largo (cubre más de la línea)
    ref_sid = max(trips, key=lambda s: cums[s][-1])
    ref = RefRoute([(a, b) for _, a, b in trips[ref_sid]])
    ref.save(args.ruta, meta={"session_id": ref_sid, "desde": desde, "hasta": hasta})
    print(f"Ruta de referencia: recorrido {ref_sid}, {ref.length_m / 1000:.1f} km, {len(ref.pts)} puntos -> {args.ruta}")

    filas = 0
    with open(args.salida, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["session_id", *FEATURES, "segundos"])
        for sid, pts in trips.items():
            cum = cums[sid]
            proj = [ref.project(a, b)[0] for _, a, b in pts]   # metros sobre la ruta de referencia
            for i in range(0, len(pts) - 1, PASO_ORIGEN):
                t_i, a_i, b_i = pts[i]
                vel = velocidad_reciente(pts, cum, i)
                for j in range(i + PASO_DESTINO, len(pts), PASO_DESTINO):
                    t_j, a_j, b_j = pts[j]
                    seg = (t_j - t_i).total_seconds()
                    if seg > MAX_ADELANTE_S:
                        break
                    dist_ruta = cum[j] - cum[i]
                    if dist_ruta < MIN_DIST_M:
                        continue
                    feats = build_features(
                        dist_ruta_m=dist_ruta,
                        dist_recta_m=haversine_m(a_i, b_i, a_j, b_j),
                        avance_m=proj[i],
                        sentido=1 if proj[j] >= proj[i] else 0,
                        vel_actual_kmh=vel,
                        dt=t_i,
                    )
                    w.writerow([sid, *[feats[k] for k in FEATURES], round(seg, 1)])
                    filas += 1
    print(f"Dataset: {filas} ejemplos -> {args.salida}")


if __name__ == "__main__":
    main()
