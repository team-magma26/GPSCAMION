"""
Piezas compartidas del modelo de tiempo de llegada (ML).

Las usan los tres lados para que las variables se calculen SIEMPRE igual:
  - ml/exportar_dataset.py  -> arma el dataset de entrenamiento
  - ml/entrenar.py          -> entrena y guarda el modelo
  - server.py (/api/eta)    -> predice en vivo

Idea: la posición del bus se expresa como "avance": cuántos metros lleva desde el punto de
inicio de la vuelta, sobre la ruta de referencia de 46 km (ruta_referencia.json, ver vueltas.py).
La ruta pasa dos veces por varias calles (de ida y de vuelta); por eso el avance no se saca con
"el punto más cercano" sino con vueltas.match(), que mira la secuencia de lecturas. Como la ruta es
una vuelta cerrada, el bus siempre llega al usuario: la distancia por la ruta es el avance que
le falta hasta la siguiente pasada por el punto del usuario.

La red neuronal (MLP) se entrena en el PC con scikit-learn y se guarda como JSON (pesos y
escalado). El servidor la evalúa con numpy, así no necesita scikit-learn ni la misma versión.
"""
import json
import math
from datetime import datetime

# Variables que recibe el modelo, en este orden
FEATURES = [
    "dist_ruta_m",      # metros por la ruta entre el bus y el destino
    "dist_recta_m",     # metros en línea recta entre el bus y el destino
    "tiempo_ref_s",     # segundos que tardó la vuelta de referencia en ese mismo tramo
    "avance_m",         # dónde va el bus: metros desde el inicio de la vuelta (0 a 46 200)
    "destino_m",        # dónde está el destino: metros desde el inicio de la vuelta
    "vel_actual_kmh",   # velocidad del bus en el último minuto (por la ruta)
    "vel_5min_kmh",     # velocidad media del bus en los últimos 5 minutos (por la ruta)
    "hora",             # hora del día en decimal (7.5 = 7:30 a. m.)
    "dia_semana",       # 0 = lunes ... 6 = domingo
    "fin_de_semana",    # 1 si es sábado o domingo
]

TIME_FMT = "%Y-%m-%d %H:%M:%S"
VEL_MAX_KMH = 90      # más que esto es un salto del GPS, no la velocidad real del bus


def haversine_m(lat1, lon1, lat2, lon2):
    r = math.radians
    h = (math.sin(r(lat2 - lat1) / 2) ** 2 +
         math.cos(r(lat1)) * math.cos(r(lat2)) * math.sin(r(lon2 - lon1) / 2) ** 2)
    return 2 * 6371000 * math.asin(math.sqrt(h))


def parse_time(text):
    try:
        return datetime.strptime(str(text)[:19], TIME_FMT)
    except (TypeError, ValueError):
        return None


def time_features(dt):
    hora = dt.hour + dt.minute / 60
    dow = dt.weekday()
    return {"hora": round(hora, 3), "dia_semana": dow, "fin_de_semana": 1 if dow >= 5 else 0}


def velocidad_kmh(prog, times, i, ventana_s):
    """Velocidad media por la ruta (km/h) en la ventana que termina en la lectura i.
    prog = avance acumulado (m) de cada lectura; times = datetime de cada lectura."""
    k = i
    while k > 0 and (times[i] - times[k - 1]).total_seconds() <= ventana_s:
        k -= 1
    dt = (times[i] - times[k]).total_seconds()
    if dt < 20:
        return 0.0
    return min(VEL_MAX_KMH, max(0.0, (prog[i] - prog[k]) / dt * 3.6))


def tiempo_ref(R, p_bus, p_destino):
    """Segundos que tardó la vuelta de referencia entre dos avances acumulados (p_destino >= p_bus)."""
    return max(0.0, R.at(p_destino)[2] - R.at(p_bus)[2])


def build_features(dist_ruta_m, dist_recta_m, tiempo_ref_s, avance_m, destino_m, vel_actual_kmh, vel_5min_kmh, dt):
    f = {
        "dist_ruta_m": round(dist_ruta_m, 1),
        "dist_recta_m": round(dist_recta_m, 1),
        "tiempo_ref_s": round(tiempo_ref_s, 1),
        "avance_m": round(avance_m, 1),
        "destino_m": round(destino_m, 1),
        "vel_actual_kmh": round(vel_actual_kmh or 0.0, 2),
        "vel_5min_kmh": round(vel_5min_kmh or 0.0, 2),
    }
    f.update(time_features(dt))
    return f


# ---------- Red neuronal ----------
LARGO_VUELTA_M = 46200.0
MLP_INPUTS = ["dist_ruta_m", "dist_recta_m", "tiempo_ref_s", "avance_sin", "avance_cos",
              "destino_sin", "destino_cos", "vel_actual_kmh", "vel_5min_kmh", "hora"]


def mlp_inputs(X, features=FEATURES):
    """Entradas de la red a partir de las variables del modelo (matriz con columnas en `features`).
    La posición en la vuelta va como seno y coseno: así el km 46,2 queda pegado al km 0
    (es el mismo punto), cosa que la red no sabría si le llega el número tal cual."""
    import numpy as np
    X = np.asarray(X, dtype=float)
    c = {k: X[:, i] for i, k in enumerate(features)}
    ang_a = 2 * np.pi * c["avance_m"] / LARGO_VUELTA_M
    ang_d = 2 * np.pi * c["destino_m"] / LARGO_VUELTA_M
    cols = {"avance_sin": np.sin(ang_a), "avance_cos": np.cos(ang_a),
            "destino_sin": np.sin(ang_d), "destino_cos": np.cos(ang_d)}
    return np.column_stack([cols[k] if k in cols else c[k] for k in MLP_INPUTS])


class MLPJson:
    """Red neuronal densa guardada como JSON y evaluada con numpy.
    entradas (mlp_inputs) escaladas -> capas ocultas ReLU -> corrección en minutos.
    Tiempo predicho = tiempo de la vuelta de referencia + corrección."""

    def __init__(self, data):
        import numpy as np
        self.np = np
        self.mean = np.array(data["x_mean"], dtype=float)
        self.scale = np.array(data["x_scale"], dtype=float)
        self.W = [np.array(w, dtype=float) for w in data["pesos"]]
        self.b = [np.array(b, dtype=float) for b in data["sesgos"]]
        self.y_scale = float(data.get("y_escala", 60.0))   # la red predice minutos

    def predict(self, rows):
        np = self.np
        X = np.asarray(rows, dtype=float)
        h = (mlp_inputs(X) - self.mean) / self.scale
        for k, (w, b) in enumerate(zip(self.W, self.b)):
            h = h @ w + b
            if k < len(self.W) - 1:
                h = np.maximum(h, 0)          # ReLU en las capas ocultas
        ref = X[:, FEATURES.index("tiempo_ref_s")]
        return np.maximum(ref + h[:, 0] * self.y_scale, 0)


def load_model(path):
    """Carga el modelo que guardó entrenar.py.
    - .json: red neuronal (solo necesita numpy)
    - .joblib: modelo de scikit-learn (necesita la misma versión con la que se entrenó)
    Devuelve un dict con "model", "nombre", "features", ... o None."""
    try:
        if path.endswith(".json"):
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            return {**{k: v for k, v in data.items() if k not in ("pesos", "sesgos")}, "model": MLPJson(data)}
        import joblib
        bundle = joblib.load(path)
        if not isinstance(bundle, dict) or "model" not in bundle:
            return None
        return bundle
    except Exception as exc:  # archivo ausente, librería no instalada, versión distinta…
        print(f"[ML] Modelo de tiempo de llegada no cargado ({path}): {exc}")
        return None


def predict_seconds(bundle, features):
    row = [[features[k] for k in bundle.get("features", FEATURES)]]
    return max(float(bundle["model"].predict(row)[0]), 0.0)
