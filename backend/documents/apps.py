from django.apps import AppConfig


class DocumentsConfig(AppConfig):
    name = 'documents'

    def ready(self):
        # Imported for the side effect of registering the receivers. Done
        # here rather than at module import because models are not loaded
        # when the AppConfig class itself is.
        from . import signals  # noqa: F401
