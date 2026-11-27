# Spark-native metrics A/B

[Back to README](../README.md)

---

### Spark metrics A/B

Both `spark` and `spark-openivm` capture homogeneous, Spark-native per-query
metrics via Spark's built-in **event log** (gated by the `spark_metrics_capture`
feature flag, default on). At the end of every experiment `benchmark-server`
parses both engines' logs (pandas + DuckDB), maps each Spark execution → dbt
model (from the physical-plan write target) and → batch (wall-clock windows),
and emits query-aligned Parquet (`metrics_long` / `metrics_by_model` /
`timeseries`), a **per-query lifecycle** A/B diff PNG (cumulative resource over
each query's runtime, `spark` vs `spark-openivm`), `RESULTS.md`, and a per-run
zip under `mount/metrics/<SF>/` (also copied to
`mount/oat-state/<run_id>/exp-NNN/metrics/`).
Query it via the DuckDB-backed `benchmark-server` routes
(`GET /metrics/kpis`, `GET /metrics/diff`, `POST /metrics/query`,
`GET /metrics/artifact`) or by reading the Parquet directly — both are a stable
contract for downstream (`openivm-spark`) consumption.

### Extended diagnostics

Set `openivm_profile_refresh` to `2` in manual CI, or set
`feature_flags.extended_spark_profile` in an experiment file. Spark archives
execution timestamps, plans, and stage events before cleanup. The execution
sidecar labels setup, timed work, and post-batch work separately. Fabric
exports executed SQL plans, jobs, and stages from Spark History into
`mount/stats/<SF>/<engine>/` before deleting the compute lakehouse. The
benchmark uses the standard batch timers in both modes.
