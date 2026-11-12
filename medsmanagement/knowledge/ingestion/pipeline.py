import hashlib
import logging
import time
from dataclasses import dataclass
 
from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Count
 
from chat.services import retrieval
 
from ..chunking import chunk_sections
from ..conf import get_setting
from ..models import Document
from .cleaning import clean_text
from .errors import IngestionError, NoReadableText
 
logger = logging.getLogger(__name__)
 
ADDED, REPLACED, SKIPPED, ERROR = "added", "replaced", "skipped", "error"
 
 
@dataclass
class IngestResult:
    item: str             # file name or URL, as the developer entered it
    status: str           # added | replaced | skipped | error
    message: str          # readable explanation for the Developer page
    document_id: int | None = None
    chunk_count: int = 0
    title: str = ""
 
    @property
    def ok(self):
        return self.status != ERROR
 
 
def _content_hash(sections):
    normalized = "\n".join(f"{h}\n{' '.join(t.split())}" for h, t in sections)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
 
 
def _is_rate_limit(exc):
    text = str(exc)
    return getattr(exc, "code", None) == 429 or "RESOURCE_EXHAUSTED" in text or " 429" in text
 
 
def _embed_with_retries(texts):
    """Gemini's free tier rate-limits quickly when a long document is ingested,
    so wait and retry instead of failing the whole item."""
    if not getattr(settings, "GEMINI_API_KEY", ""):
        raise IngestionError("The Gemini API key isn't set (GEMINI_API_KEY), so documents can't be processed.")
    delays = get_setting("EMBED_RETRY_DELAYS")
    for attempt in range(len(delays) + 1):
        try:
            return retrieval.embed_documents(texts)
        except Exception as exc:  # the Gemini SDK raises several error types
            if _is_rate_limit(exc) and attempt < len(delays):
                logger.warning("Gemini rate limit while embedding; retrying in %ss", delays[attempt])
                time.sleep(delays[attempt])
                continue
            logger.exception("Embedding failed")
            if _is_rate_limit(exc):
                raise IngestionError("Gemini's free-tier limit was reached. Wait a minute and try again.") from exc
            raise IngestionError("Couldn't reach Gemini to process this document. Try again in a minute.") from exc
 
 
def ingest_extracted(*, extracted, source_type, source_key, source_url="", item_label=""):
    """Clean, chunk, embed and store one extracted document.
    Raises IngestionError; sources.py turns that into a per-item result."""
    item_label = item_label or source_key
 
    # 1. Clean every section with the #7 cleaning step.
    sections = []
    for heading, text in extracted.sections:
        cleaned = (clean_text(text) or "").strip()
        if cleaned:
            sections.append((heading, cleaned))
    word_count = sum(len(t.split()) for _, t in sections)
    if word_count < get_setting("MIN_DOCUMENT_WORDS"):
        raise NoReadableText(f"Not enough readable text after cleaning ({word_count} words).")
 
    # 2. Duplicate check.
    content_hash = _content_hash(sections)
    same_content = (
        Document.objects.filter(content_hash=content_hash).annotate(n=Count("chunks")).first()
    )
    if same_content and same_content.n > 0:
        return IngestResult(
            item_label, SKIPPED,
            f"Already ingested (same content as “{same_content.title}”). Skipped.",
            same_content.id, same_content.n, same_content.title,
        )
    previous = Document.objects.filter(source_key=source_key).first()
    replacing = previous is not None and previous.pk != getattr(same_content, "pk", None)
 
    # 3. Chunk and embed BEFORE opening the transaction, so the slow Gemini call
    #    doesn't hold a write lock on SQLite while the chat is being used.
    #    The title and section go into the chunk text: it helps retrieval and
    #    tells the model what each excerpt is about.
    title = extracted.title[:300]
    chunks = chunk_sections(sections)
    items = [(title, c.text_for_embedding(title)) for c in chunks]
    vectors = _embed_with_retries([text for _, text in items])
 
    # 4. Store everything at once. If anything fails, nothing is saved and the
    #    old version (if any) is kept.
    try:
        with transaction.atomic():
            if same_content:          # chunks were cleared (#11): drop the empty record
                same_content.delete()
            if replacing:
                previous.delete()     # its chunks are deleted with it
            document = Document.objects.create(
                title=title,
                source_type=source_type,
                source_key=source_key[:1000],
                source_url=source_url[:1000],
                content_hash=content_hash,
                word_count=word_count,
            )
            count = retrieval.store_chunks(items, vectors, document=document)
    except IntegrityError:
        # The same thing was ingested at the same moment by someone else.
        return IngestResult(item_label, SKIPPED, "Already ingested. Skipped.")
 
    if replacing:
        status, message = REPLACED, f"Updated to the new version: {count} chunks (old version removed)."
    else:
        status, message = ADDED, f"Added: {count} chunks."
    logger.info("Ingested %s (%s): %d chunks", title, status, count)
    return IngestResult(item_label, status, message, document.id, count, title)