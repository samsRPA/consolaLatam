"""Cliente del servicio externo CEJPeruService ("el bot" de Peru): igual que Ecuador,
esta sede consulta un servicio aparte en vez de scrapear el portal con un navegador
propio. El bot consulta el portal CEJ por su cuenta (via RabbitMQ) y responde de forma
sincrona con los expedientes encontrados.

Endpoints del bot (documentados por quien lo opera, no forman parte de este repo):
  POST {BASE_URL}/api/v3/radicadosCEJ/{caseNumber}/incluir   (JSON demandante/demandado/valorParte?)
  POST {BASE_URL}/api/v3/radicadosCEJ/inclusiones            (multipart file, columnas fijas
       radicado|demandante|demandado|valorParte desde la fila 2)

Ambos responden con:
  {"batchId": "...", "total": N,
   "radicados": [ {"cases": [ {radicado, courtOfficeCode, caseReport, nroRegistro,
                                actorsRama, valorParte}, ... ]}, ... ],
   "invalid": [ {"row": N, "reason": "..."} ]}   (invalid solo en el endpoint de lote)

El endpoint individual bloquea hasta 120s del lado del bot y responde 504 si no llega a
tiempo. El de lote NO tiene ese timeout documentado (puede tardar arbitrariamente, e
incluso quedarse colgado si el collector nunca junta el conteo esperado) — por eso aca
se usa un timeout de cliente generoso pero finito, para que esta app nunca quede
esperando para siempre aunque el bot si lo haga."""

from __future__ import annotations

import os
from typing import Any

import httpx

from ..utils import clean_text
from . import db

BASE_URL = os.environ.get("PERU_BOT_BASE_URL", "http://127.0.0.1:5090").rstrip("/")
INDIVIDUAL_TIMEOUT = 150.0  # el bot documenta que bloquea hasta 120s del lado suyo
BULK_TIMEOUT = 3600.0  # sin timeout documentado del lado del bot; tope de seguridad local


class PeruBotError(RuntimeError):
    """El bot no respondio (timeout/conexion) o respondio con un error."""


def _format_bot_error_detail(detail: Any) -> str:
    """Un 422 de FastAPI/Pydantic (como el que devuelve el bot cuando el payload no
    cumple su esquema) trae 'detail' como una LISTA de {loc, msg, type}, no como texto.
    Sin esto, str(lista) queda lleno de llaves/corchetes y app.js::friendlyError() lo
    confunde con un error tecnico y lo reemplaza por un mensaje generico, dejando al
    usuario sin saber que campo rechazo el bot."""
    if isinstance(detail, list):
        partes = []
        for item in detail:
            if isinstance(item, dict) and "msg" in item:
                campo = ".".join(str(p) for p in (item.get("loc") or []) if p != "body")
                partes.append(f"{campo}: {item['msg']}" if campo else str(item["msg"]))
            else:
                partes.append(str(item))
        return "; ".join(partes) if partes else "sin detalle"
    return str(detail)


def _post_with_retry(url: str, *, timeout: float, **kwargs: Any) -> dict:
    """POST con UN reintento ante timeout o error de conexion (no ante un 4xx/5xx del
    bot, que es una respuesta real y no algo transitorio que valga la pena repetir)."""
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            with httpx.Client(timeout=timeout) as client:
                resp = client.post(url, **kwargs)
        except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError) as exc:
            last_exc = exc
            continue
        if resp.status_code >= 400:
            detail = resp.text[:300] or "sin detalle"
            try:
                body = resp.json()
            except ValueError:
                body = None
            if isinstance(body, dict):
                # El bot tiene su propio campo "message" (resumen legible, ver
                # InclusionRowDto/_flagIfScraperFailed) que es mas util que el "detail"
                # generico de FastAPI (una lista de {loc, msg, type} cuando es 422 de
                # Pydantic) -- se prioriza ese antes de caer al dump crudo del body.
                if body.get("message"):
                    detail = body["message"]
                elif "detail" in body:
                    detail = _format_bot_error_detail(body["detail"])
            raise PeruBotError(f"El bot respondio {resp.status_code}: {detail}")
        try:
            return resp.json()
        except ValueError as exc:
            raise PeruBotError("El bot respondio pero el cuerpo no es JSON valido") from exc
    raise PeruBotError(f"No se pudo contactar al bot tras 2 intentos ({BASE_URL}): {last_exc}")


def incluir_individual(
    radicado: str, demandante: str, demandado: str, valor_parte: str = "",
    documento: dict[str, Any] | None = None, timeout: float = INDIVIDUAL_TIMEOUT,
) -> dict:
    url = f"{BASE_URL}/api/v3/radicadosCEJ/{radicado}/incluir"
    payload: dict[str, Any] = {"demandante": demandante, "demandado": demandado}
    if valor_parte:
        payload["valorParte"] = valor_parte
    if documento:
        payload.update(documento)
    return _post_with_retry(url, timeout=timeout, json=payload)


def incluir_bulk(file_bytes: bytes, filename: str, timeout: float = BULK_TIMEOUT) -> dict:
    url = f"{BASE_URL}/api/v3/radicadosCEJ/inclusiones"
    files = {"file": (filename, file_bytes)}
    return _post_with_retry(url, timeout=timeout, files=files)


def extract_cases(response: dict) -> list[dict]:
    """Aplana la respuesta del bot: 'radicados' es una lista de entradas, cada una con su
    propia lista 'cases' (normalmente 0 o 1, pero el bot admite mas de un expediente por
    radicado)."""
    cases: list[dict] = []
    for entry in response.get("radicados") or []:
        cases.extend(entry.get("cases") or [])
    return cases


DOCUMENTO_KEYS = ("tipoDocumento", "numeroDocumento", "codigo", "fechaEmision", "fechaNacimiento")


def documento_fields(source: dict) -> dict:
    """Extrae tipoDocumento/numeroDocumento/codigo/fechaEmision/fechaNacimiento cuando el
    bot los hace eco en su respuesta -- tanto en un caso exitoso (junto a courtOfficeCode/
    caseReport) como en una entrada fallida de 'radicados' (junto a error/radicado/
    demandante/demandado/valorParte). Se guardan en detail para que el Excel de salida y
    la vista de proceso los muestren."""
    return {k: source[k] for k in DOCUMENTO_KEYS if source.get(k) is not None}


def party_names(actors_rama: list[dict], tipo: str) -> str:
    names = []
    for actor in actors_rama or []:
        if clean_text(actor.get("tipo_sujeto")).upper() != tipo:
            continue
        nombre = clean_text(actor.get("nombre_actor"))
        if nombre:
            names.append(nombre)
    return "; ".join(names)


def persist_case(case: dict, client_id: int | None, source: str = "peru_bot") -> dict:
    """Guarda/actualiza UN caso devuelto por el bot como proceso en 'Mis Procesos'. Sin
    actuaciones ni historial (el bot no los entrega): el detalle guardado es solo
    despacho + caseReport + partes, para que el Excel y la vista de proceso lo usen tal
    cual (ver excel_writer.write_peru_bot_processes_workbook)."""
    radicado = str(case.get("radicado", "")).strip()
    despacho = clean_text(case.get("courtOfficeCode", ""))
    reporte = case.get("caseReport") or {}
    actores = case.get("actorsRama") or []
    demandante = party_names(actores, "DEMANDANTE")
    demandado = party_names(actores, "DEMANDADO")
    detail = {
        "reporte": reporte,
        "despacho": despacho,
        "valorParte": clean_text(case.get("valorParte", "")),
        "actorsRama": actores,
        **documento_fields(case),
    }
    return db.upsert_process(
        client_id=client_id,
        radicado=radicado,
        matched_radicado=radicado,
        demandante=demandante,
        demandado=demandado,
        organo=despacho,
        materia=clean_text(reporte.get("MATERIA", "")),
        estado=clean_text(reporte.get("ESTADO", "")),
        nro_registro=str(case.get("nroRegistro", "") or ""),
        detail=detail,
        source=source,
    )
