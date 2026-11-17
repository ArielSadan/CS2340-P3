"""Entry points for the Developer page and `manage.py ingest`.

The knowledge app only *reads* sources here (files and web pages -> plain text).
Everything after that (cleaning, chunking, embedding, storing) is the chat
pipeline: chat.services.ingestion.ingest_document -> retrieval.add_chunks.
"""
import logging
import re
from dataclasses import dataclass

from django.conf import settings
from django.db import IntegrityError

from chat.models import Document
from chat.services.ingestion import ingest_document
from chat.services.retrieval import is_rate_limit

from ..conf import get_setting
from .errors import FileTooLarge, IngestionError
from .extract import extract_fetched, extract_upload
from .fetch import fetch_url, normalize_url

logger = logging.getLogger(__name__)

ADDED, REPLACED, SKIPPED, ERROR = "added", "replaced", "skipped", "error"
UNEXPECTED = "Something went wrong while ingesting this item. Check the server log."
_PAGE_HEADING = re.compile(r"^Page \d+$")


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


def _to_text(extracted):
    """Flatten extracted sections into one document. Headings stay in the text
    (the chat chunker keeps a short heading with its paragraph); PDF page labels
    are dropped because they carry no meaning."""
    parts = []
    for heading, text in extracted.sections:
        if heading and not _PAGE_HEADING.match(heading):
            parts.append(heading)
        parts.append(text)
    return "\n\n".join(p.strip() for p in parts if p and p.strip())


def _save(extracted, label, *, existing, file_name="", source_url="", source_type="other"):
    """Hand one extracted source to the chat pipeline and describe the outcome."""
    if not getattr(settings, "GEMINI_API_KEY", ""):
        return IngestResult(label, ERROR, "The Gemini API key isn't set (GEMINI_API_KEY), so documents can't be processed.")

    content = _to_text(extracted)
    title = extracted.title[:500]

    # Identical content already in the knowledge base (under any name): skip.
    same = Document.objects.filter(content=content).first()
    if same and same.chunks.exists():
        return IngestResult(
            label, SKIPPED, f"Already ingested (same content as “{same.title}”). Skipped.",
            same.id, same.chunks.count(), same.title,
        )
    if existing is None:
        existing = same  # chunks were cleared (#11): reuse the empty record
    had_chunks = existing is not None and existing.chunks.exists()

    document = existing or Document()
    document.title = title
    document.content = content
    document.file_name = file_name or document.file_name
    document.source_url = source_url
    document.source_type = source_type

    # ingest_document embeds first, then saves the document and swaps its chunks
    # in one transaction. If Gemini fails, nothing changes and the old version stays.
    try:
        count = ingest_document(document)
    except ValueError as exc:
        return IngestResult(label, ERROR, str(exc))
    except IntegrityError:
        return IngestResult(
            label, ERROR,
            f"Another document is already titled “{title}” with the same link. Rename the file or delete the other document first.",
        )
    except Exception as exc:  # the Gemini SDK raises several error types
        logger.exception("Embedding failed for %s", label)
        if is_rate_limit(exc):
            return IngestResult(label, ERROR, "Gemini's free-tier limit was reached. Wait a minute and try again.")
        return IngestResult(label, ERROR, "Couldn't reach Gemini to process this document. Try again in a minute.")

    if had_chunks:
        status, message = REPLACED, f"Updated to the new version: {count} chunks (old version removed)."
    else:
        status, message = ADDED, f"Added: {count} chunks."
    logger.info("Ingested %s (%s): %d chunks", title, status, count)
    return IngestResult(label, status, message, document.id, count, document.title)


def ingest_uploaded_file(uploaded_file):
    """Method 1: a file uploaded on the Developer page (Django UploadedFile)."""
    name = uploaded_file.name
    try:
        max_mb = get_setting("KNOWLEDGE_MAX_UPLOAD_MB")
        if uploaded_file.size > max_mb * 1024 * 1024:
            raise FileTooLarge(f"File is over the {max_mb} MB limit.")
        extracted = extract_upload(name, uploaded_file.read())
        # The same file name = a new version of that document.
        existing = Document.objects.filter(file_name__iexact=name, source_url="").first()
        return _save(extracted, name, existing=existing, file_name=name)
    except IngestionError as exc:
        return IngestResult(name, ERROR, exc.user_message)
    except Exception:
        logger.exception("Unexpected error ingesting file %s", name)
        return IngestResult(name, ERROR, UNEXPECTED)


def ingest_url(raw_url, session=None):
    """Method 2: a web page URL entered on the Developer page."""
    label = (raw_url or "").strip()
    try:
        url = normalize_url(raw_url)
        page = fetch_url(url, session=session)
        extracted = extract_fetched(page)
        existing = Document.objects.filter(source_url__in={url, page.url}).first()
        return _save(extracted, label, existing=existing, source_url=page.url, source_type="article")
    except IngestionError as exc:
        return IngestResult(label, ERROR, exc.user_message)
    except Exception:
        logger.exception("Unexpected error ingesting URL %s", label)
        return IngestResult(label, ERROR, UNEXPECTED)
