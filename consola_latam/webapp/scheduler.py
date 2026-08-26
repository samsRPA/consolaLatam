"""Programador de consultas automaticas, sin dependencias externas.

Un hilo revisa cada 30 s las programaciones activas. Cuando la hora/minuto coincide con
el reloj local y esa programacion aun no corrio HOY, dispara la corrida por el mismo
RunManager (que respeta el lock de una-corrida-a-la-vez; si hay otra en curso, la
programada se reintenta en el siguiente tick sin duplicarse)."""

from __future__ import annotations

import threading
import time
from datetime import datetime

from . import db
from .run_manager import MANAGER, RunBusyError

_CHECK_INTERVAL_SECONDS = 30


class Scheduler:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                pass  # el scheduler nunca debe morir por un error puntual
            self._stop.wait(_CHECK_INTERVAL_SECONDS)

    def _tick(self) -> None:
        # Hilo propio: el ContextVar de la sede no se hereda solo, hay que recorrer las
        # sedes explicitamente y fijar cada una antes de leer sus programaciones.
        now = datetime.now()
        today = now.date().isoformat()
        for sede in db.SEDES:
            db.set_sede(sede)
            for sched in db.list_all_schedules():
                if sched["hour"] != now.hour or sched["minute"] != now.minute:
                    continue
                if sched.get("last_run_date") == today:
                    continue  # ya corrio hoy
                try:
                    MANAGER.start_run(sched["base_id"], sched["mode"], sede)
                    db.mark_schedule_ran(sched["id"], today)
                except RunBusyError:
                    # Hay otra corrida activa; se reintentara en el proximo tick del mismo minuto.
                    pass
                except Exception:
                    db.mark_schedule_ran(sched["id"], today)  # evita reintentos infinitos ante error de datos


SCHEDULER = Scheduler()
