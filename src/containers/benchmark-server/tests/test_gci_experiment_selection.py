"""GCI runs its local-engine preset, narrowed to affected engines on PRs; CI runs what it is asked."""

import os
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

REPO = Path(__file__).resolve().parents[4]
BENCHMARK_SERVER = REPO / "src/containers/benchmark-server"
sys.path.insert(0, str(BENCHMARK_SERVER))

from models.config import CLOUD_ENGINES, ENGINE_PORTS  # noqa: E402
from models.experiments import parse_experiments_json  # noqa: E402

SELECTOR = REPO / "tools/scripts/github/select-experiments-file.sh"
WORKFLOWS = REPO / ".github/workflows"
GCI_PRESET = "src/containers/benchmark-server/experiments/gci-sf10-local-engines.json"
CI_PRESET = "src/containers/benchmark-server/experiments/ci-sf10-all-engines.json"
ALL_ENGINES = set(ENGINE_PORTS)
LOCAL_ENGINES = ALL_ENGINES - CLOUD_ENGINES


class ExperimentSelectionTest(unittest.TestCase):
    def select(self, event, override=""):
        with tempfile.TemporaryDirectory() as directory:
            # A leftover PR selector must not steer anything any more.
            selector = Path(directory) / ".github/gci.json"
            selector.parent.mkdir()
            selector.write_text('{"experiments_file": "cost-model-bench.json"}')
            result = subprocess.run(
                ["bash", str(SELECTOR)], cwd=directory, text=True, capture_output=True,
                env={**os.environ, "GITHUB_EVENT_NAME": event, "INPUT_EXPERIMENTS_FILE": override},
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_explicit_file_wins_for_every_event(self):
        for event in ("pull_request", "push", "workflow_dispatch"):
            with self.subTest(event=event):
                self.assertEqual(self.select(event, "custom.json"), "custom.json")

    def test_dispatch_without_a_file_uses_the_inline_knobs(self):
        self.assertEqual(self.select("workflow_dispatch"), "")

    def test_pull_request_ignores_the_retired_gci_json(self):
        self.assertEqual(self.select("pull_request"), "")

    def test_build_selection_uses_all_resolved_experiments(self):
        helper = REPO / "tools/scripts/github/experiment-needs-engine.py"
        for data, expected in (
            ({"baseline": {"engines": ["duckdb"]}, "experiments": [{}]}, "false"),
            ({"baseline": {"engines": ["risingwave"]}, "experiments": [{}]}, "true"),
            ({"baseline": {"engines": ["duckdb"]}, "experiments": [
                {}, {"engines": ["risingwave"]}
            ]}, "true"),
            ({"experiments": [{}]}, "false"),
        ):
            with self.subTest(data=data), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "experiments.json"
                path.write_text(json.dumps(data))
                result = subprocess.run([sys.executable, str(helper), str(path), "risingwave"],
                                        check=True, capture_output=True, text=True)
                self.assertEqual(result.stdout.strip(), expected)


class PresetCoverageTest(unittest.TestCase):
    def engines(self, preset):
        experiments = parse_experiments_json((REPO / preset).read_text(encoding="utf-8"))
        self.assertTrue(experiments)
        for experiment in experiments:
            self.assertEqual(experiment.scale_factor, 10)
            self.assertFalse(experiment.repeated_refresh)
            self.assertEqual(experiment.batch_count, 3)
        return [set(experiment.engines) for experiment in experiments]

    def test_gci_preset_runs_every_local_engine_and_no_cloud_engine(self):
        for engines in self.engines(GCI_PRESET):
            self.assertEqual(engines, LOCAL_ENGINES)

    def test_ci_preset_runs_every_engine(self):
        for engines in self.engines(CI_PRESET):
            self.assertEqual(engines, ALL_ENGINES)

    def test_presets_cannot_be_switched_to_untimed_sweeps_by_environment(self):
        with patch.dict(os.environ, {"COMPILER_BENCH": "1", "COST_MODEL_BENCH": "1"}):
            for preset in (GCI_PRESET, CI_PRESET):
                for experiment in parse_experiments_json((REPO / preset).read_text()):
                    self.assertFalse(experiment.feature_flags.compiler_bench)
                    self.assertFalse(experiment.feature_flags.cost_model_bench)

    def test_gci_is_pinned_to_its_preset_with_no_dispatch_knobs(self):
        gci = (WORKFLOWS / "gci.yaml").read_text(encoding="utf-8")
        self.assertIn("uses: ./.github/workflows/ci.yaml", gci)
        self.assertIn(f"experiments_file: {GCI_PRESET}", gci)
        self.assertNotIn("inputs.", gci)

    def test_ci_inline_default_engines_cover_every_engine(self):
        ci = (WORKFLOWS / "ci.yaml").read_text(encoding="utf-8")
        defaults = re.findall(r"inputs\.engines \|\| '([^']+)'", ci)
        defaults += re.findall(r'(?ms)^      engines:\n.*?default: "([^"]+)"', ci)
        self.assertEqual(len(defaults), 2)
        for default in defaults:
            self.assertEqual(set(default.split(",")), ALL_ENGINES)

    def test_ci_guards_engine_coverage_before_reporting(self):
        ci = (WORKFLOWS / "ci.yaml").read_text(encoding="utf-8")
        guard = ci.index("check-engine-coverage.py")
        self.assertLess(ci.index("src/.scripts/benchmark.sh"), guard)
        self.assertLess(guard, ci.index("post-pr-comment.sh"))

    def test_ci_uses_docker_assigned_dbt_server_ports(self):
        ci = yaml.load((WORKFLOWS / "ci.yaml").read_text(), Loader=yaml.BaseLoader)
        compose = yaml.load(
            (REPO / "docker/docker-compose.benchmark-server.yml").read_text(),
            Loader=yaml.BaseLoader,
        )
        self.assertEqual(ci["jobs"]["benchmark"]["env"]["DBT_SERVER_DYNAMIC_PORT"], "1")
        self.assertEqual(
            compose["services"]["benchmark-server"]["environment"]["DBT_SERVER_DYNAMIC_PORT"],
            "${DBT_SERVER_DYNAMIC_PORT:-0}",
        )

    def test_ci_runner_selection_defaults_to_the_shared_pool(self):
        ci = yaml.load((WORKFLOWS / "ci.yaml").read_text(), Loader=yaml.BaseLoader)
        runner = ci["on"]["workflow_dispatch"]["inputs"]["runner"]
        self.assertEqual(runner["type"], "choice")
        self.assertEqual(runner["default"], "any")
        self.assertEqual(runner["options"], [
            "any", "ivm-bench-vmss-runner000000", "ivm-bench-vmss-runner000001",
        ])
        self.assertNotIn("runner", ci["on"]["workflow_call"]["inputs"])
        self.assertEqual(ci["jobs"]["benchmark"]["runs-on"], [
            "self-hosted", "Linux", "X64",
            "${{ inputs.runner != 'any' && inputs.runner || 'ivm-bench-azure' }}",
        ])
        self.assertTrue(ci["concurrency"]["group"].endswith("-${{ inputs.runner || 'any' }}"))

    def test_workflow_triggers_keep_pr_main_and_manual_paths_separate(self):
        gci = yaml.load((WORKFLOWS / "gci.yaml").read_text(), Loader=yaml.BaseLoader)
        ci = yaml.load((WORKFLOWS / "ci.yaml").read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(set(gci["on"]), {"pull_request", "push", "workflow_dispatch"})
        self.assertEqual(gci["on"]["push"], {"branches": ["main"]})
        self.assertEqual(gci["on"]["workflow_dispatch"], "")
        self.assertIn("synchronize", gci["on"]["pull_request"]["types"])
        self.assertIn("ready_for_review", gci["on"]["pull_request"]["types"])
        self.assertEqual(set(ci["on"]), {"workflow_dispatch", "workflow_call"})
        self.assertEqual(len(ci["on"]["workflow_dispatch"]["inputs"]), 25)
        self.assertIn("experiments_engines", ci["on"]["workflow_call"]["inputs"])
        gci_inputs = gci["jobs"]["engines"]["with"]
        self.assertEqual(
            set(gci_inputs), {"experiments_file", "experiments_engines", "pr_number", "publish_dashboard"}
        )
        self.assertEqual(gci_inputs["publish_dashboard"], "${{ needs.changes.outputs.mode == 'full' }}")
        self.assertIn("mode == 'subset'", gci_inputs["experiments_engines"])

    def test_required_check_is_an_always_running_gate(self):
        gate = yaml.load((WORKFLOWS / "gci.yaml").read_text(), Loader=yaml.BaseLoader)["jobs"]["gate"]
        self.assertEqual(gate["name"], "benchmark / benchmark")
        self.assertEqual(set(gate["needs"]), {"changes", "engines", "checks"})
        self.assertTrue(gate["if"].startswith("always()"))

    def test_each_main_push_runs_and_publishes_in_its_own_concurrency_group(self):
        for name in ("gci.yaml", "ci.yaml"):
            workflow = yaml.load((WORKFLOWS / name).read_text(), Loader=yaml.BaseLoader)
            with self.subTest(workflow=name):
                self.assertIn("format('push-{0}', github.run_id)", workflow["concurrency"]["group"])
        publish = next(
            step for step in yaml.load((WORKFLOWS / "ci.yaml").read_text(), Loader=yaml.BaseLoader)
            ["jobs"]["benchmark"]["steps"] if step.get("name") == "Publish dashboard snapshot"
        )
        self.assertIn("github.event_name == 'push'", publish["if"])

    def test_dashboard_publisher_records_main_push_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
                            "--allow-empty", "-m", "x"], cwd=root, check=True)
            sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True,
                                 capture_output=True, text=True).stdout.strip()
            (root / "ci-results").mkdir()
            (root / "ci-results/engine-coverage.json").write_text('{"passed": true}')
            (root / "bin").mkdir()
            (root / "bin/curl").write_text('#!/bin/sh\necho "curl $*" >&2\nexit 7\n')
            (root / "bin/curl").chmod(0o755)
            result = subprocess.run(
                ["bash", str(REPO / "tools/dashboard-ci.sh")], cwd=root, text=True, capture_output=True,
                env={**os.environ, "PATH": f"{root / 'bin'}:{os.environ['PATH']}",
                     "GITHUB_EVENT_NAME": "push", "GITHUB_SHA": sha, "GITHUB_REF": "refs/heads/main",
                     "GITHUB_REPOSITORY": "openivm/ivm-bench", "GITHUB_API_URL": "https://api.invalid",
                     "GH_TOKEN": "token", "RUNNER_TEMP": directory},
            )
        self.assertIn(f"/repos/openivm/ivm-bench/commits/{sha}", result.stderr)


if __name__ == "__main__":
    unittest.main()
