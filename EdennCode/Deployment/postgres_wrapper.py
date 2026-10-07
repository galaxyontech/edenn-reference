from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Mapping, Sequence

import psycopg2
from psycopg2 import sql
from psycopg2.extras import Json, RealDictCursor, execute_values

from EdennCode.env import load_env


def _optional_env(key: str) -> str | None:
    value = os.getenv(key)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _required_env(key: str) -> str:
    value = _optional_env(key)
    if value is None:
        raise RuntimeError(
            f"Environment variable '{key}' must be set when DATABASE_URL is not provided."
        )
    return value


def _qualified_identifier(name: str) -> sql.Composed:
    parts = [part.strip() for part in name.split(".") if part.strip()]
    if not parts:
        raise ValueError("Qualified name must not be empty.")
    return sql.SQL(".").join(sql.Identifier(part) for part in parts)


def _information_schema_parts(name: str, *, default_schema: str = "public") -> tuple[str, str]:
    parts = [part.strip() for part in name.split(".") if part.strip()]
    if len(parts) == 1:
        return default_schema, parts[0]
    if len(parts) == 2:
        return parts[0], parts[1]
    raise ValueError("Table name must be unqualified or schema-qualified.")


def _adapt_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return Json(value)
    return value


def _adapt_params(params: Sequence[Any] | None) -> list[Any] | None:
    if params is None:
        return None
    return [_adapt_value(value) for value in params]


def _rows_to_dicts(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def _returning_clause(returning: Sequence[str] | str | None) -> sql.SQL:
    if returning is None:
        return sql.SQL("")
    if isinstance(returning, str):
        if returning.strip() == "*":
            target = sql.SQL("*")
        else:
            target = sql.Identifier(returning.strip())
    else:
        target = sql.SQL(", ").join(sql.Identifier(column) for column in returning)
    return sql.SQL(" RETURNING {}").format(target)


def _select_target(columns: Sequence[str] | str | None) -> sql.SQL:
    if columns is None:
        return sql.SQL("*")
    if isinstance(columns, str):
        if columns.strip() == "*":
            return sql.SQL("*")
        return sql.Identifier(columns.strip())
    return sql.SQL(", ").join(sql.Identifier(column) for column in columns)


def _validate_where_clause(where_clause: str) -> str:
    clause = where_clause.strip()
    if not clause:
        raise ValueError("An explicit WHERE clause is required for this operation.")
    return clause


@dataclass(frozen=True)
class PostgresConnectionConfig:
    dsn: str | None = None
    host: str | None = None
    port: int = 5432
    database: str | None = None
    user: str | None = None
    password: str | None = None
    sslmode: str = "require"
    connect_timeout: int = 10
    application_name: str = "edenn-postgres-wrapper"

    @classmethod
    def from_env(cls) -> "PostgresConnectionConfig":
        load_env()

        connect_timeout = int(os.getenv("PGCONNECT_TIMEOUT", "10"))
        application_name = (
            os.getenv("PGAPPNAME", "edenn-postgres-wrapper").strip()
            or "edenn-postgres-wrapper"
        )
        dsn = _optional_env("DATABASE_URL")
        if dsn:
            return cls(
                dsn=dsn,
                sslmode=os.getenv("PGSSLMODE", "require").strip() or "require",
                connect_timeout=connect_timeout,
                application_name=application_name,
            )

        return cls(
            host=_required_env("PGHOST"),
            port=int(os.getenv("PGPORT", "5432")),
            database=_required_env("PGDATABASE"),
            user=_required_env("PGUSER"),
            password=_optional_env("PGPASSWORD"),
            sslmode=os.getenv("PGSSLMODE", "require").strip() or "require",
            connect_timeout=connect_timeout,
            application_name=application_name,
        )

    @classmethod
    def from_user_db_env(cls) -> "PostgresConnectionConfig":
        """Build config from DB_HOST / DB_PORT / DB_USER / DB_PASSWORD / DB_NAME env vars."""
        load_env()
        return cls(
            host=_required_env("DB_HOST"),
            port=int(os.getenv("DB_PORT", "5432")),
            database=_required_env("DB_NAME"),
            user=_required_env("DB_USER"),
            password=_optional_env("DB_PASSWORD"),
            sslmode="prefer",
            connect_timeout=10,
        )

    def keepalive_kwargs(self) -> dict[str, Any]:
        """TCP keepalive settings applied to every connection.

        A half-open connection (e.g. dropped during a container replacement)
        otherwise blocks a synchronous read indefinitely — and the async v2
        workers run every repository call on the event loop, so one dead socket
        freezes the whole process, consumers and lease heartbeats included.
        With these settings a dead peer is detected in roughly a minute and the
        read fails; the consumer loop guard then recovers.
        """
        return {
            "keepalives": 1,
            "keepalives_idle": 30,
            "keepalives_interval": 10,
            "keepalives_count": 3,
        }

    def connect_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "host": self.host,
            "port": self.port,
            "dbname": self.database,
            "user": self.user,
            "sslmode": self.sslmode,
            "connect_timeout": self.connect_timeout,
            "application_name": self.application_name,
            **self.keepalive_kwargs(),
        }
        if self.password is not None:
            kwargs["password"] = self.password
        return kwargs


class PostgresClient:
    def __init__(
        self,
        config: PostgresConnectionConfig,
        *,
        connection: Any | None = None,
        release: "Callable[[Any], None] | None" = None,
    ) -> None:
        self._config = config
        # When `connection` is supplied the client borrows an existing (pooled)
        # connection instead of opening its own, and `release` returns it to the
        # owner on close() rather than physically closing it. This lets callers
        # that do many short operations reuse connections without paying a fresh
        # TLS handshake each time, while leaving the default open/close-per-client
        # behavior unchanged for every existing caller.
        self._connection: Any | None = connection
        self._release = release
        self._transaction_depth = 0

    @classmethod
    def from_env(cls) -> "PostgresClient":
        return cls(PostgresConnectionConfig.from_env())

    @classmethod
    def from_user_db_env(cls) -> "PostgresClient":
        return cls(PostgresConnectionConfig.from_user_db_env())

    def connect(self) -> Any:
        if self._connection is not None and not getattr(self._connection, "closed", False):
            return self._connection

        if self._config.dsn:
            self._connection = psycopg2.connect(
                self._config.dsn,
                connect_timeout=self._config.connect_timeout,
                application_name=self._config.application_name,
                **self._config.keepalive_kwargs(),
            )
        else:
            self._connection = psycopg2.connect(**self._config.connect_kwargs())

        self._connection.autocommit = False
        # Register pgvector adapter so list[float] / numpy arrays serialize as
        # vector(N) values when inserting into pgvector columns. Skip silently
        # if pgvector isn't installed or the database lacks the extension —
        # callers that don't touch vector columns must keep working.
        try:
            from pgvector.psycopg2 import register_vector
            register_vector(self._connection)
        except Exception:
            pass
        return self._connection

    def close(self) -> None:
        connection = self._connection
        if connection is None:
            return
        if self._release is not None:
            # Borrowed from a pool: hand it back instead of closing. The owner
            # decides whether to reuse or discard it.
            self._connection = None
            self._release(connection)
            return
        if not getattr(connection, "closed", False):
            connection.close()

    def __enter__(self) -> "PostgresClient":
        self.connect()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if exc and self._connection is not None and not getattr(self._connection, "closed", False):
            self._connection.rollback()
        self.close()

    @contextmanager
    def transaction(self) -> Iterator["PostgresClient"]:
        """Run multiple wrapper calls as one database transaction.

        `run_sql` and the higher-level helper methods normally commit each
        statement so simple callers stay safe by default. This context switches
        those helpers into caller-managed commit mode for durable multi-table
        handoffs such as async-pipeline stage completion. Nested transaction
        contexts share the outer transaction; only the outermost context commits
        or rolls back.
        """
        connection = self.connect()
        outermost = self._transaction_depth == 0
        self._transaction_depth += 1
        try:
            yield self
        except Exception:
            self._transaction_depth -= 1
            if outermost:
                connection.rollback()
            raise
        else:
            self._transaction_depth -= 1
            if outermost:
                connection.commit()

    @contextmanager
    def _cursor(self) -> Iterator[Any]:
        cursor = self.connect().cursor(cursor_factory=RealDictCursor)
        try:
            yield cursor
        finally:
            cursor.close()

    def _execute(
        self,
        statement: str | sql.Composable,
        *,
        params: Sequence[Any] | None = None,
        fetch: bool = False,
        fetch_one: bool = False,
    ) -> tuple[list[dict[str, Any]] | dict[str, Any] | None, int]:
        connection = self.connect()
        try:
            with self._cursor() as cursor:
                cursor.execute(statement, _adapt_params(params))
                rowcount = cursor.rowcount

                result: list[dict[str, Any]] | dict[str, Any] | None = None
                if cursor.description:
                    if fetch_one:
                        row = cursor.fetchone()
                        result = dict(row) if row is not None else None
                    elif fetch:
                        result = _rows_to_dicts(cursor.fetchall())

            if self._transaction_depth == 0:
                connection.commit()
            return result, rowcount
        except Exception:
            if self._transaction_depth == 0:
                connection.rollback()
            raise

    def quick_check(self) -> dict[str, Any]:
        statement = """
            SELECT
                current_database() AS database_name,
                current_user AS user_name,
                current_schema() AS schema_name,
                inet_server_addr()::text AS server_address,
                inet_server_port() AS server_port,
                version() AS server_version,
                now() AS connected_at
        """
        result, _ = self._execute(statement, fetch_one=True)
        return result or {}

    def list_tables(self, *, schema: str = "public") -> list[dict[str, Any]]:
        statement = """
            SELECT table_schema, table_name
            FROM information_schema.tables
            WHERE table_schema = %s
              AND table_type = 'BASE TABLE'
            ORDER BY table_name
        """
        result, _ = self._execute(statement, params=[schema], fetch=True)
        return result or []

    def describe_table(self, table_name: str) -> list[dict[str, Any]]:
        schema_name, relation_name = _information_schema_parts(table_name)
        statement = """
            SELECT
                ordinal_position,
                column_name,
                data_type,
                udt_name,
                is_nullable,
                column_default
            FROM information_schema.columns
            WHERE table_schema = %s
              AND table_name = %s
            ORDER BY ordinal_position
        """
        result, _ = self._execute(
            statement,
            params=[schema_name, relation_name],
            fetch=True,
        )
        return result or []

    def create_table(
        self,
        table_name: str,
        columns: Mapping[str, str],
        *,
        if_not_exists: bool = True,
    ) -> None:
        if not columns:
            raise ValueError("At least one column definition is required.")

        column_defs = [
            sql.SQL("{} {}").format(sql.Identifier(column_name), sql.SQL(definition))
            for column_name, definition in columns.items()
        ]
        statement = sql.SQL("CREATE TABLE {prefix}{table} ({columns})").format(
            prefix=sql.SQL("IF NOT EXISTS ") if if_not_exists else sql.SQL(""),
            table=_qualified_identifier(table_name),
            columns=sql.SQL(", ").join(column_defs),
        )
        self._execute(statement)

    def add_columns(
        self,
        table_name: str,
        columns: Mapping[str, str],
        *,
        if_not_exists: bool = True,
    ) -> None:
        if not columns:
            raise ValueError("At least one column definition is required.")

        clauses = [
            sql.SQL("ADD COLUMN {prefix}{column} {definition}").format(
                prefix=sql.SQL("IF NOT EXISTS ") if if_not_exists else sql.SQL(""),
                column=sql.Identifier(column_name),
                definition=sql.SQL(definition),
            )
            for column_name, definition in columns.items()
        ]
        statement = sql.SQL("ALTER TABLE {table} {clauses}").format(
            table=_qualified_identifier(table_name),
            clauses=sql.SQL(", ").join(clauses),
        )
        self._execute(statement)

    def drop_columns(
        self,
        table_name: str,
        columns: Sequence[str],
        *,
        if_exists: bool = True,
        cascade: bool = False,
    ) -> None:
        if not columns:
            raise ValueError("At least one column name is required.")

        clauses = [
            sql.SQL("DROP COLUMN {prefix}{column}{cascade}").format(
                prefix=sql.SQL("IF EXISTS ") if if_exists else sql.SQL(""),
                column=sql.Identifier(column_name),
                cascade=sql.SQL(" CASCADE") if cascade else sql.SQL(""),
            )
            for column_name in columns
        ]
        statement = sql.SQL("ALTER TABLE {table} {clauses}").format(
            table=_qualified_identifier(table_name),
            clauses=sql.SQL(", ").join(clauses),
        )
        self._execute(statement)

    def insert_row(
        self,
        table_name: str,
        values: Mapping[str, Any],
        *,
        returning: Sequence[str] | str | None = None,
    ) -> dict[str, Any] | int | None:
        if not values:
            raise ValueError("At least one value is required for insert.")

        columns = list(values.keys())
        params = list(values.values())
        placeholders = sql.SQL(", ").join(sql.Placeholder() for _ in columns)

        statement = sql.SQL(
            "INSERT INTO {table} ({columns}) VALUES ({placeholders}){returning}"
        ).format(
            table=_qualified_identifier(table_name),
            columns=sql.SQL(", ").join(sql.Identifier(column) for column in columns),
            placeholders=placeholders,
            returning=_returning_clause(returning),
        )
        result, rowcount = self._execute(
            statement,
            params=params,
            fetch_one=returning is not None,
        )
        return result if returning is not None else rowcount

    def update_rows(
        self,
        table_name: str,
        values: Mapping[str, Any],
        *,
        where_clause: str,
        where_params: Sequence[Any] | None = None,
        returning: Sequence[str] | str | None = None,
    ) -> list[dict[str, Any]] | int:
        if not values:
            raise ValueError("At least one value is required for update.")

        set_clauses = [
            sql.SQL("{} = {}").format(sql.Identifier(column), sql.Placeholder())
            for column in values.keys()
        ]
        params = list(values.values()) + list(where_params or [])
        statement = sql.SQL("UPDATE {table} SET {set_clause} WHERE {where}{returning}").format(
            table=_qualified_identifier(table_name),
            set_clause=sql.SQL(", ").join(set_clauses),
            where=sql.SQL(_validate_where_clause(where_clause)),
            returning=_returning_clause(returning),
        )
        result, rowcount = self._execute(
            statement,
            params=params,
            fetch=returning is not None,
        )
        return result or [] if returning is not None else rowcount

    def delete_rows(
        self,
        table_name: str,
        *,
        where_clause: str,
        where_params: Sequence[Any] | None = None,
        returning: Sequence[str] | str | None = None,
    ) -> list[dict[str, Any]] | int:
        statement = sql.SQL("DELETE FROM {table} WHERE {where}{returning}").format(
            table=_qualified_identifier(table_name),
            where=sql.SQL(_validate_where_clause(where_clause)),
            returning=_returning_clause(returning),
        )
        result, rowcount = self._execute(
            statement,
            params=where_params,
            fetch=returning is not None,
        )
        return result or [] if returning is not None else rowcount

    def fetch_rows(
        self,
        table_name: str,
        *,
        columns: Sequence[str] | str | None = None,
        where_clause: str | None = None,
        where_params: Sequence[Any] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        params = list(where_params or [])
        suffix = sql.SQL("")

        if limit is not None:
            if limit < 1:
                raise ValueError("Limit must be greater than zero.")
            suffix = sql.SQL(" LIMIT %s")
            params.append(limit)

        if where_clause:
            statement = sql.SQL("SELECT {columns} FROM {table} WHERE {where}{suffix}").format(
                columns=_select_target(columns),
                table=_qualified_identifier(table_name),
                where=sql.SQL(where_clause.strip()),
                suffix=suffix,
            )
        else:
            statement = sql.SQL("SELECT {columns} FROM {table}{suffix}").format(
                columns=_select_target(columns),
                table=_qualified_identifier(table_name),
                suffix=suffix,
            )

        result, _ = self._execute(statement, params=params, fetch=True)
        return result or []

    def fetch_first_rows(self, table_name: str, *, limit: int = 5) -> list[dict[str, Any]]:
        return self.fetch_rows(table_name, limit=limit)

    def run_sql(self, statement: str, *, params: Sequence[Any] | None = None) -> list[dict[str, Any]] | int:
        result, rowcount = self._execute(statement, params=params, fetch=True)
        return result if result is not None else rowcount

    def bulk_insert_rows(
        self,
        table_name: str,
        rows: Sequence[Mapping[str, Any]],
        *,
        page_size: int = 500,
    ) -> int:
        """Insert many rows in a single batched statement via execute_values."""
        if not rows:
            return 0
        columns = list(rows[0].keys())
        values = [
            tuple(_adapt_value(row[col]) for col in columns)
            for row in rows
        ]
        stmt = sql.SQL("INSERT INTO {table} ({columns}) VALUES %s").format(
            table=_qualified_identifier(table_name),
            columns=sql.SQL(", ").join(sql.Identifier(col) for col in columns),
        )
        connection = self.connect()
        try:
            with connection.cursor() as cur:
                execute_values(cur, stmt.as_string(connection), values, page_size=page_size)
                rowcount = cur.rowcount
            if self._transaction_depth == 0:
                connection.commit()
            return rowcount
        except Exception:
            if self._transaction_depth == 0:
                connection.rollback()
            raise


class PostgresConnectionPool:
    """Reuse a small set of long-lived connections for one connection config.

    Callers such as the async pipeline v2 workers run many short repository and
    queue operations per task. With the default `PostgresClient.from_env`
    factory, each operation opens a fresh connection and pays a TLS handshake,
    then closes it. This pool hands out `PostgresClient` instances bound to a
    borrowed connection that is returned to the pool on `close()` instead of
    being torn down. `.client` is a drop-in replacement for the
    `client_factory` callables the queue/repository already accept.

    Sizing: async v2 workers run on a single asyncio event loop where each borrow
    is acquired and released inside one synchronous method (no `await` between
    `getconn` and `putconn`), so borrows do not overlap and a handful of
    connections covers high provider concurrency. `maxconn` is a safety ceiling,
    not a throughput target. The underlying pool is thread-safe.
    """

    def __init__(
        self,
        config: PostgresConnectionConfig,
        *,
        minconn: int = 1,
        maxconn: int = 8,
    ) -> None:
        from psycopg2 import pool as _pg_pool

        if maxconn < 1:
            raise ValueError("maxconn must be at least 1.")
        minconn = max(0, min(minconn, maxconn))
        self._config = config
        if config.dsn:
            self._pool = _pg_pool.ThreadedConnectionPool(
                minconn,
                maxconn,
                config.dsn,
                connect_timeout=config.connect_timeout,
                application_name=config.application_name,
                **config.keepalive_kwargs(),
            )
        else:
            self._pool = _pg_pool.ThreadedConnectionPool(
                minconn, maxconn, **config.connect_kwargs()
            )

    def _prepare(self, connection: Any) -> Any:
        """Apply one-time per-connection setup mirroring PostgresClient.connect."""

        connection.autocommit = False
        if not getattr(connection, "_edenn_prepared", False):
            try:
                from pgvector.psycopg2 import register_vector

                register_vector(connection)
            except Exception:
                pass
            try:
                connection._edenn_prepared = True
            except Exception:
                pass
        return connection

    def client(self) -> PostgresClient:
        """Borrow a connection and wrap it in a PostgresClient."""

        connection = self._pool.getconn()
        if getattr(connection, "closed", False):
            # Discard a dead connection and try once more for a live one.
            self._pool.putconn(connection, close=True)
            connection = self._pool.getconn()
        self._prepare(connection)
        return PostgresClient(self._config, connection=connection, release=self._release)

    def _release(self, connection: Any) -> None:
        try:
            self._pool.putconn(connection, close=bool(getattr(connection, "closed", False)))
        except Exception:
            # Never let connection bookkeeping break a worker; a lost connection
            # is recreated on the next borrow.
            pass

    def closeall(self) -> None:
        self._pool.closeall()


__all__ = ["PostgresClient", "PostgresConnectionConfig", "PostgresConnectionPool"]
