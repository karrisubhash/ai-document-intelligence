"""Gunicorn configuration, and the two hooks Prometheus needs to be correct.

Without these, `/metrics` under gunicorn is not merely imprecise - it is
wrong in the flattering direction. Each worker keeps its own registry, so a
scrape answered by one of three workers reports about a third of the
requests, a third of the errors, and a latency histogram built from a third
of the samples. Nothing looks broken; the dashboard just quietly understates
every problem.

`PROMETHEUS_MULTIPROC_DIR` makes every worker write its samples to a shared
directory that the scrape aggregates across. That only stays correct if two
things happen, and both of them are hooks:

  * dead workers are reaped, or a worker recycled by --max-requests leaves
    its counters behind forever and its traffic is counted twice;
  * the directory starts empty, or samples from the PREVIOUS run of the
    container are served as though they were current.

Used by docker-compose.prod.yml:  gunicorn -c gunicorn.conf.py config.wsgi
"""

import os
import shutil

# Bind and worker counts stay in the compose command, which is where an
# operator expects to tune them. This file is only about correctness.


def _multiproc_dir():
    return os.environ.get("PROMETHEUS_MULTIPROC_DIR", "")


def on_starting(server):
    """Clear samples left by a previous run of this container.

    prometheus_client never removes these files itself, so a restarted
    container would serve its own history as live data - including
    counters for a build that is no longer deployed.
    """
    directory = _multiproc_dir()
    if not directory:
        return

    shutil.rmtree(directory, ignore_errors=True)
    os.makedirs(directory, exist_ok=True)
    server.log.info("prometheus: cleared %s", directory)


def child_exit(server, worker):
    """Reap a dead worker's samples.

    Counters are cumulative per process. A worker recycled by
    --max-tasks-per-child or killed by a timeout leaves a file behind whose
    counts are then added to its replacement's forever, so request totals
    drift upward with every recycle. mark_process_dead folds the gauges and
    drops what should not outlive the process.
    """
    if not _multiproc_dir():
        return

    from prometheus_client import multiprocess

    multiprocess.mark_process_dead(worker.pid)


def post_worker_init(worker):
    """Load the configured local models before this worker takes traffic.

    Otherwise the first search each worker serves after a deploy or a
    --max-requests recycle pays for the model load while a person waits.
    See documents/processing/warmup.py.
    """
    from documents.processing import warmup

    if warmup.enabled():
        warmup.warm(reason="gunicorn")
