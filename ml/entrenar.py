"""
Entrena y compara modelos de tiempo de llegada, incluida una red neuronal (MLP).

Uso (en el PC):
    pip install scikit-learn pandas numpy matplotlib
    python ml/entrenar.py                      # usa ml/dataset_eta.csv
    python ml/entrenar.py --datos otro.csv --rapido

Hace lo siguiente:
  1. Validación cruzada POR VUELTAS (GroupKFold): en cada ronda se prueba con vueltas que el
     modelo nunca vio. Así el error es honesto (no se "aprende" la misma vuelta que se evalúa).
  2. Compara:
       - Fórmula actual (línea recta x 1,35 / velocidad), la línea base
       - Regresión lineal
       - Random Forest
       - Gradient Boosting
       - Red neuronal (MLP): se escoge la arquitectura y la regularización con una validación
         cruzada interna, solo con las vueltas de entrenamiento de cada ronda
  3. Guarda:
       ml/resultados_modelos.csv           tabla de comparación (para el informe)
       ml/grafica_predicho_vs_real.png     predicho vs real de la red y de la fórmula
       ml/grafica_error_por_distancia.png  error según qué tan lejos está el destino
       ml/curva_aprendizaje_mlp.png        pérdida de la red por época
       ml/modelo_eta_mlp.json              la red final, entrenada con todos los datos. La carga
                                           server.py y la evalúa con numpy (no necesita scikit-learn)
"""
import argparse
import json
import os
import sys
from datetime import datetime

import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingRegressor, RandomForestRegressor
from sklearn.linear_model import LinearRegression
from sklearn.model_selection import GroupKFold
from sklearn.neural_network import MLPRegressor
from sklearn.preprocessing import StandardScaler
import warnings
from sklearn.exceptions import ConvergenceWarning

# Al escoger la arquitectura, las redes de prueba entrenan máximo 150 épocas para que sea rápido;
# algunas todavía estarían mejorando un poco y scikit-learn avisa "hasn't converged yet". No es un
# error: solo se usan para comparar entre ellas. La red final entrena hasta 300 épocas con parada temprana.
warnings.filterwarnings("ignore", category=ConvergenceWarning)

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
from ml.eta_ml import FEATURES, MLP_INPUTS, MLPJson, mlp_inputs  # noqa: E402

FALLBACK_KMH = 22  # igual que ETA_FALLBACK_KMH en server.py

# Arquitecturas y regularización (alpha = penalización L2) que se prueban para la red
MLP_GRID = [
    {"hidden_layer_sizes": (32,), "alpha": 1e-1},
    {"hidden_layer_sizes": (64, 32), "alpha": 1e-2},
    {"hidden_layer_sizes": (64, 32), "alpha": 1e-1},
]


def formula_actual(df):
    """La fórmula que usa hoy el servidor cuando no hay TomTom."""
    kmh = np.where(df["vel_actual_kmh"] >= 8, df["vel_actual_kmh"], FALLBACK_KMH)
    return (df["dist_recta_m"] * 1.35 / (kmh / 3.6)).values


def metricas(y_real, y_pred):
    err = y_pred - y_real
    return {
        "MAE_min": round(float(np.mean(np.abs(err))) / 60, 2),
        "RMSE_min": round(float(np.sqrt(np.mean(err ** 2))) / 60, 2),
        "MAPE_%": round(float(np.mean(np.abs(err) / np.maximum(y_real, 60))) * 100, 1),
        "dentro_de_2_min_%": round(float(np.mean(np.abs(err) <= 120)) * 100, 1),
    }


class RedNeuronal:
    """MLP (perceptrón multicapa).
    Entradas: mlp_inputs() (posición en la vuelta como seno/coseno), escaladas.
    Salida: cuántos minutos más (o menos) que la vuelta de referencia tarda el bus en ese tramo.
    Predecir la corrección y no el tiempo completo le funcionó mejor con pocas vueltas."""

    def __init__(self, hidden_layer_sizes=(64, 32), alpha=1e-3, max_iter=300, seed=42, verbose=False):
        self.scaler = StandardScaler()
        self.net = MLPRegressor(hidden_layer_sizes=hidden_layer_sizes, activation="relu", solver="adam",
                                alpha=alpha, learning_rate_init=1e-3, batch_size=256, max_iter=max_iter,
                                early_stopping=True, validation_fraction=0.15, n_iter_no_change=15,
                                random_state=seed, verbose=verbose)
        self.hidden = hidden_layer_sizes
        self.alpha = alpha

    def fit(self, X, y):
        ref = X[:, FEATURES.index("tiempo_ref_s")]
        self.net.fit(self.scaler.fit_transform(mlp_inputs(X)), (y - ref) / 60.0)
        return self

    def predict(self, X):
        ref = X[:, FEATURES.index("tiempo_ref_s")]
        return np.maximum(ref + self.net.predict(self.scaler.transform(mlp_inputs(X))) * 60.0, 0)

    def a_json(self):
        return {"x_mean": self.scaler.mean_.tolist(), "x_scale": self.scaler.scale_.tolist(),
                "pesos": [w.tolist() for w in self.net.coefs_],
                "sesgos": [b.tolist() for b in self.net.intercepts_], "y_escala": 60.0,
                "entradas": MLP_INPUTS, "arquitectura": [len(MLP_INPUTS), *self.hidden, 1], "alpha": self.alpha}


def elegir_mlp(X, y, g, max_filas, rng):
    """Validación cruzada interna (por vueltas) para escoger arquitectura y alpha."""
    if len(X) > max_filas:
        idx = rng.choice(len(X), max_filas, replace=False)
        X, y, g = X[idx], y[idx], g[idx]
    k = min(3, len(set(g)))
    best = None
    print("   probando arquitecturas de la red (error con vueltas que no vio):")
    for params in MLP_GRID:
        errs = []
        for tr, te in GroupKFold(n_splits=k).split(X, y, g):
            m = RedNeuronal(**params, max_iter=150).fit(X[tr], y[tr])
            errs.append(np.mean(np.abs(m.predict(X[te]) - y[te])))
        e = float(np.mean(errs))
        capas = " -> ".join(str(c) for c in (10, *params["hidden_layer_sizes"], 1))
        print(f"      {capas:16s} alpha {params['alpha']:<5}  error {e / 60:5.2f} min")
        if best is None or e < best[0]:
            best = (e, params)
    return best[1]


def main():
    ap = argparse.ArgumentParser(description="Entrena el modelo de tiempo de llegada.")
    ap.add_argument("--datos", default=os.path.join(BASE, "ml", "dataset_eta.csv"))
    ap.add_argument("--salida", default=os.path.join(BASE, "ml", "modelo_eta_mlp.json"))
    ap.add_argument("--rondas", type=int, default=5, help="Rondas de validación cruzada (por vueltas)")
    ap.add_argument("--rapido", action="store_true", help="Usa menos filas para escoger la red (más rápido)")
    ap.add_argument("--ver", action="store_true", help="Muestra la pérdida de la red final en cada época")
    args = ap.parse_args()
    carpeta = os.path.dirname(os.path.abspath(args.salida))
    rng = np.random.default_rng(42)

    df = pd.read_csv(args.datos)
    n_v = df["vuelta"].nunique()
    print(f"Dataset: {len(df)} ejemplos de {n_v} vueltas ({df['bus'].nunique()} buses)")
    if n_v < 4:
        sys.exit("Se necesitan al menos 4 vueltas para validar. Junten más recorridos reales.")

    X = df[FEATURES].values.astype(float)
    y = df["segundos"].values.astype(float)
    g = df["vuelta"].values
    base = formula_actual(df)
    max_filas = 6000 if args.rapido else 15000

    modelos = {
        "Regresión lineal": lambda: LinearRegression(),
        "Random Forest": lambda: RandomForestRegressor(n_estimators=80, min_samples_leaf=10, max_depth=12,
                                                       n_jobs=-1, random_state=42),
        "Gradient Boosting": lambda: GradientBoostingRegressor(n_estimators=300, max_depth=4, learning_rate=0.05,
                                                               subsample=0.8, random_state=42),
    }
    pred = {k: np.zeros(len(y)) for k in ["Fórmula actual (línea base)", *modelos, "Red neuronal (MLP)"]}
    pred["Fórmula actual (línea base)"] = base
    elegidas = []

    k = min(args.rondas, n_v)
    for r, (tr, te) in enumerate(GroupKFold(n_splits=k).split(X, y, g), 1):
        print(f"Ronda {r}/{k}: entrena con {len(set(g[tr]))} vueltas, prueba con {len(set(g[te]))}")
        for nombre, hacer in modelos.items():
            pred[nombre][te] = hacer().fit(X[tr], y[tr]).predict(X[te])
        params = elegir_mlp(X[tr], y[tr], g[tr], max_filas, rng)
        elegidas.append(params)
        pred["Red neuronal (MLP)"][te] = RedNeuronal(**params).fit(X[tr], y[tr]).predict(X[te])
        capas = " -> ".join(str(c) for c in (10, *params["hidden_layer_sizes"], 1))
        print(f"   red escogida (la de menor error): {capas}, alpha {params['alpha']}")

    # Predicciones de la validación (cada fila predicha por un modelo que no vio su vuelta)
    out = df[["vuelta", "bus", "hora_origen", "dist_ruta_m", "segundos"]].copy()
    for n, p in pred.items():
        out[n] = np.round(p, 1)
    out.to_csv(os.path.join(carpeta, "predicciones_validacion.csv"), index=False)

    # Error por bus: ¿el modelo se equivoca distinto según quién maneja?
    print("\nError promedio por bus (min):")
    for bus, d in out.groupby("bus"):
        print(f"   {bus}: " + ", ".join(f"{n.split(' (')[0]} {np.mean(np.abs(d[n] - d['segundos'])) / 60:.2f}"
                                       for n in pred))

    # Matriz de confusión: el tiempo es un número, así que se agrupa en rangos
    # y se cuenta en qué rango cayó la predicción frente al real
    rangos = [0, 5, 10, 20, 30, 1e9]
    nombres_r = ["< 5", "5–10", "10–20", "20–30", "> 30"]
    def matriz(p):
        r = np.digitize(y / 60, rangos[1:-1]); q = np.digitize(np.asarray(p) / 60, rangos[1:-1])
        m = np.zeros((len(nombres_r), len(nombres_r)), dtype=int)
        for a_, b_ in zip(r, q):
            m[a_, b_] += 1
        return m
    matrices = {n: matriz(pred[n]) for n in ["Red neuronal (MLP)", "Gradient Boosting", "Fórmula actual (línea base)"]}
    for n, m in matrices.items():
        acierto = np.trace(m) / m.sum() * 100
        cerca = sum(m[i, j] for i in range(len(m)) for j in range(len(m)) if abs(i - j) <= 1) / m.sum() * 100
        print(f"\nMatriz de confusión por rangos de minutos — {n}")
        print(f"   (filas = real, columnas = predicho)   acierto exacto {acierto:.1f} %, a un rango {cerca:.1f} %")
        print(pd.DataFrame(m, index=[f"real {x}" for x in nombres_r], columns=nombres_r).to_string())

    filas = [{"modelo": n, **metricas(y, p)} for n, p in pred.items()]
    tabla = pd.DataFrame(filas).sort_values("MAE_min")
    print("\nError con vueltas que el modelo no vio (validación cruzada, menor es mejor):")
    print(tabla.to_string(index=False))
    tabla.to_csv(os.path.join(carpeta, "resultados_modelos.csv"), index=False)

    # Red final: la arquitectura que más veces ganó, entrenada con todos los datos
    claves = [json.dumps({"h": list(p["hidden_layer_sizes"]), "a": p["alpha"]}) for p in elegidas]
    top = json.loads(max(set(claves), key=claves.count))
    if args.ver:
        print(f"\nEntrenando la red final {[10, *top['h'], 1]} con todos los datos (pérdida por época):")
    final = RedNeuronal(hidden_layer_sizes=tuple(top["h"]), alpha=top["a"], verbose=args.ver).fit(X, y)
    data = {
        "nombre": "Red neuronal (MLP)", "features": FEATURES, **final.a_json(),
        "metricas_validacion": next(f for f in filas if f["modelo"] == "Red neuronal (MLP)"),
        "vueltas": int(n_v), "ejemplos": int(len(df)),
        "entrenado": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    with open(args.salida, "w", encoding="utf-8") as f:
        json.dump(data, f)
    dif = np.max(np.abs(MLPJson(data).predict(X[:500]) - final.predict(X[:500])))
    print(f"Comprobación: la red guardada en JSON difiere {dif:.4f} s de la original")
    print(f"\nRed final {data['arquitectura']} (alpha {top['a']}) guardada en {args.salida}")

    # Gráficas para el informe
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        mlp = pred["Red neuronal (MLP)"]
        fig, ax = plt.subplots(figsize=(6, 6))
        ax.scatter(y / 60, base / 60, s=4, alpha=0.15, color="#e5484d", label="Fórmula actual")
        ax.scatter(y / 60, mlp / 60, s=4, alpha=0.25, color="#2f74e6", label="Red neuronal (MLP)")
        lim = y.max() / 60
        ax.plot([0, lim], [0, lim], color="#333", lw=1, label="Predicción perfecta")
        ax.set_xlim(0, lim); ax.set_ylim(0, lim * 1.3)
        ax.set_xlabel("Tiempo real (min)"); ax.set_ylabel("Tiempo predicho (min)")
        ax.set_title("Tiempo de llegada: predicho vs real\n(vueltas no vistas en el entrenamiento)")
        ax.legend(); fig.tight_layout()
        fig.savefig(os.path.join(carpeta, "grafica_predicho_vs_real.png"), dpi=150); plt.close(fig)

        bins = np.array([0, 2, 5, 10, 15, 20, 30, 50]) * 1000
        cat = np.digitize(df["dist_ruta_m"].values, bins) - 1
        etiquetas = [f"{bins[i] // 1000}–{bins[i + 1] // 1000}" for i in range(len(bins) - 1)]
        fig, ax = plt.subplots(figsize=(8, 4.5))
        for nombre, color in [("Fórmula actual (línea base)", "#e5484d"), ("Gradient Boosting", "#999999"),
                              ("Red neuronal (MLP)", "#2f74e6")]:
            mae = [np.mean(np.abs(pred[nombre][cat == i] - y[cat == i])) / 60 if np.any(cat == i) else np.nan
                   for i in range(len(etiquetas))]
            ax.plot(etiquetas, mae, marker="o", color=color, label=nombre)
        ax.set_xlabel("Distancia por la ruta hasta el destino (km)"); ax.set_ylabel("Error promedio (min)")
        ax.set_title("Error según la distancia"); ax.grid(alpha=0.3); ax.legend(); fig.tight_layout()
        fig.savefig(os.path.join(carpeta, "grafica_error_por_distancia.png"), dpi=150); plt.close(fig)

        fig, ax = plt.subplots(figsize=(7, 4))
        ax.plot(final.net.loss_curve_, label="Entrenamiento (pérdida)")
        if final.net.validation_scores_:
            ax2 = ax.twinx()
            ax2.plot(final.net.validation_scores_, color="#3fb27f", label="Validación (R²)")
            ax2.set_ylabel("R² de validación")
        ax.set_xlabel("Época"); ax.set_ylabel("Pérdida (error cuadrático / 2)")
        ax.set_title("Curva de aprendizaje de la red final"); fig.tight_layout()
        fig.savefig(os.path.join(carpeta, "curva_aprendizaje_mlp.png"), dpi=150); plt.close(fig)
        fig, axs = plt.subplots(1, len(matrices), figsize=(5.2 * len(matrices), 4.6))
        for ax, (n, m) in zip(axs, matrices.items()):
            pct = m / np.maximum(m.sum(axis=1, keepdims=True), 1) * 100
            ax.imshow(pct, cmap="Blues", vmin=0, vmax=100)
            for i in range(len(m)):
                for j in range(len(m)):
                    ax.text(j, i, f"{pct[i, j]:.0f} %", ha="center", va="center", fontsize=9,
                            color="white" if pct[i, j] > 55 else "#222")
            ax.set_xticks(range(len(nombres_r))); ax.set_xticklabels(nombres_r)
            ax.set_yticks(range(len(nombres_r))); ax.set_yticklabels(nombres_r)
            ax.set_xlabel("Predicho (min)"); ax.set_ylabel("Real (min)")
            ax.set_title(f"{n.split(' (')[0]}\nacierto {np.trace(m) / m.sum() * 100:.0f} %")
        fig.suptitle("Matriz de confusión por rangos de tiempo (% de cada fila real)")
        fig.tight_layout()
        fig.savefig(os.path.join(carpeta, "matriz_confusion.png"), dpi=150); plt.close(fig)
        print("Gráficas guardadas en", carpeta)
    except Exception as exc:
        print(f"(No se pudieron hacer las gráficas: {exc})")


if __name__ == "__main__":
    main()
