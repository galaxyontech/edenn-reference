import os
import unittest
from unittest.mock import patch

from psycopg2 import sql
from psycopg2.extras import Json, RealDictCursor

from EdennCode.Deployment.postgres_wrapper import (
    PostgresClient,
    PostgresConnectionConfig,
)


class _FakeCursor:
    def __init__(self, connection) -> None:
        self._connection = connection
        self.description = None
        self.rowcount = -1
        self._rows = []

    def execute(self, statement, params=None) -> None:
        self._connection.executed.append((statement, params))
        response = self._connection.responses.pop(0) if self._connection.responses else {}
        self.description = response.get("description")
        self.rowcount = response.get("rowcount", -1)
        self._rows = response.get("rows", [])

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def close(self) -> None:
        return None


class _FakeConnection:
    def __init__(self, responses=None) -> None:
        self.responses = list(responses or [])
        self.executed = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = 0
        self.autocommit = True
        self.cursor_factory = None

    def cursor(self, cursor_factory=None):
        self.cursor_factory = cursor_factory
        return _FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = 1


class PostgresConnectionConfigTests(unittest.TestCase):
    def test_from_env_prefers_database_url(self) -> None:
        with patch("EdennCode.Deployment.postgres_wrapper.load_env", return_value=False):
            with patch.dict(
                os.environ,
                {
                    "DATABASE_URL": "postgresql://demo:REDACTED@example.com:5432/telemetry?sslmode=require",
                    "PGHOST": "ignored",
                    "PGDATABASE": "ignored",
                    "PGUSER": "ignored",
                },
                clear=True,
            ):
                config = PostgresConnectionConfig.from_env()

        self.assertEqual(
            config.dsn,
            "postgresql://demo:REDACTED@example.com:5432/telemetry?sslmode=require",
        )
        self.assertIsNone(config.host)

    def test_from_env_builds_pg_kwargs_without_database_url(self) -> None:
        with patch("EdennCode.Deployment.postgres_wrapper.load_env", return_value=False):
            with patch.dict(
                os.environ,
                {
                    "PGHOST": "example.com",
                    "PGPORT": "6432",
                    "PGDATABASE": "telemetry",
                    "PGUSER": "dbadmin",
                    "PGPASSWORD": "secret",
                    "PGSSLMODE": "require",
                },
                clear=True,
            ):
                config = PostgresConnectionConfig.from_env()

        self.assertIsNone(config.dsn)
        self.assertEqual(config.connect_kwargs()["host"], "example.com")
        self.assertEqual(config.connect_kwargs()["port"], 6432)
        self.assertEqual(config.connect_kwargs()["dbname"], "telemetry")


class PostgresClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self._register_vector_patch = patch("pgvector.psycopg2.register_vector")
        self.register_vector = self._register_vector_patch.start()
        self.addCleanup(self._register_vector_patch.stop)

    def test_connect_registers_pgvector_adapter(self) -> None:
        """Keep vector adapter registration covered without exercising pgvector SQL."""

        connection = _FakeConnection()

        with patch("EdennCode.Deployment.postgres_wrapper.psycopg2.connect", return_value=connection):
            client = PostgresClient(PostgresConnectionConfig(dsn="postgresql://demo"))
            client.connect()

        self.register_vector.assert_called_once_with(connection)

    def test_quick_check_returns_server_metadata(self) -> None:
        """Verify quick_check returns server metadata from the wrapper cursor."""

        connection = _FakeConnection(
            responses=[
                {
                    "description": [("database_name",)],
                    "rows": [{"database_name": "telemetry", "user_name": "dbadmin"}],
                    "rowcount": 1,
                }
            ]
        )

        with patch("EdennCode.Deployment.postgres_wrapper.psycopg2.connect", return_value=connection):
            client = PostgresClient(PostgresConnectionConfig(dsn="postgresql://demo"))
            result = client.quick_check()

        self.assertEqual(result["database_name"], "telemetry")
        self.assertEqual(result["user_name"], "dbadmin")
        self.assertEqual(connection.commits, 1)
        self.assertEqual(connection.cursor_factory, RealDictCursor)

    def test_create_table_executes_composed_sql(self) -> None:
        """Verify create_table builds a psycopg2 composable SQL statement."""

        connection = _FakeConnection(responses=[{"rowcount": -1}])

        with patch("EdennCode.Deployment.postgres_wrapper.psycopg2.connect", return_value=connection):
            client = PostgresClient(PostgresConnectionConfig(dsn="postgresql://demo"))
            client.create_table(
                "public.telemetry_events",
                {
                    "id": "BIGSERIAL PRIMARY KEY",
                    "event_name": "TEXT NOT NULL",
                },
            )

        statement, params = connection.executed[0]
        self.assertIsInstance(statement, sql.Composable)
        self.assertIsNone(params)
        self.assertEqual(connection.commits, 1)

    def test_insert_row_wraps_json_values(self) -> None:
        """Verify insert_row adapts dict/list values through psycopg2 Json."""

        connection = _FakeConnection(responses=[{"rowcount": 1}])

        with patch("EdennCode.Deployment.postgres_wrapper.psycopg2.connect", return_value=connection):
            client = PostgresClient(PostgresConnectionConfig(dsn="postgresql://demo"))
            rowcount = client.insert_row(
                "telemetry_events",
                {"event_name": "demo", "payload": {"ok": True}},
            )

        self.assertEqual(rowcount, 1)
        _, params = connection.executed[0]
        self.assertEqual(params[0], "demo")
        self.assertIsInstance(params[1], Json)

    def test_describe_table_uses_schema_and_table_name(self) -> None:
        """Verify describe_table splits schema-qualified names for information_schema."""

        connection = _FakeConnection(
            responses=[{"description": [("column_name",)], "rows": [], "rowcount": 0}]
        )

        with patch("EdennCode.Deployment.postgres_wrapper.psycopg2.connect", return_value=connection):
            client = PostgresClient(PostgresConnectionConfig(dsn="postgresql://demo"))
            client.describe_table("analytics.telemetry_events")

        _, params = connection.executed[0]
        self.assertEqual(params, ["analytics", "telemetry_events"])

    def test_fetch_first_rows_returns_rows(self) -> None:
        """Verify fetch_first_rows returns RealDict-style rows as plain dicts."""

        connection = _FakeConnection(
            responses=[
                {
                    "description": [("id",)],
                    "rows": [{"id": 1}, {"id": 2}],
                    "rowcount": 2,
                }
            ]
        )

        with patch("EdennCode.Deployment.postgres_wrapper.psycopg2.connect", return_value=connection):
            client = PostgresClient(PostgresConnectionConfig(dsn="postgresql://demo"))
            rows = client.fetch_first_rows("telemetry_events")

        self.assertEqual(rows, [{"id": 1}, {"id": 2}])

    def test_update_rows_requires_where_clause(self) -> None:
        """Verify update_rows rejects empty WHERE clauses before connecting."""

        client = PostgresClient(PostgresConnectionConfig(dsn="postgresql://demo"))

        with self.assertRaises(ValueError):
            client.update_rows("telemetry_events", {"event_name": "renamed"}, where_clause="  ")

    def test_delete_rows_with_returning_returns_rows(self) -> None:
        """Verify delete_rows returns rows when RETURNING is requested."""

        connection = _FakeConnection(
            responses=[
                {
                    "description": [("id",)],
                    "rows": [{"id": 7}],
                    "rowcount": 1,
                }
            ]
        )

        with patch("EdennCode.Deployment.postgres_wrapper.psycopg2.connect", return_value=connection):
            client = PostgresClient(PostgresConnectionConfig(dsn="postgresql://demo"))
            rows = client.delete_rows(
                "telemetry_events",
                where_clause="id = %s",
                where_params=[7],
                returning="*",
            )

        self.assertEqual(rows, [{"id": 7}])

    def test_run_sql_returns_rowcount_for_non_select(self) -> None:
        """Verify run_sql returns rowcount when the statement has no result rows."""

        connection = _FakeConnection(responses=[{"rowcount": 3}])

        with patch("EdennCode.Deployment.postgres_wrapper.psycopg2.connect", return_value=connection):
            client = PostgresClient(PostgresConnectionConfig(dsn="postgresql://demo"))
            rowcount = client.run_sql("DELETE FROM telemetry_events WHERE created_at < now() - interval '1 day'")

        self.assertEqual(rowcount, 3)


class BorrowedConnectionTests(unittest.TestCase):
    """Pooling relies on a client returning, not closing, a borrowed connection."""

    def test_connect_returns_injected_connection_without_opening(self) -> None:
        connection = _FakeConnection()
        client = PostgresClient(
            PostgresConnectionConfig(dsn="postgresql://demo"),
            connection=connection,
            release=lambda _conn: None,
        )

        with patch(
            "EdennCode.Deployment.postgres_wrapper.psycopg2.connect"
        ) as connect_mock:
            self.assertIs(client.connect(), connection)
            connect_mock.assert_not_called()

    def test_close_releases_borrowed_connection_instead_of_closing(self) -> None:
        connection = _FakeConnection()
        released: list[object] = []
        client = PostgresClient(
            PostgresConnectionConfig(dsn="postgresql://demo"),
            connection=connection,
            release=released.append,
        )

        client.close()

        self.assertEqual(released, [connection])
        self.assertEqual(connection.closed, 0)  # handed back, not torn down
        # A second close is a no-op (connection already released).
        client.close()
        self.assertEqual(released, [connection])

    def test_context_exit_rolls_back_then_releases_on_error(self) -> None:
        connection = _FakeConnection()
        released: list[object] = []
        client = PostgresClient(
            PostgresConnectionConfig(dsn="postgresql://demo"),
            connection=connection,
            release=released.append,
        )

        with self.assertRaises(ValueError):
            with client:
                raise ValueError("boom")

        self.assertEqual(connection.rollbacks, 1)
        self.assertEqual(released, [connection])
        self.assertEqual(connection.closed, 0)


if __name__ == "__main__":
    unittest.main()
