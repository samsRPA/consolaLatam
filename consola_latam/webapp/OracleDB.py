import asyncio
import logging
import oracledb


class OracleDB():
    def __init__(self, user: str, password: str, host: str, port: int, dbName: str, pooled: bool = False, poolClass: str = "APP") -> None:
        self._user = user
        self._password = password
        self._host = host
        self._port = port
        self._service_name = dbName
        self._pooled = pooled
        self._poolClass = poolClass
        self._pool = None
        self.logger = logging.getLogger(__name__)

    @property
    def isConnected(self) -> bool:
        return self._pool is not None

    async def connect(self) -> None:
        try:
            suffix = ":pooled" if self._pooled else ""
            dsn = f"{self._host}:{self._port}/{self._service_name}{suffix}"
            extra = {"cclass": self._poolClass, "purity": oracledb.PURITY_SELF} if self._pooled else {}
            self._pool = oracledb.create_pool_async(
                user=self._user,
                password=self._password,
                dsn=dsn,
                min=1,
                max=2,
                increment=1,
                getmode=oracledb.POOL_GETMODE_WAIT,
                homogeneous=True,
                ping_interval=10,
                timeout=15,
                max_lifetime_session=600,
                **extra,
            )
            conn = await asyncio.wait_for(self._pool.acquire(), timeout=10.0)
            await self._pool.release(conn)
            mode = f"DRCP (cclass={self._poolClass})" if self._pooled else "local"
            self.logger.info(f"🔵 Pool de Oracle creado y conexión validada exitosamente. Modo: {mode}")
        except asyncio.TimeoutError:
            self.logger.error("🔴 Timeout al validar conexión Oracle — DB no alcanzable")
            raise
        except Exception as e:
            self.logger.error(f"🔴 Error al crear el pool de Oracle: {e}")
            raise e

    async def acquireConnection(self, timeout: float = 30.0) -> oracledb.AsyncConnection:
        if not self._pool:
            raise Exception("Pool no inicializado, llama a connect primero")
        try:
            conn = await asyncio.wait_for(self._pool.acquire(), timeout=timeout)
            return conn
        except asyncio.TimeoutError:
            self.logger.warning("🟡 Timeout en pool Oracle — intentando reconectar...")
            try:
                await self.closeConnection(force=True)
                await self.connect()
                conn = await asyncio.wait_for(self._pool.acquire(), timeout=timeout)
                return conn
            except Exception as e:
                raise Exception(f"🔴 Timeout y reconexión fallida: {e}")

    async def releaseConnection(self, conn: oracledb.AsyncConnection) -> None:
        try:
            await asyncio.wait_for(conn.rollback(), timeout=5.0)
        except Exception:
            pass
        finally:
            try:
                await asyncio.wait_for(self._pool.release(conn), timeout=5.0)
            except Exception:
                pass

    async def commit(self, conn: oracledb.AsyncConnection) -> None:
        try:
            await conn.commit()
        except Exception as e:
            raise e

    async def closeConnection(self, force: bool = False) -> None:
        if self._pool:
            try:
                await asyncio.wait_for(self._pool.close(force=force), timeout=120.0)
            except Exception:
                pass
            finally:
                self._pool = None
            self.logger.info("🔌 Conexión a Oracle cerrada correctamente.")
