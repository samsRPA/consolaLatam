"""API FastAPI del aplicativo local del scraper CEJ Peru + Ecuador.

Endpoints (todos bajo /api/<sede> salvo la raiz y las paginas que sirven el frontend):
  clientes:   GET/POST /api/<sede>/clients, DELETE /api/<sede>/clients/{id}
  bases:      GET /api/<sede>/clients/{id}/bases, POST (upload) /api/<sede>/clients/{id}/bases,
              GET/DELETE /api/<sede>/bases/{id}
  corridas:   POST /api/<sede>/bases/{id}/run, GET /api/<sede>/runs/{id},
              GET /api/<sede>/runs/{id}/stream (SSE), GET /api/<sede>/runs/{id}/download
  programado: GET/POST /api/<sede>/bases/{id}/schedules, PATCH/DELETE /api/<sede>/schedules/{id}

<sede> es "peru" o "ecuador". Las rutas se definen UNA vez sobre `api_router` y se montan
dos veces (una por prefijo/sede) mas abajo; un middleware fija db.CURRENT_SEDE segun el
prefijo por el que entro la peticion, asi cada endpoint automaticamente lee/escribe en la
base de datos y carpetas de la sede correcta (ver db.py) sin tener que duplicar codigo.

Nota tecnica sobre por que esto es un middleware y NO una dependencia de FastAPI: para
funciones sync, FastAPI resuelve cada `Depends()` y el propio endpoint con llamadas
SEPARADAS a `run_in_threadpool`, cada una con su propia copia de contextvars — un
`ContextVar.set()` hecho dentro de una dependencia no le llega al endpoint (se probo y
efectivamente no aisla nada). Un middleware ASGI, en cambio, envuelve TODO el ciclo de la
peticion (ruteo + dependencias + endpoint) en una sola cadena de corrutinas continua, asi
que fijar el ContextVar ahi, antes de `call_next`, si se propaga correctamente hacia
adentro (incluido el hilo del threadpool del endpoint, que copia el contexto vigente en
ese momento)."""

from __future__ import annotations

import json
import queue
import shutil
import time
import zipfile
from datetime import date
from pathlib import Path

from fastapi import APIRouter, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from ..detect import DetectionError, detect_columns
from ..base_reader import read_base_auto
from . import db
from . import ecuador_client
from .run_manager import MANAGER, RunBusyError
from .scheduler import SCHEDULER

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Scraper CEJ Peru - Consola")
api_router = APIRouter()


@app.on_event("startup")
def _startup() -> None:
    # Migra datos de la ubicacion antigua (Descargas, vulnerable a Storage Sense) a
    # AppData\Local la primera vez que se detecta, ANTES de inicializar el esquema.
    # Exclusiva de Peru (ver db.migrate_legacy_data_dir); CURRENT_SEDE arranca en "peru".
    db.migrate_legacy_data_dir()
    for sede in db.SEDES:
        db.set_sede(sede)
        db.init_db()
        # Respaldo automatico silencioso al arrancar: red de seguridad ante cualquier
        # perdida futura (siempre hay un punto de restauracion reciente en backups/).
        db.backup_database()
    db.set_sede(db.DEFAULT_SEDE)
    SCHEDULER.start()


@app.middleware("http")
async def _sede_middleware(request: Request, call_next):
    """Fija la sede de la base de datos/carpetas para toda la peticion actual, segun el
    prefijo (/api/peru o /api/ecuador) por el que entro. Ver nota tecnica arriba sobre
    por que esto tiene que ser un middleware y no un Depends()."""
    path = request.url.path
    if path == "/api/ecuador" or path.startswith("/api/ecuador/"):
        db.set_sede("ecuador")
    else:
        db.set_sede("peru")
    return await call_next(request)


# ---------- frontend ----------
# "/" sirve la landing de selección de sede; cada sede es un flujo (HTML+JS+CSS)
# independiente para no arriesgar el de Perú al tocar el de Ecuador o viceversa.

@app.get("/", response_class=HTMLResponse)
def landing() -> HTMLResponse:
    return HTMLResponse((STATIC_DIR / "landing.html").read_text(encoding="utf-8"))


@app.get("/CEJ", response_class=HTMLResponse)
def index_peru() -> HTMLResponse:
    return HTMLResponse((STATIC_DIR / "cej.html").read_text(encoding="utf-8"))


@app.get("/CJ", response_class=HTMLResponse)
def index_ecuador() -> HTMLResponse:
    return HTMLResponse((STATIC_DIR / "cj.html").read_text(encoding="utf-8"))


# ---------- clientes ----------

@api_router.get("/clients")
def api_list_clients() -> list[dict]:
    return db.list_clients()


@api_router.post("/clients")
def api_create_client(
    name: str = Form(...), description: str = Form(""), importance: str = Form("MEDIA"),
    client_type: str = Form(""),
) -> dict:
    if not name.strip():
        raise HTTPException(400, "El nombre del cliente es obligatorio")
    return db.create_client(name, description, importance, client_type)


@api_router.delete("/clients/{client_id}")
def api_delete_client(client_id: int) -> dict:
    db.delete_client(client_id)
    return {"ok": True}


@api_router.get("/clients/{client_id}/history")
def api_client_history(client_id: int) -> list[dict]:
    return db.list_query_history(client_id)


# ---------- carpetas ----------

@api_router.get("/clients/{client_id}/folders")
def api_list_folders(client_id: int) -> list[dict]:
    return db.list_folders(client_id)


@api_router.post("/clients/{client_id}/folders")
def api_create_folder(client_id: int, name: str = Form(...)) -> dict:
    if not name.strip():
        raise HTTPException(400, "El nombre de la carpeta es obligatorio")
    return db.create_folder(client_id, name)


@api_router.delete("/folders/{folder_id}")
def api_delete_folder(folder_id: int) -> dict:
    db.delete_folder(folder_id)
    return {"ok": True}


# ---------- bases ----------

@api_router.get("/clients/{client_id}/bases")
def api_list_bases(client_id: int) -> list[dict]:
    return db.list_bases(client_id)


@api_router.post("/clients/{client_id}/bases")
async def api_upload_base(client_id: int, name: str = Form(""), file: UploadFile = File(...)) -> dict:
    try:
        db.get_client(client_id)
    except KeyError:
        raise HTTPException(404, "Cliente no existe")
    if not file.filename or not file.filename.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(400, "Sube un archivo Excel (.xlsx)")

    db.UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = f"c{client_id}_{int(time.time())}_{Path(file.filename).name}"
    stored_path = db.UPLOADS_DIR / safe_name
    with stored_path.open("wb") as fh:
        shutil.copyfileobj(file.file, fh)

    try:
        cases, mapping = read_base_auto(stored_path)
    except DetectionError as exc:
        stored_path.unlink(missing_ok=True)
        raise HTTPException(422, str(exc))
    except Exception as exc:  # noqa: BLE001
        stored_path.unlink(missing_ok=True)
        raise HTTPException(422, f"No se pudo leer el Excel: {exc}")

    base = db.create_base(
        client_id=client_id,
        name=name.strip() or Path(file.filename).stem,
        original_filename=file.filename,
        stored_path=str(stored_path),
        mapping_json=mapping.to_json(),
        row_count=len(cases),
    )
    return base


@api_router.get("/bases/{base_id}")
def api_get_base(base_id: int) -> dict:
    try:
        base = db.get_base(base_id)
    except KeyError:
        raise HTTPException(404, "Base no existe")
    base["runs"] = db.list_runs(base_id)
    base["schedules"] = db.list_schedules(base_id)
    return base


@api_router.delete("/bases/{base_id}")
def api_delete_base(base_id: int) -> dict:
    db.delete_base(base_id)
    return {"ok": True}


# ---------- corridas ----------

@api_router.post("/bases/{base_id}/run")
def api_start_run(base_id: int, mode: str = Form("total")) -> dict:
    mode = mode.lower()
    if mode not in {"total", "daily"}:
        raise HTTPException(400, "mode debe ser total o daily")
    try:
        run = MANAGER.start_run(base_id, mode, db.CURRENT_SEDE.get())
    except RunBusyError as exc:
        raise HTTPException(409, str(exc))
    except KeyError:
        raise HTTPException(404, "Base no existe")
    return run


@api_router.get("/runs/{run_id}")
def api_get_run(run_id: int) -> dict:
    try:
        return db.get_run(run_id)
    except KeyError:
        raise HTTPException(404, "Corrida no existe")


@api_router.post("/runs/{run_id}/cancel")
def api_cancel_run(run_id: int) -> dict:
    try:
        MANAGER.cancel_run(run_id, db.CURRENT_SEDE.get())
    except KeyError:
        raise HTTPException(404, "No hay una corrida activa con ese id")
    return {"ok": True}


@api_router.post("/runs/{run_id}/retry-errors")
def api_retry_errors(run_id: int) -> dict:
    try:
        return MANAGER.start_retry_errors(run_id, db.CURRENT_SEDE.get())
    except RunBusyError as exc:
        raise HTTPException(409, str(exc))
    except KeyError:
        raise HTTPException(404, "Corrida no existe")
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@api_router.get("/runs/{run_id}/cases")
def api_list_run_cases(run_id: int, case_filter: str = "all") -> dict:
    return {
        "counts": db.run_case_counts(run_id),
        "cases": db.list_run_cases(run_id, case_filter=None if case_filter == "all" else case_filter),
    }


@api_router.get("/runs/{run_id}/stream")
def api_run_stream(run_id: int) -> StreamingResponse:
    """Server-Sent Events: transmite cada evento de progreso al navegador en vivo."""
    # Se captura como variable normal (closure) en vez de leerse dentro del generador: el
    # generador puede terminar iterandose en otro hilo/contexto, donde el ContextVar de la
    # sede no viaja solo.
    sede = db.CURRENT_SEDE.get()

    def event_source():
        db.set_sede(sede)
        q = MANAGER.subscribe(sede, run_id)
        try:
            # Snapshot inicial desde la BD por si la corrida ya termino antes de conectar.
            try:
                snapshot = db.get_run(run_id)
                yield f"data: {json.dumps({'type': 'snapshot', **snapshot}, ensure_ascii=False)}\n\n"
            except KeyError:
                pass
            while True:
                try:
                    event = q.get(timeout=20)
                except queue.Empty:
                    yield ": keep-alive\n\n"
                    continue
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                # El flag puede venir suelto (evento stream_end real) o fusionado dentro
                # del ultimo evento "real" reenviado a un suscriptor tardio (ver
                # RunManager._publish) — ambos casos cierran el stream igual.
                if event.get("type") == "stream_end" or event.get("stream_end"):
                    break
        finally:
            MANAGER.unsubscribe(sede, run_id, q)

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@api_router.get("/runs/{run_id}/download")
def api_download(run_id: int) -> FileResponse:
    try:
        run = db.get_run(run_id)
    except KeyError:
        raise HTTPException(404, "Corrida no existe")
    output = run.get("output_path") or ""
    if not output or not Path(output).exists():
        raise HTTPException(404, "El Excel de esta corrida aun no esta disponible")
    return FileResponse(output, filename=Path(output).name)


# ---------- programaciones ----------

@api_router.get("/bases/{base_id}/schedules")
def api_list_schedules(base_id: int) -> list[dict]:
    return db.list_schedules(base_id)


@api_router.post("/bases/{base_id}/schedules")
def api_create_schedule(
    base_id: int, mode: str = Form("daily"), hour: int = Form(...), minute: int = Form(0)
) -> dict:
    mode = mode.lower()
    if mode not in {"total", "daily"}:
        raise HTTPException(400, "mode debe ser total o daily")
    if not (0 <= hour <= 23) or not (0 <= minute <= 59):
        raise HTTPException(400, "Hora invalida")
    return db.create_schedule(base_id, mode, hour, minute)


@api_router.patch("/schedules/{schedule_id}")
def api_toggle_schedule(schedule_id: int, enabled: bool = Form(...)) -> dict:
    db.set_schedule_enabled(schedule_id, enabled)
    return db.get_schedule(schedule_id)


@api_router.delete("/schedules/{schedule_id}")
def api_delete_schedule(schedule_id: int) -> dict:
    db.delete_schedule(schedule_id)
    return {"ok": True}


# ---------- Mis Procesos (repositorio persistente) ----------

@api_router.get("/processes")
def api_list_processes(client_id: int | None = None, folder_id: int | None = None, search: str = "") -> list[dict]:
    return db.list_processes(client_id=client_id, folder_id=folder_id, search=search)


@api_router.delete("/processes")
def api_delete_processes(client_id: int | None = None) -> dict:
    """Borra en bloque: todos los procesos del repositorio, o solo los del cliente
    indicado si se pasa client_id."""
    deleted = db.delete_processes(client_id=client_id)
    return {"ok": True, "deleted": deleted}


@api_router.get("/processes/{process_id}")
def api_get_process(process_id: int) -> dict:
    try:
        proc = db.get_process(process_id)
    except KeyError:
        raise HTTPException(404, "Proceso no existe")
    proc["actuaciones"] = db.list_actuaciones(process_id)
    return proc


@api_router.post("/processes/{process_id}/seen")
def api_mark_process_seen(process_id: int) -> dict:
    db.clear_new_flags(process_id)
    return {"ok": True}


@api_router.patch("/processes/{process_id}/assign")
def api_assign_process(process_id: int, client_id: int | None = Form(None), folder_id: int | None = Form(None)) -> dict:
    db.assign_process(process_id, client_id, folder_id)
    return db.get_process(process_id)


@api_router.delete("/processes/{process_id}")
def api_delete_process(process_id: int) -> dict:
    db.delete_process(process_id)
    return {"ok": True}


def _process_with_actuaciones(process_id: int) -> dict:
    proc = db.get_process(process_id)
    proc["actuaciones"] = db.list_actuaciones(process_id)
    return proc


@api_router.post("/processes/{process_id}/consultar")
def api_consultar_process(process_id: int) -> dict:
    """Consulta Única: re-consulta solo este proceso; el stream reporta si quedó
    ACTUALIZADO o trajo actuaciones nuevas."""
    try:
        return MANAGER.start_process_query(process_id, db.CURRENT_SEDE.get())
    except RunBusyError as exc:
        raise HTTPException(409, str(exc))
    except KeyError:
        raise HTTPException(404, "Proceso no existe")


@api_router.get("/processes/{process_id}/download")
def api_download_process(process_id: int) -> FileResponse:
    from ..excel_writer import write_ecuador_processes_workbook, write_peru_bot_processes_workbook
    try:
        proc = _process_with_actuaciones(process_id)
    except KeyError:
        raise HTTPException(404, "Proceso no existe")
    db.OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch for ch in proc["radicado"] if ch.isalnum() or ch in "-_") or f"proceso_{process_id}"
    out = db.OUTPUTS_DIR / f"{safe}.xlsx"
    if db.CURRENT_SEDE.get() == "ecuador":
        write_ecuador_processes_workbook([proc], out)
    else:
        write_peru_bot_processes_workbook([proc], out)
    return FileResponse(out, filename=f"{safe}.xlsx")


@api_router.get("/actuaciones/{actuacion_id}/attachment")
def api_download_attachment(actuacion_id: int) -> FileResponse:
    try:
        act = db.get_actuacion(actuacion_id)
    except KeyError:
        raise HTTPException(404, "Actuacion no existe")
    path = Path(act.get("attachment_path") or "")
    if not path.exists():
        raise HTTPException(404, "La actuacion no tiene adjunto")
    return FileResponse(path, filename=act.get("attachment_filename") or path.name)


@api_router.post("/actuaciones/{actuacion_id}/attachment")
async def api_attach_actuacion_document(actuacion_id: int, file: UploadFile = File(...)) -> dict:
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Sube un archivo PDF")
    try:
        db.get_actuacion(actuacion_id)
    except KeyError:
        raise HTTPException(404, "Actuacion no existe")
    db.ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = "".join(ch for ch in Path(file.filename).name if ch.isalnum() or ch in "._- ") or "documento.pdf"
    stored = db.ATTACHMENTS_DIR / f"act_{actuacion_id}_{int(time.time())}_{safe_name}"
    with stored.open("wb") as fh:
        shutil.copyfileobj(file.file, fh)
    return {"ok": True, "actuacion": db.attach_actuacion_document(actuacion_id, str(stored), file.filename)}


@api_router.get("/processes-download")
def api_download_all_processes(client_id: int | None = None) -> FileResponse:
    from ..excel_writer import write_ecuador_processes_workbook, write_peru_bot_processes_workbook
    procs = db.list_processes(client_id=client_id)
    if not procs:
        raise HTTPException(404, "No hay procesos para exportar")
    # list_processes() trae client_name pero no actuaciones; get_process() es al revés.
    # Se fusionan preservando client_name (get_process no lo tiene, así que nunca lo pisa).
    full = [{**p, **_process_with_actuaciones(p["id"])} for p in procs]
    db.OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    out = db.OUTPUTS_DIR / ("mis_procesos.xlsx" if client_id is None else f"procesos_cliente_{client_id}.xlsx")
    if db.CURRENT_SEDE.get() == "ecuador":
        write_ecuador_processes_workbook(full, out)
    else:
        write_peru_bot_processes_workbook(full, out)
    return FileResponse(out, filename=out.name)


@api_router.get("/processes-download-batch")
def api_download_processes_batch(ids: str) -> FileResponse:
    """Excel de un lote puntual de procesos (ids separados por coma) en vez de TODOS.
    Lo usa el resultado de una inclusion (individual o masiva) para que 'Descargar
    Excel' ahi entregue solo lo que se acaba de incluir, no el repositorio completo."""
    id_list = [int(part) for part in ids.split(",") if part.strip().isdigit()]
    if not id_list:
        raise HTTPException(400, "No se especificaron procesos")
    client_names = {c["id"]: c["name"] for c in db.list_clients()}
    procs = []
    for pid in id_list:
        try:
            p = _process_with_actuaciones(pid)
        except KeyError:
            continue
        p["client_name"] = client_names.get(p.get("client_id"), "")
        procs.append(p)
    if not procs:
        raise HTTPException(404, "No hay procesos para exportar")
    from ..excel_writer import write_ecuador_processes_workbook, write_peru_bot_processes_workbook
    db.OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    out = db.OUTPUTS_DIR / f"inclusion_{int(time.time())}.xlsx"
    if db.CURRENT_SEDE.get() == "ecuador":
        write_ecuador_processes_workbook(procs, out)
    else:
        write_peru_bot_processes_workbook(procs, out)
    return FileResponse(out, filename=out.name)


# ---------- Inclusiones (agregar proceso por radicado + parte) ----------

def _parse_iso_date(value: str, field_label: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(400, f"{field_label} inválida: usa el formato AAAA-MM-DD")


def _build_documento(
    tipo_documento: str, numero_documento: str, codigo: str, fecha_emision: str, fecha_nacimiento: str,
) -> dict:
    """Valida y arma el bloque de identidad (documento) que se envia al bot: tipo/numero de
    documento, codigo (solo DNI) y fechas de emision/nacimiento son obligatorios en el
    formulario de Inclusiones individual. Replica las reglas del modelo del bot
    (tipoDocumento/numeroDocumento/codigo) mas las validaciones de fecha que pide el negocio."""
    tipo_documento = (tipo_documento or "").strip().upper()
    numero_documento = (numero_documento or "").strip()
    codigo = (codigo or "").strip()
    fecha_emision = (fecha_emision or "").strip()
    fecha_nacimiento = (fecha_nacimiento or "").strip()

    if not tipo_documento:
        raise HTTPException(400, "El tipo de documento es obligatorio")
    if tipo_documento not in ("DNI", "CE"):
        raise HTTPException(400, "Tipo de documento inválido: usa DNI o CE")
    if not numero_documento:
        raise HTTPException(400, "El número de documento es obligatorio")
    if not numero_documento.isdigit():
        raise HTTPException(400, "El número de documento debe ser numérico")
    if not fecha_emision:
        raise HTTPException(400, "La fecha de emisión es obligatoria")
    if not fecha_nacimiento:
        raise HTTPException(400, "La fecha de nacimiento es obligatoria")

    if tipo_documento == "DNI":
        if len(numero_documento) != 8:
            raise HTTPException(400, "El DNI debe tener 8 dígitos numéricos.")
        if not codigo:
            raise HTTPException(400, "El código es obligatorio")
        if not codigo.isdigit():
            raise HTTPException(400, "El código debe ser numérico")
    else:  # CE
        codigo = ""  # no aplica para CE
        if not (9 <= len(numero_documento) <= 12):
            raise HTTPException(400, "El Carné de Extranjería debe tener entre 9 y 12 dígitos.")

    emision_dt = _parse_iso_date(fecha_emision, "Fecha de emisión")
    nacimiento_dt = _parse_iso_date(fecha_nacimiento, "Fecha de nacimiento")
    hoy = date.today()
    if emision_dt > hoy:
        raise HTTPException(400, "La fecha de emisión no puede ser una fecha futura")
    if nacimiento_dt > hoy:
        raise HTTPException(400, "La fecha de nacimiento no puede ser una fecha futura")
    if nacimiento_dt > emision_dt:
        raise HTTPException(400, "La fecha de nacimiento no puede ser posterior a la fecha de emisión")

    documento: dict = {
        "tipoDocumento": tipo_documento,
        "numeroDocumento": int(numero_documento),
        "fechaEmision": fecha_emision,
        "fechaNacimiento": fecha_nacimiento,
    }
    if codigo:
        documento["codigo"] = int(codigo)
    return documento


@api_router.post("/inclusiones")
def api_inclusion(
    radicado: str = Form(...), demandante: str = Form(""), demandado: str = Form(""),
    client_id: int | None = Form(None), valor_parte: str = Form(""),
    tipo_documento: str = Form(""), numero_documento: str = Form(""), codigo: str = Form(""),
    fecha_emision: str = Form(""), fecha_nacimiento: str = Form(""),
) -> dict:
    if not radicado.strip():
        raise HTTPException(400, "El radicado es obligatorio")
    if not demandante.strip():
        raise HTTPException(400, "El demandante es obligatorio")
    if not demandado.strip():
        raise HTTPException(400, "El demandado es obligatorio")
    documento = _build_documento(tipo_documento, numero_documento, codigo, fecha_emision, fecha_nacimiento)
    try:
        return MANAGER.start_inclusion(
            radicado.strip(), demandante.strip(), demandado.strip(), client_id, db.CURRENT_SEDE.get(),
            valor_parte.strip(), documento,
        )
    except RunBusyError as exc:
        raise HTTPException(409, str(exc))


# ---------- Inclusiones Ecuador (bot externo) ----------
# Ecuador no usa MANAGER: el bot que consulta la Funcion Judicial vive en otro servicio
# y responde de forma sincrona (ver ecuador_client.py). Por eso estos endpoints no crean
# un "run" ni transmiten progreso por SSE, solo esperan la respuesta y persisten los
# procesos encontrados.

def _require_ecuador() -> None:
    if db.CURRENT_SEDE.get() != "ecuador":
        raise HTTPException(400, "Este endpoint es exclusivo de la sede Ecuador")


@api_router.post("/inclusiones/bot")
def api_inclusion_ecuador(radicado: str = Form(...), client_id: int | None = Form(None)) -> dict:
    _require_ecuador()
    if not radicado.strip():
        raise HTTPException(400, "El radicado es obligatorio")
    try:
        data = ecuador_client.incluir_individual(radicado.strip())
    except ecuador_client.EcuadorBotError as exc:
        raise HTTPException(502, str(exc))
    procesados = [ecuador_client.persist_radicado(r, client_id) for r in data.get("radicados", [])]
    return {"batchId": data.get("batchId"), "total": len(procesados), "process_ids": [p["id"] for p in procesados]}


@api_router.post("/inclusiones/bot/bulk")
async def api_inclusion_ecuador_bulk(file: UploadFile = File(...), client_id: int | None = Form(None)) -> dict:
    _require_ecuador()
    if not file.filename or not file.filename.lower().endswith((".xlsx", ".xls")):
        raise HTTPException(400, "Sube un archivo Excel (.xlsx/.xls)")
    content = await file.read()
    try:
        data = ecuador_client.incluir_bulk(content, file.filename)
    except ecuador_client.EcuadorBotError as exc:
        raise HTTPException(502, str(exc))
    procesados = [ecuador_client.persist_radicado(r, client_id) for r in data.get("radicados", [])]
    return {"batchId": data.get("batchId"), "total": len(procesados), "process_ids": [p["id"] for p in procesados]}


# ---------- Notificaciones ----------

@api_router.get("/notifications")
def api_list_notifications(notif_filter: str = "all") -> dict:
    return {
        "counts": db.notification_counts(),
        "items": db.list_notifications(notif_filter=None if notif_filter == "all" else notif_filter),
    }


@api_router.post("/notifications/read-all")
def api_read_all_notifications() -> dict:
    db.mark_all_notifications_read()
    return {"ok": True}


@api_router.patch("/notifications/{notif_id}/read")
def api_read_notification(notif_id: int, read: bool = Form(True)) -> dict:
    db.mark_notification_read(notif_id, read)
    return {"ok": True}


@api_router.post("/notifications/{notif_id}/resolve")
def api_resolve_notification(notif_id: int) -> dict:
    """Marca una alerta manual como 'Auto cargado en Plataforma' (resuelta)."""
    db.resolve_manual_notification(notif_id)
    return {"ok": True}


@api_router.post("/notifications/{notif_id}/attachment")
async def api_attach_manual_document(notif_id: int, file: UploadFile = File(...)) -> dict:
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Sube un archivo PDF")
    db.ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = "".join(ch for ch in Path(file.filename).name if ch.isalnum() or ch in "._- ") or "documento.pdf"
    stored = db.ATTACHMENTS_DIR / f"notif_{notif_id}_{int(time.time())}_{safe_name}"
    with stored.open("wb") as fh:
        shutil.copyfileobj(file.file, fh)
    try:
        act = db.attach_manual_notification_document(notif_id, str(stored), file.filename)
    except KeyError as exc:
        stored.unlink(missing_ok=True)
        raise HTTPException(404, str(exc))
    return {"ok": True, "actuacion": act}


@api_router.post("/notifications/delete")
def api_delete_notifications(ids: str = Form(...)) -> dict:
    id_list = [int(x) for x in ids.split(",") if x.strip().isdigit()]
    db.delete_notifications(id_list)
    return {"ok": True, "deleted": len(id_list)}


@api_router.delete("/notifications")
def api_delete_all_notifications() -> dict:
    db.delete_all_notifications()
    return {"ok": True}


# ---------- Dashboard ----------

@api_router.get("/dashboard")
def api_dashboard() -> dict:
    return db.dashboard_stats()


@api_router.get("/search")
def api_search(q: str = "") -> dict:
    return db.global_search(q)


@api_router.get("/clients/{client_id}/report.pdf")
def api_client_report(client_id: int) -> FileResponse:
    from .reporting import build_client_report

    try:
        snapshot = db.client_report_snapshot(client_id)
    except KeyError:
        raise HTTPException(404, "Cliente no existe")
    safe = "".join(ch for ch in snapshot["client"]["name"] if ch.isalnum() or ch in "-_ ") or f"cliente_{client_id}"
    out = db.OUTPUTS_DIR / f"informe_{safe.strip().replace(' ', '_')}.pdf"
    build_client_report(snapshot, out, sede=db.CURRENT_SEDE.get())
    return FileResponse(out, filename=out.name, media_type="application/pdf")


# ---------- Gestión (calendario) ----------

@api_router.get("/gestion/month")
def api_gestion_month(year: int, month: int) -> dict:
    return db.gestion_month(year, month)


@api_router.get("/gestion/day")
def api_gestion_day(date: str) -> dict:
    return db.gestion_day(date)


# ---------- recordatorios / tareas / anotaciones ----------

@api_router.get("/reminders")
def api_list_reminders(date: str | None = None, client_id: int | None = None) -> list[dict]:
    return db.list_reminders(due_date=date, client_id=client_id)


@api_router.post("/reminders")
def api_create_reminder(
    title: str = Form(...), due_date: str = Form(...), due_time: str = Form(""),
    notes: str = Form(""), kind: str = Form("tarea"), client_id: int | None = Form(None),
) -> dict:
    if not title.strip():
        raise HTTPException(400, "El título es obligatorio")
    if not due_date.strip():
        raise HTTPException(400, "La fecha es obligatoria")
    return db.create_reminder(title, notes, due_date, due_time, kind, client_id)


@api_router.patch("/reminders/{reminder_id}/done")
def api_reminder_done(reminder_id: int, done: bool = Form(...)) -> dict:
    db.set_reminder_done(reminder_id, done)
    return {"ok": True}


@api_router.delete("/reminders/{reminder_id}")
def api_delete_reminder(reminder_id: int) -> dict:
    db.delete_reminder(reminder_id)
    return {"ok": True}


# ---------- respaldo ----------

@api_router.get("/backup/export")
def api_export_backup() -> FileResponse:
    db.checkpoint()  # fusiona el WAL antes de leer el archivo .db en crudo
    db.OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    out = db.OUTPUTS_DIR / f"respaldo_consola_cej_{time.strftime('%Y%m%d_%H%M%S')}.zip"
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        if db.DB_PATH.exists():
            zf.write(db.DB_PATH, "scraper_app.db")
        for root_dir, prefix in ((db.UPLOADS_DIR, "bases"), (db.ATTACHMENTS_DIR, "adjuntos")):
            if not root_dir.exists():
                continue
            for path in root_dir.rglob("*"):
                if path.is_file():
                    zf.write(path, f"{prefix}/{path.relative_to(root_dir).as_posix()}")
    return FileResponse(out, filename=out.name, media_type="application/zip")


@api_router.post("/backup/import")
async def api_import_backup(file: UploadFile = File(...)) -> dict:
    if MANAGER.active_run_id is not None:
        raise HTTPException(409, "No importes un respaldo mientras hay una consulta en curso")
    if not file.filename or not file.filename.lower().endswith(".zip"):
        raise HTTPException(400, "Sube un respaldo .zip")
    temp_zip = db.OUTPUTS_DIR / f"import_{int(time.time())}.zip"
    db.OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    with temp_zip.open("wb") as fh:
        shutil.copyfileobj(file.file, fh)
    try:
        with zipfile.ZipFile(temp_zip) as zf:
            names = zf.namelist()
            if "scraper_app.db" not in names:
                raise HTTPException(400, "El respaldo no contiene scraper_app.db")
            for name in names:
                if name.startswith("/") or ".." in Path(name).parts:
                    raise HTTPException(400, "El respaldo contiene rutas no seguras")
            db.DATA_DIR.mkdir(parents=True, exist_ok=True)
            backup_current = db.DATA_DIR / f"scraper_app_antes_import_{time.strftime('%Y%m%d_%H%M%S')}.db"
            db.checkpoint()  # fusiona el WAL antes del respaldo pre-import
            db.close_connection()
            if db.DB_PATH.exists():
                shutil.copy2(db.DB_PATH, backup_current)
            with zf.open("scraper_app.db") as src, db.DB_PATH.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            for name in names:
                if name.startswith("bases/") and not name.endswith("/"):
                    target = db.UPLOADS_DIR / Path(name).relative_to("bases")
                elif name.startswith("adjuntos/") and not name.endswith("/"):
                    target = db.ATTACHMENTS_DIR / Path(name).relative_to("adjuntos")
                else:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(name) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
        db.init_db()
    finally:
        temp_zip.unlink(missing_ok=True)
    return {"ok": True}


@api_router.get("/status")
def api_status() -> dict:
    return {"active_run_id": MANAGER.active_run_id}


class NoCacheStaticFiles(StaticFiles):
    """Fuerza revalidacion (ETag) en cada carga: sin esto el navegador cachea
    heuristicamente styles.css/app.js y los cambios no se ven sin hard-refresh."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


if STATIC_DIR.exists():
    app.mount("/static", NoCacheStaticFiles(directory=str(STATIC_DIR)), name="static")

# Monta el mismo set de endpoints dos veces, uno por sede: /api/peru/* y /api/ecuador/*.
# El middleware de arriba fija db.CURRENT_SEDE segun el prefijo antes de que corra el
# endpoint, asi cada uno usa la base de datos y carpetas de esa sede sin tener que
# duplicar ni parametrizar cada ruta.
app.include_router(api_router, prefix="/api/peru")
app.include_router(api_router, prefix="/api/ecuador")
