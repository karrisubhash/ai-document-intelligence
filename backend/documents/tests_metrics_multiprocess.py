"""/metrics tells the truth when more than one process serves it.

Under gunicorn each worker keeps its own registry, so a scrape answered by
one of three workers reports roughly a third of the traffic. That is not
noise - it is wrong in the flattering direction, and every error count and
latency reads low while nothing appears broken.

These tests use REAL subprocesses rather than mocks, because the thing
under test is precisely what happens across process boundaries. A mock of
two processes would pass without the mechanism working at all.
"""

import os
import subprocess
import sys
import tempfile
from unittest import mock

from django.test import TestCase

from documents.observability import metrics

# Runs in a fresh interpreter with PROMETHEUS_MULTIPROC_DIR already set,
# which matters: prometheus_client picks its value class at import time, so
# a process that imports it first and sets the variable afterwards writes
# nothing to the shared directory.
WORKER = """
import os, sys
from prometheus_client import Counter, CollectorRegistry

registry = CollectorRegistry()
counter = Counter("adi_probe_total", "probe", registry=registry)
counter.inc(float(sys.argv[1]))
"""


def run_worker(directory, amount):
    environment = dict(os.environ, PROMETHEUS_MULTIPROC_DIR=directory)
    result = subprocess.run(
        [sys.executable, "-c", WORKER, str(amount)],
        env=environment,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result


class MultiProcessAggregationTests(TestCase):
    def test_samples_from_separate_processes_are_summed(self):
        """The actual claim: N workers, one honest total."""
        with tempfile.TemporaryDirectory() as directory:
            run_worker(directory, 3)
            run_worker(directory, 4)

            self.assertTrue(
                os.listdir(directory),
                "no sample files were written; the workers were not in "
                "multiprocess mode at all",
            )

            with mock.patch.dict(
                os.environ, {"PROMETHEUS_MULTIPROC_DIR": directory}
            ):
                payload, _ = metrics.render()

            body = payload.decode()

        self.assertIn("adi_probe_total", body)
        total = next(
            float(line.rsplit(" ", 1)[1])
            for line in body.splitlines()
            if line.startswith("adi_probe_total ")
        )
        self.assertEqual(total, 7.0, f"expected 3+4 across two processes\n{body}")

    def test_one_process_is_not_multiplied(self):
        with tempfile.TemporaryDirectory() as directory:
            run_worker(directory, 5)

            with mock.patch.dict(
                os.environ, {"PROMETHEUS_MULTIPROC_DIR": directory}
            ):
                payload, _ = metrics.render()

            body = payload.decode()

        total = next(
            float(line.rsplit(" ", 1)[1])
            for line in body.splitlines()
            if line.startswith("adi_probe_total ")
        )
        self.assertEqual(total, 5.0)

    def test_without_the_variable_the_in_process_registry_is_used(self):
        """Single-process deployments and the dev stack must be unaffected."""
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PROMETHEUS_MULTIPROC_DIR", None)
            payload, content_type = metrics.render()

        self.assertIn("adi_http_requests_total", payload.decode())
        self.assertIn("text/plain", content_type)

    def test_the_directory_is_reported(self):
        with mock.patch.dict(os.environ, {"PROMETHEUS_MULTIPROC_DIR": "/tmp/p"}):
            self.assertEqual(metrics.multiprocess_dir(), "/tmp/p")

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PROMETHEUS_MULTIPROC_DIR", None)
            self.assertEqual(metrics.multiprocess_dir(), "")


class GunicornHookTests(TestCase):
    """The hooks are what keep the aggregation honest over time."""

    def load_config(self):
        import importlib.util
        from pathlib import Path

        path = Path(__file__).resolve().parent.parent / "gunicorn.conf.py"
        spec = importlib.util.spec_from_file_location("gunicorn_conf", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_a_previous_runs_samples_are_cleared_on_start(self):
        """A restarted container must not serve its own history as live
        data - including counters for a build no longer deployed."""
        config = self.load_config()

        with tempfile.TemporaryDirectory() as directory:
            stale = os.path.join(directory, "counter_999.db")
            with open(stale, "wb") as handle:
                handle.write(b"stale")

            with mock.patch.dict(
                os.environ, {"PROMETHEUS_MULTIPROC_DIR": directory}
            ):
                config.on_starting(mock.Mock())

            self.assertTrue(os.path.isdir(directory))
            self.assertEqual(os.listdir(directory), [])

    def test_a_dead_worker_is_reaped(self):
        """A worker recycled by --max-requests otherwise leaves counters
        behind that are added to its replacement's forever."""
        config = self.load_config()

        with tempfile.TemporaryDirectory() as directory:
            run_worker(directory, 2)

            with mock.patch.dict(
                os.environ, {"PROMETHEUS_MULTIPROC_DIR": directory}
            ):
                with mock.patch(
                    "prometheus_client.multiprocess.mark_process_dead"
                ) as reap:
                    config.child_exit(mock.Mock(), mock.Mock(pid=4321))

        reap.assert_called_once_with(4321)

    def test_the_hooks_are_inert_without_the_variable(self):
        """The dev stack runs a single process and must not be touched."""
        config = self.load_config()

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PROMETHEUS_MULTIPROC_DIR", None)
            config.on_starting(mock.Mock())
            config.child_exit(mock.Mock(), mock.Mock(pid=1))
