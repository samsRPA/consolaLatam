"""Arranca la consola web local:  python -m consola_latam.webapp

Abre el navegador automaticamente para mostrar la consola (localhost). La consulta real
a los portales (Peru/Ecuador) la resuelven los servicios/bots externos, no este proceso.

Si el puerto pedido esta ocupado, primero intenta recuperarlo: si lo tiene una instancia
anterior de esta misma app que no cerro bien (identificada por un pidfile propio, ver
_reclaim_own_port), la cierra y reintenta en ese puerto -- asi "siempre" termina en 8000
en vez de ir derivando a 8001, 8002... en cada reinicio. Si el puerto lo tiene otra cosa
(no una instancia propia), no la toca: busca el siguiente puerto libre en vez de fallar
con winerror 10048, igual que antes."""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

import uvicorn

from dotenv import load_dotenv

from . import db

if sys.platform == "win32":
    # ProactorEventLoop (el default en Windows) imprime un "Exception in callback"
    # inofensivo cuando el cliente corta la conexion de golpe -- muy comun aca porque
    # el progreso de las corridas se transmite por SSE de larga duracion y basta con
    # cerrar/recargar la pestana para disparar un ConnectionResetError en la limpieza
    # del socket. SelectorEventLoop no tiene ese problema y esta app no usa nada
    # exclusivo de Proactor (pipes con nombre, subprocesos asincronos).
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def _pid_file() -> Path:
    return db.DATA_DIR / "webapp.pid"


def _port_free(bind_host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((bind_host, port))
            return True
        except OSError:
            return False


def _looks_like_own_process(pid: int) -> bool:
    """Mejor esfuerzo para no matar un proceso ajeno que por casualidad reciclo el mismo
    PID que guardamos: en Linux se puede confirmar leyendo /proc/<pid>/cmdline. En
    plataformas sin /proc (Windows, macOS) no hay forma barata de verificarlo sin sumar
    una dependencia (psutil) solo para esto, asi que ahi se confia en el pidfile tal
    cual -- el riesgo de colision de PID en una maquina de un solo desarrollador es
    minimo, y si el kill fallara igual no rompe nada (ver _reclaim_own_port)."""
    cmdline_path = Path(f"/proc/{pid}/cmdline")
    if not cmdline_path.exists():
        return True  # sin /proc para verificar -- confiar en el pidfile
    try:
        cmdline = cmdline_path.read_bytes().decode("utf-8", "ignore")
    except OSError:
        return True
    return "consola_latam.webapp" in cmdline


def _reclaim_own_port(bind_host: str, port: int, wait: float = 3.0) -> None:
    """Si `port` esta ocupado por una instancia previa de esta misma app (pidfile propio
    de un cierre que no fue limpio), la cierra para que el arranque de ahora vuelva a caer
    en `port` en vez de derivar al siguiente libre. No toca nada si el puerto esta libre,
    si no hay pidfile, o si quien lo ocupa no parece ser esta app."""
    if _port_free(bind_host, port):
        return
    pid_file = _pid_file()
    try:
        recorded_pid, recorded_port = pid_file.read_text().strip().split(":")
        recorded_pid, recorded_port = int(recorded_pid), int(recorded_port)
    except (OSError, ValueError):
        return
    if recorded_port != port or not _looks_like_own_process(recorded_pid):
        return
    try:
        os.kill(recorded_pid, signal.SIGTERM)
    except OSError:
        return  # ya no existe o no se pudo -- _find_free_port hace de red de seguridad
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if _port_free(bind_host, port):
            return
        time.sleep(0.2)


def _remember_own_port(port: int) -> None:
    try:
        db.DATA_DIR.mkdir(parents=True, exist_ok=True)
        _pid_file().write_text(f"{os.getpid()}:{port}")
    except OSError:
        pass  # el pidfile es solo una comodidad -- que no arranque nunca bloquea esto


def _find_free_port(host: str, preferred: int, attempts: int = 20) -> int:
    """Devuelve `preferred` si esta libre; si no, prueba puertos consecutivos.

    Importante: NO usar SO_REUSEADDR en la sonda. En Windows esa opcion permite
    bindear encima de un puerto que ya esta en uso (semantica distinta a Linux), con
    lo que la sonda daria "libre" un puerto ocupado y uvicorn fallaria igual. Sin la
    opcion, bindear un puerto ocupado lanza OSError y lo detectamos correctamente."""
    bind_host = "127.0.0.1" if host == "0.0.0.0" else host
    for candidate in range(preferred, preferred + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((bind_host, candidate))
                return candidate
            except OSError:
                continue
    # Ultimo recurso: dejar que el SO asigne uno cualquiera.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((bind_host, 0))
        return sock.getsockname()[1]


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Consola web del scraper CEJ Peru")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-browser", action="store_true", help="No abrir el navegador automaticamente")
    args = parser.parse_args()

    display_host = "127.0.0.1" if args.host == "0.0.0.0" else args.host
    _reclaim_own_port(display_host, args.port)
    port = _find_free_port(args.host, args.port)
    url = f"http://{display_host}:{port}"

    if port != args.port:
        print(f"[aviso] El puerto {args.port} estaba ocupado; usando {port} en su lugar.")
    _remember_own_port(port)
    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    print("=" * 54)
    print(f"  Consola CEJ disponible en:  {url}")
    print("  Para cerrar: presiona Ctrl+C en esta ventana.")
    print("=" * 54)
    # loop="none": uvicorn 0.36+ ya no respeta asyncio.set_event_loop_policy() para elegir
    # el loop -- su factory "auto"/"asyncio" fuerza ProactorEventLoop en Windows sin mirar
    # la politica (ver uvicorn/loops/asyncio.py). Con "none" no usa ninguna factory propia
    # y deja que asyncio arme el loop por su cuenta, ahi si respetando la politica de
    # arriba.
    uvicorn.run("consola_latam.webapp.app:app", host=args.host, port=port, log_level="warning", loop="none")


if __name__ == "__main__":
    main()
