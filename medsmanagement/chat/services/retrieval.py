import threading
from dataclasses import dataclass

import numpy as np
from django.db.models import Count, Max

from chat.models import DocumentChunk

from .gemini import embed_documents, embed_query


@dataclass
class Chunk:
    text: str
    source: str
    score: float
    
class RetrievalError(Exception):
    """Raised when embedding or the database fails."""


def to_bytes(vector) -> bytes:
    return np.asarray(vector, dtype=np.float32).tobytes()


def from_bytes(blob) -> np.ndarray:
    return np.frombuffer(bytes(blob), dtype=np.float32)

def _non_empty(items):
    return [(s, t) for s, t in items if t and t.strip()]

def add_chunks(items: list[tuple[str, str]], document=None) -> int:
    """For ingestion teammates: items = [(source, text), ...]. Returns rows created."""
    items = _non_empty(items)
    if not items:
        return 0
    vectors = embed_documents([t for _, t in items])
    return store_chunks(items, vectors, document)

def store_chunks(items, vectors, document=None) -> int:
    items = _non_empty(items)
    if len(items) != len(vectors):
        raise ValueError("store_chunks needs one vector per non-empty chunk")
    DocumentChunk.objects.bulk_create(
        DocumentChunk(source=s, text=t, embedding=to_bytes(v), document=document)
        for (s, t), v in zip(items, vectors)
    )
    return len(items)

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
            Chunk(text=rows[i].text, source=rows[i].source, score=float(scores[j]))
            for j, i in zip(top, top_ids)
            if i in rows  # J: row may have been deleted mid-request
        ]
    except RetrievalError:
        raise
    except Exception as exc:
        raise RetrievalError(str(exc)) from exc
