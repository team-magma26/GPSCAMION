# Modelo propio de tiempo de llegada (ML)

Entrena un modelo con los **recorridos reales** del bus (tabla `locations` de la base de datos) para predecir cuántos segundos tarda el bus en llegar a un punto de la ruta. Corre en la instancia, dentro de `server.py`.

**No se borra nada de la base de datos.** El exportador abre la conexión en modo solo lectura y usa solo los recorridos desde la fecha que se le indique.

## Archivos

| Archivo | Qué hace |
|---|---|
| `eta_ml.py` | Piezas compartidas: variables del modelo, ruta de referencia y proyección de una posición sobre la ruta |
| `exportar_dataset.py` | Lee los recorridos desde una fecha y genera `dataset_eta.csv` y `ruta_referencia.json` |
| `entrenar.py` | Compara la fórmula actual, regresión lineal, Random Forest y Gradient Boosting, y guarda el mejor en `modelo_eta.joblib` |
| `modelo_eta.joblib` | El modelo entrenado (lo carga `server.py` al arrancar) |
| `ruta_referencia.json` | La ruta de la línea (el recorrido real más largo) |
| `resultados_modelos.csv`, `grafica_predicho_vs_real.png` | Resultados para el informe |

## Variables del modelo

| Variable | Qué es |
|---|---|
| `dist_ruta_m` | Metros por la ruta entre el bus y el destino |
| `dist_recta_m` | Metros en línea recta entre el bus y el destino |
| `avance_m` | Metros que lleva el bus desde el inicio de la ruta de referencia |
| `sentido` | 1 hacia el final de la ruta, 0 hacia el inicio |
| `vel_actual_kmh` | Velocidad del bus en el último minuto |
| `hora`, `dia_semana`, `fin_de_semana` | Cuándo es (la hora pico va más lento) |

Etiqueta (lo que se predice): `segundos` reales que tardó el bus.

## Cómo entrenarlo (en la instancia writer de main)

```bash
cd ~/GPSCAMION
source venv/bin/activate              # si usan entorno virtual
pip install -r requirements.txt       # instala scikit-learn, pandas, joblib, numpy
python ml/exportar_dataset.py --desde "AAAA-MM-DD HH:MM"     # fecha desde la que hay recorridos reales
python ml/entrenar.py
```

Entrénenlo en la instancia (o con la misma versión de scikit-learn que tienen las instancias): un modelo guardado con una versión distinta puede no cargar.

Se necesitan **al menos 3 recorridos**; con 20–30 a distintas horas el modelo aprende la hora pico.

## Cómo se usa en el servidor

En el `.env`, `ETA_METHOD` elige cómo se calcula el tiempo:

| Valor | Qué hace |
|---|---|
| `auto` (por defecto) | Modelo propio si existe; si no, TomTom; si no, el estimado |
| `ml` | Modelo propio; si no se puede, el estimado (sin TomTom) |
| `tomtom` | Como antes: TomTom y, si falla, el estimado |
| `estimado` | Solo la fórmula (distancia / velocidad) |

Si `modelo_eta.joblib` no existe o falta scikit-learn, el servidor funciona igual que antes.

El modelo no se usa cuando el bus está a más de 300 m de la ruta conocida o va en sentido contrario al usuario (tiene que dar la vuelta); en esos casos se usa TomTom o el estimado. En la tarjeta de la página aparece "Modelo propio (recorridos reales)" cuando el tiempo sale del modelo.
