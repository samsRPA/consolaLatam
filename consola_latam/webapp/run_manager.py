"""Orquesta las corridas del scraper desde la web app.

Reglas duras que impone este modulo:
  * UNA sola corrida a la vez EN TODO EL PROCESO (global lock): las corridas que llegan
    mientras hay una activa se rechazan con 409.
  * El progreso de cada corrida se publica a una cola por-suscriptor, de modo que el
    endpoint SSE pueda transmitirlo en vivo al navegador (radicado, parte, x de y).

Sobre la sede (Peru/Ecuador) y los hilos: cada corrida real se ejecuta en un hilo propio.
Los ContextVar de Python (que es como db.py sabe que sede usar) NO se propagan solos a un
hilo nuevo -- cada uno arranca con su propio contexto en blanco. Por eso `sede` se pasa
aqui como parametro explicito de principio a fin, y cada funcion que corre en un hilo
nuevo empieza fijandola con `db.set_sede(sede)` antes de tocar la base de datos.

Las claves de los diccionarios en memoria (_subscribers, _last_event, _cancel_events) son
(sede, run_id) en vez de solo run_id: cada sede tiene su propia secuencia de ids de corrida
(su propia tabla `runs`), asi que un run_id=3 de Peru y un run_id=3 de Ecuador son corridas
distintas y no deben pisarse."""

from __future__ import annotations

import queue
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from openpyxl import Workbook

from ..base_reader import read_base_auto
from ..detect import ColumnMapping
from . import db, peru_bot_runner

RunKey = tuple[str, int]


class RunBusyError(RuntimeError):
    """Ya hay una corrida activa; el scraper no admite dos a la vez."""


class RunManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active_run_id: int | None = None
        self._active_sede: str | None = None
        self._subscribers: dict[RunKey, list[queue.Queue]] = {}
        self._sub_lock = threading.Lock()
        self._last_event: dict[RunKey, dict] = {}
        self._cancel_events: dict[RunKey, threading.Event] = {}
        self._cancel_lock = threading.Lock()

    @property
    def active_run_id(self) -> int | None:
        return self._active_run_id

    def start_run(self, base_id: int, mode: str, sede: str) -> dict[str, Any]:
        db.set_sede(sede)
        base = db.get_base(base_id)
        if not self._lock.acquire(blocking=False):
            raise RunBusyError(
                f"Ya hay una consulta en curso (run {self._active_run_id}). Espera a que termine."
            )
        try:
            mapping = ColumnMapping.from_json(base["mapping"])
            total = int(base.get("row_count") or 0)
            run = db.create_run(base_id, mode, total)
            run_id = run["id"]
            key: RunKey = (sede, run_id)
            self._active_run_id = run_id
            self._active_sede = sede
            with self._sub_lock:
                self._subscribers[key] = []
                self._last_event[key] = {"type": "run_started", "total": total, "mode": mode}
            with self._cancel_lock:
                self._cancel_events[key] = threading.Event()
        except Exception:
            self._lock.release()
            raise

        thread = threading.Thread(
            target=self._execute,
            args=(sede, run_id, base, mode, mapping),
            daemon=True,
        )
        thread.start()
        return run

    def start_retry_errors(self, source_run_id: int, sede: str) -> dict[str, Any]:
        db.set_sede(sede)
        source_run = db.get_run(source_run_id)
        base = db.get_base(source_run["base_id"])
        errors = db.list_run_cases(source_run_id, "error")
        if not errors:
            raise ValueError("La corrida no tiene errores para reintentar.")
        if not self._lock.acquire(blocking=False):
            raise RunBusyError(
                f"Ya hay una consulta en curso (run {self._active_run_id}). Espera a que termine."
            )
        try:
            retry_run = db.create_run(base["id"], "retry", len(errors))
            retry_run_id = retry_run["id"]
            temp_path = self._build_retry_workbook(source_run_id, retry_run_id, base, errors)
            retry_base = {**base, "stored_path": str(temp_path), "row_count": len(errors)}
            mapping = ColumnMapping(
                sheet="Errores",
                header_row=1,
                radicado_col=0,
                demandante_col=1,
                demandado_col=2,
                id_col=None,
                confidence=1.0,
            )
            key: RunKey = (sede, retry_run_id)
            self._active_run_id = retry_run_id
            self._active_sede = sede
            with self._sub_lock:
                self._subscribers[key] = []
                self._last_event[key] = {"type": "run_started", "total": len(errors), "mode": "retry"}
            with self._cancel_lock:
                self._cancel_events[key] = threading.Event()
        except Exception:
            self._lock.release()
            raise

        thread = threading.Thread(
            target=self._execute,
            args=(sede, retry_run_id, retry_base, "daily", mapping),
            daemon=True,
        )
        thread.start()
        return retry_run

    def _build_retry_workbook(self, source_run_id: int, retry_run_id: int, base: dict, errors: list[dict]) -> Path:
        retry_dir = db.OUTPUTS_DIR / "reintentos"
        retry_dir.mkdir(parents=True, exist_ok=True)
        path = retry_dir / f"reintento_{source_run_id}_{retry_run_id}.xlsx"
        wb = Workbook()
        ws = wb.active
        ws.title = "Errores"
        ws.append(["RADICADO", "DEMANDANTE", "DEMANDADO"])
        for item in errors:
            proc = db.find_process(base.get("client_id"), item["radicado"])
            if proc:
                demandante = proc.get("demandante", "")
                demandado = proc.get("demandado", "")
            else:
                demandante = item.get("parte", "")
                demandado = ""
            ws.append([item["radicado"], demandante, demandado])
        wb.save(path)
        return path

    def start_inclusion(
        self, radicado: str, demandante: str, demandado: str, client_id: int | None, sede: str,
        valor_parte: str = "", documento: dict | None = None,
    ) -> dict[str, Any]:
        """Modulo Inclusiones: consulta UN expediente por radicado + parte, lo persiste y
        crea su card. Comparte el candado de una-consulta-a-la-vez con las corridas masivas."""
        db.set_sede(sede)
        if not self._lock.acquire(blocking=False):
            raise RunBusyError(
                f"Ya hay una consulta en curso (run {self._active_run_id}). Espera a que termine."
            )
        inclusion_id = -1  # id virtual para el stream (no es una corrida de base)
        key: RunKey = (sede, inclusion_id)
        try:
            with self._sub_lock:
                self._subscribers[key] = []
                self._last_event[key] = {"type": "run_started", "total": 1, "mode": "inclusion"}
        except Exception:
            self._lock.release()
            raise
        thread = threading.Thread(
            target=self._execute_single,
            args=(sede, radicado, demandante, demandado, client_id, "inclusion", valor_parte, documento),
            daemon=True,
        )
        thread.start()
        return {"id": inclusion_id}

    def start_process_query(self, process_id: int, sede: str) -> dict[str, Any]:
        """Consulta UNICA: re-consulta un solo proceso ya registrado (refresca despacho,
        caseReport y partes con lo ultimo que devuelva el bot). Comparte el candado global."""
        db.set_sede(sede)
        proc = db.get_process(process_id)
        if not self._lock.acquire(blocking=False):
            raise RunBusyError(
                f"Ya hay una consulta en curso (run {self._active_run_id}). Espera a que termine."
            )
        key: RunKey = (sede, -1)
        try:
            with self._sub_lock:
                self._subscribers[key] = []
                self._last_event[key] = {"type": "run_started", "total": 1, "mode": "seguimiento"}
        except Exception:
            self._lock.release()
            raise
        valor_parte = (proc.get("detail") or {}).get("valorParte", "")
        thread = threading.Thread(
            target=self._execute_single,
            args=(sede, proc["radicado"], proc["demandante"], proc["demandado"], proc["client_id"],
                  "seguimiento", valor_parte),
            daemon=True,
        )
        thread.start()
        return {"id": -1}

    def _execute_single(
        self, sede: str, radicado, demandante, demandado, client_id, scope, valor_parte: str = "",
        documento: dict | None = None,
    ) -> None:
        db.set_sede(sede)  # hilo nuevo: el ContextVar de la sede no se hereda solo
        rid = -1
        self._publish(sede, rid, {"type": "case_started", "index": 1, "total": 1,
                            "radicado": radicado, "parte": demandante or demandado})
        try:
            result = peru_bot_runner.consult_single(radicado, demandante, demandado, valor_parte, client_id, documento)
            proc = result["process"]
            process_ids = ",".join(str(p["id"]) for p in result.get("processes") or [])
            db.add_query_history(client_id, scope=scope, mode="total", total=1, con_mov=0, sin_mov=0,
                                 errores=1 if result["status"] == "ERROR" else 0)
            self._publish(sede, rid, {"type": "case_done", "index": 1, "total": 1, "done": 1,
                               "radicado": radicado, "parte": proc.get("demandante") or proc.get("demandado"),
                               "status": result["status"], "movimientos": "NO",
                               "tipo_movimiento": "", "error": result["error"]})
            status = "done" if result["status"] == "OK" else "error"
            self._publish(sede, rid, {"type": "run_finished", "status": status, "error": result["error"],
                               "process_id": proc.get("id"), "process_ids": process_ids, "movimiento": False,
                               "new_count": 0, "scope": scope})
        except Exception as exc:  # noqa: BLE001
            self._publish(sede, rid, {"type": "run_finished", "status": "error", "error": str(exc)})
        finally:
            self._active_run_id = None
            self._active_sede = None
            self._lock.release()
            self._publish(sede, rid, {"type": "stream_end"})

    def cancel_run(self, run_id: int, sede: str) -> None:
        """Pide detener una corrida activa. Si el lote ya se envio al bot externo (una
        sola llamada bloqueante), esto no la interrumpe -- solo evita que arranque si
        todavia no salio. El caso/lote en vuelo termina solo."""
        key: RunKey = (sede, run_id)
        with self._cancel_lock:
            event = self._cancel_events.get(key)
        if event is None:
            raise KeyError(f"No hay una corrida activa con id {run_id}")
        event.set()
        self._publish(sede, run_id, {"type": "cancel_requested"})

    def _execute(self, sede: str, run_id: int, base: dict, mode: str, mapping: ColumnMapping) -> None:
        db.set_sede(sede)  # hilo nuevo: el ContextVar de la sede no se hereda solo
        key: RunKey = (sede, run_id)
        base_path = Path(base["stored_path"])
        client_id = base["client_id"]
        client_dir = db.OUTPUTS_DIR / f"cliente_{client_id}" / f"base_{base['id']}"
        client_dir.mkdir(parents=True, exist_ok=True)
        cancel_event = self._cancel_events[key]

        def on_progress(event: dict) -> None:
            db.set_sede(sede)
            self._publish(sede, run_id, event)
            if event.get("type") == "case_done":
                db.update_run(run_id, done=int(event.get("done", 0)))
                db.add_run_case(
                    run_id,
                    radicado=str(event.get("radicado", "")),
                    parte=str(event.get("parte", "")),
                    status=str(event.get("status", "")),
                    movimientos_estado=str(event.get("movimientos", "")),
                    tipo_movimiento=str(event.get("tipo_movimiento", "")),
                    error=str(event.get("error", "")),
                )

        try:
            # Se lee la base con el detector de columnas de siempre, pero la consulta la
            # resuelve el bot externo en un solo lote (ver peru_client.py). Sin
            # actuaciones, "daily" y "total" ya no tienen una distincion real -- ambos
            # hacen una pasada completa (el bot siempre devuelve el estado actual, no un
            # diff contra lo guardado).
            cases, _ = read_base_auto(base_path, mapping=mapping)
            output = peru_bot_runner.run_bulk(
                cases=cases,
                client_id=client_id,
                output_dir=client_dir,
                file_bytes=base_path.read_bytes(),
                filename=base.get("original_filename") or base_path.name,
                progress_callback=on_progress,
                cancel_event=cancel_event,
            )
            ok = self._last_event.get(key, {}).get("ok", 0)
            status = "cancelled" if cancel_event.is_set() else "done"
            db.update_run(
                run_id,
                status=status,
                output_path=str(output),
                ok=int(ok),
                finished_at=datetime.now().isoformat(timespec="seconds"),
            )
            counts = db.run_case_counts(run_id)
            db.add_query_history(
                client_id, scope="base", mode=mode, total=counts["total"],
                con_mov=counts["con_movimientos"], sin_mov=counts["sin_movimientos"], errores=counts["error"],
            )
            self._publish(sede, run_id, {"type": "run_finished", "status": status, "output": str(output)})
        except Exception as exc:  # noqa: BLE001 - queremos reportar cualquier fallo al usuario
            db.update_run(
                run_id,
                status="error",
                error=str(exc),
                finished_at=datetime.now().isoformat(timespec="seconds"),
            )
            self._publish(sede, run_id, {"type": "run_finished", "status": "error", "error": str(exc)})
        finally:
            with self._cancel_lock:
                self._cancel_events.pop(key, None)
            self._active_run_id = None
            self._active_sede = None
            self._lock.release()
            self._publish(sede, run_id, {"type": "stream_end"})

    def _publish(self, sede: str, run_id: int, event: dict) -> None:
        key: RunKey = (sede, run_id)
        if event.get("type") == "stream_end":
            # NO pisa el ultimo evento "real" (run_finished, con el resultado/error) con
            # el marcador vacio de cierre: si la corrida termina muy rapido, un
            # suscriptor que se conecta tarde -tipico, streamRun() abre el EventSource
            # despues de que ya arranco el hilo- necesita ver POR QUE termino, no solo
            # que termino. Se fusiona el flag en el ultimo evento en vez de reemplazarlo.
            self._last_event[key] = {**self._last_event.get(key, {}), "stream_end": True}
        else:
            self._last_event[key] = event
        with self._sub_lock:
            for q in self._subscribers.get(key, []):
                q.put(event)

    def subscribe(self, sede: str, run_id: int) -> queue.Queue:
        key: RunKey = (sede, run_id)
        q: queue.Queue = queue.Queue()
        with self._sub_lock:
            self._subscribers.setdefault(key, []).append(q)
            # Reenvia el ultimo evento conocido para que un cliente que se conecta tarde
            # vea de inmediato el estado actual en vez de una pantalla en blanco.
            last = self._last_event.get(key)
        if last:
            q.put(last)
        return q

    def unsubscribe(self, sede: str, run_id: int, q: queue.Queue) -> None:
        key: RunKey = (sede, run_id)
        with self._sub_lock:
            subs = self._subscribers.get(key)
            if subs and q in subs:
                subs.remove(q)


MANAGER = RunManager()
