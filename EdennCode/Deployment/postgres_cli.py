from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from EdennCode.Deployment.postgres_wrapper import PostgresClient


def _parse_json_argument(raw: str, *, label: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON for {label}: {exc}") from exc


def _parse_returning(raw: str | None) -> str | list[str] | None:
    if raw is None:
        return None
    if raw.strip() == "*":
        return "*"
    return [column.strip() for column in raw.split(",") if column.strip()]


def _print_payload(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Quick Postgres admin helper for ad hoc checks and CRUD operations.",
    )
    parser.add_argument(
        "--user-db",
        action="store_true",
        help="Connect to the user DB (DB_HOST/DB_NAME/…) instead of the telemetry DB (PGHOST/…).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("check", help="Validate connectivity and print basic server info.")

    tables_parser = subparsers.add_parser("tables", help="List tables for a schema.")
    tables_parser.add_argument("--schema", default="public", help="Schema to inspect.")

    describe_parser = subparsers.add_parser("describe", help="Describe a table schema.")
    describe_parser.add_argument("table", help="Table name, optionally schema-qualified.")

    head_parser = subparsers.add_parser("head", help="Print the first rows from a table.")
    head_parser.add_argument("table", help="Table name, optionally schema-qualified.")
    head_parser.add_argument("--limit", type=int, default=5, help="Number of rows to fetch.")

    create_parser = subparsers.add_parser("create-table", help="Create a table from JSON column defs.")
    create_parser.add_argument("table", help="Table name, optionally schema-qualified.")
    create_parser.add_argument(
        "--columns-json",
        required=True,
        help='JSON object like {"id":"BIGSERIAL PRIMARY KEY","name":"TEXT NOT NULL"}.',
    )
    create_parser.add_argument(
        "--no-if-not-exists",
        action="store_true",
        help="Fail instead of using CREATE TABLE IF NOT EXISTS.",
    )

    add_columns_parser = subparsers.add_parser(
        "add-columns",
        help="Add one or more columns from JSON column defs.",
    )
    add_columns_parser.add_argument("table", help="Table name, optionally schema-qualified.")
    add_columns_parser.add_argument(
        "--columns-json",
        required=True,
        help='JSON object like {"payload":"JSONB","created_at":"TIMESTAMPTZ DEFAULT NOW()"}.',
    )
    add_columns_parser.add_argument(
        "--no-if-not-exists",
        action="store_true",
        help="Fail instead of using ADD COLUMN IF NOT EXISTS.",
    )

    drop_columns_parser = subparsers.add_parser("drop-columns", help="Drop one or more columns.")
    drop_columns_parser.add_argument("table", help="Table name, optionally schema-qualified.")
    drop_columns_parser.add_argument("columns", nargs="+", help="Column names to drop.")
    drop_columns_parser.add_argument(
        "--no-if-exists",
        action="store_true",
        help="Fail instead of using DROP COLUMN IF EXISTS.",
    )
    drop_columns_parser.add_argument(
        "--cascade",
        action="store_true",
        help="Append CASCADE to the drop operation.",
    )

    insert_parser = subparsers.add_parser("insert", help="Insert a row from a JSON object.")
    insert_parser.add_argument("table", help="Table name, optionally schema-qualified.")
    insert_parser.add_argument(
        "--values-json",
        required=True,
        help='JSON object like {"event_name":"demo","payload":{"ok":true}}.',
    )
    insert_parser.add_argument(
        "--returning",
        default=None,
        help='Optional RETURNING target, for example "*" or "id,event_name".',
    )

    update_parser = subparsers.add_parser("update", help="Update rows with an explicit WHERE clause.")
    update_parser.add_argument("table", help="Table name, optionally schema-qualified.")
    update_parser.add_argument("--values-json", required=True, help="JSON object of columns to update.")
    update_parser.add_argument("--where", required=True, help='SQL WHERE fragment, for example "id = %s".')
    update_parser.add_argument(
        "--params-json",
        default="[]",
        help='JSON array for WHERE parameters, for example "[123]".',
    )
    update_parser.add_argument(
        "--returning",
        default=None,
        help='Optional RETURNING target, for example "*" or "id,event_name".',
    )

    delete_parser = subparsers.add_parser("delete", help="Delete rows with an explicit WHERE clause.")
    delete_parser.add_argument("table", help="Table name, optionally schema-qualified.")
    delete_parser.add_argument("--where", required=True, help='SQL WHERE fragment, for example "id = %s".')
    delete_parser.add_argument(
        "--params-json",
        default="[]",
        help='JSON array for WHERE parameters, for example "[123]".',
    )
    delete_parser.add_argument(
        "--returning",
        default=None,
        help='Optional RETURNING target, for example "*" or "id,event_name".',
    )

    query_parser = subparsers.add_parser("query", help="Run arbitrary SQL.")
    query_parser.add_argument("--sql", required=True, help="SQL statement to execute.")
    query_parser.add_argument(
        "--params-json",
        default="[]",
        help='JSON array for SQL parameters, for example "[123, \\"demo\\"]".',
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    client_factory = PostgresClient.from_user_db_env if args.user_db else PostgresClient.from_env
    with client_factory() as client:
        if args.command == "check":
            payload = client.quick_check()
        elif args.command == "tables":
            payload = client.list_tables(schema=args.schema)
        elif args.command == "describe":
            payload = client.describe_table(args.table)
        elif args.command == "head":
            payload = client.fetch_first_rows(args.table, limit=args.limit)
        elif args.command == "create-table":
            client.create_table(
                args.table,
                _parse_json_argument(args.columns_json, label="--columns-json"),
                if_not_exists=not args.no_if_not_exists,
            )
            payload = {"ok": True, "table": args.table}
        elif args.command == "add-columns":
            client.add_columns(
                args.table,
                _parse_json_argument(args.columns_json, label="--columns-json"),
                if_not_exists=not args.no_if_not_exists,
            )
            payload = {"ok": True, "table": args.table}
        elif args.command == "drop-columns":
            client.drop_columns(
                args.table,
                args.columns,
                if_exists=not args.no_if_exists,
                cascade=args.cascade,
            )
            payload = {"ok": True, "table": args.table, "columns": args.columns}
        elif args.command == "insert":
            payload = client.insert_row(
                args.table,
                _parse_json_argument(args.values_json, label="--values-json"),
                returning=_parse_returning(args.returning),
            )
        elif args.command == "update":
            payload = client.update_rows(
                args.table,
                _parse_json_argument(args.values_json, label="--values-json"),
                where_clause=args.where,
                where_params=_parse_json_argument(args.params_json, label="--params-json"),
                returning=_parse_returning(args.returning),
            )
        elif args.command == "delete":
            payload = client.delete_rows(
                args.table,
                where_clause=args.where,
                where_params=_parse_json_argument(args.params_json, label="--params-json"),
                returning=_parse_returning(args.returning),
            )
        else:
            payload = client.run_sql(
                args.sql,
                params=_parse_json_argument(args.params_json, label="--params-json"),
            )

    _print_payload(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
