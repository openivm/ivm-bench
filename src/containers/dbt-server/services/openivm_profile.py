"""OpenIVM profiling export helpers."""

import csv
import math
import os
import subprocess
import tempfile
from pathlib import Path

from sql_catalog import load_sql

WORK_DIR = Path(os.environ.get("DUCKDB_OPENIVM_WORK_DIR", "/data/processed/duckdb-openivm"))
OPENIVM_BIN = os.environ.get("DUCKDB_OPENIVM_BIN", "/data/bin/duckdb-openivm/duckdb")
MEM_LIMIT = os.environ.get("DUCKDB_OPENIVM_MEM_LIMIT", "115GB")
TEMP_DIR = Path(os.environ.get("DUCKDB_OPENIVM_TEMP_DIR", str(WORK_DIR / "_tmp")))
THREADS = os.environ.get("DUCKDB_OPENIVM_THREADS", "")
CAPTURE_SQL = os.environ.get("OPENIVM_CAPTURE_SQL", "0") == "1"


def summarize_refresh_statements(rows):
    """Summarize non-overlapping statement spans per captured refresh."""
    refreshes = {}
    for row in rows:
        step = row.get("step_name")
        if step not in ("execute_refresh_sql_stmt", "command_span", "total_refresh",
                        "generate_refresh_sql.metadata_lookup"):
            continue
        view, refresh = row["view_name"], str(row["refresh_id"])
        summary = refreshes.setdefault((view, refresh), {
            "view_name": view, "refresh_id": refresh, "statements": {},
        })
        detail, _, sql = (row.get("detail") or "").partition("sql=")
        fields = dict(part.strip().split("=", 1) for part in detail.replace(", ", ";").split(";")
                      if "=" in part)
        if "refresh_type" in fields:
            summary["strategy"] = fields["refresh_type"]
        if "outcome" in fields:
            summary["outcome"] = fields["outcome"]
        if step == "generate_refresh_sql.metadata_lookup":
            continue
        try:
            duration = float(row.get("duration_ms"))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(duration):
            continue
        if step == "execute_refresh_sql_stmt":
            kind = fields.get("stmt_kind") or (sql.split(maxsplit=1)[0].lower() if sql.strip() else "unknown")
            entry = summary["statements"].setdefault(kind, {"count": 0, "duration_ms": 0.0})
            entry["count"] += 1
            entry["duration_ms"] += duration
        else:
            summary[step + "_ms"] = duration
    return list(refreshes.values())


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _copy_query_to_csv(query: str, output_path: Path) -> None:
    """Run a COPY query through the OpenIVM CLI and write CSV to output_path."""
    db_file = WORK_DIR / "openivm.duckdb"
    TEMP_DIR.mkdir(parents=True, exist_ok=True)

    preamble = [
        ".bail on",
        ".timer off",
        load_sql(
            "duckdb-openivm", "inline", "dbt_server/services_ducklake_sources/set_memory_limit",
            memory_limit=_sql_literal(MEM_LIMIT),
        ),
        load_sql(
            "duckdb-openivm", "inline", "dbt_server/services_ducklake_sources/set_temp_directory",
            temp_directory=_sql_literal(str(TEMP_DIR)),
        ),
    ]
    if THREADS:
        preamble.append(load_sql(
            "duckdb-openivm", "inline", "dbt_server/services_ducklake_sources/set_threads",
            threads=int(THREADS),
        ))
    preamble.append(load_sql("duckdb-openivm", "inline", "dbt_server/services_openivm_profile/load_extension"))

    copy_sql = load_sql(
        "duckdb-openivm", "inline", "dbt_server/services_openivm_profile/copy_query",
        query=query,
        output_path=_sql_literal(str(output_path)),
    )
    sql = "\n".join(preamble + [copy_sql]) + "\n"
    proc = subprocess.run(
        [OPENIVM_BIN, str(db_file)],
        input=sql,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=3600,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"OpenIVM profile export failed:\n{proc.stdout[-4000:]}")


def _export_compiled_sql(batch_num: int) -> list[str]:
    if not CAPTURE_SQL:
        return []
    output_dir = WORK_DIR / "compiled-sql"
    output_dir.mkdir(parents=True, exist_ok=True)
    tables = (
        ("openivm_compiled_programs", "view_name, operation, version"),
        ("openivm_compiled_statements", "view_name, operation, version, stmt_order"),
    )
    paths = []
    for table, order in tables:
        output = output_dir / f"{table}-batch{batch_num}.csv"
        _copy_query_to_csv(f"SELECT * FROM {table} ORDER BY {order}", output)
        paths.append(str(output.relative_to(WORK_DIR)))
    return paths


def export_profile(run_id: str, batch_num: int) -> dict:
    """Export OpenIVM profile rows and summaries as CSV strings.

    The profile table is cumulative for the OpenIVM database. Exporting after
    each batch gives the artifact a snapshot that can be inspected independently
    even when later benchmark phases clean or replace mount/results.
    """
    run_id_sql = _sql_literal(run_id)
    with tempfile.TemporaryDirectory(prefix="openivm-profile-") as tmp:
        tmp_dir = Path(tmp)
        profile_path = tmp_dir / "profile.csv"
        by_step_path = tmp_dir / "by_step.csv"
        by_view_step_path = tmp_dir / "by_view_step.csv"

        exported_cols = f"{batch_num} AS exported_after_batch, {run_id_sql} AS exported_after_run_id"
        _copy_query_to_csv(
            load_sql(
                "duckdb-openivm", "inline", "dbt_server/services_openivm_profile/profile_rows",
                exported_columns=exported_cols,
            ),
            profile_path,
        )
        _copy_query_to_csv(
            load_sql(
                "duckdb-openivm", "inline", "dbt_server/services_openivm_profile/profile_by_step",
                exported_columns=exported_cols,
            ),
            by_step_path,
        )
        _copy_query_to_csv(
            load_sql(
                "duckdb-openivm", "inline", "dbt_server/services_openivm_profile/profile_by_view_step",
                exported_columns=exported_cols,
            ),
            by_view_step_path,
        )

        cli_path = TEMP_DIR / "cli-timings.csv"
        cli_timings = "timestamp_s,sql,attempt,returncode,wall_s,setup_sql_s,operation_sql_s,outside_sql_s\n"
        if cli_path.exists():
            cli_timings += cli_path.read_text()
        profile_csv = profile_path.read_text()
        rows = list(csv.DictReader(profile_csv.splitlines()))
        sql_archive_paths = _export_compiled_sql(batch_num)
        return {
            "status": "ok",
            "run_id": run_id,
            "batch_num": batch_num,
            "row_count": len(rows),
            "view_count": len({row["view_name"] for row in rows if row.get("view_name")}),
            "sql_archive_paths": sql_archive_paths,
            "refresh_statements": summarize_refresh_statements(rows),
            "csv": {
                "profile": profile_csv,
                "cli_timings": cli_timings,
                "by_step": by_step_path.read_text(),
                "by_view_step": by_view_step_path.read_text(),
            },
        }
