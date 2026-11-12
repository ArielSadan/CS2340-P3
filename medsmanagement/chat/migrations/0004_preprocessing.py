from django.db import migrations, models


def number_existing_chunks(apps, schema_editor):
    Document = apps.get_model("chat", "Document")
    Chunk = apps.get_model("chat", "DocumentChunk")
    database = schema_editor.connection.alias
    for document_id in Document.objects.using(database).values_list("id", flat=True).iterator():
        ids = Chunk.objects.using(database).filter(document_id=document_id).order_by("id").values_list("id", flat=True)
        for position, chunk_id in enumerate(ids, start=1):
            Chunk.objects.using(database).filter(pk=chunk_id).update(position=position)


class Migration(migrations.Migration):
    dependencies = [("chat", "0003_document")]
    operations = [
        migrations.AddField(
            model_name="document", name="cleaned_content",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="documentchunk", name="position",
            field=models.PositiveIntegerField(default=1),
        ),
        migrations.RunPython(number_existing_chunks, migrations.RunPython.noop),
    ]
