#!/usr/bin/env bash
# Atajo para lanzar la consola web en Linux/macOS (equivalente a cej_peru.ps1 en Windows).
set -euo pipefail

# Ruta relativa al propio script (no a una maquina/usuario en particular): funciona sin
# importar donde se copie o clone la carpeta del proyecto.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON="$PROJECT_DIR/.venv/bin/python"

if [ ! -x "$PYTHON" ]; then
    echo "No existe el Python del entorno virtual: $PYTHON" >&2
    echo "Crealo con: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
    exit 1
fi

echo ""
echo -e "\033[36mCEJ PERU - Consola web del scraper\033[0m"
echo -e "\033[90mProyecto: $PROJECT_DIR\033[0m"
echo ""

args=(-m consola_latam.webapp)
if [[ "${1:-}" == "--no-browser" ]]; then
    args+=(--no-browser)
fi

cd "$PROJECT_DIR"
exec "$PYTHON" "${args[@]}"
