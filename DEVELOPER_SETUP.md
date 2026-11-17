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

- **Upload files:** PDF, DOCX, MD, TXT, CSV, JSON or HTML, several at once.
- **Add web pages:** paste links; the page is fetched and its main article text is used.
- **Paste text:** "Paste text instead" link at the top of the page.
- **Preview / Re-ingest / Delete:** links on each row of the document table.
  Preview shows the stored chunks; re-ingest lets you edit the saved text.

Uploading a file with the same name, or the same URL again, replaces that document.
Identical content is skipped. From the command line: `python manage.py ingest file.pdf https://...`

How it fits together: `knowledge/ingestion/` only reads files and web pages into
text. Cleaning, chunking, embedding and storing are done by the chat pipeline
(`chat.services.ingestion.ingest_document`), and every document is a `chat.models.Document`.
Keep API keys out of Git. Each environment needs its own ingested documents.

## Tests

From `medsmanagement/`, run `python manage.py test chat knowledge evaluation`.
See [PREPROCESSING.md](PREPROCESSING.md) for cleaning and chunking details.
