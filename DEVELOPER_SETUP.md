# Developer knowledge-base page

This page manages the documents used by the existing medication RAG chatbot.
It uses Django staff accounts, SQLite, and the existing Gemini embedding helper.

## Run locally

From the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cd medsmanagement
python manage.py migrate
python manage.py createsuperuser
```

Set `GEMINI_API_KEY` in the same terminal before running the server. Do not put
the key in a tracked file. Settings currently read environment variables directly;
creating a `.env` file alone does not load it.

```bash
python manage.py runserver
```

Open <http://127.0.0.1:8000/chat/developer/> and sign in with the account you
created. Only active users with `is_staff=True` can access any developer page
or submit its actions. Regular chat access remains public. Logged-in staff also
see a Developer tools link on the chat page.

## Add, update, and delete

- **Add:** enter a readable title, source type (Article, FDA dataset, or Other),
  optional citation URL, and the text or an uploaded document.
- **Uploads:** TXT, CSV, JSON, HTML, and PDFs with selectable text are supported.
  The file replaces the text field when both are supplied. Files are limited to
  5 MB, with up to 100,000 extracted characters per document. CSV and JSON are
  treated as text; a specialized FDA record importer belongs in the ingestion
  story when the team chooses the actual dataset format.
- **URLs:** stored as citation links, not fetched automatically. Supply the
  source text or file as well. This avoids adding a crawler to this story.
- **Re-ingest:** open the document's form, edit the text or upload a replacement,
  then save. Saving unchanged text rebuilds its embeddings. A changed citation
  URL alone does not download new source content.
- **Preview before saving:** choose Preview cleaned text and chunks on the form.
  It makes no Gemini calls and saves nothing. Uploaded text is kept in the form
  so you can then ingest it without uploading again.
- **Preview after saving:** choose Preview in the document table to see the actual
  stored chunks, their positions and dates, and the saved cleaned text.
- **Delete:** review the confirmation page, then submit the deletion. It removes
  the document and all its vector chunks. GET requests never perform deletions.

The original file is not saved; its name and extracted text are stored in SQLite.
The table shows title, source type, URL/file name, chunk count, and the most recent
successful ingestion date. The page displays the date only. Titles and citation URLs together identify a
document. Re-ingest an existing entry instead of adding the same source twice.

## How it fits the existing code

`Document` owns the metadata and saved full text. `DocumentChunk.document` links
the existing vector rows to their document, with cascading deletion. Migration
`0003_document` groups any existing chunks by source title and URL, preserving
their text and embeddings. Existing sources start with type Other because their
original category is unknown.

`chat.services.ingestion.ingest_document(document)` cleans and chunks the source
through `chat.services.preprocessing` before embedding. See PREPROCESSING.md for
the rules, configurable size/overlap, and metadata. This remains the shared
entry point for the developer page and future importers.

It calls the existing `retrieval.add_chunks(items, document=document)`. Embeddings
use the same configured Gemini model as chat queries. The remote embedding call
finishes before the database transaction begins. The transaction saves metadata
and replaces the selected document's chunks together. Failures keep old data.

Teammates can still call `add_chunks([(source, text), ...])` or pass
`(source, text, source_url)` tuples. These imports now also create document
records and appear in the developer table. Repeated calls for the same source
append chunks; use `ingest_document` to replace that source instead.

The existing retrieval cache checks the chunk count and highest chunk ID on
each question. Adding, deleting, or replacing chunks changes that version, so
the next chat query reloads the index without a server restart.

## Initial curated dataset

The selected Midterm Part B articles and FDA dataset were not supplied in this
repository. No guessed medical content or placeholder documents are seeded.
To finish this acceptance criterion, add the team's actual selected sources
through the developer page, choosing Article or FDA dataset as appropriate.
Keep readable titles and original citation URLs. Larger or structured FDA
datasets will need the agreed P3-6 importer rather than uploading an entire dump.

`db.sqlite3` stays ignored. Other teammates must ingest their own local copies;
database changes are not distributed through Git.

## Verify

With the virtual environment active, from `medsmanagement/`:

```bash
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test chat
```

The new tests cover staff access, CSRF, metadata, text/file ingestion, invalid
uploads, replacement and rollback, cache refresh, deletion, compatibility with
the existing importer, and preserving existing chunks during migration. Gemini
is mocked; these checks do not validate real medical answer accuracy.
