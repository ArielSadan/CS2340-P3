import logging
import threading
import time
from dataclasses import dataclass

import numpy as np
from django.conf import settings
from django.db import transaction
from django.db.models import Count, Max
from django.utils import timezone

from chat.models import Document, DocumentChunk

from .gemini import embed_documents, embed_query

logger = logging.getLogger(__name__)

# Seconds to wait between retries when Gemini's free tier says "too many requests".
DEFAULT_EMBED_RETRY_DELAYS = [2, 5, 10, 20]


@dataclass
class Chunk:
    text: str
    source: str
    score: float
    source_url: str = ""


class RetrievalError(Exception):
    """Raised when embedding or the database fails."""


def to_bytes(vector) -> bytes:
    return np.asarray(vector, dtype=np.float32).tobytes()


def from_bytes(blob) -> np.ndarray:
    return np.frombuffer(bytes(blob), dtype=np.float32)


def is_rate_limit(exc):
    text = str(exc)
    return getattr(exc, "code", None) == 429 or "RESOURCE_EXHAUSTED" in text or " 429" in text


def embed_with_retries(texts):
    """Long documents hit Gemini's free-tier rate limit quickly, so wait and
    retry on 429s. Any other error is raised straight away."""
    delays = getattr(settings, "EMBED_RETRY_DELAYS", DEFAULT_EMBED_RETRY_DELAYS)
    for attempt in range(len(delays) + 1):
        try:
            return embed_documents(texts)
        except Exception as exc:  # the Gemini SDK raises several error types
            if is_rate_limit(exc) and attempt < len(delays):
                logger.warning("Gemini rate limit while embedding; retrying in %ss", delays[attempt])
                time.sleep(delays[attempt])
                continue
            raise


def add_chunks(items: list[tuple[str, str] | tuple[str, str, str]], document=None) -> int:
    """Add chunks, or replace one document's chunks when document is supplied."""
    cleaned_items = []
    for item in items:
        source, text = item[:2]
        source_url = item[2] if len(item) == 3 else ""

        if text and text.strip():
            cleaned_items.append((source, text.strip(), source_url))

    if not cleaned_items:
        return 0

    vectors = embed_with_retries([text for _, text, _ in cleaned_items])

    if len(vectors) != len(cleaned_items):
        raise RetrievalError(
            "Embedding failed for some chunks. Check GEMINI_EMBED_MODEL and the embedding service."
        )

    # Embed before opening the transaction. A slow or failed API call should not
    # lock SQLite or delete the document's existing knowledge.
    rows = [
        DocumentChunk(source=source, text=text, embedding=to_bytes(vector), source_url=source_url)
        for (source, text, source_url), vector in zip(cleaned_items, vectors)
    ]

    with transaction.atomic():
        if document is not None:
            document.ingested_at = timezone.now()
            document.save()
            document.chunks.all().delete()
            for position, row in enumerate(rows, start=1):
                row.document = document
                row.position = position
        else:
            # Those imports should also appear on the developer page.
            grouped = {}
            for row in rows:
                grouped.setdefault((row.source, row.source_url), []).append(row)
            for (source, url), source_rows in grouped.items():
                imported, _ = Document.objects.get_or_create(
                    title=source, source_url=url, defaults={"content": ""}
                )
                new_text = "\n\n".join(row.text for row in source_rows)
                imported.content = "\n\n".join(filter(None, [imported.content, new_text]))
                imported.cleaned_content = ""  # Appended chunks have no saved full-text cleaning snapshot.
                imported.ingested_at = timezone.now()
                imported.save()
                last_position = imported.chunks.aggregate(last=Max("position"))["last"] or 0
                for position, row in enumerate(source_rows, start=last_position + 1):
                    row.document = imported
                    row.position = position

        DocumentChunk.objects.bulk_create(rows)

    return len(cleaned_items)


_lock = threading.Lock()
_index = {"version": None, "ids": None, "matrix": None}


def _current_version():
    agg = DocumentChunk.objects.aggregate(n=Count("id"), last=Max("id"))
    return (agg["n"], agg["last"])


def _load_index():
    """Return (ids, normalized matrix), reloading from SQLite if the table changed."""
    version = _current_version()
    with _lock:
        if _index["version"] != version:
            rows = list(DocumentChunk.objects.values_list("id", "embedding"))
            if rows:
                ids = np.array([r[0] for r in rows])
                matrix = np.vstack([from_bytes(r[1]) for r in rows])
                norms = np.linalg.norm(matrix, axis=1, keepdims=True)
                matrix = matrix / np.where(norms == 0, 1, norms)
            else:
                ids, matrix = np.array([], dtype=int), None
            _index.update(version=version, ids=ids, matrix=matrix)
        return _index["ids"], _index["matrix"]


def retrieve_chunks(question: str, k: int) -> list[Chunk]:
    try:
        ids, matrix = _load_index()
        if matrix is None:
            return []  # J: empty knowledge base -> rag.py treats as off-topic

        query = np.asarray(embed_query(question), dtype=np.float32)
        if query.shape[0] != matrix.shape[1]:
            raise RetrievalError(
                f"Embedding size mismatch: query {query.shape[0]} vs stored {matrix.shape[1]}. "
                "Ingestion and chat must use the same GEMINI_EMBED_MODEL."
            )
        query /= (np.linalg.norm(query) or 1.0)

        scores = matrix @ query
        top = np.argsort(-scores)[:k]
        top_ids = [int(ids[i]) for i in top]

        rows = DocumentChunk.objects.in_bulk(top_ids)
        return [
            Chunk(
                text=rows[i].text,
                source=rows[i].source,
                score=float(scores[j]),
                source_url=rows[i].source_url
            )
            for j, i in zip(top, top_ids) if i in rows
        ]
    except RetrievalError:
        raise
    except Exception as exc:
        raise RetrievalError(str(exc)) from exc