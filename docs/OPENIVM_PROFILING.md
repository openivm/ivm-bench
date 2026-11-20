# OpenIVM refresh profiling

[Back to README](../README.md)

---

### OpenIVM profiling on Fabric and local Spark

`OPENIVM_PROFILE_REFRESH=1` exports refresh-step CSVs after each timed batch
for both `spark-openivm` and `fabric-openivm-jvm-35`. `OPENIVM_QUERY_LOG=1`
exports their refresh SQL. Files use the engine name so the two engines can
be compared without overwriting each other's results:

- `mount/results/<sf>/dbt-server/<engine>-profile-batch<N>.csv`
- `mount/results/<sf>/dbt-server/<engine>-profile-by-step-batch<N>.csv`
- `mount/results/<sf>/dbt-server/<engine>-query-log-batch<N>.json`
- `mount/results/<sf>/<engine>/query-log/<view>/<refresh>/` (formatted working copy; OAT cleanup removes it)

Fabric attaches to the existing dbt Livy session in the resolved compute
lakehouse. If that session has expired, export fails instead of starting a
new driver with an empty catalog. Export requests run after the batch timer
stops. Profile catalogs are cumulative; use refresh IDs and timestamps to
identify the batch, not the export filename alone.

Both dbt profiles set `livy.rsc.sql.num-rows` to 100,000. Livy's default
1,000-row cap silently clips cumulative profiles. Export rejects results
of exactly 1,000 rows (possibly an old session) or at least 100,000 rows;
these checks are conservative and do not prove completeness for arbitrary
server-side limits. Use fresh sessions with the checked-in configuration.
The Fabric adapter contract tests in `test_fabric_profile.py` require the
dbt-server requirements; they are skipped on hosts without the adapter.

Batch-1 profile metadata also records a small allowlist of effective SQL
execution settings from `SET -v` (broadcast, shuffle, AQE and Delta MERGE
source materialization). Missing keys mean the runtime did not report them;
they must not be interpreted as default values. Arbitrary session properties
and credentials are not saved.

Set `openivm_profile_refresh=2` in the CI dispatch to collect Fabric SQL plans,
jobs, and stage counters after each timed batch and before correctness
validation. The snapshots are stored under `mount/stats/<sf>/<engine>/` as
`fabric-spark-history-batch<N>.jsonl.gz` with matching summaries. Fabric retains
only a recent portion of Spark history, so check the timestamps against the
batch's `wall_window_start_ms` and `wall_window_end_ms`. Each file is a snapshot
of retained history and can include executions seen in earlier files. Batch
results record the export duration separately. The final snapshot is captured
before teardown and includes any later validation jobs still retained.
