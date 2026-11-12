from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
 
from chat.services.rag import answer_question
from chat.services.retrieval import RetrievalError, retrieve_chunks
 
 
class Command(BaseCommand):
    help = "Ask the chatbot a question, or print retrieval scores to tune RAG_MIN_SIMILARITY."
 
    def add_arguments(self, parser):
        parser.add_argument("questions", nargs="*")
        parser.add_argument("--scores", action="store_true", help="Only print retrieval scores")
        parser.add_argument("--file", help="Text file with one question per line (# = comment)")
 
    def handle(self, *args, **options):
        questions = list(options["questions"])
        if options["file"]:
            with open(options["file"], encoding="utf-8") as fh:
                questions += [q.strip() for q in fh if q.strip() and not q.lstrip().startswith("#")]
        if not questions:
            raise CommandError("Give at least one question.")
 
        threshold = settings.RAG_MIN_SIMILARITY
        for question in questions:
            if options["scores"]:
                try:
                    chunks = retrieve_chunks(question, 1)
                except RetrievalError as exc:
                    raise CommandError(f"Retrieval failed: {exc}")
                if not chunks:
                    self.stdout.write(f"  (empty knowledge base)  {question}")
                    continue
                best = chunks[0]
                mark = "PASS" if best.score >= threshold else "fail"
                self.stdout.write(f"{best.score:.3f} {mark}  {question}\n             best match: {best.source}")
                continue
 
            result = answer_question(question)
            self.stdout.write(self.style.MIGRATE_HEADING(f"\nQ: {question}"))
            self.stdout.write(
                f"[{result.status} / {result.reason}] {result.latency_ms} ms (threshold {threshold})"
            )
            self.stdout.write(result.answer)
            if result.sources:
                self.stdout.write("Sources: " + ", ".join(result.sources))