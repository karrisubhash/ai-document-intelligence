import os

from celery import Celery
from celery.signals import worker_init, worker_process_init, worker_process_shutdown

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("config")

app.config_from_object(
    "django.conf:settings",
    namespace="CELERY",
)

app.autodiscover_tasks()

# Celery's autodiscovery looks for a module called exactly `tasks` in each
# installed app, and nothing else. `documents/tasks_reindex.py` is not that,
# so every task it defines was invisible to the worker:
#
#   $ python manage.py reindex
#   Queued 226 on the index queue
#   worker | Received unregistered task of type
#           'documents.tasks_reindex.reindex_content_task'.
#           The message has been ignored and discarded.
#
# The command reported success, the queue drained, and not one vector was
# written - the failure was on the WORKER, while the process that looked
# like it had done the work exited 0. That is the worst shape a bug can
# have, and it meant the controlled re-index of section 11.4 had never
# actually run end to end.
#
# Declared as a second autodiscovery pass rather than by importing the
# module from `tasks.py`, because an import for its side effect is the kind
# of line someone tidies away later, and this is where a reader already
# looks to find out which modules the worker knows about.
app.autodiscover_tasks(related_name="tasks_reindex")


@worker_process_init.connect
def warm_models(**kwargs):
    """Start loading the embedding model in each worker child - in a thread.

    Indexing embeds every chunk, so an unwarmed child loads the model inside
    the first document's INDEXING stage. Embedding only: workers never
    rerank. See documents/processing/warmup.py.

    NEVER INLINE. This hook runs inside the child before it reports itself
    up, and Celery's master waits only `worker_proc_alive_timeout` (4 s by
    default) before killing a child that has not. Importing torch and
    loading the model takes about 10 s, so the first version of this hook -
    which loaded inline - had every recycled child SIGKILLed during start-up
    and replaced by another that met the same fate. Measured on the
    production stack: with --max-tasks-per-child=100 and two children,
    ingestion stopped dead after exactly 200 documents, with the worker
    looping "Timed out waiting for UP message" forever.

    In a thread, the child reports up at once. A task that needs the model
    meanwhile waits on the provider's own load lock, so nothing ever runs
    against a half-loaded model.
    """
    import threading

    from documents.processing import warmup

    if warmup.enabled():
        threading.Thread(
            target=warmup.warm,
            kwargs={"reason": "celery", "components": ("embedding",)},
            name="model-warmup",
            daemon=True,
        ).start()


@worker_init.connect
def serve_worker_metrics(**kwargs):
    """Expose this worker container's metrics for Prometheus to scrape.

    The pipeline's metrics - stage counts and durations, job outcomes,
    failures, pages, indexed documents - are recorded where the pipeline
    runs: in the worker's child processes. Prometheus scraped only the
    backend, so every one of them read zero forever and the alerts built on
    them could never fire. Each child writes its samples to the container's
    PROMETHEUS_MULTIPROC_DIR (a tmpfs, so it starts empty with the
    container); this main process serves them aggregated, the way gunicorn's
    /metrics does for its workers.
    """
    port = os.environ.get("CELERY_METRICS_PORT", "")
    directory = os.environ.get("PROMETHEUS_MULTIPROC_DIR", "")
    if not port or not directory:
        return

    from prometheus_client import CollectorRegistry, multiprocess, start_http_server

    registry = CollectorRegistry()
    multiprocess.MultiProcessCollector(registry, path=directory)
    start_http_server(int(port), registry=registry)


@worker_process_shutdown.connect
def reap_worker_metrics(pid=None, **kwargs):
    """Drop a recycled child's live gauges, as gunicorn's child_exit does."""
    if not os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        return

    from prometheus_client import multiprocess

    multiprocess.mark_process_dead(pid or os.getpid())
