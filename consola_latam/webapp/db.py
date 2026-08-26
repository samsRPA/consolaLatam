"""Modulo de memoria: SQLite (stdlib, sin servidor) para clientes, bases, corridas y
programaciones. La base de datos y los archivos subidos viven en una carpeta de datos
del usuario para que persistan entre reinicios de la app.

IMPORTANTE - por que NO vive en Descargas: Windows "Storage Sense" puede configurarse
(y en esta maquina esta configurado, de forma muy agresiva: borra a partir de 1 dia de
inactividad) para eliminar automaticamente archivos de la carpeta Descargas que no se
"abren" recientemente. Un archivo SQLite que solo se toca mientras la app esta corriendo
cae directo en esa regla y Windows lo borra en silencio -> la app "pierde la memoria"
sin ningun error visible. Por eso los datos viven en AppData\\Local (la carpeta que
Windows reserva para datos de aplicaciones y que Storage Sense nunca toca)."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

# Ubicacion legacy (version antigua, vivia dentro de Descargas -> vulnerable a Storage
# Sense). Se usa solo para migrar datos existentes la primera vez que se detecta. Es
# exclusiva de la sede Peru (la unica que existia cuando vivia en esa ubicacion).
LEGACY_DATA_DIR = Path.home() / "Downloads" / "SCRAPER PERU" / "_app"

_LOCAL_APP_DATA = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))

# Carpeta base de datos (BD + bases subidas + resultados). Configurable por env para
# pruebas o para reubicar el almacenamiento; por defecto vive en AppData\Local, fuera
# del alcance de cualquier limpieza automatica de la carpeta Descargas.
_BASE_DATA_DIR = Path(os.environ.get("SCRAPER_APP_DATA_DIR", str(_LOCAL_APP_DATA / "ConsolaCEJPeru")))

# ---------- separacion por sede (Peru / Ecuador) ----------
# Cada sede tiene su PROPIA carpeta de datos (BD + bases + resultados + adjuntos), asi
# que no comparten clientes, procesos, notificaciones ni nada entre si. Peru se queda en
# la carpeta raiz de siempre (compatibilidad con instalaciones existentes); Ecuador vive
# en una subcarpeta nueva. CURRENT_SEDE indica, para el hilo/tarea actual, cual de las
# dos se debe usar; la fija el request (ver app.py) o, en hilos que el propio backend
# lanza para correr el scraper, quien los lanza (ver run_manager.py) - los ContextVar NO
# se propagan solos a un threading.Thread nuevo ni a un ThreadPoolExecutor, asi que cada
# punto donde arranca un hilo/tarea nueva debe fijarla explicitamente al entrar.
SEDES = ("peru", "ecuador")
DEFAULT_SEDE = "peru"
CURRENT_SEDE: ContextVar[str] = ContextVar("current_sede", default=DEFAULT_SEDE)


def set_sede(sede: str) -> None:
    if sede not in SEDES:
        raise ValueError(f"Sede desconocida: {sede!r}")
    CURRENT_SEDE.set(sede)


def _data_dir_for(sede: str) -> Path:
    return _BASE_DATA_DIR if sede == "peru" else _BASE_DATA_DIR / sede


def data_dir() -> Path:
    return _data_dir_for(CURRENT_SEDE.get())


def db_path() -> Path:
    return data_dir() / "scraper_app.db"


def uploads_dir() -> Path:
    return data_dir() / "bases"


def outputs_dir() -> Path:
    return data_dir() / "resultados"


def attachments_dir() -> Path:
    return data_dir() / "adjuntos"


def backups_dir() -> Path:
    return data_dir() / "backups"


# Acceso tipo "constante" (db.DATA_DIR, db.UPLOADS_DIR, etc.) para no tener que tocar
# cada uno de sus muchos usos en app.py/run_manager.py: se resuelven dinamicamente segun
# CURRENT_SEDE en el momento en que se leen, en vez de fijarse una sola vez al importar.
def __getattr__(name: str) -> Any:
    resolver = {
        "DATA_DIR": data_dir, "DB_PATH": db_path, "UPLOADS_DIR": uploads_dir,
        "OUTPUTS_DIR": outputs_dir, "ATTACHMENTS_DIR": attachments_dir, "BACKUPS_DIR": backups_dir,
    }.get(name)
    if resolver is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return resolver()


_LOCAL = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    importance TEXT DEFAULT 'MEDIA',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    mapping_json TEXT NOT NULL,
    row_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    base_id INTEGER NOT NULL REFERENCES bases(id) ON DELETE CASCADE,
    mode TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    total INTEGER DEFAULT 0,
    done INTEGER DEFAULT 0,
    ok INTEGER DEFAULT 0,
    output_path TEXT DEFAULT '',
    error TEXT DEFAULT '',
    started_at TEXT NOT NULL,
    finished_at TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    base_id INTEGER NOT NULL REFERENCES bases(id) ON DELETE CASCADE,
    mode TEXT NOT NULL,
    hour INTEGER NOT NULL,
    minute INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    last_run_date TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS run_cases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    radicado TEXT NOT NULL,
    parte TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'OK',
    movimientos_estado TEXT DEFAULT '',
    tipo_movimiento TEXT DEFAULT '',
    error TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_run_cases_run ON run_cases(run_id);

-- Carpetas dentro de un cliente para agrupar procesos (ej. por materia o sub-cartera).
CREATE TABLE IF NOT EXISTS folders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_folders_client ON folders(client_id);

-- REPOSITORIO JURIDICO: un expediente persistente por radicado/cliente. Es la fuente de
-- verdad del modulo "Mis Procesos" y sobrevive a cualquier corrida: las corridas solo lo
-- actualizan. detail_json guarda el reporte + partes del CEJ para re-exportar a Excel.
CREATE TABLE IF NOT EXISTS processes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER REFERENCES clients(id) ON DELETE SET NULL,
    folder_id INTEGER REFERENCES folders(id) ON DELETE SET NULL,
    radicado TEXT NOT NULL,
    matched_radicado TEXT DEFAULT '',
    demandante TEXT DEFAULT '',
    demandado TEXT DEFAULT '',
    organo TEXT DEFAULT '',
    materia TEXT DEFAULT '',
    estado TEXT DEFAULT '',
    nro_registro TEXT DEFAULT '',
    last_sig TEXT DEFAULT '',
    last_fecha TEXT DEFAULT '',
    last_acto TEXT DEFAULT '',
    needs_manual INTEGER NOT NULL DEFAULT 0,
    source TEXT DEFAULT 'base',
    detail_json TEXT DEFAULT '',
    last_status TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_consulted_at TEXT DEFAULT '',
    UNIQUE(client_id, radicado)
);
CREATE INDEX IF NOT EXISTS idx_processes_client ON processes(client_id);

-- Actuaciones (movimientos) de un expediente. Cada fila es una actuacion del historial CEJ.
-- data_json conserva todas las columnas originales de la actuacion para la tabla y el Excel.
CREATE TABLE IF NOT EXISTS actuaciones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    process_id INTEGER NOT NULL REFERENCES processes(id) ON DELETE CASCADE,
    sig TEXT NOT NULL,
    n TEXT DEFAULT '',
    fecha TEXT DEFAULT '',
    resolucion TEXT DEFAULT '',
    acto TEXT DEFAULT '',
    sumilla TEXT DEFAULT '',
    descripcion TEXT DEFAULT '',
    data_json TEXT DEFAULT '',
    attachment_path TEXT DEFAULT '',
    attachment_filename TEXT DEFAULT '',
    is_new INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_actuaciones_process ON actuaciones(process_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_actuaciones_sig ON actuaciones(process_id, sig);

-- Notificaciones del sistema: movimiento / sin_movimiento / manual / error.
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    process_id INTEGER REFERENCES processes(id) ON DELETE CASCADE,
    client_id INTEGER REFERENCES clients(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT DEFAULT '',
    radicado TEXT DEFAULT '',
    read INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notifications_read ON notifications(read);

-- Historial de consultas por cliente (cada corrida/consulta queda registrada).
CREATE TABLE IF NOT EXISTS query_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER REFERENCES clients(id) ON DELETE CASCADE,
    scope TEXT NOT NULL,
    mode TEXT DEFAULT '',
    total INTEGER DEFAULT 0,
    con_mov INTEGER DEFAULT 0,
    sin_mov INTEGER DEFAULT 0,
    errores INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_query_history_client ON query_history(client_id);

-- Tareas, recordatorios y anotaciones del modulo Gestion. Se fijan a una fecha y hora,
-- opcionalmente asociadas a un cliente. done marca las cumplidas.
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER REFERENCES clients(id) ON DELETE SET NULL,
    process_id INTEGER REFERENCES processes(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    notes TEXT DEFAULT '',
    due_date TEXT NOT NULL,
    due_time TEXT DEFAULT '',
    kind TEXT DEFAULT 'tarea',
    source_key TEXT DEFAULT '',
    done INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reminders_date ON reminders(due_date);
"""

# Columnas agregadas despues de la version inicial. SQLite no tiene "ADD COLUMN IF NOT
# EXISTS", asi que se intenta y se ignora el error si la columna ya existe (migracion
# idempotente para bases de datos creadas por versiones anteriores del aplicativo).
_MIGRATIONS = [
    "ALTER TABLE clients ADD COLUMN client_type TEXT DEFAULT ''",
    # resolved: notificaciones manuales que el usuario ya cargó a la plataforma
    # ("Auto cargado en Plataforma"). Permite restar las resueltas del total pendiente.
    "ALTER TABLE notifications ADD COLUMN resolved INTEGER NOT NULL DEFAULT 0",
    # resolved_at: fecha en que se resolvió (para el calendario del módulo Gestión).
    "ALTER TABLE notifications ADD COLUMN resolved_at TEXT DEFAULT ''",
    "ALTER TABLE actuaciones ADD COLUMN attachment_path TEXT DEFAULT ''",
    "ALTER TABLE actuaciones ADD COLUMN attachment_filename TEXT DEFAULT ''",
    "ALTER TABLE reminders ADD COLUMN process_id INTEGER",
    "ALTER TABLE reminders ADD COLUMN source_key TEXT DEFAULT ''",
]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def init_db() -> None:
    """Crea el esquema para la sede ACTUAL (CURRENT_SEDE). Se llama una vez por sede al
    arrancar (ver app.py), no toca las demas sedes."""
    for directory in (data_dir(), uploads_dir(), outputs_dir(), attachments_dir()):
        directory.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.executescript(SCHEMA)
        for statement in _MIGRATIONS:
            try:
                conn.execute(statement)
            except sqlite3.OperationalError:
                pass  # la columna ya existe: migracion ya aplicada
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_reminders_source_key ON reminders(source_key) WHERE source_key <> ''")


def checkpoint() -> None:
    """Fusiona el WAL (write-ahead log) al archivo principal .db y lo trunca. Es
    OBLIGATORIO antes de copiar/zipear el archivo .db a mano (shutil, zipfile): en modo
    WAL los commits recientes pueden vivir solo en el archivo -wal hasta que se hace este
    checkpoint, y una copia de archivo cruda del .db sin esto pierde esos commits aunque
    ya esten confirmados. Opera sobre la sede ACTUAL."""
    try:
        _connect().execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error:
        pass


def migrate_legacy_data_dir() -> bool:
    """Copia una sola vez los datos de la ubicacion antigua (dentro de Descargas, donde
    Storage Sense podia borrarlos) a la nueva ubicacion en AppData\\Local. Solo actua si
    la ubicacion nueva todavia no tiene base de datos (primera vez tras la actualizacion)
    y la antigua si tiene una — asi nunca sobrescribe datos ya migrados. Se llama SOLO
    desde el arranque real de la app (nunca desde init_db(), para no afectar los tests
    que apuntan DATA_DIR a un directorio temporal). Exclusiva de la sede Peru: la
    ubicacion legacy es de antes de que existiera Ecuador, nunca tuvo sus datos."""
    if CURRENT_SEDE.get() != "peru":
        return False
    legacy_db = LEGACY_DATA_DIR / "scraper_app.db"
    if db_path().exists() or not legacy_db.exists():
        return False
    # Fusiona cualquier WAL pendiente del archivo legacy ANTES de copiarlo en crudo,
    # para no dejar atras commits que solo vivan en scraper_app.db-wal.
    try:
        legacy_conn = sqlite3.connect(legacy_db)
        legacy_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        legacy_conn.close()
    except sqlite3.Error:
        pass
    dd = data_dir()
    dd.mkdir(parents=True, exist_ok=True)
    for name in ("scraper_app.db", "bases", "resultados", "adjuntos"):
        src = LEGACY_DATA_DIR / name
        if not src.exists():
            continue
        dst = dd / name
        try:
            if src.is_dir():
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)
        except OSError:
            continue
    return True


def backup_database(keep: int = 30) -> Path | None:
    """Copia la base de datos de la sede ACTUAL a backups/ con marca de tiempo y conserva
    solo las ultimas `keep`. Es la red de seguridad ante cualquier perdida futura (borrado
    accidental, corrupcion, etc.): siempre hay un punto de restauracion reciente. Se
    llama una vez por sede en cada arranque real de la app (no en tests)."""
    dbp = db_path()
    if not dbp.exists():
        return None
    checkpoint()  # fusiona el WAL antes de copiar el archivo en crudo
    bdir = backups_dir()
    bdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = bdir / f"scraper_app_{stamp}.db"
    try:
        shutil.copy2(dbp, dest)
    except OSError:
        return None
    existing = sorted(bdir.glob("scraper_app_*.db"))
    for old in existing[:-keep]:
        try:
            old.unlink()
        except OSError:
            pass
    return dest


def close_connection() -> None:
    conns: dict[str, sqlite3.Connection] | None = getattr(_LOCAL, "conns", None)
    if conns:
        for conn in conns.values():
            conn.close()
        _LOCAL.conns = {}


def _connect() -> sqlite3.Connection:
    """Una conexion por (hilo, sede): SQLite no comparte conexiones entre hilos con
    seguridad, y cada sede vive en su propio archivo .db."""
    sede = CURRENT_SEDE.get()
    conns: dict[str, sqlite3.Connection] | None = getattr(_LOCAL, "conns", None)
    if conns is None:
        conns = {}
        _LOCAL.conns = conns
    conn = conns.get(sede)
    if conn is None:
        dd = data_dir()
        dd.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path(), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        # Varios hilos (run thread + worker de progreso) escriben durante una corrida;
        # busy_timeout evita errores "database is locked" transitorios.
        conn.execute("PRAGMA busy_timeout = 5000")
        # WAL: mas resiliente ante un cierre abrupto del proceso (Stop-Process, cuelgue,
        # corte de luz) - los commits ya confirmados sobreviven aunque el archivo principal
        # no se haya "fusionado" todavia. synchronous=NORMAL es el balance recomendado
        # por SQLite para WAL (seguro para commits, mejor rendimiento que FULL).
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conns[sede] = conn
    return conn


@contextmanager
def _tx() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


# ---------- clientes ----------

def create_client(name: str, description: str, importance: str, client_type: str = "") -> dict[str, Any]:
    with _tx() as conn:
        cur = conn.execute(
            "INSERT INTO clients (name, description, importance, client_type, created_at) VALUES (?, ?, ?, ?, ?)",
            (name.strip(), description.strip(), importance.strip().upper() or "MEDIA", client_type.strip().upper(), _now()),
        )
        return get_client(cur.lastrowid)  # type: ignore[arg-type]


def get_client(client_id: int) -> dict[str, Any]:
    row = _connect().execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
    if row is None:
        raise KeyError(f"Cliente {client_id} no existe")
    data = _row_to_dict(row)
    data["bases_count"] = _connect().execute(
        "SELECT COUNT(*) c FROM bases WHERE client_id = ?", (client_id,)
    ).fetchone()["c"]
    data["process_count"] = _connect().execute(
        "SELECT COUNT(*) c FROM processes WHERE client_id = ?", (client_id,)
    ).fetchone()["c"]
    return data


def list_clients() -> list[dict[str, Any]]:
    rows = _connect().execute("SELECT * FROM clients ORDER BY created_at DESC").fetchall()
    result = []
    for row in rows:
        data = _row_to_dict(row)
        data["bases_count"] = _connect().execute(
            "SELECT COUNT(*) c FROM bases WHERE client_id = ?", (row["id"],)
        ).fetchone()["c"]
        data["process_count"] = _connect().execute(
            "SELECT COUNT(*) c FROM processes WHERE client_id = ?", (row["id"],)
        ).fetchone()["c"]
        result.append(data)
    return result


def delete_client(client_id: int) -> None:
    # Al eliminar un cliente se elimina TODA su cartera (procesos + bases + notifs). Se hace
    # explicitamente porque processes.client_id es ON DELETE SET NULL (para no perder procesos
    # de Inclusiones sin cliente); aqui SI queremos borrar los procesos que son de este cliente.
    with _tx() as conn:
        conn.execute("DELETE FROM processes WHERE client_id = ?", (client_id,))
        conn.execute("DELETE FROM clients WHERE id = ?", (client_id,))


# ---------- bases ----------

def create_base(
    client_id: int, name: str, original_filename: str, stored_path: str, mapping_json: dict, row_count: int
) -> dict[str, Any]:
    with _tx() as conn:
        cur = conn.execute(
            "INSERT INTO bases (client_id, name, original_filename, stored_path, mapping_json, row_count, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (client_id, name, original_filename, stored_path, json.dumps(mapping_json, ensure_ascii=False), row_count, _now()),
        )
        return get_base(cur.lastrowid)  # type: ignore[arg-type]


def get_base(base_id: int) -> dict[str, Any]:
    row = _connect().execute("SELECT * FROM bases WHERE id = ?", (base_id,)).fetchone()
    if row is None:
        raise KeyError(f"Base {base_id} no existe")
    data = _row_to_dict(row)
    data["mapping"] = json.loads(data.pop("mapping_json"))
    return data


def list_bases(client_id: int) -> list[dict[str, Any]]:
    rows = _connect().execute(
        "SELECT * FROM bases WHERE client_id = ? ORDER BY created_at DESC", (client_id,)
    ).fetchall()
    result = []
    for row in rows:
        data = _row_to_dict(row)
        data["mapping"] = json.loads(data.pop("mapping_json"))
        last = _connect().execute(
            "SELECT id, status, finished_at, mode FROM runs WHERE base_id = ? ORDER BY id DESC LIMIT 1", (row["id"],)
        ).fetchone()
        if last is not None:
            last_run = _row_to_dict(last)
            last_run["counts"] = run_case_counts(last_run["id"])
            data["last_run"] = last_run
        else:
            data["last_run"] = None
        result.append(data)
    return result


def delete_base(base_id: int) -> None:
    with _tx() as conn:
        conn.execute("DELETE FROM bases WHERE id = ?", (base_id,))


# ---------- corridas ----------

def create_run(base_id: int, mode: str, total: int) -> dict[str, Any]:
    with _tx() as conn:
        cur = conn.execute(
            "INSERT INTO runs (base_id, mode, status, total, started_at) VALUES (?, ?, 'running', ?, ?)",
            (base_id, mode, total, _now()),
        )
        return get_run(cur.lastrowid)  # type: ignore[arg-type]


def get_run(run_id: int) -> dict[str, Any]:
    row = _connect().execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise KeyError(f"Run {run_id} no existe")
    return _row_to_dict(row)


def update_run(run_id: int, **fields: Any) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    with _tx() as conn:
        conn.execute(f"UPDATE runs SET {cols} WHERE id = ?", (*fields.values(), run_id))


def list_runs(base_id: int, limit: int = 20) -> list[dict[str, Any]]:
    rows = _connect().execute(
        "SELECT * FROM runs WHERE base_id = ? ORDER BY id DESC LIMIT ?", (base_id, limit)
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


# ---------- resultados por proceso (para filtrar "con movimientos" / "sin movimientos") ----------

def add_run_case(
    run_id: int, radicado: str, parte: str, status: str, movimientos_estado: str, tipo_movimiento: str, error: str
) -> None:
    with _tx() as conn:
        conn.execute(
            "INSERT INTO run_cases (run_id, radicado, parte, status, movimientos_estado, tipo_movimiento, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, radicado, parte, status, movimientos_estado, tipo_movimiento, error),
        )


# Filtros validos para el panel de resultados: "con_movimientos" agrupa SI (modo diaria)
# y TODOS (modo total, que siempre trae todo el historial cuando hay actuaciones).
_MOVEMENT_FILTERS = {
    "con_movimientos": "status = 'OK' AND movimientos_estado IN ('SI', 'TODOS')",
    "sin_movimientos": "status = 'OK' AND movimientos_estado = 'NO'",
    "error": "status = 'ERROR'",
}


def list_run_cases(run_id: int, case_filter: str | None = None) -> list[dict[str, Any]]:
    where = _MOVEMENT_FILTERS.get(case_filter or "", "1 = 1")
    rows = _connect().execute(
        f"SELECT * FROM run_cases WHERE run_id = ? AND ({where}) ORDER BY id", (run_id,)
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def run_case_counts(run_id: int) -> dict[str, int]:
    counts = {"con_movimientos": 0, "sin_movimientos": 0, "error": 0, "total": 0}
    rows = _connect().execute(
        "SELECT status, movimientos_estado, COUNT(*) c FROM run_cases WHERE run_id = ? GROUP BY status, movimientos_estado",
        (run_id,),
    ).fetchall()
    for row in rows:
        counts["total"] += row["c"]
        if row["status"] == "ERROR":
            counts["error"] += row["c"]
        elif row["movimientos_estado"] in ("SI", "TODOS"):
            counts["con_movimientos"] += row["c"]
        elif row["movimientos_estado"] == "NO":
            counts["sin_movimientos"] += row["c"]
    return counts


# ---------- programaciones ----------

def create_schedule(base_id: int, mode: str, hour: int, minute: int) -> dict[str, Any]:
    with _tx() as conn:
        cur = conn.execute(
            "INSERT INTO schedules (base_id, mode, hour, minute, enabled, created_at) VALUES (?, ?, ?, ?, 1, ?)",
            (base_id, mode, hour, minute, _now()),
        )
        return get_schedule(cur.lastrowid)  # type: ignore[arg-type]


def get_schedule(schedule_id: int) -> dict[str, Any]:
    row = _connect().execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
    if row is None:
        raise KeyError(f"Schedule {schedule_id} no existe")
    return _row_to_dict(row)


def list_schedules(base_id: int) -> list[dict[str, Any]]:
    rows = _connect().execute(
        "SELECT * FROM schedules WHERE base_id = ? ORDER BY hour, minute", (base_id,)
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def list_all_schedules() -> list[dict[str, Any]]:
    rows = _connect().execute("SELECT * FROM schedules WHERE enabled = 1").fetchall()
    return [_row_to_dict(r) for r in rows]


def set_schedule_enabled(schedule_id: int, enabled: bool) -> None:
    with _tx() as conn:
        conn.execute("UPDATE schedules SET enabled = ? WHERE id = ?", (1 if enabled else 0, schedule_id))


def mark_schedule_ran(schedule_id: int, date_str: str) -> None:
    with _tx() as conn:
        conn.execute("UPDATE schedules SET last_run_date = ? WHERE id = ?", (date_str, schedule_id))


def delete_schedule(schedule_id: int) -> None:
    with _tx() as conn:
        conn.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))


# ---------- carpetas ----------

def create_folder(client_id: int, name: str) -> dict[str, Any]:
    with _tx() as conn:
        cur = conn.execute(
            "INSERT INTO folders (client_id, name, created_at) VALUES (?, ?, ?)",
            (client_id, name.strip(), _now()),
        )
        row = conn.execute("SELECT * FROM folders WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _row_to_dict(row)


def list_folders(client_id: int) -> list[dict[str, Any]]:
    rows = _connect().execute(
        "SELECT * FROM folders WHERE client_id = ? ORDER BY name", (client_id,)
    ).fetchall()
    result = []
    for row in rows:
        data = _row_to_dict(row)
        data["process_count"] = _connect().execute(
            "SELECT COUNT(*) c FROM processes WHERE folder_id = ?", (row["id"],)
        ).fetchone()["c"]
        result.append(data)
    return result


def delete_folder(folder_id: int) -> None:
    with _tx() as conn:
        conn.execute("DELETE FROM folders WHERE id = ?", (folder_id,))


# ---------- procesos (repositorio juridico) ----------

def get_process(process_id: int) -> dict[str, Any]:
    row = _connect().execute("SELECT * FROM processes WHERE id = ?", (process_id,)).fetchone()
    if row is None:
        raise KeyError(f"Proceso {process_id} no existe")
    return _hydrate_process(row)


def find_process(client_id: int | None, radicado: str) -> dict[str, Any] | None:
    if client_id is None:
        row = _connect().execute(
            "SELECT * FROM processes WHERE client_id IS NULL AND radicado = ?", (radicado,)
        ).fetchone()
    else:
        row = _connect().execute(
            "SELECT * FROM processes WHERE client_id = ? AND radicado = ?", (client_id, radicado)
        ).fetchone()
    return _hydrate_process(row) if row else None


def _hydrate_process(row: sqlite3.Row) -> dict[str, Any]:
    data = _row_to_dict(row)
    data["detail"] = json.loads(data.pop("detail_json") or "{}") if data.get("detail_json") else {}
    data["needs_manual"] = bool(data["needs_manual"])
    return data


def upsert_process(
    *,
    client_id: int | None,
    radicado: str,
    matched_radicado: str,
    demandante: str,
    demandado: str,
    organo: str,
    materia: str,
    estado: str,
    nro_registro: str,
    detail: dict,
    source: str,
    folder_id: int | None = None,
) -> dict[str, Any]:
    existing = find_process(client_id, radicado)
    now = _now()
    detail_json = json.dumps(detail or {}, ensure_ascii=False)
    if existing is None:
        with _tx() as conn:
            cur = conn.execute(
                "INSERT INTO processes (client_id, folder_id, radicado, matched_radicado, demandante, demandado, "
                "organo, materia, estado, nro_registro, detail_json, source, created_at, updated_at, last_consulted_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (client_id, folder_id, radicado, matched_radicado, demandante, demandado, organo, materia,
                 estado, nro_registro, detail_json, source, now, now, now),
            )
            return get_process(cur.lastrowid)  # type: ignore[arg-type]
    # Actualiza metadatos pero conserva folder_id/cliente asignados manualmente.
    with _tx() as conn:
        conn.execute(
            "UPDATE processes SET matched_radicado = ?, demandante = COALESCE(NULLIF(?, ''), demandante), "
            "demandado = COALESCE(NULLIF(?, ''), demandado), organo = COALESCE(NULLIF(?, ''), organo), "
            "materia = COALESCE(NULLIF(?, ''), materia), estado = COALESCE(NULLIF(?, ''), estado), "
            "nro_registro = COALESCE(NULLIF(?, ''), nro_registro), detail_json = ?, updated_at = ?, last_consulted_at = ? "
            "WHERE id = ?",
            (matched_radicado, demandante, demandado, organo, materia, estado, nro_registro,
             detail_json, now, now, existing["id"]),
        )
    return get_process(existing["id"])


def set_process_latest(process_id: int, sig: str, fecha: str, acto: str, needs_manual: bool, last_status: str) -> None:
    with _tx() as conn:
        conn.execute(
            "UPDATE processes SET last_sig = ?, last_fecha = ?, last_acto = ?, needs_manual = ?, "
            "last_status = ?, updated_at = ? WHERE id = ?",
            (sig, fecha, acto, 1 if needs_manual else 0, last_status, _now(), process_id),
        )


def list_processes(
    client_id: int | None = None, folder_id: int | None = None, search: str = ""
) -> list[dict[str, Any]]:
    clauses, params = [], []
    if client_id is not None:
        clauses.append("client_id = ?")
        params.append(client_id)
    if folder_id is not None:
        clauses.append("folder_id = ?")
        params.append(folder_id)
    if search:
        like = f"%{search.lower()}%"
        clauses.append("(LOWER(radicado) LIKE ? OR LOWER(demandante) LIKE ? OR LOWER(demandado) LIKE ?)")
        params.extend([like, like, like])
    where = " AND ".join(clauses) if clauses else "1 = 1"
    rows = _connect().execute(
        f"SELECT * FROM processes WHERE {where} ORDER BY updated_at DESC", params
    ).fetchall()
    result = []
    client_names = {c["id"]: c["name"] for c in list_clients()}
    for row in rows:
        data = _hydrate_process(row)
        data["actuaciones_count"] = _connect().execute(
            "SELECT COUNT(*) c FROM actuaciones WHERE process_id = ?", (row["id"],)
        ).fetchone()["c"]
        data["client_name"] = client_names.get(data["client_id"], "")
        result.append(data)
    return result


def delete_process(process_id: int) -> None:
    with _tx() as conn:
        conn.execute("DELETE FROM processes WHERE id = ?", (process_id,))


def delete_processes(client_id: int | None = None) -> int:
    """Borra en bloque: todos los procesos del repositorio, o solo los de un cliente si
    se pasa client_id. Devuelve cuantos se borraron."""
    with _tx() as conn:
        if client_id is None:
            cur = conn.execute("DELETE FROM processes")
        else:
            cur = conn.execute("DELETE FROM processes WHERE client_id = ?", (client_id,))
        return cur.rowcount


def assign_process(process_id: int, client_id: int | None, folder_id: int | None) -> None:
    with _tx() as conn:
        conn.execute(
            "UPDATE processes SET client_id = ?, folder_id = ?, updated_at = ? WHERE id = ?",
            (client_id, folder_id, _now(), process_id),
        )


# ---------- actuaciones ----------

def list_actuaciones(process_id: int) -> list[dict[str, Any]]:
    rows = _connect().execute(
        "SELECT * FROM actuaciones WHERE process_id = ? ORDER BY id", (process_id,)
    ).fetchall()
    result = []
    for row in rows:
        data = _row_to_dict(row)
        data["data"] = json.loads(data.pop("data_json") or "{}")
        data["is_new"] = bool(data["is_new"])
        result.append(data)
    return result


def existing_actuacion_sigs(process_id: int) -> set[str]:
    rows = _connect().execute(
        "SELECT sig FROM actuaciones WHERE process_id = ?", (process_id,)
    ).fetchall()
    return {r["sig"] for r in rows}


def add_actuacion(
    process_id: int, sig: str, n: str, fecha: str, resolucion: str, acto: str,
    sumilla: str, descripcion: str, data: dict, is_new: bool,
) -> None:
    with _tx() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO actuaciones (process_id, sig, n, fecha, resolucion, acto, sumilla, "
            "descripcion, data_json, is_new, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (process_id, sig, n, fecha, resolucion, acto, sumilla, descripcion,
             json.dumps(data or {}, ensure_ascii=False), 1 if is_new else 0, _now()),
        )


def clear_new_flags(process_id: int) -> None:
    with _tx() as conn:
        conn.execute("UPDATE actuaciones SET is_new = 0 WHERE process_id = ?", (process_id,))


# ---------- notificaciones ----------

def create_notification(
    kind: str, title: str, body: str, radicado: str,
    process_id: int | None = None, client_id: int | None = None,
) -> dict[str, Any]:
    with _tx() as conn:
        cur = conn.execute(
            "INSERT INTO notifications (process_id, client_id, kind, title, body, radicado, read, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
            (process_id, client_id, kind, title, body, radicado, _now()),
        )
        row = conn.execute("SELECT * FROM notifications WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _row_to_dict(row)


def get_notification(notif_id: int) -> dict[str, Any]:
    row = _connect().execute("SELECT * FROM notifications WHERE id = ?", (notif_id,)).fetchone()
    if row is None:
        raise KeyError(f"Notificacion {notif_id} no existe")
    return _row_to_dict(row)


_NOTIF_FILTERS = {
    "no_leidas": "read = 0",
    "con_movimiento": "kind = 'movimiento'",
    "sin_movimiento": "kind = 'sin_movimiento'",
    "manual": "kind = 'manual' AND resolved = 0",  # solo las que aun requieren carga manual
    "manual_resuelto": "kind = 'manual' AND resolved = 1",
}


def list_notifications(notif_filter: str | None = None, limit: int = 300) -> list[dict[str, Any]]:
    where = _NOTIF_FILTERS.get(notif_filter or "", "1 = 1")
    rows = _connect().execute(
        f"SELECT * FROM notifications WHERE {where} ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def notification_counts() -> dict[str, int]:
    conn = _connect()
    total = conn.execute("SELECT COUNT(*) c FROM notifications").fetchone()["c"]
    unread = conn.execute("SELECT COUNT(*) c FROM notifications WHERE read = 0").fetchone()["c"]
    con = conn.execute("SELECT COUNT(*) c FROM notifications WHERE kind = 'movimiento'").fetchone()["c"]
    sin = conn.execute("SELECT COUNT(*) c FROM notifications WHERE kind = 'sin_movimiento'").fetchone()["c"]
    manual = conn.execute("SELECT COUNT(*) c FROM notifications WHERE kind = 'manual'").fetchone()["c"]
    manual_pend = conn.execute("SELECT COUNT(*) c FROM notifications WHERE kind = 'manual' AND resolved = 0").fetchone()["c"]
    manual_ok = conn.execute("SELECT COUNT(*) c FROM notifications WHERE kind = 'manual' AND resolved = 1").fetchone()["c"]
    return {"total": total, "no_leidas": unread, "con_movimiento": con, "sin_movimiento": sin,
            "manual": manual, "manual_pendiente": manual_pend, "manual_resuelto": manual_ok}


def mark_notification_read(notif_id: int, read: bool = True) -> None:
    with _tx() as conn:
        conn.execute("UPDATE notifications SET read = ? WHERE id = ?", (1 if read else 0, notif_id))


def resolve_manual_notification(notif_id: int) -> None:
    """Marca una notificacion manual como "Auto cargado en Plataforma" (resuelta) y, si el
    proceso ya no tiene notificaciones manuales pendientes, le quita la marca needs_manual."""
    with _tx() as conn:
        conn.execute("UPDATE notifications SET resolved = 1, read = 1, resolved_at = ? WHERE id = ?", (_now(), notif_id))
        row = conn.execute("SELECT process_id FROM notifications WHERE id = ?", (notif_id,)).fetchone()
        pid = row["process_id"] if row else None
        if pid is not None:
            pend = conn.execute(
                "SELECT COUNT(*) c FROM notifications WHERE process_id = ? AND kind = 'manual' AND resolved = 0", (pid,)
            ).fetchone()["c"]
            if pend == 0:
                conn.execute("UPDATE processes SET needs_manual = 0 WHERE id = ?", (pid,))


def get_actuacion(actuacion_id: int) -> dict[str, Any]:
    row = _connect().execute("SELECT * FROM actuaciones WHERE id = ?", (actuacion_id,)).fetchone()
    if row is None:
        raise KeyError(f"Actuacion {actuacion_id} no existe")
    data = _row_to_dict(row)
    data["data"] = json.loads(data.pop("data_json") or "{}")
    data["is_new"] = bool(data["is_new"])
    return data


def find_manual_actuacion(process_id: int) -> dict[str, Any] | None:
    rows = _connect().execute(
        "SELECT * FROM actuaciones WHERE process_id = ? ORDER BY id DESC", (process_id,)
    ).fetchall()
    for row in rows:
        text = " ".join(str(row[key] or "") for key in ("acto", "sumilla", "descripcion")).lower()
        if "audiencia" in text or "señala" in text or "senala" in text:
            return get_actuacion(row["id"])
    return get_actuacion(rows[0]["id"]) if rows else None


def attach_actuacion_document(actuacion_id: int, stored_path: str, original_filename: str) -> dict[str, Any]:
    with _tx() as conn:
        conn.execute(
            "UPDATE actuaciones SET attachment_path = ?, attachment_filename = ? WHERE id = ?",
            (stored_path, original_filename, actuacion_id),
        )
    return get_actuacion(actuacion_id)


def attach_manual_notification_document(notif_id: int, stored_path: str, original_filename: str) -> dict[str, Any]:
    notif = get_notification(notif_id)
    process_id = notif.get("process_id")
    if process_id is None:
        raise KeyError("La notificacion no esta asociada a un proceso")
    actuacion = find_manual_actuacion(int(process_id))
    if actuacion is None:
        raise KeyError("No hay actuaciones para adjuntar el documento")
    attached = attach_actuacion_document(actuacion["id"], stored_path, original_filename)
    resolve_manual_notification(notif_id)
    return attached


def mark_all_notifications_read() -> None:
    with _tx() as conn:
        conn.execute("UPDATE notifications SET read = 1 WHERE read = 0")


def delete_notifications(ids: list[int]) -> None:
    if not ids:
        return
    placeholders = ",".join("?" for _ in ids)
    with _tx() as conn:
        conn.execute(f"DELETE FROM notifications WHERE id IN ({placeholders})", ids)


def delete_all_notifications() -> None:
    with _tx() as conn:
        conn.execute("DELETE FROM notifications")


# ---------- historial de consultas ----------

def add_query_history(
    client_id: int | None, scope: str, mode: str, total: int, con_mov: int, sin_mov: int, errores: int
) -> None:
    with _tx() as conn:
        conn.execute(
            "INSERT INTO query_history (client_id, scope, mode, total, con_mov, sin_mov, errores, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (client_id, scope, mode, total, con_mov, sin_mov, errores, _now()),
        )


def list_query_history(client_id: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
    if client_id is None:
        rows = _connect().execute(
            "SELECT * FROM query_history ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    else:
        rows = _connect().execute(
            "SELECT * FROM query_history WHERE client_id = ? ORDER BY id DESC LIMIT ?", (client_id, limit)
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


# ---------- dashboard ----------

def dashboard_stats() -> dict[str, Any]:
    conn = _connect()
    total_clients = conn.execute("SELECT COUNT(*) c FROM clients").fetchone()["c"]
    total_processes = conn.execute("SELECT COUNT(*) c FROM processes").fetchone()["c"]
    total_actuaciones = conn.execute("SELECT COUNT(*) c FROM actuaciones").fetchone()["c"]
    notif = notification_counts()
    # "Requieren manual" pendientes = notificaciones manuales aun no cargadas a plataforma.
    needs_manual = notif["manual_pendiente"]

    by_type_rows = conn.execute(
        "SELECT COALESCE(NULLIF(client_type, ''), 'SIN TIPO') t, COUNT(*) c FROM clients GROUP BY t"
    ).fetchall()
    by_importance_rows = conn.execute(
        "SELECT importance i, COUNT(*) c FROM clients GROUP BY i"
    ).fetchall()
    # Procesos por cliente (top para el grafico de barras).
    by_client_rows = conn.execute(
        "SELECT c.name n, COUNT(p.id) cnt FROM clients c LEFT JOIN processes p ON p.client_id = c.id "
        "GROUP BY c.id ORDER BY cnt DESC LIMIT 8"
    ).fetchall()

    # Estado de la cartera (decisión): procesos con requerimiento manual pendiente vs
    # el resto (al día). Ayuda a ver cuánto trabajo manual queda abierto.
    procesos_manual = conn.execute("SELECT COUNT(DISTINCT process_id) c FROM notifications WHERE kind='manual' AND resolved=0").fetchone()["c"]

    # Actividad de consultas por día (últimos 14 días) — cadencia operativa.
    activity_rows = conn.execute(
        "SELECT substr(created_at,1,10) d, SUM(total) t, SUM(con_mov) cm FROM query_history "
        "GROUP BY d ORDER BY d DESC LIMIT 14"
    ).fetchall()
    activity = [{"day": r["d"], "total": r["t"] or 0, "con_mov": r["cm"] or 0} for r in reversed(activity_rows)]

    # Procesos más activos (por nº de actuaciones) — dónde concentrar la atención.
    top_active_rows = conn.execute(
        "SELECT p.radicado r, COUNT(a.id) cnt FROM processes p LEFT JOIN actuaciones a ON a.process_id = p.id "
        "GROUP BY p.id ORDER BY cnt DESC LIMIT 8"
    ).fetchall()

    # Salud del seguimiento: acumulado histórico de con-movimiento vs sin-movimiento.
    mov_totals = conn.execute("SELECT SUM(con_mov) cm, SUM(sin_mov) sm FROM query_history").fetchone()

    return {
        "total_clients": total_clients,
        "total_processes": total_processes,
        "needs_manual": needs_manual,
        "total_actuaciones": total_actuaciones,
        "notifications": notif,
        "procesos_al_dia": max(total_processes - procesos_manual, 0),
        "procesos_manual": procesos_manual,
        "clients_by_type": {r["t"]: r["c"] for r in by_type_rows},
        "clients_by_importance": {r["i"]: r["c"] for r in by_importance_rows},
        "processes_by_client": [{"name": r["n"], "count": r["cnt"]} for r in by_client_rows],
        "notifications_breakdown": {
            "Con movimiento": notif["con_movimiento"], "Sin movimiento": notif["sin_movimiento"],
            "Requieren manual": notif["manual_pendiente"], "Errores": conn.execute("SELECT COUNT(*) c FROM notifications WHERE kind='error'").fetchone()["c"],
        },
        "activity_by_day": activity,
        "top_active_processes": [{"radicado": r["r"], "count": r["cnt"]} for r in top_active_rows],
        "movimiento_totales": {"Con movimiento": (mov_totals["cm"] or 0), "Sin movimiento": (mov_totals["sm"] or 0)},
    }


# ---------- módulo Gestión (calendario) ----------

def gestion_month(year: int, month: int) -> dict[str, Any]:
    """Resumen de actividad por día de un mes, para marcar el calendario."""
    prefix = f"{year:04d}-{month:02d}"
    conn = _connect()
    days: dict[str, dict[str, int]] = {}
    for r in conn.execute(
        "SELECT substr(created_at,1,10) d, kind, COUNT(*) c FROM notifications WHERE substr(created_at,1,7)=? GROUP BY d, kind",
        (prefix,),
    ).fetchall():
        day = days.setdefault(r["d"], {"notif": 0, "movimiento": 0, "manual": 0, "consultas": 0})
        day["notif"] += r["c"]
        if r["kind"] == "movimiento":
            day["movimiento"] += r["c"]
        if r["kind"] == "manual":
            day["manual"] += r["c"]
    for r in conn.execute(
        "SELECT substr(created_at,1,10) d, COUNT(*) c FROM query_history WHERE substr(created_at,1,7)=? GROUP BY d",
        (prefix,),
    ).fetchall():
        day = days.setdefault(r["d"], {"notif": 0, "movimiento": 0, "manual": 0, "consultas": 0, "recordatorios": 0})
        day["consultas"] += r["c"]
    for date_str, cnt in reminders_month(year, month).items():
        day = days.setdefault(date_str, {"notif": 0, "movimiento": 0, "manual": 0, "consultas": 0, "recordatorios": 0})
        day["recordatorios"] = cnt
    return {"month": prefix, "days": days}


def gestion_day(date_str: str) -> dict[str, Any]:
    """Detalle de un día: consultas realizadas, movimientos, manuales resueltas/pendientes
    y la lista de notificaciones de ese día."""
    conn = _connect()
    notif_by_kind = {r["kind"]: r["c"] for r in conn.execute(
        "SELECT kind, COUNT(*) c FROM notifications WHERE substr(created_at,1,10)=? GROUP BY kind", (date_str,)
    ).fetchall()}
    manual_resueltas = conn.execute(
        "SELECT COUNT(*) c FROM notifications WHERE kind='manual' AND resolved=1 AND substr(resolved_at,1,10)=?", (date_str,)
    ).fetchone()["c"]
    manual_pendientes = conn.execute(
        "SELECT COUNT(*) c FROM notifications WHERE kind='manual' AND resolved=0 AND substr(created_at,1,10)=?", (date_str,)
    ).fetchone()["c"]
    consultas = [
        {"scope": r["scope"], "veces": r["c"], "total": r["t"] or 0, "con_mov": r["cm"] or 0,
         "sin_mov": r["sm"] or 0, "errores": r["e"] or 0}
        for r in conn.execute(
            "SELECT scope, COUNT(*) c, SUM(total) t, SUM(con_mov) cm, SUM(sin_mov) sm, SUM(errores) e "
            "FROM query_history WHERE substr(created_at,1,10)=? GROUP BY scope", (date_str,)
        ).fetchall()
    ]
    notifs = [_row_to_dict(r) for r in conn.execute(
        "SELECT * FROM notifications WHERE substr(created_at,1,10)=? ORDER BY id DESC", (date_str,)
    ).fetchall()]
    return {
        "date": date_str,
        "notif_by_kind": notif_by_kind,
        "manual_resueltas": manual_resueltas,
        "manual_pendientes": manual_pendientes,
        "consultas": consultas,
        "notifications": notifs,
        "reminders": list_reminders(due_date=date_str),
        "total_consultas": sum(c["veces"] for c in consultas),
    }


# ---------- recordatorios / tareas / anotaciones (Gestión) ----------

def create_reminder(
    title: str,
    notes: str,
    due_date: str,
    due_time: str,
    kind: str,
    client_id: int | None,
    process_id: int | None = None,
    source_key: str = "",
) -> dict[str, Any]:
    with _tx() as conn:
        cur = conn.execute(
            "INSERT INTO reminders (client_id, process_id, title, notes, due_date, due_time, kind, source_key, done, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
            (client_id, process_id, title.strip(), notes.strip(), due_date, due_time, kind, source_key, _now()),
        )
        row = conn.execute("SELECT * FROM reminders WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _row_to_dict(row)


def create_reminder_once(
    title: str,
    notes: str,
    due_date: str,
    due_time: str,
    kind: str,
    client_id: int | None,
    process_id: int | None,
    source_key: str,
) -> dict[str, Any] | None:
    if source_key:
        existing = _connect().execute("SELECT * FROM reminders WHERE source_key = ?", (source_key,)).fetchone()
        if existing is not None:
            return None
    return create_reminder(title, notes, due_date, due_time, kind, client_id, process_id, source_key)


def list_reminders(due_date: str | None = None, client_id: int | None = None) -> list[dict[str, Any]]:
    clauses, params = [], []
    if due_date is not None:
        clauses.append("r.due_date = ?")
        params.append(due_date)
    if client_id is not None:
        clauses.append("r.client_id = ?")
        params.append(client_id)
    where = " AND ".join(clauses) if clauses else "1 = 1"
    rows = _connect().execute(
        f"SELECT r.*, c.name client_name, p.radicado process_radicado FROM reminders r "
        f"LEFT JOIN clients c ON c.id = r.client_id "
        f"LEFT JOIN processes p ON p.id = r.process_id "
        f"WHERE {where} ORDER BY r.done, r.due_time, r.id", params
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def reminders_month(year: int, month: int) -> dict[str, int]:
    prefix = f"{year:04d}-{month:02d}"
    rows = _connect().execute(
        "SELECT due_date d, COUNT(*) c FROM reminders WHERE substr(due_date,1,7)=? GROUP BY due_date", (prefix,)
    ).fetchall()
    return {r["d"]: r["c"] for r in rows}


def set_reminder_done(reminder_id: int, done: bool) -> None:
    with _tx() as conn:
        conn.execute("UPDATE reminders SET done = ? WHERE id = ?", (1 if done else 0, reminder_id))


def delete_reminder(reminder_id: int) -> None:
    with _tx() as conn:
        conn.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))


# ---------- busqueda / reportes ----------

def global_search(query: str, limit: int = 12) -> dict[str, list[dict[str, Any]]]:
    q = query.strip().lower()
    if not q:
        return {"clients": [], "processes": []}
    like = f"%{q}%"
    conn = _connect()
    clients = [
        _row_to_dict(r)
        for r in conn.execute(
            "SELECT * FROM clients WHERE LOWER(name) LIKE ? OR LOWER(description) LIKE ? OR LOWER(client_type) LIKE ? "
            "ORDER BY name LIMIT ?",
            (like, like, like, limit),
        ).fetchall()
    ]
    process_rows = conn.execute(
        "SELECT p.*, c.name client_name FROM processes p LEFT JOIN clients c ON c.id = p.client_id "
        "WHERE LOWER(p.radicado) LIKE ? OR LOWER(p.matched_radicado) LIKE ? OR LOWER(p.demandante) LIKE ? "
        "OR LOWER(p.demandado) LIKE ? OR LOWER(c.name) LIKE ? ORDER BY p.updated_at DESC LIMIT ?",
        (like, like, like, like, like, limit),
    ).fetchall()
    processes = []
    for row in process_rows:
        data = _hydrate_process(row)
        data["client_name"] = row["client_name"]
        data["actuaciones_count"] = conn.execute(
            "SELECT COUNT(*) c FROM actuaciones WHERE process_id = ?", (row["id"],)
        ).fetchone()["c"]
        processes.append(data)
    return {"clients": clients, "processes": processes}


def client_report_snapshot(client_id: int) -> dict[str, Any]:
    client = get_client(client_id)
    processes = list_processes(client_id=client_id)
    process_ids = [p["id"] for p in processes]
    conn = _connect()
    manual = conn.execute(
        "SELECT COUNT(*) c FROM notifications WHERE client_id = ? AND kind = 'manual' AND resolved = 0",
        (client_id,),
    ).fetchone()["c"]
    unread = conn.execute(
        "SELECT COUNT(*) c FROM notifications WHERE client_id = ? AND read = 0", (client_id,)
    ).fetchone()["c"]
    history = list_query_history(client_id, limit=8)
    reminders = [
        r for r in list_reminders(client_id=client_id)
        if not bool(r.get("done"))
    ][:8]
    top = sorted(processes, key=lambda p: int(p.get("actuaciones_count") or 0), reverse=True)[:8]
    return {
        "client": client,
        "processes": processes,
        "total_processes": len(processes),
        "manual_pending": manual,
        "unread_notifications": unread,
        "history": history,
        "reminders": reminders,
        "top_processes": top,
        "generated_at": _now(),
        "process_ids": process_ids,
    }
