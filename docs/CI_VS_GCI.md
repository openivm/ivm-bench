# GCI and CI workflows

[Back to README](../README.md)

---

### GCI vs CI

| Workflow | When | What | Knobs |
|---|---|---|---|
| [`gci.yaml`](../.github/workflows/gci.yaml) | Non-draft same-repo PR, every push to `main`, or manual dispatch on the default branch | SF10 TPC-DI ([`gci-sf10-local-engines.json`](../src/containers/benchmark-server/experiments/gci-sf10-local-engines.json)) on the local engines a PR affects; every local engine on `main` | `gci-full` PR label |
| [`ci.yaml`](../.github/workflows/ci.yaml) | Ad hoc only | Every engine ([`ci-sf10-all-engines.json`](../src/containers/benchmark-server/experiments/ci-sf10-all-engines.json)), or any experiments file | All dispatch inputs |

The benchmark server uses fixed host port 9000. Before a CI benchmark starts,
[`cleanup-benchmark-port.sh`](../tools/scripts/github/cleanup-benchmark-port.sh)
removes stale Compose `benchmark-server` containers publishing that port and
stops orphan Docker proxy listeners. These runners execute one job at a time.
An unrelated container or host process on port 9000 fails the job without being
terminated. Local benchmark runs never perform this cleanup.

### Affected engines

Like `nx affected`, GCI benchmarks only the local engines a PR can change.
The `changes` job runs
[`select-engines.py`](../tools/scripts/github/select-engines.py) against the
PR's merge base, using the
[`engine-impact.json`](../tools/scripts/github/engine-impact.json) manifest.
A rename counts as a change to both its old and new path.

| Changed path | Engines |
|---|---|
| `no_effect` (docs, Markdown, images, catalog exceptions) | None |
| An engine's `rules` (compose file, dbt project, container, handlers, pins, tests) | That engine plus its `depends_on` partners |
| SQL catalog data (`sql_catalog` globs) | Engines whose rendered SQL differs between base and head |
| `global` (workflows, tools, benchmark-server, dbt-server core, shared compose, generator code, project targets, dependency pins) | Every local engine |
| Anything unclassified, or a selector error | Every local engine |

More than `full_suite_fraction` (half) of the local engines also runs the
full suite. Cloud engines are never benchmarked by GCI; a cloud-only change
selects no engine, and GCI then runs only the SQL catalog checks and unit
tests. Pushes to `main` and dispatches always run every local engine.

To force the full suite on a PR, add the `gci-full` label and re-run GCI.
The selected engines and the reason for each path appear in the `changes`
job summary.

Only full runs, including every push to `main`, publish the dashboard;
subset runs never publish a partial snapshot. Each push to `main` runs in its
own concurrency group, so merges queue rather than cancel each other. PR runs
still cancel superseded runs.

The required check `benchmark / benchmark` is the always-running `gate` job.
It passes when the selected engines pass, or when no engine was selected and the
unit tests pass.

#### Maintaining the manifest

- **New engine:** add it to `engines` (`kind` `local` or `cloud`, plus any
  `depends_on`) and add a `rules` entry for its compose file, dbt project,
  container directory, handlers and pins. `test_engine_selection.py` fails until
  every registered engine, compose file and dbt project is mapped.
- **`global`:** code shared by several engines, such as orchestration,
  workflows, dependency files and the SQL generator.
- **`no_effect`:** files that no build, benchmark or test reads.
- Every tracked file must match a rule; the test lists any that do not.
  Unclassified files run the full suite.

Both fail unless every requested engine completes every batch. Run anything
else, such as cloud engines or the cost-model sweep, through CI. For cloud
credentials and dispatch commands, see [manual CI](CI.md):

```bash
gh workflow run ci.yaml --ref <branch> \
  -f experiments_file=src/containers/benchmark-server/experiments/cost-model-bench.json
```

Use `cost-model-bench-ladder.json` for the full cost-model sweep, or another
experiments JSON for a TPC-DI workload. Without `experiments_file`, CI
synthesises one experiment from its per-knob inputs.

Manual dispatches write results to the job summary by default. Set the optional
string input `pr_number` (for example, `-f pr_number=100`) to also post a results
comment, including the best-effort per-batch chart, on that pull request.
Dispatch uses `batch_1_pct` for the initial insert percentage; the duplicate
`batch_1_insert_pct` input is omitted to stay within GitHub's 25-input limit.
Experiments JSON files still accept `batch_1_insert_pct`.

To compare OpenIVM revisions, change both `ARG OPENIVM_COMMIT` defaults in
[`src/containers/duckdb-openivm/Dockerfile`](../src/containers/duckdb-openivm/Dockerfile)
to the same commit SHA, push a branch, and dispatch CI on it. Changing the pin
invalidates the cached builder image. Each run uses one OpenIVM revision;
use separate branches or dispatches to compare revisions. The revision must provide
`cost_model_benchmark` and support the selected arguments; DuckDB and the build
tooling remain separately pinned in that Dockerfile.

Cost-model CSVs are uploaded with the CI artifacts under
`mount/cost-model-bench/<run-id>/<experiment-index>-<label>/`. Scale factors
run sequentially, and the CI job timeout and cancellation rules apply.
A newer dispatch of the same branch and experiments file cancels the previous
run (an active run on `main` is kept), so wait for a run to finish before
dispatching the next revision if you need complete comparison results.


## Execution boundaries

GitHub Actions runs the benchmark job on the `self-hosted`, `Linux`, `X64`,
`ivm-bench-azure` runner. The benchmark orchestrator and local engines run in
Docker on that runner. This includes TPC-DI generation, the benchmark-server,
dbt-server, Spark, DuckDB, Feldera, DbspNet, RisingWave, and Materialize Emulator.

Databricks Enzyme and OpenIVM, and the Fabric JVM and OpenIVM engines, execute
on their respective cloud services; the local Docker containers orchestrate
those runs.

GCI is the SF10 gate for the eight local engines listed above. PRs run the
affected subset; pushes to `main` run all eight. Use `gh workflow run gci.yaml` to run the full preset on the default branch without a PR. Successful coverage-passing full GCI runs publish to the benchmark dashboard; subset PR runs and configurable manual CI runs do not.
CI is the configurable workflow used by manual dispatch; its
default SF10 preset adds Databricks and Fabric for twelve engines total. CI can
also run other experiment files, including cloud-engine and cost-model sweeps.

Materialize Emulator is a non-production deployment, not representative of
production Materialize performance; its results retain the Emulator label.

The GitHub Actions results comment includes a per-batch stacked bar chart.
