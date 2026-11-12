# Preprocessing

- Removes HTML tags, scripts, common ads, navigation, and footer text.
- Normalizes encoding and removes extra whitespace.
- Splits text near paragraph or word boundaries and skips empty or tiny chunks.

## Chunk settings

Configured in `medsmanagement/medsmanagement/settings.py`:

| Setting | Characters |
| --- | --- |
| `RAG_CHUNK_SIZE_CHARS` | 2400 |
| `RAG_CHUNK_OVERLAP_CHARS` | 360 |
| `RAG_MIN_CHUNK_CHARS` | 80 |

This targets about 600 tokens with 15% overlap, estimating four characters per token.
Each chunk keeps its source, title, position, and ingestion date.

## Preview

Use Preview before saving or from the document table after ingestion.
Previewing makes no Gemini calls. Check that important source text is retained.
Importers can use `chat.services.ingestion.ingest_document(document)` for the full pipeline.
