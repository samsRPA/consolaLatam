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
responder), aqui simplemente se hace la peticion con un timeout generoso. Solo se
reintenta UNA vez si no se pudo conectar; un timeout o corte de lectura NO se reintenta
porque el bot ya recibio la peticion: reenviarla crearia otro lote con los mismos
radicados, y el bot cancela el lote anterior al detectar que se cerro la conexion."""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

from . import db

BASE_URL = os.environ.get("ECUADOR_BOT_BASE_URL", "http://localhost:5000").rstrip("/")
INDIVIDUAL_TIMEOUT = 330.0  # el bot espera hasta 300s la respuesta de un radicado; margen extra
BULK_TIMEOUT = 3 * 60 * 60.0  # 3 horas: el bot no tiene timeout para el lote (Excel); tope de seguridad local
CONNECT_TIMEOUT = 10.0


class EcuadorBotError(RuntimeError):
    """El bot no respondio (timeout/conexion) o respondio con un error."""


def _post_with_retry(url: str, *, timeout: float, **kwargs: Any) -> dict:
    """POST con UN reintento solo si no se pudo conectar (la peticion no llego al bot). Un
    timeout de lectura o un corte de conexion NO se reintenta: el bot ya recibio la
    peticion y reenviarla duplicaria el lote (ver docstring del modulo). Un 4xx/5xx del bot
    tampoco, es una respuesta real y no algo transitorio."""
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            with httpx.Client(timeout=httpx.Timeout(timeout, connect=CONNECT_TIMEOUT)) as client:
                resp = client.post(url, **kwargs)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            last_exc = exc
            continue
        except (httpx.TimeoutException, httpx.ReadError) as exc:
            raise EcuadorBotError(
                f"El bot no respondio en {timeout:.0f}s o corto la conexion: {exc}"
            ) from exc
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


def incluir_individual(radicado: str, clientes: list[dict], timeout: float = INDIVIDUAL_TIMEOUT) -> dict:
    url = f"{BASE_URL}/api/v2/radicadosCJ/{radicado}/incluir"
    return _post_with_retry(url, timeout=timeout, json={"clientes": clientes})


def incluir_bulk(file_bytes: bytes, filename: str, clientes: list[dict], timeout: float = BULK_TIMEOUT) -> dict:
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
