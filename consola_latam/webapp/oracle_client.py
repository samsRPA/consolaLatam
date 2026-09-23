"""Cliente de la base de datos Oracle de facturacion (LITIPDB).

Al crear un cliente en la consola, el usuario ingresa un unico "Cliente ID": el
CLIENTE_ID de Oracle del cliente padre de facturacion (el mismo id que ya se usaba para
identificar al cliente ante el bot externo -- ver app.py:api_create_client). Este modulo
valida que ese id exista en la tabla CLIENTES, trae su nombre oficial (f_nombre_cliente,
usado como external_username en vez de pedirselo al usuario) y, via la jerarquia CONNECT
BY sobre CLIENTE_PADRE, todos sus clientes hijos activos en facturacion -- guardados en
la base local con db.set_client_children.

Tambien permite asociar clientes a un proceso ya incluido via el bot de Ecuador
(PROCESOS_CLIENTES), reutilizando el mismo Cliente ID/jerarquia -- ver
app.py:api_add_process_oracle_clientes.

Las credenciales salen de variables de entorno (.env / docker-compose): DB_USERNAME,
DB_PASSWORD, DB_HOST, DB_PORT, DB_NAME, DB_POOLED."""

from __future__ import annotations

import os

from .OracleDB import OracleDB

# El nombre oficial se busca aparte de la jerarquia (y no se toma de ahi) porque sirve
# tambien como chequeo de existencia independiente del estado de facturacion: si el
# cliente padre no factura ('S'/'C'), no apareceria en _HIERARCHY_QUERY aunque SI exista.
_NAME_QUERY = "SELECT f_nombre_cliente(cliente_id) FROM CLIENTES WHERE CLIENTE_ID = :cliente_id"

# Trae al cliente padre y todos sus descendientes (CONNECT BY sobre CLIENTE_PADRE) que
# sigan activos en facturacion ('S' = factura, 'C' = ¿congelado? -- estados heredados del
# sistema de facturacion externo, no definidos aqui).
_HIERARCHY_QUERY = """
    SELECT f_nombre_cliente(cliente_id), cliente_id
      FROM CLIENTES
     WHERE CLIENTE_ESTADO_FACTURACION IN ('S', 'C')
       AND CLIENTE_ID IN (
            SELECT CLIENTE_ID
              FROM CLIENTES
           CONNECT BY PRIOR CLIENTE_ID = CLIENTE_PADRE
             START WITH CLIENTE_ID = :cliente_id
           )
     ORDER BY cliente_id
"""

_LIST_PROCESO_CLIENTES_QUERY = """
    SELECT cliente_id, f_nombre_cliente(cliente_id)
      FROM PROCESOS_CLIENTES
     WHERE proceso_id = :proceso_id
     ORDER BY cliente_id
"""

_INSERT_PROCESO_CLIENTE_STMT = """
    INSERT INTO PROCESOS_CLIENTES (PROCESO_ID, CLIENTE_ID, USUARIO)
    VALUES (:proceso_id, :cliente_id, :usuario)
"""

# Usuario fijo con el que esta consola inserta en PROCESOS_CLIENTES, para distinguir
# estas asociaciones de las hechas desde otros sistemas.
PROCESOS_CLIENTES_USUARIO = "INCLUSIONES_APP_LATAM"


class ClienteIdNoExisteError(ValueError):
    """El Cliente ID (Oracle) ingresado no existe en la tabla CLIENTES."""


class OracleUnavailableError(RuntimeError):
    """No se pudo conectar o consultar la base de datos Oracle."""


DB = OracleDB(
    user=os.environ.get("DB_USERNAME", ""),
    password=os.environ.get("DB_PASSWORD", ""),
    host=os.environ.get("DB_HOST", ""),
    port=int(os.environ.get("DB_PORT") or 1521),
    dbName=os.environ.get("DB_NAME", ""),
    pooled=os.environ.get("DB_POOLED", "").strip().lower() in ("1", "true", "s", "si", "yes"),
)


async def ensure_connected() -> None:
    if not DB.isConnected:
        await DB.connect()


async def close() -> None:
    await DB.closeConnection()


async def _acquire():
    """Asegura la conexion y adquiere una conexion del pool, traduciendo cualquier falla
    a OracleUnavailableError (usado por todas las funciones de este modulo)."""
    try:
        await ensure_connected()
        return await DB.acquireConnection()
    except Exception as exc:
        raise OracleUnavailableError(f"No se pudo conectar a la base de datos Oracle: {exc}") from exc


async def fetch_client_hierarchy(cliente_id: int) -> tuple[str, list[dict]]:
    """Devuelve (nombre_oficial, hijos) para `cliente_id`, con hijos = [{"cliente_id":
    int, "nombre": str}, ...] -- SOLO descendientes, nunca el propio cliente padre
    (_HIERARCHY_QUERY lo trae de vuelta porque el START WITH de Oracle se incluye a si
    mismo, se descarta aqui antes de devolver).

    Lanza ClienteIdNoExisteError si `cliente_id` no existe en Oracle, u
    OracleUnavailableError si no se pudo conectar/consultar la base."""
    conn = await _acquire()
    try:
        cursor = conn.cursor()
        await cursor.execute(_NAME_QUERY, {"cliente_id": cliente_id})
        row = await cursor.fetchone()
        if row is None:
            raise ClienteIdNoExisteError(f"El Cliente ID {cliente_id} no existe en Oracle")
        nombre = (row[0] or "").strip()
        await cursor.execute(_HIERARCHY_QUERY, {"cliente_id": cliente_id})
        rows = await cursor.fetchall()
        hijos = [
            {"nombre": (n or "").strip(), "cliente_id": int(hijo_id)}
            for n, hijo_id in rows if int(hijo_id) != cliente_id
        ]
        return nombre, hijos
    except ClienteIdNoExisteError:
        raise
    except Exception as exc:
        raise OracleUnavailableError(f"Error consultando la base de datos Oracle: {exc}") from exc
    finally:
        await DB.releaseConnection(conn)


async def list_proceso_clientes(proceso_id: int) -> list[dict]:
    """Clientes ya asociados a `proceso_id` en PROCESOS_CLIENTES."""
    conn = await _acquire()
    try:
        cursor = conn.cursor()
        await cursor.execute(_LIST_PROCESO_CLIENTES_QUERY, {"proceso_id": proceso_id})
        rows = await cursor.fetchall()
        return [{"cliente_id": int(cid), "nombre": (n or "").strip()} for cid, n in rows]
    except Exception as exc:
        raise OracleUnavailableError(f"Error consultando la base de datos Oracle: {exc}") from exc
    finally:
        await DB.releaseConnection(conn)


async def agregar_clientes_a_proceso(proceso_id: int, clientes: list[dict]) -> dict:
    """Inserta en PROCESOS_CLIENTES un registro por cada cliente de `clientes`
    ({"cliente_id", "nombre"}) que TODAVIA no este asociado a `proceso_id` -- los que ya
    esten (mismo proceso_id + cliente_id) se omiten sin error, nunca se insertan dos
    veces. Devuelve {"agregados": [cliente_id, ...], "omitidos": [cliente_id, ...]}."""
    conn = await _acquire()
    try:
        cursor = conn.cursor()
        await cursor.execute(_LIST_PROCESO_CLIENTES_QUERY, {"proceso_id": proceso_id})
        existentes = {int(cid) for cid, _ in await cursor.fetchall()}
        agregados: list[int] = []
        omitidos: list[int] = []
        for c in clientes:
            cliente_id = int(c["cliente_id"])
            if cliente_id in existentes:
                omitidos.append(cliente_id)
                continue
            await cursor.execute(
                _INSERT_PROCESO_CLIENTE_STMT,
                {"proceso_id": proceso_id, "cliente_id": cliente_id, "usuario": PROCESOS_CLIENTES_USUARIO},
            )
            existentes.add(cliente_id)
            agregados.append(cliente_id)
        await DB.commit(conn)
        return {"agregados": agregados, "omitidos": omitidos}
    except Exception as exc:
        raise OracleUnavailableError(f"Error insertando en la base de datos Oracle: {exc}") from exc
    finally:
        await DB.releaseConnection(conn)
