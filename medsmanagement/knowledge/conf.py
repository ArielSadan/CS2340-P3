"""Settings for reading files and web pages. Chunking settings live in
settings.py as RAG_CHUNK_* (used by chat/services/preprocessing.py)."""

DEFAULTS = {
    # Web pages with fewer words than this are treated as "no article found"
    "MIN_DOCUMENT_WORDS": 30,

    # Ingestion limits
    "KNOWLEDGE_MAX_UPLOAD_MB": 10,
    "KNOWLEDGE_MAX_ITEMS_PER_SUBMIT": 10,
    "URL_CONNECT_TIMEOUT": 5,
    "URL_READ_TIMEOUT": 20,
    "URL_MAX_REDIRECTS": 5,
    "URL_USER_AGENT": "MediGuideBot/1.0 (student project; knowledge ingestion)",
}


def get_setting(name):
    try:
        from django.conf import settings

        if settings.configured:
            return getattr(settings, name, DEFAULTS[name])
    except ImportError:
        pass
    return DEFAULTS[name]
