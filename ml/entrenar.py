"""
Entrena y compara modelos de tiempo de llegada con el dataset propio.

Uso:
    python ml/entrenar.py                      # usa ml/dataset_eta.csv
    python ml/entrenar.py --datos otro.csv

Hace lo siguiente:
  1. Separa los datos POR RECORRIDOS: unos recorridos para entrenar y otros
     (que el modelo nunca vio) para probar. Así el error es honesto.
  2. Compara:
       - Fórmula actual (línea recta x 1,35 / velocidad), la línea base
       - Regresión lineal
       - Random Forest
       - Gradient Boosting
  3. Muestra el error promedio (MAE) en minutos y guarda:
       ml/resultados_modelos.csv     tabla de comparación (para el informe)
       ml/grafica_predicho_vs_real.png
       ml/modelo_eta.joblib          el mejor modelo, reentrenado con todos los datos
"""
import argparse
import json
import os
import sys
from datetime import datetime

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingRegressor, RandomForestRegressor
from sklearn.linear_model import LinearRegression
from sklearn.model_selection import GroupShuffleSplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ml.eta_ml import FEATURES  # noqa: E402

FALLBACK_KMH = 22  # igual que ETA_FALLBACK_KMH en server.py


def formula_actual(df):
    """La fórmula que usa hoy el servidor cuando no hay TomTom."""
    kmh = np.where(df["vel_actual_kmh"] >= 8, df["vel_actual_kmh"], FALLBACK_KMH)
    return df["dist_recta_m"] * 1.35 / (kmh / 3.6)


def metricas(y_real, y_pred):
    err = y_pred - y_real
    return {
        "MAE_min": round(float(np.mean(np.abs(err))) / 60, 2),
        "RMSE_min": round(float(np.sqrt(np.mean(err ** 2))) / 60, 2),
        "MAPE_%": round(float(np.mean(np.abs(err) / np.maximum(y_real, 60))) * 100, 1),
        "dentro_de_2_min_%": round(float(np.mean(np.abs(err) <= 120)) * 100, 1),
    }


def main():
    ap = argparse.ArgumentParser(description="Entrena el modelo de tiempo de llegada.")
    ap.add_argument("--datos", default=os.path.join("ml", "dataset_eta.csv"))
    ap.add_argument("--salida", default=os.path.join("ml", "modelo_eta.joblib"))
    ap.add_argument("--prueba", type=float, default=0.25, help="Fracción de recorridos para probar (0.25 = 25 %%)")
    args = ap.parse_args()

    df = pd.read_csv(args.datos)
    n_trips = df["session_id"].nunique()
    print(f"Dataset: {len(df)} ejemplos de {n_trips} recorridos")
    if n_trips < 3:
        sys.exit("Se necesitan al menos 3 recorridos para entrenar y probar. Junten más recorridos reales.")

    X = df[FEATURES].values
    y = df["segundos"].values
    groups = df["session_id"].values

    split = GroupShuffleSplit(n_splits=1, test_size=args.prueba, random_state=42)
    tr, te = next(split.split(X, y, groups))
    print(f"Entrenamiento: {len(set(groups[tr]))} recorridos · Prueba: {len(set(groups[te]))} recorridos")

    modelos = {
        "Regresión lineal": LinearRegression(),
        # tamaño limitado para que el archivo del modelo sea liviano (se sube con git a las instancias)
        "Random Forest": RandomForestRegressor(n_estimators=80, min_samples_leaf=10, max_depth=12,
                                               n_jobs=-1, random_state=42),
        "Gradient Boosting": GradientBoostingRegressor(n_estimators=300, max_depth=4, learning_rate=0.05,
                                                       subsample=0.8, random_state=42),
    }

    filas = [{"modelo": "Fórmula actual (línea base)", **metricas(y[te], formula_actual(df.iloc[te]).values)}]
    preds = {}
    for nombre, m in modelos.items():
        m.fit(X[tr], y[tr])
        p = m.predict(X[te])
        preds[nombre] = p
        filas.append({"modelo": nombre, **metricas(y[te], p)})

    tabla = pd.DataFrame(filas).sort_values("MAE_min")
    print("\nError en los recorridos de prueba (menor es mejor):")
    print(tabla.to_string(index=False))
    tabla.to_csv(os.path.join(os.path.dirname(args.salida), "resultados_modelos.csv"), index=False)

    mejor = min(preds, key=lambda k: metricas(y[te], preds[k])["MAE_min"])
    print(f"\nMejor modelo: {mejor}")

    # Gráfica predicho vs real del mejor modelo
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(6, 6))
        ax.scatter(y[te] / 60, preds[mejor] / 60, s=6, alpha=0.35, color="#2f74e6", label=mejor)
        ax.scatter(y[te] / 60, formula_actual(df.iloc[te]).values / 60, s=6, alpha=0.2, color="#e5484d",
                   label="Fórmula actual")
        lim = max(y[te].max(), preds[mejor].max()) / 60
        ax.plot([0, lim], [0, lim], color="#333", lw=1, label="Predicción perfecta")
        ax.set_xlabel("Tiempo real (min)")
        ax.set_ylabel("Tiempo predicho (min)")
        ax.set_title("Tiempo de llegada: predicho vs real")
        ax.legend()
        fig.tight_layout()
        fig.savefig(os.path.join(os.path.dirname(args.salida), "grafica_predicho_vs_real.png"), dpi=150)
    except Exception as exc:
        print(f"(No se pudo hacer la gráfica: {exc})")

    # Reentrena el mejor con TODOS los datos y lo guarda
    final = modelos[mejor]
    final.fit(X, y)
    bundle = {
        "model": final,
        "nombre": mejor,
        "features": FEATURES,
        "metricas_prueba": next(f for f in filas if f["modelo"] == mejor),
        "recorridos": int(n_trips),
        "ejemplos": int(len(df)),
        "entrenado": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    joblib.dump(bundle, args.salida, compress=3)
    print(f"Modelo guardado en {args.salida}")
    print(json.dumps(bundle["metricas_prueba"], ensure_ascii=False))


if __name__ == "__main__":
    main()
