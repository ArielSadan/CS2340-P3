# Developer setup

Run from the project root:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export GEMINI_API_KEY="your-key-here"
cd medsmanagement
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Open http://127.0.0.1:8000/developer/knowledge/ and log in with your superuser account.

## Manage documents

Adding knowledge is two steps:

1. **Choose what to add** on `/developer/knowledge/`: upload files (PDF, DOCX, MD, TXT,
   CSV, JSON or HTML, several at once), paste links (the page is fetched and its main
   article text is used), or paste text.
2. **Review each item** on the document form, which opens already filled in with the
   title, source type, link, file name and extracted text. Fix anything, use
   *Preview cleaned text and chunks* if you like (no Gemini call), then *Add and ingest*.
   With several items you review them one after another (*Skip this one* / *Cancel all*).

Each row of the document table has *Preview* (stored chunks), *Re-ingest* (edit the
saved text) and *Delete*.

Uploading a file with the same name, or the same URL again, opens as a replacement for that document.
Identical content is skipped. From the command line: `python manage.py ingest file.pdf https://...`

`manage.py ingest` skips the review step and adds the items straight away.

How it fits together: `knowledge/ingestion/sources.py` reads files, web pages and
pasted text into drafts. `chat/developer_views.py` (`document_review`) shows each
draft on the document form. Cleaning, chunking, embedding and storing are done by
`chat.services.ingestion.ingest_document`, and every document is a `chat.models.Document`.
Keep API keys out of Git. Each environment needs its own ingested documents.

## Tests

From `medsmanagement/`, run `python manage.py test chat knowledge evaluation`.
See [PREPROCESSING.md](PREPROCESSING.md) for cleaning and chunking details.
