import logging
 
from ..conf import get_setting
from ..models import Document
from .errors import FileTooLarge, IngestionError
from .extract import extract_fetched, extract_upload
from .fetch import fetch_url, normalize_url
from .pipeline import ERROR, IngestResult, ingest_extracted
 
logger = logging.getLogger(__name__)
 
UNEXPECTED = "Something went wrong while ingesting this item. Check the server log."
 
 
def ingest_uploaded_file(uploaded_file):
    """Method 1: a file uploaded on the Developer page (Django UploadedFile)."""
    name = uploaded_file.name
    try:
        max_mb = get_setting("KNOWLEDGE_MAX_UPLOAD_MB")
        if uploaded_file.size > max_mb * 1024 * 1024:
            raise FileTooLarge(f"File is over the {max_mb} MB limit.")
        data = uploaded_file.read()
        extracted = extract_upload(name, data)
        return ingest_extracted(
            extracted=extracted,
            source_type=Document.SOURCE_FILE,
            source_key=f"file:{name.lower()}",
            item_label=name,
        )
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
        return ingest_extracted(
            extracted=extracted,
            source_type=Document.SOURCE_URL,
            source_key=url,
            source_url=page.url,
            item_label=label,
        )
    except IngestionError as exc:
        return IngestResult(label, ERROR, exc.user_message)
    except Exception:
        logger.exception("Unexpected error ingesting URL %s", label)
        return IngestResult(label, ERROR, UNEXPECTED)