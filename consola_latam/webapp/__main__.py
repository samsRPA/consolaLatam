"""Arranca la consola web local:  python -m consola_latam.webapp

Abre el navegador automaticamente para mostrar la consola (localhost). La consulta real
a los portales (Peru/Ecuador) la resuelven los servicios/bots externos, no este proceso.

Si el puerto pedido esta ocupado (por ejemplo una instancia anterior que no cerro bien),
busca automaticamente el siguiente puerto libre en vez de fallar con winerror 10048."""

from __future__ import annotations

import argparse
import asyncio
import socket
import sys
import threading
import webbrowser

import uvicorn

from dotenv import load_dotenv

if sys.platform == "win32":
    # ProactorEventLoop (el default en Windows) imprime un "Exception in callback"
    # inofensivo cuando el cliente corta la conexion de golpe -- muy comun aca porque
    # el progreso de las corridas se transmite por SSE de larga duracion y basta con
    # cerrar/recargar la pestana para disparar un ConnectionResetError en la limpieza
    # del socket. SelectorEventLoop no tiene ese problema y esta app no usa nada
    # exclusivo de Proactor (pipes con nombre, subprocesos asincronos).
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


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

    port = _find_free_port(args.host, args.port)
    display_host = "127.0.0.1" if args.host == "0.0.0.0" else args.host
    url = f"http://{display_host}:{port}"

    if port != args.port:
        print(f"[aviso] El puerto {args.port} estaba ocupado; usando {port} en su lugar.")
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
