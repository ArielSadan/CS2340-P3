"""One Document model: chunks that were ingested through the old knowledge
pipeline (knowledge.Document) are moved to chat.Document before the second
foreign key is removed, so they stay visible on the Developer page."""
from django.db import migrations


def move_knowledge_documents(apps, schema_editor):
    KnowledgeDocument = apps.get_model("knowledge", "Document")
    Document = apps.get_model("chat", "Document")
    DocumentChunk = apps.get_model("chat", "DocumentChunk")
    database = schema_editor.connection.alias

    for old in KnowledgeDocument.objects.using(database).iterator():
        chunks = DocumentChunk.objects.using(database).filter(knowledge_document_id=old.pk)
        if not chunks.exists():
            continue
        file_name = old.source_key[5:] if old.source_key.startswith("file:") else ""
        document, _ = Document.objects.using(database).get_or_create(
            title=old.title[:500],
            source_url=old.source_url,
            defaults={
                "source_type": "article" if old.source_type == "url" else "other",
                "file_name": file_name[:255],
                "content": "\n\n".join(chunks.order_by("position", "id").values_list("text", flat=True)),
                "ingested_at": old.updated_at,
            },
        )
        chunks.update(document_id=document.pk)


class Migration(migrations.Migration):

    dependencies = [
        ("chat", "0006_alter_documentchunk_document"),
        ("knowledge", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(move_knowledge_documents, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name="documentchunk",
            name="knowledge_document",
        ),
    ]
