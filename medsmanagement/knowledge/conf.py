DEFAULTS = {
    # Chunking
    "CHUNK_MAX_WORDS": 300,
    "CHUNK_OVERLAP_WORDS": 50,
    "MIN_DOCUMENT_WORDS": 30,
 
    # Ingestion limits
    "KNOWLEDGE_MAX_UPLOAD_MB": 10,
    "KNOWLEDGE_MAX_ITEMS_PER_SUBMIT": 10,
    "URL_CONNECT_TIMEOUT": 5,
    "URL_READ_TIMEOUT": 20,
    "URL_MAX_REDIRECTS": 5,
    "URL_USER_AGENT": "MediGuideBot/1.0 (student project; knowledge ingestion)",
 
    # Seconds to wait between retries when Gemini's free tier says "too many requests"
    "EMBED_RETRY_DELAYS": [2, 5, 10, 20],
}
 
 
def get_setting(name):
    try:
        from django.conf import settings
 
        if settings.configured:
            return getattr(settings, name, DEFAULTS[name])
    except ImportError:
        pass
    return DEFAULTS[name]
 