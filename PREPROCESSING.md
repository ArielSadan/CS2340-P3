# Data preprocessing

The developer ingestion flow is: upload or paste text → clean → chunk → embed
with the existing Gemini helper → save. Previewing stops before embedding or
saving and makes no API calls. No additional Python packages are required.

## Cleaning

Text uploads support UTF-8 (including BOM), UTF-16 with BOM, and Windows-1252.
The cleaner normalizes Unicode to NFC, removes BOM/zero-width markers and control
characters, decodes HTML entities, collapses extra whitespace, and keeps paragraph
breaks. PDF text still comes from the existing PDF extraction step.

Python's HTMLParser removes markup while skipping scripts, styles, navigation,
footers, forms, hidden elements, and common ad/cookie/sidebar containers identified
by their HTML tags, roles, IDs, or classes. Article headings, lists, tables, and
boxed warnings remain readable. Plain-text boilerplate removal uses a short list
of exact lines, such as "Privacy policy", rather than deleting whole paragraphs
that happen to contain those words. Unmarked ads and unusual site templates may
need additional rules; always inspect the preview for a new source.

## Chunk configuration

These values live in `medsmanagement/medsmanagement/settings.py`:

| Setting | Default | Purpose |
| --- | --- | --- |
| `RAG_CHUNK_SIZE_CHARS` | 2400 | Target about 600 tokens |
| `RAG_CHUNK_OVERLAP_CHARS` | 360 | Target about 90 tokens, or 15% overlap |
| `RAG_MIN_CHUNK_CHARS` | 80 | Reject empty/near-empty chunks |

The token estimate is **four characters per token**, not Gemini token counting.
It keeps previews local and inexpensive. Actual token counts vary by content and
language. The splitter prefers paragraph/heading breaks in the last 20% of its
target window, then line or word breaks. Short headings stay with their following
paragraph when a suitable boundary is available. Overlap starts at a word boundary,
so it can be slightly shorter than configured. Very long unbroken text is split
at the character limit. A short final sentence is retained with the preceding
chunk, allowing up to the minimum size in extra characters.

Whitespace-only, punctuation-only, and undersized content is not embedded. If
cleaning leaves no usable chunks, the form explains why and old knowledge stays
unchanged. Parameters must satisfy `1 <= minimum <= size` and `0 <= overlap < size`.

## Metadata and preview

Every stored chunk keeps its readable `source`, `source_url`, linked document
title (`chunk.document.title`), one-based `position`, and ingestion timestamp
(`created_at`). The document saves its full cleaned text in `cleaned_content`.
The original text is retained for editing and future re-ingestion.

Before ingestion, use the form's Preview button. After ingestion, use the table's
Preview link to see **stored** text/chunks rather than recomputing with potentially
changed settings. Both routes are staff-only. New positions start at 1 when a
document is replaced. The migration numbers existing chunks without modifying
their text or embeddings. Older imports have no saved full cleaned text until
they are re-ingested, but their existing chunks remain visible.

The low-level `retrieval.add_chunks()` API still accepts already-prepared chunks
for compatibility with other stories. Importers using that API are responsible
for preprocessing first; use `ingest_document()` for the complete pipeline.

Run `python manage.py migrate` after applying this change, then
`python manage.py test chat` to check cleaning, chunking, previews, metadata,
migration preservation, and existing chatbot behavior. Tests mock Gemini.
