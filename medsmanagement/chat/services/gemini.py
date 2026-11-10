# J: Ingestion and chat must use the SAME embedding model so both should import from here.
from django.conf import settings
from google import genai
from google.genai import types

_client = None
EMBED_BATCH_SIZE = 100


def get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(
            api_key=settings.GEMINI_API_KEY,
            # J: Timeout is in milliseconds to cover the "LLM timeout" acceptance criterion
            http_options=types.HttpOptions(timeout=settings.LLM_TIMEOUT_SECONDS * 1000),
        )
    return _client


def embed_query(text: str) -> list[float]:
    """Embed a user question."""
    result = get_client().models.embed_content(
        model=settings.GEMINI_EMBED_MODEL,
        contents=text,
        config=types.EmbedContentConfig(task_type="RETRIEVAL_QUERY"),
    )
    return result.embeddings[0].values


def embed_documents(texts: list[str]) -> list[list[float]]:
    """Embed knowledge-base chunks (used by ingestion through retrieval.add_chunks)."""
    vectors = []
    for i in range(0, len(texts), EMBED_BATCH_SIZE):
        result = get_client().models.embed_content(
            model=settings.GEMINI_EMBED_MODEL,
            contents=texts[i:i + EMBED_BATCH_SIZE],
            config=types.EmbedContentConfig(task_type="RETRIEVAL_DOCUMENT"),
        )
        vectors.extend(e.values for e in result.embeddings)
    return vectors
