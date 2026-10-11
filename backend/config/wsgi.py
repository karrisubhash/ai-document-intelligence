"""
WSGI config for config project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/6.1/howto/deployment/wsgi/
"""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

# Before get_wsgi_application(): Django instrumentation inserts its
# middleware, and the middleware chain is built when the app is created.
# A no-op unless OTEL_EXPORTER_OTLP_ENDPOINT is set (spec 20).
from documents.observability.tracing import configure_tracing  # noqa: E402

configure_tracing("adi-api")

application = get_wsgi_application()
