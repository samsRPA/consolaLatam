# Consola Latam

Consola web local para gestionar y consultar expedientes judiciales de Peru (portal CEJ)
y Ecuador (Funcion Judicial). Ya no scrapea los portales con un navegador propio: cada
sede consulta un servicio/bot externo por HTTP y guarda los resultados en un repositorio
local ("Mis Procesos") por cliente.

## Instalacion

Windows (PowerShell):

```powershell
cd C:\ruta\a\ConsolaLatam
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Linux / macOS:

```bash
cd /ruta/a/ConsolaLatam
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Configuracion

Copia `.env.example` a `.env` y ajusta segun donde corran los bots externos:

```text
PERU_BOT_BASE_URL=http://127.0.0.1:5090
```

Si no se define, Peru usa `http://127.0.0.1:5090` por defecto. Ecuador tiene su propia
variable (`ECUADOR_BOT_BASE_URL`, default `http://localhost:5000`, ver
`consola_latam/webapp/ecuador_client.py`).

## Uso

Windows:

```powershell
.\.venv\Scripts\python.exe -m consola_latam.webapp
```

o con el atajo `scripts\cej_peru.ps1` (agrega `-NoBrowser` para no abrir el navegador).

Linux / macOS:

```bash
.venv/bin/python -m consola_latam.webapp
```

o con el atajo `scripts/cej_peru.sh` (agrega `--no-browser` para no abrir el navegador).

Esto abre el navegador en `http://127.0.0.1:8000` con la pantalla de seleccion de sede
(Peru / Ecuador).

Flags disponibles: `--host`, `--port`, `--no-browser` (no abre el navegador
automaticamente).

## Modulos (por sede)

- **Cargar Base**: sube cualquier Excel; detecta sola en que hoja/columnas estan
  radicado, demandante y demandado (tolera nombres y orden distintos), arma el lote y lo
  envia al servicio externo. Se puede programar (`Programar`) para que se repita
  automaticamente a una hora del dia.
- **Inclusiones**: agrega un proceso individual (radicado, demandante y demandado
  obligatorios; en Peru tambien el documento de la parte consultada -- tipo DNI/CE,
  numero, codigo si es DNI, fecha de emision y de nacimiento -- y "valor parte" opcional)
  o por Excel, sin necesidad de crear una base primero.
- **Mis Procesos**: repositorio persistente por cliente. Cada proceso guarda lo ultimo
  que devolvio el bot (despacho/organo, materia, estado, partes, y el documento usado en
  la consulta si aplica). Se exporta a Excel (todo el cliente o un proceso suelto).
- **Notificaciones / Gestion**: alertas y calendario de actividad. El seguimiento de
  "movimiento nuevo" (actuaciones) es historico: aplica a los procesos que ya lo tenian
  de una version anterior del sistema; los servicios externos actuales no devuelven
  historial de actuaciones, asi que los procesos nuevos no generan ese tipo de alerta.

## Datos

Clientes, bases subidas, resultados y la base de datos SQLite se guardan por sede en:

```text
%LOCALAPPDATA%\ConsolaCEJPeru            (Peru, Windows)
%LOCALAPPDATA%\ConsolaCEJPeru\ecuador    (Ecuador, Windows)
```

En Linux/macOS, sin `LOCALAPPDATA` definido, cae en `~/AppData/Local/ConsolaCEJPeru`
(mismo formato, solo que bajo el home). Se puede cambiar la carpeta base en cualquier
plataforma con la variable de entorno `SCRAPER_APP_DATA_DIR` (por ejemplo,
`~/.local/share/ConsolaCEJPeru` para algo mas idiomatico en Linux).

## Estructura del proyecto

```text
consola_latam/
  webapp/            API FastAPI + frontend (cej.html/app.js para Peru, cj.html/app-ecuador.js para Ecuador)
    peru_client.py       cliente HTTP del bot de Peru (CEJPeruService)
    peru_bot_runner.py   arma lotes y persiste lo que devuelve el bot de Peru
    ecuador_client.py    cliente HTTP del bot de Ecuador
    run_manager.py       orquesta corridas/inclusiones (una a la vez, progreso via SSE)
    db.py                SQLite (clientes, bases, procesos, notificaciones, etc.)
  detect.py / base_reader.py   deteccion automatica de columnas al subir un Excel
  excel_writer.py               genera los Excel de salida
scripts/
  cej_peru.ps1        atajo para lanzar la consola web (Windows)
  cej_peru.sh         atajo para lanzar la consola web (Linux/macOS)
```
