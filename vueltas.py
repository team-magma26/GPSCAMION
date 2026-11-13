"""OnRoute · Separar los datos de un bus en vueltas y reconstruir los tramos sin datos.

Idea general
------------
1. Ruta de referencia (ruta_referencia.json): una vuelta completa sin huecos, tomada de los datos
   reales (Bus de Luis, 10 de noviembre, 07:52 a 09:46), remuestreada cada 25 m. Cada punto
   guarda su avance en metros desde el inicio (s) y los segundos desde la salida (t).
2. Emparejar: a cada lectura GPS se le busca su avance s en la referencia (algoritmo de Viterbi:
   escoge la secuencia de posiciones más lógica para todo el día, no punto por punto). Tiene en
   cuenta que el bus avanza hacia adelante, a una velocidad posible y en el sentido de la ruta,
   porque varias calles se recorren dos veces (de ida y de vuelta). Si una lectura está a más de
   ~140 m de la referencia (calle paralela, desvío), queda "fuera de la ruta".
3. Vueltas: el avance se acumula durante el día; cada vez que pasa de 46 km (largo de la vuelta)
   empieza otra vuelta. La vuelta empieza cuando el bus sale del punto de inicio (no se cuenta la
   espera) y termina cuando llega de nuevo.
4. Huecos: si el celular dejó de mandar (más de 90 s) pero el bus siguió avanzando a una
   velocidad normal, ese tramo se rellena con la referencia y se marca como ESTIMADO (est=True),
   con horas repartidas según cómo se mueve el bus en la referencia. Nada de esto se guarda en
   la base de datos: se calcula al pedir la vuelta.
5. Vuelta corta: si el bus se devuelve antes de Puerto Colombia, el avance salta varios km en
   segundos (se saltó ese tramo de la referencia).
"""
import json
import math
from datetime import datetime

FMT = "%Y-%m-%d %H:%M:%S"
KX = 111320 * math.cos(math.radians(11.0))   # metros por grado de longitud (Barranquilla)
KY = 110540                                   # metros por grado de latitud

NEAR_M = 200          # candidatos en la referencia a menos de esto
OFF_COST = 4.0        # costo de declarar una lectura "fuera de la ruta" (~140 m)
GAP_S = 90            # más de 90 s sin datos = hueco
SKIP_V = 25           # avanzar más rápido que esto (m/s) = se saltó un tramo (vuelta corta)
SHORT_SKIP_M = 5000   # salto mínimo para decir que fue una vuelta corta
MAX_BRIDGE_MIN = 150  # un hueco más largo no se rellena (no se sabe qué hizo el bus)
BRIDGE_V = (2.0, 16.0)  # velocidad media (m/s) aceptable para rellenar un hueco de más de 20 min
MIN_OBS_M = 2000      # vueltas con menos de 2 km de datos reales no se muestran
STEP_EST_M = 50       # separación de los puntos estimados


def _hav(a, b, c, d):
    r = math.radians
    h = math.sin(r(c - a) / 2) ** 2 + math.cos(r(a)) * math.cos(r(c)) * math.sin(r(d - b) / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


def _pt(text):
    return datetime.strptime(str(text)[:19], FMT)


class Ruta:
    """Ruta de referencia: puntos [(lat, lon, s_m, t_s)] cada 25 m."""

    CELL = 250

    def __init__(self, pts):
        self.p = [tuple(x) for x in pts]
        self.L = self.p[-1][2]
        self.T = self.p[-1][3]
        self.xy = [(lo * KX, la * KY) for la, lo, _, _ in self.p]
        n = len(self.p)
        self.head = []                         # sentido de la ruta en cada punto (vector unitario)
        for j in range(n):
            a, b = self.xy[max(0, j - 2)], self.xy[min(n - 1, j + 2)]
            dx, dy = b[0] - a[0], b[1] - a[1]
            h = math.hypot(dx, dy) or 1
            self.head.append((dx / h, dy / h))
        self.grid = {}
        for j, (x, y) in enumerate(self.xy):
            self.grid.setdefault((int(x // self.CELL), int(y // self.CELL)), []).append(j)

    @classmethod
    def from_file(cls, path):
        with open(path, encoding="utf-8") as f:
            return cls(json.load(f)["points"])

    def cands(self, la, lo):
        """Pasadas de la referencia cerca del punto: [(índice, distancia_m)], una por pasada."""
        x, y = lo * KX, la * KY
        cx, cy = int(x // self.CELL), int(y // self.CELL)
        c = []
        for i in (-1, 0, 1):
            for k in (-1, 0, 1):
                for j in self.grid.get((cx + i, cy + k), ()):
                    d = math.hypot(x - self.xy[j][0], y - self.xy[j][1])
                    if d <= NEAR_M:
                        c.append((j, d))
        c.sort()
        groups = []
        for j, d in c:
            if groups and j - groups[-1][-1][0] <= 8:
                groups[-1].append((j, d))
            else:
                groups.append([(j, d)])
        return [min(g, key=lambda q: q[1]) for g in groups]

    def ds(self, j, k, dt=0):
        """Cuánto avanzó (m) de la posición j a la k. Tiene en cuenta que la vuelta vuelve a empezar."""
        a, b = self.p[j][2], self.p[k][2]
        if a > self.L - 3000 and b < 3000:
            return b + self.L - a              # terminó la vuelta y arrancó otra
        if dt > GAP_S and b < a - 500:
            return b + self.L - a              # cerró la vuelta mientras no había datos
        return b - a

    def at(self, s):
        """(lat, lon, t_s) de la referencia en el avance s (cualquier valor; se repite cada vuelta)."""
        lap = math.floor(s / self.L)
        s -= lap * self.L
        lo, hi = 0, len(self.p) - 1
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if self.p[mid][2] <= s:
                lo = mid
            else:
                hi = mid
        a, b = self.p[lo], self.p[hi]
        f = 0 if b[2] == a[2] else min(1, max(0, (s - a[2]) / (b[2] - a[2])))
        return a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, a[3] + (b[3] - a[3]) * f + lap * self.T


def _trans(ds, d, dt):
    """Costo de pasar de una posición a otra: avanzar ds por la ruta habiendo recorrido d en línea recta."""
    if ds < -80:
        return 1e6                              # el bus no se devuelve por la ruta
    if dt < 60:
        return abs(ds - d) / max(40, 0.5 * d)
    ds = max(ds, 0)
    v = ds / dt
    return max(0, d - ds) / 100 + max(0, v - 18) * 3 + 0.3 * abs(v - 7) / 7


def _emis(R, rows, tt, b, k, dk):
    """Costo de que la lectura b esté en el punto k: distancia + ir en sentido contrario a la ruta."""
    c = (dk / 50) ** 2 / 2
    for a in (b - 1, b + 1):
        if 0 <= a < len(rows) and abs((tt[a] - tt[b]).total_seconds()) <= 30:
            i0, i1 = min(a, b), max(a, b)
            x0, y0 = rows[i0][1] * KX, rows[i0][0] * KY
            x1, y1 = rows[i1][1] * KX, rows[i1][0] * KY
            d = math.hypot(x1 - x0, y1 - y0)
            if d > 40 and ((x1 - x0) * R.head[k][0] + (y1 - y0) * R.head[k][1]) / d < -0.5:
                c += 4
            break
    return c


def match(rows, R, max_off=10):
    """Para cada lectura: índice en la referencia, o None si va por fuera de ella (Viterbi)."""
    n = len(rows)
    if not n:
        return []
    tt = [_pt(r[2]) for r in rows]
    # estado = (costo, índice o None, ancla=(índice, lectura) del último punto en la ruta, de dónde viene)
    layer = [(_emis(R, rows, tt, 0, j, d), j, (j, 0), None) for j, d in R.cands(rows[0][0], rows[0][1])]
    layer.append((OFF_COST, None, None, None))
    hist = [layer]
    for b in range(1, n):
        cur = []
        for k, dk in R.cands(rows[b][0], rows[b][1]):
            best = None
            for p, (c, _j, anc, _) in enumerate(layer):
                if anc is None:
                    tc = c + 1.0
                else:
                    a = anc[1]
                    dt = max(1, (tt[b] - tt[a]).total_seconds())
                    d = _hav(rows[a][0], rows[a][1], rows[b][0], rows[b][1])
                    tc = c + _trans(R.ds(anc[0], k, dt), d, dt)
                if best is None or tc < best[0]:
                    best = (tc, p)
            cur.append((best[0] + _emis(R, rows, tt, b, k, dk), k, (k, b), best[1]))
        offs = {}                               # cada hipótesis puede seguir "fuera de la ruta"
        for p, (c, _j, anc, _) in enumerate(layer):
            key = None if anc is None else int(R.p[anc[0]][2] // 1000)
            if key not in offs or c + OFF_COST < offs[key][0]:
                offs[key] = (c + OFF_COST, None, anc, p)
        cur += sorted(offs.values(), key=lambda s: s[0])[:max_off]
        layer = cur
        hist.append(layer)
    p = min(range(len(layer)), key=lambda q: layer[q][0])
    out = [None] * n
    for b in range(n - 1, -1, -1):
        st = hist[b][p]
        out[b] = st[1]
        p = st[3]
    return out


def day_loops(rows, R):
    """Vueltas de un bus en un día.

    rows = [(lat, lon, 'YYYY-MM-DD HH:MM:SS'), ...] ordenadas por hora.
    Devuelve una lista de dict: start, end, kind (vuelta | vuelta corta | vuelta estimada |
    vuelta corta estimada | incompleta), points [(lat, lon, hora, estimado)], observed_km, est_km,
    gaps [(desde, hasta, metros)], from_km, to_km."""
    rows = [(float(r[0]), float(r[1]), str(r[2])[:19]) for r in rows]
    m = match(rows, R)
    L = R.L
    on = [i for i, x in enumerate(m) if x is not None]
    if len(on) < 2:
        return []

    # 1) avance acumulado P de cada lectura en la ruta y tipo de cada tramo
    P = {on[0]: R.p[m[on[0]]][2]}
    links = {}
    for a, b in zip(on, on[1:]):
        dt = (_pt(rows[b][2]) - _pt(rows[a][2])).total_seconds()
        ds = R.ds(m[a], m[b], dt)
        kind = "ok"
        if dt > GAP_S:
            v = ds / max(dt, 1)
            if dt > MAX_BRIDGE_MIN * 60 or (dt > 1200 and ds > 1000 and not BRIDGE_V[0] <= v <= BRIDGE_V[1]):
                kind = "cut"                    # hueco demasiado largo: no se sabe qué pasó
            elif ds > 300 and v <= SKIP_V:
                kind = "gap"                    # hueco que se puede rellenar
            elif ds > 300:
                kind = "skip"
        elif ds > 1000 and ds / max(dt, 1) > SKIP_V:
            kind = "skip"                       # se saltó un tramo de la referencia
        P[b] = (math.floor(P[a] / L) + 1) * L + R.p[m[b]][2] if kind == "cut" else P[a] + ds
        links[b] = (a, kind)

    # 2) cortar en vueltas: el número de vuelta solo sube
    loops, cur = [], None
    for i in on:
        a, kind = links.get(i, (None, "ok"))
        k = int(P[i] // L) if cur is None or kind == "cut" else max(cur["k"], int(P[i] // L))
        if cur is None or k != cur["k"] or kind == "cut":
            if cur:
                loops.append(cur)
            cur = {"k": k, "idx": [], "cut_before": kind == "cut" or a is None}
        cur["idx"].append(i)
    loops.append(cur)

    def tref(s):
        return R.at(s)[2]

    def fill(a, b, pa, pb, t_b=None):
        """Puntos estimados entre las lecturas a y b siguiendo la referencia."""
        ta = _pt(rows[a][2])
        tb = t_b or _pt(rows[b][2])
        T0, T1 = tref(pa), tref(pb)
        res, s = [], pa + STEP_EST_M
        while s < pb - STEP_EST_M / 2:
            la, lo, tr = R.at(s)
            f = (tr - T0) / (T1 - T0) if T1 > T0 else (s - pa) / (pb - pa)
            res.append((round(la, 6), round(lo, 6), (ta + (tb - ta) * f).strftime(FMT), True))
            s += STEP_EST_M
        return res

    out = []
    for n_, lp in enumerate(loops):
        idx, base = lp["idx"], lp["k"] * L
        first = 0                               # sin la espera en el inicio
        for q, i in enumerate(idx):
            if P[i] - base <= 150:
                first = q
            elif P[i] - base > 1000:
                break
        idx = idx[first:]
        arrived = next((q for q, i in enumerate(idx) if q > 0 and P[i] - base >= L - 150), None)
        if arrived is not None:                 # sin la espera al llegar
            idx = idx[:arrived + 1]
        nxt = loops[n_ + 1] if n_ + 1 < len(loops) else None
        next_i = nxt["idx"][0] if nxt and not nxt["cut_before"] else None
        s0, s1 = P[idx[0]] - base, P[idx[-1]] - base

        pts, gaps, obs, est, skip = [], [], 0.0, 0.0, False
        for q, i in enumerate(idx):
            if q > 0:
                a, kind = links[i]
                if kind == "gap":
                    pts += fill(a, i, P[a], P[i])
                    est += P[i] - P[a]
                    gaps.append((rows[a][2], rows[i][2], round(P[i] - P[a])))
                elif kind == "skip":
                    skip = skip or P[i] - P[a] >= SHORT_SKIP_M
                elif P[i] > P[a]:
                    obs += P[i] - P[a]
            pts.append((rows[i][0], rows[i][1], rows[i][2], False))

        reached = arrived is not None or next_i is not None
        if arrived is not None:
            end = rows[idx[-1]][2]
        elif next_i is not None:                # llegó al inicio mientras no había datos (o justo entre dos lecturas)
            a, b = idx[-1], next_i
            target = base + L - 150
            ta, tb = _pt(rows[a][2]), _pt(rows[b][2])
            f = min(1, max(0, (target - P[a]) / max(1, P[b] - P[a])))
            if links[b][1] == "gap":
                T0, T1, Tt = tref(P[a]), tref(P[b]), tref(target)
                f = (Tt - T0) / (T1 - T0) if T1 > T0 else f
            t_end = ta + (tb - ta) * f
            if links[b][1] == "gap" and target - P[a] > 300:
                ext = fill(a, b, P[a], P[b])
                pts += [p for p in ext if p[2] <= t_end.strftime(FMT)]
                est += target - P[a]
                gaps.append((rows[a][2], t_end.strftime(FMT), round(target - P[a])))
            else:
                obs += max(0, target - P[a])
            la, lo, _ = R.at(target)
            pts.append((round(la, 6), round(lo, 6), t_end.strftime(FMT), True))
            end = t_end.strftime(FMT)
        else:
            end = rows[idx[-1]][2]

        if obs < MIN_OBS_M:
            continue
        if reached and s0 <= 1000:
            kind = "vuelta corta" if skip else "vuelta"
            if obs < 0.5 * (obs + est):
                kind += " estimada"
        else:
            kind = "incompleta"
        out.append({
            "start": rows[idx[0]][2], "end": end, "kind": kind, "points": pts,
            "observed_km": round(obs / 1000, 1), "est_km": round(est / 1000, 1), "gaps": gaps,
            "from_km": round(s0 / 1000, 1), "to_km": round((L if reached else s1) / 1000, 1),
        })
    return out
