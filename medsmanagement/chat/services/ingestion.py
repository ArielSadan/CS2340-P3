"""Small ingestion entry point shared by the developer page and future importers."""
from .retrieval import add_chunks
from .preprocessing import preprocess_text


def ingest_document(document):
    cleaned, chunks = preprocess_text(document.content)
    if not chunks:
        raise ValueError("No usable chunks remain after cleaning. Add more document text before ingesting.")
    document.cleaned_content = cleaned
    items = [(document.title, text, document.source_url) for text in chunks]
    return add_chunks(items, document=document)
