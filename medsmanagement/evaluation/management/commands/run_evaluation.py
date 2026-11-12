
from django.core.management.base import BaseCommand, CommandError

from evaluation.runner import run_evaluation


class Command(BaseCommand):
    help = "Run golden question accuracy tests against the RAG chatbot"

    def handle(self, *args, **options):
        self.stdout.write("Starting chatbot accuracy evaluation...")

        try:
            run = run_evaluation()
        except (ValueError, OSError) as error:
            raise CommandError(str(error)) from error

        self.stdout.write(
            self.style.SUCCESS(
                f"Evaluation #{run.id} completed.\n"
                f"Passed: {run.passed}/{run.total}\n"
                f"Accuracy: {run.accuracy:.2f}%\n"
                f"Source hit rate: {run.source_hit_rate:.2f}%\n"
                f"Average latency: {run.average_latency_ms:.0f} ms"
            )
        )
