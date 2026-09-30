import os

from celery import Celery

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
