import threading

from django.apps import AppConfig

_dev_warmup_lock = threading.Lock()
_dev_warmup_started = False


def warm_dev_server(sender=None, **kwargs):
    """Load the models in runserver's SERVING process, before a question does.

    Gunicorn workers and Celery children are warmed by their own hooks; the
    dev server had none, so its first chat question after every start or
    autoreload spent ~35 s importing torch and loading two models while the
    user watched "Searching your documents" (measured 2026-10-11).

    `autoreload_started` is sent only by the reloader CHILD (the process that
    serves requests), after apps are ready - never by the reloader parent,
    the test runner or any other management command. Each autoreload is a
    new child, so each warms once. In a thread, like the Celery hook: the
    receiver runs on the reloader's main thread, and a question arriving
    mid-warm-up simply waits on the provider's own load lock.
    """
    global _dev_warmup_started
    from documents.processing import warmup

    if not warmup.enabled():
        return
    with _dev_warmup_lock:
        if _dev_warmup_started:
            return
        _dev_warmup_started = True
    threading.Thread(
        target=warmup.warm,
        kwargs={"reason": "runserver"},
        name="model-warmup",
        daemon=True,
    ).start()


class DocumentsConfig(AppConfig):
    name = "documents"

    def ready(self):
        # Imported for the side effect of registering the receivers. Done
        # here rather than at module import because models are not loaded
        # when the AppConfig class itself is.
        from django.utils.autoreload import autoreload_started

        from . import signals  # noqa: F401

        autoreload_started.connect(
            warm_dev_server, dispatch_uid="documents.warm_dev_server"
        )
