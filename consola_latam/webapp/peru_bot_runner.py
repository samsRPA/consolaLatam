"""Motor de corridas masivas de Peru: consulta el bot externo (ver peru_client.py) en
vez de scrapear el portal con un navegador. Emite eventos de progreso
(case_started/case_done/generating/run_done) para que la consola SSE del frontend
funcione igual que antes.

Diferencia clave con el scraper de navegador: el bot resuelve el LOTE COMPLETO en una
sola llamada HTTP bloqueante, no caso por caso. No hay progreso incremental real desde
su lado, asi que los eventos case_started/case_done se emiten en secuencia rapida recien
cuando la respuesta ya llego. Por el mismo motivo, cancelar una corrida en vuelo no
interrumpe la peticion HTTP ya enviada (a diferencia del scraper de navegador, que
revisa cancel_event entre caso y caso): cancel_event solo evita ENVIAR la peticion si
todavia no salio."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

from ..excel_writer import write_peru_bot_processes_workbook
from ..models import InputCase
from ..utils import next_output_path
from . import db, peru_client

ProgressCallback = Callable[[dict], None]


def _persist_bot_cases(case: InputCase, entry: dict, client_id: int | None) -> list[dict]:
    """Persiste todo lo que el bot devolvio para UN radicado de entrada. Si vino vacio
    (el bot no pudo resolverlo, p.ej. RENIEC no encontro coincidencia), NO se toca 'Mis
    Procesos': no se crea un proceso nuevo vacio, y si el radicado ya existia (re-subida
    de un Excel sobre casos ya trackeados) tampoco se pisa -- upsert_process reemplaza
    detail_json entero, asi que un fallo puntual borraria el reporte bueno de una
    consulta anterior. Solo se notifica y se arma una fila suelta para el Excel, con el
    radicado/demandante/demandado/valorParte y el "error" que el propio bot devuelve en
    esa fila de "radicados" (mas confiable que lo enviado, que es justo lo que no pudo usar)."""
    bot_cases = entry.get("cases") or []
    if bot_cases:
        return [peru_client.persist_case(bc, client_id) for bc in bot_cases]
    radicado = entry.get("radicado") or case.radicado
    demandante = entry.get("demandante") or case.demandante
    demandado = entry.get("demandado") or case.demandado
    valor_parte = entry.get("valorParte") or ""
    error = entry.get("error") or "El bot no pudo obtener informacion de este radicado."
    existing = db.find_process(client_id, radicado)
    db.create_notification(
        "error", f"ERROR: {radicado}", error, radicado, existing["id"] if existing else None, client_id,
    )
    return [{
        "id": existing["id"] if existing else None,
        "radicado": radicado, "client_name": (existing or {}).get("client_name", ""),
        "organo": "", "demandante": demandante, "demandado": demandado,
        "detail": {"valorParte": valor_parte, "error": error, **peru_client.documento_fields(entry)},
    }]


def _invalid_row_result(item: dict) -> dict:
    """Filas que el bot rechazo antes de consultar nada (Excel mal formado: radicado/
    demandante/demandado vacios). No representan un proceso real, asi que no se guardan
    en 'Mis Procesos' -- solo se agregan al Excel de salida como rastro de que existieron
    y por que se rechazaron."""
    return {
        "radicado": (item.get("radicado") or "").strip(),
        "client_name": "",
        "organo": "",
        "demandante": (item.get("demandante") or "").strip(),
        "demandado": (item.get("demandado") or "").strip(),
        "detail": {
            "valorParte": (item.get("valorParte") or "").strip(), "error": item.get("reason", ""),
            **peru_client.documento_fields(item),
        },
    }


def run_bulk(
    cases: list[InputCase],
    client_id: int | None,
    output_dir: Path,
    file_bytes: bytes,
    filename: str,
    progress_callback: ProgressCallback | None = None,
    cancel_event: threading.Event | None = None,
) -> Path:
    def emit(event: dict) -> None:
        if progress_callback is not None:
            try:
                progress_callback(event)
            except Exception:
                pass  # el progreso nunca debe tumbar la corrida

    total = len(cases)
    emit({"type": "run_started", "total": total, "mode": "total"})

    if cancel_event is not None and cancel_event.is_set():
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = next_output_path(output_dir)
        write_peru_bot_processes_workbook([], output_path)
        emit({"type": "run_done", "total": total, "ok": 0, "output": str(output_path), "cancelled": True})
        return output_path

    # Se manda el Excel ORIGINAL que subio el usuario, sin reconstruirlo: el bot lee sus
    # propias 9 columnas por posicion (radicado, demandante, demandado, valorParte,
    # numeroDocumento, codigo, fechaEmision, fechaNacimiento, tipoDocumento) directo del
    # archivo, y esos campos de documento no existen en `InputCase`/`cases` (ver
    # base_reader.py) -- reconstruir el archivo aca los perdia por completo.
    response = peru_client.incluir_bulk(file_bytes, filename)

    radicados_out = response.get("radicados") or []
    invalid_out = response.get("invalid") or []
    invalid_rows = {item.get("row") for item in invalid_out}
    # `cases` viene de nuestra propia deteccion de columnas (read_base_auto), que puede no
    # alinear 1:1 con el numero de fila que usa el bot (lee el archivo por su cuenta, con
    # sus propias columnas fijas). Se usa solo como aproximacion para mostrar progreso y
    # como respaldo de datos -- lo que manda es lo que el propio bot devuelve por fila.
    valid_cases = [c for idx, c in enumerate(cases) if (idx + 2) not in invalid_rows]

    all_results: list[dict] = []
    ok_count = 0
    for index, (case, entry) in enumerate(zip(valid_cases, radicados_out), start=1):
        emit({
            "type": "case_started", "index": index, "total": total,
            "radicado": case.radicado, "parte": case.demandante or case.demandado,
        })
        procs = _persist_bot_cases(case, entry, client_id)
        all_results.extend(procs)
        status = "OK" if entry.get("cases") else "ERROR"
        if status == "OK":
            ok_count += 1
        proc = procs[0]
        emit({
            "type": "case_done", "index": index, "total": total, "done": index,
            "radicado": proc.get("radicado") or case.radicado,
            "parte": proc.get("demandante") or proc.get("demandado") or case.demandante,
            "status": status, "movimientos": "NO", "tipo_movimiento": "",
            "error": "" if status == "OK" else (proc.get("detail") or {}).get("error", ""),
        })

    # Filas invalidas (radicado/demandante/demandado vacios en el Excel de entrada): el
    # bot nunca las consulto, asi que no generan proceso ni progreso en vivo -- solo se
    # agregan directo al Excel de salida para que quede constancia de por que se
    # rechazaron, con el resto de columnas (despacho, materia, estado, etc.) vacias.
    all_results.extend(_invalid_row_result(item) for item in invalid_out)

    emit({"type": "generating", "total": total})
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = next_output_path(output_dir)
    write_peru_bot_processes_workbook(all_results, output_path)
    emit({"type": "run_done", "total": total, "ok": ok_count, "output": str(output_path), "cancelled": False})
    return output_path


def consult_single(
    radicado: str, demandante: str, demandado: str, valor_parte: str, client_id: int | None,
    documento: dict | None = None,
) -> dict:
    """Usado tanto por Inclusiones (individual) como por 'Consulta Unica' (re-consultar
    un proceso ya guardado): sin actuaciones no hay diferencia real entre ambos casos,
    en los dos se llama al bot y se refresca/crea el proceso con lo que devuelva."""
    response = peru_client.incluir_individual(radicado, demandante, demandado, valor_parte, documento)
    bot_cases = peru_client.extract_cases(response)
    if bot_cases:
        procs = [peru_client.persist_case(bc, client_id) for bc in bot_cases]
        return {"status": "OK", "process": procs[0], "processes": procs, "error": ""}
    # "message" es el resumen legible que arma el bot (ver InclusionRowDto /
    # _flagIfScraperFailed); en un lote de 1 (inclusion individual) coincide con el
    # "error" de la unica fila en "radicados", que se usa aqui como respaldo si el bot
    # no llega a mandar el resumen (p.ej. RENIEC no encontro coincidencia).
    entries = response.get("radicados") or []
    row_error = entries[0].get("error") if entries else None
    error = response.get("message") or row_error or "El bot no pudo obtener informacion de este radicado."
    # Si falla, no se toca 'Mis Procesos': una Inclusion nueva que falla no debe aparecer
    # ahi (no hay expediente real), y si es una Consulta Unica sobre un proceso YA
    # existente, se deja tal cual (upsert_process pisaria detail_json entero, borrando el
    # reporte bueno de la ultima consulta exitosa solo porque esta puntual fallo).
    existing = db.find_process(client_id, radicado)
    proc = existing or {"id": None, "radicado": radicado, "demandante": demandante, "demandado": demandado}
    db.create_notification("error", f"ERROR: {radicado}", error, radicado, proc.get("id"), client_id)
    return {"status": "ERROR", "process": proc, "processes": [], "error": error}
