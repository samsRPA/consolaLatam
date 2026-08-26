param(
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"

# Ruta relativa al propio script (no a una maquina/usuario en particular): funciona
# sin importar donde se copie o clone la carpeta del proyecto.
$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = Join-Path $ProjectDir ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $ProjectDir)) {
    throw "No existe el proyecto del scraper: $ProjectDir"
}

if (-not (Test-Path -LiteralPath $Python)) {
    throw "No existe el Python del entorno virtual: $Python"
}

Write-Host ""
Write-Host "CEJ PERU - Consola web del scraper" -ForegroundColor Cyan
Write-Host "Proyecto: $ProjectDir" -ForegroundColor DarkGray
Write-Host ""

$arguments = @("-m", "consola_latam.webapp")
if ($NoBrowser) {
    $arguments += "--no-browser"
}

Push-Location $ProjectDir
try {
    & $Python @arguments
} finally {
    Pop-Location
}
