#!/usr/bin/env bash
# Catalog checks and unit tests shared by ci.yaml and the GCI no-engine path.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

if ! python3 -m venv --system-site-packages .runtime/ci-tests; then
  sudo apt-get update
  sudo apt-get install -y python3-venv
  python3 -m venv --system-site-packages .runtime/ci-tests
fi
py=.runtime/ci-tests/bin/python
"$py" -m pip install -r src/containers/benchmark-server/requirements.txt -r src/containers/dbt-server/requirements-test.txt psycopg2-binary "boto3>=1.35"
"$py" -c "import duckdb; duckdb.connect().execute(\"FORCE INSTALL delta FROM 'https://extensions.duckdb.org'\")"
export PYTHONPATH="$PWD/src/containers${PYTHONPATH:+:$PYTHONPATH}"
"$py" -m sql_catalog generate --all
"$py" -m sql_catalog check --all
"$py" -m sql_catalog parity --all
"$py" tools/scripts/check-sql-catalog.py --all
"$py" -m unittest discover -s src/containers/dbt-server/tests -v
"$py" -m unittest discover -s src/containers/benchmark-server/tests -v
"$py" -m unittest discover -s src/workloads/extended-tpcds -p 'test_*.py' -v
