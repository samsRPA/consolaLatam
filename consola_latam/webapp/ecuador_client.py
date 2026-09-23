"""Cliente del bot externo de Ecuador (Funcion Judicial): a diferencia de Peru, esta
sede NO usa el scraper de navegador de este proyecto. Un servicio aparte ("el bot")
expone dos endpoints HTTP que consultan el portal por su cuenta (via RabbitMQ) y
responden de forma sincrona con los radicados encontrados.

Endpoints del bot (documentados por quien lo opera, no forman parte de este repo):
  POST {BASE_URL}/api/v2/radicadosCJ/{caseNumber}/incluir  (JSON: {"clientes": [...]})     -> un radicado
  POST {BASE_URL}/api/v2/radicadosCJ/inclusiones           (multipart: file + clientes)    -> lote via Excel

`clientes` es un arreglo de {"clienteId": int, "nombreCliente": str} -- SIEMPRE incluye
al cliente padre seleccionado en la consola (obligatorio) y, opcionalmente, los clientes
hijos de facturacion (jerarquia Oracle) que el usuario haya marcado con checkbox. Los
valores salen de external_client_id/external_username del cliente y de client_children
(ver app.py:_build_clientes_payload y db.py); nombreCliente es siempre el nombre oficial
que trae Oracle, no uno escrito a mano.

Ambos responden 200 con:
  {"batchId": "...", "total": N, "radicados": [ {radicado, materia, fechaIngreso,
   tipoAccion, delitoAsunto, judicatura, ciudad}, ... ]}

Como la llamada ya es sincrona del lado del bot (espera la respuesta real antes de
responder), aqui simplemente se hace la peticion con un timeout generoso; si falla por
timeout o caida de conexion se reintenta UNA vez antes de rendirse."""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

from . import db

BASE_URL = os.environ.get("ECUADOR_BOT_BASE_URL", "http://localhost:5000").rstrip("/")
DEFAULT_TIMEOUT = 300.0  # 5 minutos: lotes grandes pueden tardar en resolver via RabbitMQ


class EcuadorBotError(RuntimeError):
    """El bot no respondio (timeout/conexion) o respondio con un error."""


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
        if resp.status_code != 200:
            raise EcuadorBotError(
                f"El bot respondio {resp.status_code}: {resp.text[:300] or 'sin detalle'}"
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise EcuadorBotError("El bot respondio 200 pero el cuerpo no es JSON valido") from exc
    raise EcuadorBotError(
        f"No se pudo contactar al bot tras 2 intentos ({BASE_URL}): {last_exc}"
    )


def incluir_individual(radicado: str, clientes: list[dict], timeout: float = DEFAULT_TIMEOUT) -> dict:
    url = f"{BASE_URL}/api/v2/radicadosCJ/{radicado}/incluir"
    return _post_with_retry(url, timeout=timeout, json={"clientes": clientes})


def incluir_bulk(file_bytes: bytes, filename: str, clientes: list[dict], timeout: float = DEFAULT_TIMEOUT) -> dict:
    url = f"{BASE_URL}/api/v2/radicadosCJ/inclusiones"
    files = {"file": (filename, file_bytes)}
    data = {"clientes": json.dumps(clientes, ensure_ascii=False)}
    return _post_with_retry(url, timeout=timeout, files=files, data=data)


def persist_radicado(radicado: dict, client_id: int | None) -> dict:
    """Guarda/actualiza UN radicado devuelto por el bot como proceso en 'Mis Procesos'
    de la sede actual (debe llamarse con db.CURRENT_SEDE ya en 'ecuador'). El bot no
    entrega demandante/demandado ni historial de actuaciones a nivel de radicado (a
    diferencia de Peru), asi que esos campos quedan vacios; el resto de metadatos va al
    detalle del proceso para que aparezca en el Excel de descarga (ya existente) sin
    tocar excel_writer.py.

    Un mismo radicado puede tener varios "expedientes" (uno por idJudicatura), cada uno
    con su propia judicatura/ciudad y sus propios actores/demandados -- se guardan tal
    cual en detail.expedientes para que write_ecuador_processes_workbook y el detalle del
    proceso en el front los usen por expediente en vez de los campos de nivel radicado
    (que el bot puede mandar en null cuando hay mas de un expediente)."""
    numero = str(radicado.get("radicado", "")).strip()
    # El bot ha usado ambos nombres para estos dos campos segun la version/respuesta
    # (judicatura/ciudad y despachoNombre/localidadNombre); se aceptan los dos.
    judicatura = radicado.get("judicatura") or radicado.get("despachoNombre") or ""
    ciudad = radicado.get("ciudad") or radicado.get("localidadNombre") or ""
    reporte = {
        "Materia": radicado.get("materia", "") or "",
        "Fecha de Ingreso": radicado.get("fechaIngreso", "") or "",
        "Tipo de Acción": radicado.get("tipoAccion", "") or "",
        "Delito/Asunto": radicado.get("delitoAsunto", "") or "",
        "Judicatura": judicatura,
        "Ciudad": ciudad,
    }
    return db.upsert_process(
        client_id=client_id,
        radicado=numero,
        matched_radicado=numero,
        demandante="",
        demandado="",
        organo=judicatura,
        materia=radicado.get("materia", "") or "",
        estado=radicado.get("tipoAccion", "") or "",
        nro_registro="",
        detail={
            "reporte": reporte,
            "partes": [],
            "expedientes": radicado.get("expedientes") or [],
            # error: el bot puede devolver el radicado con sus datos Y un error (ej. no
            # pudo incluirlo en el sistema aunque si lo encontro) -- se guarda tal cual
            # para revisarlo despues en "Mis Procesos" o en el Excel de descarga.
            "error": radicado.get("error", "") or "",
            "radicadoConGuiones": radicado.get("radicadoConGuiones", "") or "",
            # procesoId: id que el bot asigna al incluirlo en su propio sistema. Si hubo
            # error el bot no lo entrega, asi que queda en None (no "" -- distingue "no
            # se genero" de "se genero vacio").
            "procesoId": radicado.get("procesoId"),
        },
        source="ecuador_bot",
    )
