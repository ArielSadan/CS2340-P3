
import logging
from dataclasses import dataclass, field

from django.db import transaction

from chat.models import Document, DocumentChunk
from chat.services.retrieval import add_chunks

logger = logging.getLogger(__name__)

# Temporary splitter for rebuilding saved document text.
# Uses overlapping word groups to preserve context.
#only deletes documentchunk rows and does not delete records so originial content is still available
CHUNK_WORDS = 150
OVERLAP_WORDS = 30


def split_text(text):
    words = text.split()

    if not words:
        return []

    step = CHUNK_WORDS - OVERLAP_WORDS
    chunks = []

    for start in range(0, len(words), step):
        part = words[start:start + CHUNK_WORDS]

        if part:
            chunks.append(" ".join(part))

        if start + CHUNK_WORDS >= len(words):
            break

    return chunks


@transaction.atomic
def clear_knowledge_base():

    deleted_count, _ = DocumentChunk.objects.all().delete()
    logger.info("Knowledge base cleared: %s chunks", deleted_count)
    return deleted_count


@dataclass
class ReloadSummary:
    processed: int = 0
    skipped: int = 0
    chunks_created: int = 0
    failures: list = field(default_factory=list)


def reload_knowledge_base():
  
    summary = ReloadSummary()

    for document in Document.objects.order_by("id").iterator():
        text = (document.cleaned_content or document.content).strip()

        if not text:
            summary.skipped += 1
            logger.warning(
                "Skipping document %s: no saved text", document.pk
            )
            continue

        chunks = split_text(text)

        if not chunks:
            summary.skipped += 1
            continue

        items = [
            (document.title, chunk, document.source_url)
            for chunk in chunks
        ]

        try:
            # Uses the teams existing Gemini embedding pipeline.
            # Embeds first then replaces the document's old chunks.
            count = add_chunks(items, document=document)

            summary.processed += 1
            summary.chunks_created += count

        except Exception as exc:
            logger.exception(
                "Failed to reload document %s", document.pk
            )
            summary.failures.append(
                f"{document.title}: {type(exc).__name__}"
            )

    return summary
