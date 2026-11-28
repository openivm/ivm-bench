"""DbspNet batches are timed like Feldera's: the engine's own resume→drained duration, with
deploy (translate + compile + wire connectors) and its CPU left out of the batch."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

BENCHMARK_SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BENCHMARK_SERVER))

from services.engine_runner import EngineRunner  # noqa: E402


def _response(body, status=200):
    resp = Mock()
    resp.status_code = status
    resp.json.return_value = body
    return resp


def _runner(calls):
    runner = EngineRunner.__new__(EngineRunner)
    runner._dbt_url = "http://dbt-server:5000"
    runner._config = SimpleNamespace(scale_factor=10)
    runner._result = SimpleNamespace(extra={})
    runner._measured_duration_s = None
    runner._emit = Mock()
    runner._save_dbspnet_result = Mock()
    runner._start_cpu_measurement = Mock(side_effect=lambda b: calls.append(("cpu-start", b)))
    runner._finish_cpu_measurement = Mock(side_effect=lambda b: calls.append(("cpu-finish", b)))
    return runner


class DbspNetEngineRunnerTest(unittest.TestCase):
    def test_batch1_duration_is_the_engines_own_and_excludes_deploy(self):
        calls = []
        runner = _runner(calls)
        bodies = {
            "/deploy/dbspnet": {"compileTimeS": 1.65},
            "/resume/dbspnet": {"resumed_at_epoch_s": 1000},
            "/wait/dbspnet": {"duration_s": 13.7, "outputs": []},
        }

        def post(url, **_):
            path = url.removeprefix(runner._dbt_url)
            calls.append(("post", path))
            return _response(bodies[path])

        with patch("services.engine_runner.requests.post", side_effect=post):
            runner._run_dbspnet_batch1()

        self.assertEqual(runner._measured_duration_s, 13.7)
        # CPU is measured from resume to wait, after deploy, as for Feldera.
        self.assertEqual(calls, [
            ("post", "/deploy/dbspnet"),
            ("cpu-start", 1),
            ("post", "/resume/dbspnet"),
            ("post", "/wait/dbspnet"),
            ("cpu-finish", 1),
        ])

    def test_later_batches_use_the_engines_own_duration(self):
        calls = []
        runner = _runner(calls)
        bodies = {
            "/resume/dbspnet": {"resumed_at_epoch_s": 1000},
            "/wait/dbspnet": {"duration_s": 0.12, "outputs": []},
        }

        def post(url, **_):
            path = url.removeprefix(runner._dbt_url)
            calls.append(("post", path))
            return _response(bodies[path])

        with patch("services.engine_runner.requests.post", side_effect=post):
            runner._run_dbspnet_wait(2)

        self.assertEqual(runner._measured_duration_s, 0.12)
        self.assertEqual(calls, [
            ("cpu-start", 2),
            ("post", "/resume/dbspnet"),
            ("post", "/wait/dbspnet"),
            ("cpu-finish", 2),
        ])

    def test_a_missing_duration_falls_back_to_wall_clock(self):
        runner = _runner([])
        runner._record_dbspnet_duration({"outputs": []})
        self.assertIsNone(runner._measured_duration_s)
        runner._record_dbspnet_duration({"duration_s": True})
        self.assertIsNone(runner._measured_duration_s)

    def test_a_failed_wait_raises_and_records_no_duration(self):
        runner = _runner([])

        def post(url, **_):
            path = url.removeprefix(runner._dbt_url)
            if path == "/wait/dbspnet":
                return _response({"error": "boom"}, status=500)
            return _response({"resumed_at_epoch_s": 1000})

        with patch("services.engine_runner.requests.post", side_effect=post):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                runner._run_dbspnet_wait(3)
        self.assertIsNone(runner._measured_duration_s)
        runner._finish_cpu_measurement.assert_called_once_with(3)


if __name__ == "__main__":
    unittest.main()
