"""
Piezas compartidas del modelo de tiempo de llegada (ML).

Las usan los tres lados para que las variables se calculen SIEMPRE igual:
  - ml/exportar_dataset.py  -> arma el dataset de entrenamiento
  - ml/entrenar.py          -> entrena y guarda el modelo
  - server.py (/api/eta)    -> predice en vivo

Idea: la ruta del bus se representa con una "ruta de referencia" (un recorrido
real de la línea, guardado como lista de puntos con su distancia acumulada).
Cualquier posición se "proyecta" sobre esa ruta: así se sabe cuántos metros
lleva el bus desde el inicio de la línea y cuántos le faltan hasta el punto
de la ruta más cercano al usuario.
"""
import json
import math
from datetime import datetime

# Variables que recibe el modelo, en este orden
FEATURES = [
    "dist_ruta_m",      # metros por la ruta entre el bus y el destino
    "dist_recta_m",     # metros en línea recta entre el bus y el destino
    "avance_m",         # metros que lleva el bus desde el inicio de la ruta de referencia
    "sentido",          # 1 = hacia el final de la ruta de referencia, 0 = hacia el inicio
    "vel_actual_kmh",   # velocidad del bus en el último minuto
    "hora",             # hora del día en decimal (7.5 = 7:30 a. m.)
    "dia_semana",       # 0 = lunes ... 6 = domingo
    "fin_de_semana",    # 1 si es sábado o domingo
]

TIME_FMT = "%Y-%m-%d %H:%M:%S"


def haversine_m(lat1, lon1, lat2, lon2):
    r = math.radians
    h = (math.sin(r(lat2 - lat1) / 2) ** 2 +
         math.cos(r(lat1)) * math.cos(r(lat2)) * math.sin(r(lon2 - lon1) / 2) ** 2)
    return 2 * 6371000 * math.asin(math.sqrt(h))


def parse_time(text):
    try:
        return datetime.strptime(text, TIME_FMT)
    except (TypeError, ValueError):
        return None


def time_features(dt):
    hora = dt.hour + dt.minute / 60
    dow = dt.weekday()
    return {"hora": round(hora, 3), "dia_semana": dow, "fin_de_semana": 1 if dow >= 5 else 0}


class RefRoute:
    """Ruta de referencia: puntos (lat, lon) con su distancia acumulada en metros."""

    def __init__(self, points):
        self.pts = [(float(a), float(b)) for a, b in points]
        self.cum = [0.0]
        for (a1, b1), (a2, b2) in zip(self.pts, self.pts[1:]):
            self.cum.append(self.cum[-1] + haversine_m(a1, b1, a2, b2))

    @property
    def length_m(self):
        return self.cum[-1] if self.cum else 0.0

    @classmethod
    def load(cls, path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return cls(data["points"])

    def save(self, path, meta=None):
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"points": [[round(a, 6), round(b, 6)] for a, b in self.pts],
                       "length_m": round(self.length_m, 1), **(meta or {})}, f)

    def project(self, lat, lon):
        """Punto de la ruta más cercano a (lat, lon).
        Devuelve (metros desde el inicio de la ruta, índice del punto, metros hasta la ruta)."""
        best_i, best_d = 0, float("inf")
        for i, (a, b) in enumerate(self.pts):
            d = haversine_m(lat, lon, a, b)
            if d < best_d:
                best_i, best_d = i, d
        return self.cum[best_i], best_i, best_d

    def between(self, i, j):
        """Puntos de la ruta entre dos índices (para dibujar la línea en el mapa)."""
        if i <= j:
            seg = self.pts[i:j + 1]
        else:
            seg = list(reversed(self.pts[j:i + 1]))
        step = max(1, len(seg) // 250)
        out = seg[::step]
        if seg and out[-1] != seg[-1]:
            out.append(seg[-1])
        return out


def build_features(dist_ruta_m, dist_recta_m, avance_m, sentido, vel_actual_kmh, dt):
    f = {
        "dist_ruta_m": round(dist_ruta_m, 1),
        "dist_recta_m": round(dist_recta_m, 1),
        "avance_m": round(avance_m, 1),
        "sentido": int(sentido),
        "vel_actual_kmh": round(vel_actual_kmh or 0.0, 2),
    }
    f.update(time_features(dt))
    return f


def load_model(path):
    """Carga el modelo guardado por entrenar.py. Devuelve None si no existe o falta joblib/scikit-learn."""
    try:
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
