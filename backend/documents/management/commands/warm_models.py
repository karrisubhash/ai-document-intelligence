"""Download and load the configured local models, once, at deploy time.

    python manage.py warm_models

Run it after a deploy on a machine with network access to populate the
model cache (HF_HOME, a named volume in the compose files). Afterwards the
providers load from the cache without contacting the Hub, and
HF_HUB_OFFLINE=1 can be set to make that guaranteed rather than merely
likely.
"""

from django.core.management.base import BaseCommand, CommandError

from documents.processing import warmup


class Command(BaseCommand):
    help = "Download (if needed) and load the configured in-process AI models."

    def handle(self, *args, **options):
        loaded = warmup.warm(reason="command")
        if not loaded:
            self.stdout.write(
                "No in-process models are configured (hashing embeddings and the "
                "lexical reranker need none)."
            )
            return

        failed = False
        for component, outcome in loaded.items():
            if isinstance(outcome, float):
                self.stdout.write(f"  {component}: loaded in {outcome:.2f}s")
            elif component == "answerer":
                # A hosted answerer's client, not a model: often no key on
                # the machine that runs this. The answer path falls back.
                self.stdout.write(
                    self.style.WARNING(f"  answerer: client not ready ({outcome})")
                )
            else:
                failed = True
                self.stdout.write(
                    self.style.ERROR(f"  {component}: FAILED ({outcome})")
                )

        if failed:
            raise CommandError(
                "A configured model could not be loaded. Search will degrade to "
                "lexical results until it can; see the log for the cause."
            )
        self.stdout.write(self.style.SUCCESS("Models are cached and loadable."))
