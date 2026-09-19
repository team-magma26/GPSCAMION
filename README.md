# GPS Truck Tracker

Sistema de rastreo GPS en tiempo real. Un dispositivo GPS instalado en un camión envía su posición por UDP, un servidor la guarda en una base de datos PostgreSQL (RDS) y varias páginas web la muestran en vivo sobre un mapa (Leaflet), actualizándose sin recargar mediante Server-Sent Events (SSE).

## Arquitectura

```
                    UDP (puerto 5000)
   [GPS / App Android] ──────────────► [EC2 Writer]
                                             │
                                             ▼
                                     [RDS PostgreSQL]
                                             ▲
                    ┌────────────────────────┼────────────────────────┐
                    │                        │                        │
              [EC2 Reader 1]           [EC2 Reader 2]           [EC2 Reader 3]
                    │                        │                        │
                    └──────────── nginx (80/443) ───────────────────┘
                                             │
                                     [Navegador del usuario]
```

- **1 Writer**: recibe los paquetes UDP del GPS, los parsea y los inserta en la base de datos. También sirve la página web como cualquier reader.
- **N Readers**: solo leen de la base de datos y sirven la página web. No reciben UDP.
- **RDS PostgreSQL**: única fuente de verdad. Todas las instancias (writer y readers) se conectan a la misma base.
- **LISTEN/NOTIFY de Postgres**: el writer notifica cada inserción nueva por un canal (`new_location`), y todas las instancias (incluida él mismo) están escuchando ese canal para reenviar la actualización a los navegadores conectados vía SSE, sin necesidad de que el navegador esté "consultando" todo el tiempo.
- **nginx**: recibe el tráfico público en los puertos 80/443, gestiona HTTPS (Let's Encrypt / Certbot) y redirige internamente al puerto donde corre Flask (8000 en producción).

## Stack

- **Backend**: Python 3, Flask, psycopg2, python-dotenv
- **Base de datos**: PostgreSQL (Amazon RDS)
- **Frontend**: HTML, Leaflet.js (mapas), Server-Sent Events
- **Infraestructura**: 4 instancias EC2 (1 writer + 3 readers), nginx como reverse proxy, Let's Encrypt para SSL, DuckDNS para el dominio
- **CI/CD**: GitHub Actions (self-hosted runners en cada EC2)

## Estructura del repositorio

```
GPSCAMION/
├── server.py              # Backend Flask + listener UDP + notificaciones en vivo
├── templates/
│   └── index.html         # Página del mapa en tiempo real
├── requirements.txt        # Dependencias de Python
├── .env                    # Variables de entorno (no versionado)
└── .github/
    └── workflows/
        ├── deploy.yml       # Despliegue automático a producción (rama main)
        └── deploy-test.yml  # Despliegue automático a test (rama test)
```

## Variables de entorno (`.env`)

| Variable | Descripción | Ejemplo |
|---|---|---|
| `DB_HOST` | Endpoint del RDS | `postgre-1.xxxx.us-east-1.rds.amazonaws.com` |
| `DB_PORT` | Puerto de Postgres | `5432` |
| `DB_NAME` | Nombre de la base | `postgres` |
| `DB_USER` | Usuario de la base | `-` |
| `DB_PASSWORD` | Contraseña de la base | — |
| `DB_SSLMODE` | Modo SSL de conexión | `require` |
| `APP_NAME` | Nombre visible en la página | `- Truck Tracker` |

## Ejecución local

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Modo writer (recibe UDP + inserta + sirve la web)
python3 server.py --role writer --udp-port 5000 --http-port 8000

# Modo reader (solo sirve la web, no recibe UDP)
python3 server.py --role reader --http-port 8000
```

## Endpoints HTTP

| Ruta | Método | Descripción |
|---|---|---|
| `/` | GET | Página principal con el mapa |
| `/api/latest` | GET | Última posición conocida (lat, lon, fecha, hora) |
| `/api/session` | GET | ID de la sesión/recorrido activo |
| `/api/history` | GET | Historial de puntos de la sesión actual (hasta 1000) |
| `/events` | GET | Stream de eventos en vivo (SSE) — actualizaciones de posición y avisos de nueva sesión |

## Formato del paquete UDP

El GPS envía un mensaje de texto plano con campos separados por `;`:

```
Lat=4.60971;Lon=-74.08175;Time=2026-09-14 10:30:00
```

## Sesiones / recorridos

Si pasan más de `SESSION_GAP_SECONDS` (60s por defecto) sin recibir un paquete UDP, el siguiente paquete que llega abre una **sesión nueva**: se limpia el trazo del mapa en todos los navegadores conectados (vía SSE) y el historial (`/api/history`) empieza a filtrar solo por esa nueva sesión. Los datos viejos no se borran de la base, solo dejan de mostrarse en el mapa.

## Despliegue en producción

Cada EC2 corre un servicio `systemd` (`GPSCAMION.service`) que mantiene el proceso Flask corriendo permanentemente. Un push a la rama `main` dispara el workflow `deploy.yml`, que en cada instancia hace:

```bash
cd ~/GPSCAMION
git pull origin main
sudo systemctl restart GPSCAMION
```

nginx expone la app en `https://---.duckdns.org`, haciendo proxy al puerto `8000` donde corre Flask.

## Ambiente de test

Existe un ambiente paralelo para probar cambios sin afectar producción:

- **Rama de Git**: `test`
- **Base de datos**: `gpstracker_test` (misma instancia RDS, base separada)
- **Carpeta**: `~/GPSCAMION-test` (clon independiente en cada EC2, con su propio `venv` y `.env`)
- **Puertos**: Flask en `8001`, UDP en `5001` (solo en la instancia writer)
- **Servicio**: `GPSCAMION-test.service`
- **Dominio**: `https://---test.duckdns.org`, con proxy de nginx al puerto `8001`
- **CI/CD**: un push a la rama `test` dispara `deploy-test.yml`, que actualiza y reinicia `GPSCAMION-test` en las 4 instancias

**Importante**: el ambiente de test no recibe automáticamente los datos del GPS real. El envío UDP depende de a qué IP:puerto apunta el dispositivo/app — si sigue apuntando al puerto `5000`, solo alimenta producción. Para probar el flujo completo hay que mandar paquetes UDP de prueba al puerto `5001` del writer.


## Notas de infraestructura
- El firewall de AWS (Security Group) debe permitir tráfico entrante en los puertos **80** y **443** desde `0.0.0.0/0` en la instancia donde corre nginx.
- Los certificados SSL se gestionan con Certbot (renovación automática programada).
- Las tablas (`locations`, `sessions`) se crean automáticamente al arrancar el proceso en modo `writer` (`init_db()`); los readers asumen que ya existen, por lo que **siempre debe iniciarse primero el writer** en un ambiente/base nueva.
