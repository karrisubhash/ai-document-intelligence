from django.apps import AppConfig


class NotificationsConfig(AppConfig):
    name = "notifications"

    def ready(self):
        # The producers listen to other apps' models rather than being
        # called by them: the pipeline and the sign-up services stay unaware
        # that anyone is told anything, and removing this app removes every
        # notification without touching them. Connected here, not at import,
        # because the models are not loaded when this module is.
        from django.db.models.signals import post_save

        from accounts.models import SignupRequest
        from documents.models import ProcessingJob

        from . import attention, processing, signups

        post_save.connect(
            processing.job_saved,
            sender=ProcessingJob,
            dispatch_uid="notifications.processing.job_saved",
        )
        post_save.connect(
            signups.signup_saved,
            sender=SignupRequest,
            dispatch_uid="notifications.signups.signup_saved",
        )
        post_save.connect(
            attention.job_saved,
            sender=ProcessingJob,
            dispatch_uid="notifications.attention.job_saved",
        )
        post_save.connect(
            attention.signup_saved,
            sender=SignupRequest,
            dispatch_uid="notifications.attention.signup_saved",
        )
