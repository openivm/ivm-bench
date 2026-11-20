"""GCI benchmarks only the local engines a pull request can affect."""

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO / "src/containers/benchmark-server"))

from models.config import CLOUD_ENGINES, ENGINE_COMPOSE_FILES, ENGINE_PORTS  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "select_engines", REPO / "tools/scripts/github/select-engines.py"
)
selector = importlib.util.module_from_spec(_spec)
sys.modules["select_engines"] = selector
_spec.loader.exec_module(selector)

MANIFEST = selector.load_manifest()
LOCAL = [engine for engine in ENGINE_PORTS if engine not in CLOUD_ENGINES]


def git(repo, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()


def unexpected_catalog_render():
    raise AssertionError("catalog rendered without a catalog change")


class ManifestTest(unittest.TestCase):
    def test_engines_and_kinds_match_the_benchmark_registry(self):
        engines = MANIFEST["engines"]
        self.assertEqual({e for e, v in engines.items() if v["kind"] != "framework"}, set(ENGINE_PORTS))
        self.assertEqual({e for e, v in engines.items() if v["kind"] == "cloud"}, CLOUD_ENGINES)
        catalog = json.loads((REPO / "src/containers/sql_catalog/engines.json").read_text())
        self.assertLessEqual(set(catalog), set(engines))

    def test_engine_owned_files_select_their_engine(self):
        projects = REPO / "src/containers/dbt-server/dbt-projects"
        for engine, compose in ENGINE_COMPOSE_FILES.items():
            with self.subTest(engine=engine):
                self.assertEqual(selector.classify(compose, MANIFEST), ("engine", {engine}))
        for project in projects.glob("*/dbt_project.yml"):
            with self.subTest(project=project.parent.name):
                path = project.relative_to(REPO).as_posix()
                self.assertEqual(selector.classify(path, MANIFEST), ("engine", {project.parent.name}))

    def test_every_tracked_file_is_classified(self):
        unclassified = [
            path for path in git(REPO, "ls-files").splitlines()
            if selector.classify(path, MANIFEST)[0] == "unclassified"
        ]
        self.assertEqual(unclassified, [])


class SelectTest(unittest.TestCase):
    def select(self, *paths, catalog=unexpected_catalog_render):
        selection = selector.select(paths, MANIFEST, catalog)
        return selection.mode, set(selection.engines)

    def test_framework_and_unclassified_paths_run_every_local_engine(self):
        for path in ("src/containers/benchmark-server/services/engine_runner.py",
                     ".github/workflows/gci.yaml", "src/new-tool/run.sh",
                     "src/containers/sql_catalog/project_targets.json"):
            with self.subTest(path=path):
                self.assertEqual(self.select("docs/ENGINES.md", path), ("full", set(LOCAL)))

    def test_engine_only_change_runs_that_engine_and_its_partner(self):
        self.assertEqual(self.select("src/containers/risingwave/pins.env"), ("subset", {"risingwave"}))
        self.assertEqual(
            self.select("src/containers/dbt-server/handlers/duckdb_openivm.py"),
            ("subset", {"duckdb", "duckdb-openivm"}),
        )

    def test_cloud_and_docs_changes_run_nothing(self):
        self.assertEqual(
            self.select("docker/docker-compose.benchmark.fabric-jvm-35.yml", "docs/CI.md", "README.md"),
            ("none", set()),
        )

    def test_selecting_most_local_engines_runs_the_full_suite(self):
        four = ("docker/docker-compose.benchmark.spark.yml", "docker/docker-compose.benchmark.feldera.yml",
                "docker/docker-compose.benchmark.dbspnet.yml")
        self.assertEqual(self.select(*four)[0], "subset")
        self.assertEqual(self.select(*four, "docker/docker-compose.benchmark.risingwave.yml")[0], "full")

    def test_catalog_changes_select_the_engines_whose_rendered_sql_changed(self):
        path = "src/containers/sql_catalog/sql/spark/models/a.sql"
        self.assertEqual(self.select(path, catalog=lambda: {"feldera", "databricks-enzyme"}),
                         ("subset", {"feldera"}))
        self.assertEqual(self.select(path, catalog=set), ("none", set()))
        self.assertEqual(self.select(path, catalog=lambda: {"sqlite"})[0], "full")
        self.assertEqual(self.select(path, catalog=lambda: {"new-engine"})[0], "full")

    def test_backtest_pin_bumps(self):
        self.assertEqual(
            self.select("docker/docker-compose.benchmark.dbspnet.yml", "src/containers/dbspnet/Dockerfile"),
            ("subset", {"dbspnet"}),
        )
        self.assertEqual(self.select("src/containers/spark-openivm-build/Dockerfile"),
                         ("subset", {"spark", "spark-openivm"}))


class ResolveTest(unittest.TestCase):
    def resolve(self, event, force_full=False, base="", head=""):
        args = Namespace(event=event, force_full=force_full, base=base, head=head)
        return selector.resolve(args)

    def test_non_pull_request_events_forced_runs_and_errors_run_every_local_engine(self):
        for selection in (self.resolve("push"), self.resolve("workflow_dispatch"),
                          self.resolve("pull_request", force_full=True),
                          self.resolve("pull_request", base="0" * 40, head="HEAD")):
            self.assertEqual((selection.mode, set(selection.engines)), ("full", set(LOCAL)))


class GitDiffTest(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)
        git(self.repo, "init", "-q", "-b", "main")

    def commit(self, files):
        for path, content in files.items():
            target = self.repo / path
            if content is None:
                target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "change")
        return git(self.repo, "rev-parse", "HEAD")

    def test_renames_and_deletes_report_old_and_new_paths(self):
        base = self.commit({"a/keep.txt": "same\n" * 20, "a/gone.txt": "x"})
        head = self.commit({"a/keep.txt": None, "b/moved.txt": "same\n" * 20, "a/gone.txt": None})
        self.assertEqual(sorted(selector.changed_paths(base, head, self.repo)),
                         ["a/gone.txt", "a/keep.txt", "b/moved.txt"])

    def test_render_diff_selects_only_engines_whose_output_changed(self):
        catalog = Path(selector.CATALOG_DIR)
        for name in ("__init__.py", "generator.py"):
            target = self.repo / catalog / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(REPO / catalog / name, target)
        files = {
            catalog / "engines.json": json.dumps({"spark": "spark", "duckdb": "duckdb", "feldera": "postgres"}),
            catalog / "shared_overrides.json": "[]",
            catalog / "skips.json": "[]",
            catalog / "project_targets.json": json.dumps({"models": [
                {"engines": ["duckdb", "feldera", "spark"], "paths": ["a"]}
            ], "macros": []}),
            catalog / "sql/spark/models/a.sql": "SELECT 1 AS x\n",
            catalog / "overrides/feldera/models/a.sql": "-- catalog-override: pinned\nSELECT 1 AS x\n",
        }
        base = self.commit({path.as_posix(): text for path, text in files.items()})
        head = self.commit({(catalog / "sql/spark/models/a.sql").as_posix(): "SELECT 2 AS x\n"})
        self.assertEqual(selector.rendered_engine_diff(base, head, self.repo), {"spark", "duckdb"})

    def test_unexpected_render_errors_force_full_even_if_unchanged(self):
        catalog = Path(selector.CATALOG_DIR)
        for name in ("__init__.py", "generator.py"):
            target = self.repo / catalog / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(REPO / catalog / name, target)
        files = {
            catalog / "engines.json": json.dumps({"duckdb": "duckdb"}),
            catalog / "shared_overrides.json": "[]",
            catalog / "skips.json": "[]",
            catalog / "project_targets.json": json.dumps({"models": [
                {"engines": ["duckdb"], "paths": ["a"]}
            ], "macros": []}),
            catalog / "sql/spark/models/a.sql": "{% if bad %}SELECT 1{% endif %}\n",
            catalog / "sql/spark/inline/b.sql": "SELECT 1\n",
        }
        base = self.commit({path.as_posix(): text for path, text in files.items()})
        head = self.commit({(catalog / "sql/spark/inline/b.sql").as_posix(): "SELECT 2\n"})
        selection = selector.resolve(
            Namespace(event="pull_request", force_full=False, base=base, head=head),
            repo=self.repo,
        )
        self.assertEqual((selection.mode, set(selection.engines)), ("full", set(LOCAL)))
        self.assertIn("selector error", selection.reasons[0][0])


if __name__ == "__main__":
    unittest.main()
