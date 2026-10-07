#!/usr/bin/env bash
# Start a throwaway PostgreSQL for the billing store tests and print its DSN.
#
# The test fixture DROPs and recreates the `public` schema on every test, so it
# must never point at a database anyone cares about. This script exists so
# nobody is tempted to point BILLING_TEST_DATABASE_URL at the real one.
#
#   eval "$(EdennCode/Scripts/billing_pg_testdb.sh)"
#   .venv/bin/python -m pytest EdennCode/Deployment/Testing/test_billing_pg_stores.py
#   EdennCode/Scripts/billing_pg_testdb.sh stop
#
# Requires a local postgresql install (brew install postgresql@16).

set -euo pipefail

PGDIR="${BILLING_TEST_PGDIR:-${TMPDIR:-/tmp}/billing-testpg}"
PORT="${BILLING_TEST_PGPORT:-55433}"

for candidate in /opt/homebrew/opt/postgresql@16/bin \
                 /opt/homebrew/opt/postgresql@15/bin \
                 /opt/homebrew/opt/postgresql@14/bin; do
    [ -d "$candidate" ] && export PATH="$candidate:$PATH" && break
done
command -v initdb >/dev/null || { echo "initdb not found — install postgresql" >&2; exit 1; }

if [ "${1:-start}" = "stop" ]; then
    pg_ctl -D "$PGDIR/data" stop -m immediate >/dev/null 2>&1 || true
    rm -rf "$PGDIR"
    echo "# stopped and removed $PGDIR" >&2
    exit 0
fi

if [ ! -d "$PGDIR/data" ]; then
    rm -rf "$PGDIR"; mkdir -p "$PGDIR"
    initdb -D "$PGDIR/data" -U postgres --auth=trust >/dev/null
fi

if ! pg_ctl -D "$PGDIR/data" status >/dev/null 2>&1; then
    # Unix socket in $PGDIR, no TCP listener: nothing outside this machine can
    # reach a database that gets its schema dropped on every test.
    pg_ctl -D "$PGDIR/data" -l "$PGDIR/log" \
        -o "-p $PORT -k $PGDIR -c listen_addresses=''" start >/dev/null
    for _ in $(seq 1 20); do
        pg_isready -h "$PGDIR" -p "$PORT" >/dev/null 2>&1 && break
        sleep 0.25
    done
fi

psql -h "$PGDIR" -p "$PORT" -U postgres -tc \
    "SELECT 1 FROM pg_database WHERE datname='billing_test'" | grep -q 1 \
    || createdb -h "$PGDIR" -p "$PORT" -U postgres billing_test

echo "export BILLING_TEST_DATABASE_URL='postgresql://postgres@/billing_test?host=$PGDIR&port=$PORT'"
