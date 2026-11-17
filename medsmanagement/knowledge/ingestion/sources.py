"""Turn files, web pages and pasted text into drafts for the review form.

The knowledge app only *reads* sources (files and web pages -> plain text).
A draft is then reviewed by a developer on the document form
(chat/developer_views.py: document_review) and saved through the chat pipeline:
chat.services.ingestion.ingest_document -> cleaning, chunking, embedding, storing.

`manage.py ingest` has no one to review, so it uses ingest_uploaded_file /
ingest_url, which save the draft straight away.
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
UNEXPECTED = "Something went wrong while reading this item. Check the server log."
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


def _draft(item, *, title, content, existing=None, source_type="other", source_url="", file_name=""):
    """A draft is a plain dict so it can wait in the session until it's reviewed.
    The keys match the fields of chat.forms.DocumentForm."""
    return {
        "item": item,
        "title": title[:500],
        "source_type": source_type,
        "source_url": source_url,
        "file_name": file_name[:255],
        "content": content,
        # Set when this replaces a document that's already in the knowledge base.
        "document_id": existing.pk if existing else None,
    }


def _duplicate(item, content):
    """Identical content that's already ingested (under any name) -> a 'skipped' result."""
    same = Document.objects.filter(content=content).first()
    if same and same.chunks.exists():
        return IngestResult(
            item, SKIPPED, f"Already ingested (same content as “{same.title}”). Skipped.",
            same.id, same.chunks.count(), same.title,
        )
    return None


def prepare_uploaded_file(uploaded_file):
    """Read an uploaded file. Returns a draft dict, or an IngestResult for errors/skips."""
    name = uploaded_file.name
    try:
        max_mb = get_setting("KNOWLEDGE_MAX_UPLOAD_MB")
        if uploaded_file.size > max_mb * 1024 * 1024:
            raise FileTooLarge(f"File is over the {max_mb} MB limit.")
        extracted = extract_upload(name, uploaded_file.read())
        content = _to_text(extracted)
        duplicate = _duplicate(name, content)
        if duplicate:
            return duplicate
        # The same file name = a new version of that document.
        existing = (
            Document.objects.filter(file_name__iexact=name, source_url="").first()
            or Document.objects.filter(content=content).first()  # chunks were cleared (#11)
        )
        return _draft(name, title=extracted.title, content=content, existing=existing, file_name=name)
    except IngestionError as exc:
        return IngestResult(name, ERROR, exc.user_message)
    except Exception:
        logger.exception("Unexpected error reading file %s", name)
        return IngestResult(name, ERROR, UNEXPECTED)


def prepare_url(raw_url, session=None):
    """Fetch a web page. Returns a draft dict, or an IngestResult for errors/skips."""
    label = (raw_url or "").strip()
    try:
        url = normalize_url(raw_url)
        page = fetch_url(url, session=session)
        extracted = extract_fetched(page)
        content = _to_text(extracted)
        duplicate = _duplicate(label, content)
        if duplicate:
            return duplicate
        existing = (
            Document.objects.filter(source_url__in={url, page.url}).first()
            or Document.objects.filter(content=content).first()
        )
        return _draft(label, title=extracted.title, content=content, existing=existing,
                      source_type="article", source_url=page.url)
    except IngestionError as exc:
        return IngestResult(label, ERROR, exc.user_message)
    except Exception:
        logger.exception("Unexpected error reading URL %s", label)
        return IngestResult(label, ERROR, UNEXPECTED)


def prepare_text(title, text, source_url=""):
    """Pasted text. Returns a draft dict, or an IngestResult when it's empty."""
    title, text = (title or "").strip(), (text or "").strip()
    label = title or "Pasted text"
    if not text:
        return IngestResult(label, ERROR, "Paste some text first.")
    existing = Document.objects.filter(title=title, source_url=source_url).first() if title else None
    return _draft(label, title=title, content=text, existing=existing, source_url=source_url)


# ------------------------------------------------------------ no review (manage.py ingest)

def save_draft(draft):
    """Ingest a draft without the review form."""
    label = draft["item"]
    if not getattr(settings, "GEMINI_API_KEY", ""):
        return IngestResult(label, ERROR, "The Gemini API key isn't set (GEMINI_API_KEY), so documents can't be processed.")

    existing = Document.objects.filter(pk=draft["document_id"]).first() if draft["document_id"] else None
    had_chunks = existing is not None and existing.chunks.exists()
    document = existing or Document()
    for field in ("title", "source_type", "source_url", "content"):
        setattr(document, field, draft[field])
    document.file_name = draft["file_name"] or document.file_name

    # ingest_document embeds first, then saves the document and swaps its chunks
    # in one transaction. If Gemini fails, nothing changes and the old version stays.
    try:
        count = ingest_document(document)
    except ValueError as exc:
        return IngestResult(label, ERROR, str(exc))
    except IntegrityError:
        return IngestResult(
            label, ERROR,
            f"Another document is already titled “{draft['title']}” with the same link. Rename it or delete the other document first.",
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
    logger.info("Ingested %s (%s): %d chunks", document.title, status, count)
    return IngestResult(label, status, message, document.id, count, document.title)


def ingest_uploaded_file(uploaded_file):
    prepared = prepare_uploaded_file(uploaded_file)
    return prepared if isinstance(prepared, IngestResult) else save_draft(prepared)


def ingest_url(raw_url, session=None):
    prepared = prepare_url(raw_url, session=session)
    return prepared if isinstance(prepared, IngestResult) else save_draft(prepared)
