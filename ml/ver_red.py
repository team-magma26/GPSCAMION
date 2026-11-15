"""
Muestra por dentro la red neuronal del tiempo de llegada (ml/modelo_eta_mlp.json).

Uso (desde la carpeta del proyecto):
    python ml/ver_red.py
    python ml/ver_red.py --desde 4 --hasta 12 --vel 25 --hora 7.5

Lee el archivo que carga el servidor y:
  1. Muestra la arquitectura: cuántas entradas, neuronas y salidas tiene, y el tamaño de cada
     matriz de pesos (de ahí salen las 32 neuronas).
  2. Hace una predicción paso a paso, con Python puro (sin numpy ni scikit-learn), para que se vea
     que el tiempo sale de multiplicar por esos pesos.
"""
import argparse
import json
import math
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
import vueltas  # noqa: E402

L = 46200.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--desde", type=float, default=4, help="km de la vuelta donde está el bus")
    ap.add_argument("--hasta", type=float, default=12, help="km de la vuelta donde está el usuario")
    ap.add_argument("--vel", type=float, default=25, help="velocidad del bus en km/h")
    ap.add_argument("--hora", type=float, default=7.5, help="hora del día (7.5 = 7:30 a. m.)")
    a = ap.parse_args()

    m = json.load(open(os.path.join(BASE, "ml", "modelo_eta_mlp.json"), encoding="utf-8"))
    W, B = m["pesos"], m["sesgos"]

    print("=" * 60)
    print("RED NEURONAL DEL TIEMPO DE LLEGADA  (ml/modelo_eta_mlp.json)")
    print("=" * 60)
    print(f"Entrenada:      {m['entrenado']} con {m['ejemplos']} ejemplos de {m['vueltas']} vueltas")
    print(f"Arquitectura:   {' -> '.join(map(str, m['arquitectura']))}  (entradas -> neuronas ocultas -> salida)")
    for k, (w, b) in enumerate(zip(W, B), 1):
        print(f"  Capa {k}: matriz de pesos {len(w)} x {len(w[0])}  +  {len(b)} sesgos")
    n = sum(len(w) * len(w[0]) for w in W) + sum(len(b) for b in B)
    print(f"Parámetros entrenados: {n}  (10x32 + 32 + 32x1 + 1)")
    print(f"Activación de la capa oculta: ReLU  ·  regularización L2 alpha = {m['alpha']}")
    print(f"Error en validación por vueltas: {m['metricas_validacion']['MAE_min']} min en promedio")

    # ---- Predicción paso a paso ----
    R = vueltas.Ruta.from_file(os.path.join(BASE, "ruta_referencia.json"))
    p_bus, p_dest = a.desde * 1000, a.hasta * 1000
    if p_dest <= p_bus:
        p_dest += L                                   # la ruta es una vuelta: el bus llega en la siguiente
    la1, lo1, _ = R.at(p_bus)
    la2, lo2, _ = R.at(p_dest)
    dist_ruta = p_dest - p_bus
    dist_recta = vueltas._hav(la1, lo1, la2, lo2)
    t_ref = max(0.0, R.at(p_dest)[2] - R.at(p_bus)[2])
    ang = lambda s: 2 * math.pi * (s % L) / L
    x = {
        "dist_ruta_m": dist_ruta, "dist_recta_m": dist_recta, "tiempo_ref_s": t_ref,
        "avance_sin": math.sin(ang(p_bus)), "avance_cos": math.cos(ang(p_bus)),
        "destino_sin": math.sin(ang(p_dest)), "destino_cos": math.cos(ang(p_dest)),
        "vel_actual_kmh": a.vel, "vel_5min_kmh": a.vel, "hora": a.hora,
    }
    print()
    print("=" * 60)
    print(f"EJEMPLO: bus en el km {a.desde}, usuario en el km {a.hasta}, {a.vel} km/h, hora {a.hora}")
    print("=" * 60)
    print("1) Entradas (las 10 de la red):")
    for k in m["entradas"]:
        print(f"     {k:16s} = {x[k]:10.2f}")

    # escalado igual que en el entrenamiento
    z = [(x[k] - mu) / sd for k, mu, sd in zip(m["entradas"], m["x_mean"], m["x_scale"])]
    # capa oculta: 32 neuronas, cada una = ReLU(suma de entradas x pesos + sesgo)
    h = []
    for j in range(len(B[0])):
        s = B[0][j] + sum(z[i] * W[0][i][j] for i in range(len(z)))
        h.append(max(0.0, s))
    activas = sum(1 for v in h if v > 0)
    print(f"2) Capa oculta: {len(h)} neuronas, {activas} se activaron (ReLU > 0)")
    print("     " + " ".join(f"{v:.2f}" for v in h))
    # salida: 1 neurona = suma de las 32 x pesos + sesgo  ->  corrección en minutos
    corr = B[1][0] + sum(h[j] * W[1][j][0] for j in range(len(h)))
    total = max(0.0, t_ref + corr * m.get("y_escala", 60))
    print(f"3) Salida: corrección = {corr:+.2f} min sobre la vuelta de referencia")
    print(f"4) Tiempo de llegada = {t_ref / 60:.1f} min (referencia) {corr:+.1f} min = {total / 60:.1f} min")


if __name__ == "__main__":
    main()
