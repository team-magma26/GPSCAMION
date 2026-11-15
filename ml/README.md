# Modelo propio de tiempo de llegada (ML)

Una **red neuronal (MLP)** entrenada con las **vueltas reales** de los buses predice cuántos segundos tarda el bus en llegar al punto de la ruta donde está el usuario. Se compara con regresión lineal, Random Forest, Gradient Boosting y la fórmula que usaba el servidor.

Se entrena en el PC. El servidor solo carga la red (`modelo_eta_mlp.json`) y la evalúa con numpy, así que **las instancias no necesitan scikit-learn**.

**No se borra nada de la base de datos.** El exportador abre la conexión en modo solo lectura. Al dataset solo entran lecturas reales: los tramos que la página dibuja punteados (estimados) no se usan.

## Archivos

| Archivo | Qué hace |
|---|---|
| `eta_ml.py` | Piezas compartidas: variables del modelo, entradas de la red y la red en numpy (`MLPJson`) |
| `exportar_dataset.py` | Lee las lecturas (base de datos o archivos JSON) y genera `dataset_eta.csv` |
| `entrenar.py` | Compara los modelos con validación cruzada por vueltas, hace las gráficas y guarda la red |
| `modelo_eta_mlp.json` | La red entrenada (pesos, sesgos y escalado). La carga `server.py` |
| `resultados_modelos.csv` | Tabla de comparación para el informe |
| `grafica_predicho_vs_real.png` | Tiempo predicho vs real (red y fórmula) |
| `grafica_error_por_distancia.png` | Error según qué tan lejos está el destino |
| `curva_aprendizaje_mlp.png` | Pérdida de la red por época |
| `matriz_confusion.png` | Matriz de confusión por rangos de tiempo (red, Gradient Boosting y fórmula) |
| `predicciones_validacion.csv` | Cada ejemplo con el tiempo real y lo que predijo cada modelo (se regenera; no se sube a git) |

La ruta de referencia es `ruta_referencia.json`, en la raíz del proyecto (la misma que usa `vueltas.py` para separar los recorridos).

## Cómo se arma el dataset

1. Cada lectura del bus se ubica en la ruta de 46,2 km con `vueltas.avance()`. Se usa el avance (metros desde el punto de inicio) y no "el punto más cercano", porque la ruta pasa dos veces por varias calles.
2. Ejemplo = (bus en la lectura *i*) → (bus en una lectura *j* más adelante, hasta 60 min). La etiqueta es el tiempo real entre las dos.
3. No se usan como origen las lecturas del bus esperando en el punto de inicio: no se sabe cuándo va a salir.
4. Se corta donde hubo un hueco demasiado largo o una vuelta corta (el avance salta km que el bus no recorrió).
5. La vuelta con la que se hizo la ruta de referencia no entra: para ella `tiempo_ref_s` sería exactamente la respuesta.

## Variables

| Variable | Qué es |
|---|---|
| `dist_ruta_m` | Metros por la ruta entre el bus y el destino |
| `dist_recta_m` | Metros en línea recta entre el bus y el destino |
| `tiempo_ref_s` | Segundos que tardó la vuelta de referencia en ese mismo tramo |
| `avance_m`, `destino_m` | Dónde están el bus y el destino en la vuelta (0 a 46 200 m) |
| `vel_actual_kmh`, `vel_5min_kmh` | Velocidad del bus por la ruta en el último minuto y en los últimos 5 min |
| `hora`, `dia_semana`, `fin_de_semana` | Cuándo es |

Etiqueta: `segundos` reales que tardó el bus.

## La red neuronal

- **Entradas (10):** las variables de arriba, pero la posición en la vuelta va como seno y coseno (`avance_sin`, `avance_cos`, `destino_sin`, `destino_cos`). El km 46,2 es el mismo punto que el km 0; con el número tal cual la red no lo sabría.
- **Salida:** cuántos minutos más (o menos) que la vuelta de referencia tarda el bus en ese tramo. El tiempo predicho es `tiempo_ref_s` + esa corrección. Con pocas vueltas, esto le funcionó mejor que predecir el tiempo completo.
- **Capas:** densas con ReLU. Se escoge entre (32), (64, 32) con distintas penalizaciones L2 (`alpha`) mediante validación cruzada interna. Se entrena con Adam y parada temprana (`early_stopping`).
- **Validación:** `GroupKFold` por vueltas. Cada vuelta se predice con un modelo que nunca la vio.

## Resultados (18 vueltas, 9 al 13 de noviembre)

| Modelo | Error promedio (min) | RMSE (min) | Dentro de 2 min |
|---|---|---|---|
| Gradient Boosting | 6,07 | 8,92 | 32,2 % |
| Random Forest | 6,16 | 9,25 | 35,0 % |
| Regresión lineal | 7,35 | 9,62 | 16,6 % |
| **Red neuronal (MLP)** | **7,73** | 10,76 | 22,7 % |
| Fórmula anterior | 13,39 | 18,72 | 17,8 % |

### Matriz de confusión

El tiempo de llegada es un número (regresión), así que no hay matriz de confusión directa. Para tenerla, los tiempos real y predicho se agrupan en rangos (< 5, 5–10, 10–20, 20–30 y > 30 min) y se cuenta en qué rango cayó cada predicción (`matriz_confusion.png`):

| Modelo | Rango exacto | Exacto o el de al lado |
|---|---|---|
| Gradient Boosting | 68,3 % | 96,0 % |
| Red neuronal (MLP) | 62,4 % | 94,8 % |
| Fórmula anterior | 37,7 % | 70,1 % |

La fórmula se equivoca casi siempre hacia abajo (dice "ya casi llega" cuando falta mucho), porque mide en línea recta y la ruta da vueltas. La red y Gradient Boosting confunden sobre todo rangos vecinos.

### Bus y conductor

Bus 1 = Luis, bus 2 = Andrés. Los horarios se turnan (un día Luis sale primero, al otro Andrés), así que el modelo **no usa el bus ni el conductor**: usa la posición en la ruta, la velocidad reciente y la hora. Una misma hora la puede hacer cualquiera de los dos.

Se probó agregar el bus como variable: Gradient Boosting pasa de 6,07 a 6,14 min (no mejora). El error por bus es parecido (red: 7,9 min bus 1 y 7,1 min bus 2). Con estos datos no se ve una diferencia por conductor. Ojo: solo el 11 de noviembre hay datos de los dos buses el mismo día; hay que confirmarlo con más días.

### Conclusiones

La red reduce el error de la fórmula anterior en un 42 %. Con tan pocas vueltas, los modelos de árboles todavía la superan, sobre todo en distancias largas. Las redes necesitan más datos: hay que volver a entrenar cuando haya más vueltas.

1. Usar el avance sobre la ruta (no la distancia en línea recta) es lo que más mejora la predicción: todos los modelos entrenados superan a la fórmula.
2. Con 18 vueltas, Gradient Boosting (6,1 min) es el más preciso; la red (7,7 min) es mejor que la fórmula (13,4 min) pero peor que los árboles.
3. Los errores grandes vienen de cosas que el modelo no ve: esperas largas en paradas o en el inicio, y huecos sin señal.
4. El conductor no cambia el resultado con los datos actuales; el modelo sirve para los dos buses aunque se turnen los horarios.
5. Siguiente paso: juntar más vueltas (varias semanas, los dos buses a todas las horas) y reentrenar. Con más datos se espera que la red se acerque a los árboles.

## Cómo entrenarla (en el PC)

```bash
cd GPSCAMION
pip install scikit-learn pandas numpy matplotlib
# Opción A: con los JSON descargados de la página
python ml/exportar_dataset.py --json C:\Users\atuir\Downloads\OnRoute_datos
# Opción B: desde la base de datos (en la instancia, con el .env de main)
python ml/exportar_dataset.py --desde "2026-11-09 06:00"
python ml/entrenar.py            # agrega --rapido para que tarde menos (~3 min)
```

Luego se sube `ml/modelo_eta_mlp.json` con git y se reinicia el servicio.

## Cómo se usa en el servidor

En el `.env`, `ETA_METHOD` elige cómo se calcula el tiempo:

| Valor | Qué hace |
|---|---|
| `auto` (por defecto) | Modelo propio si se puede; si no, TomTom; si no, el estimado |
| `ml` | Modelo propio; si no se puede, el estimado (sin TomTom) |
| `tomtom` | TomTom y, si falla, el estimado |
| `estimado` | Solo la fórmula (distancia / velocidad) |

`ml_eta()` en `server.py`:

1. Ubica al bus con sus lecturas de los últimos 10 min.
2. Busca la siguiente pasada del bus por el punto de la ruta más cercano al usuario. La ruta es una vuelta cerrada, así que el bus siempre llega.
3. Arma las variables y le pide el tiempo a la red.

No se usa (y pasa a TomTom o al estimado) cuando el usuario está a más de 200 m de la ruta, la última lectura del bus está fuera de la ruta o el bus está esperando en el punto de inicio. En la tarjeta de la página aparece "Modelo propio (recorridos reales)" cuando el tiempo sale de la red.
