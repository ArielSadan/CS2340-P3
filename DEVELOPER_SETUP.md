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

Open http://127.0.0.1:8000/chat/developer/ and log in with your superuser account.

## Manage documents

- **Add:** paste text or upload TXT, CSV, JSON, HTML, or a text-based PDF.
- **Preview:** check cleaned text and chunks without saving or calling Gemini.
- **Re-ingest:** update the text or upload a replacement.
- **Delete:** remove a document and its chunks.

URLs are citation links; provide the actual text or file too.
Keep API keys out of Git. Each environment needs its own ingested documents.

## Tests

From `medsmanagement/`, run `python manage.py test chat`.
See [PREPROCESSING.md](PREPROCESSING.md) for cleaning and chunking details.
