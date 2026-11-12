from django.db import migrations, models
from django.db.models import Max
import django.db.models.deletion
import django.utils.timezone


def group_existing_chunks(apps, schema_editor):
    Document = apps.get_model("chat", "Document")
    DocumentChunk = apps.get_model("chat", "DocumentChunk")
    database = schema_editor.connection.alias

    sources = list(
        DocumentChunk.objects.using(database)
        .values("source", "source_url")
        .annotate(ingested_at=Max("created_at"))
    )
    for source in sources:
        chunks = DocumentChunk.objects.using(database).filter(
            source=source["source"], source_url=source["source_url"]
        )
        document = Document.objects.using(database).create(
            title=source["source"],
            source_url=source["source_url"],
            content="\n\n".join(chunks.order_by("id").values_list("text", flat=True)),
            ingested_at=source["ingested_at"],
        )
        chunks.update(document_id=document.pk)


class Migration(migrations.Migration):
    dependencies = [("chat", "0002_documentchunk_source_url")]

    operations = [
        migrations.CreateModel(
            name="Document",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("title", models.CharField(max_length=500)),
                ("source_type", models.CharField(choices=[("article", "Article"), ("fda", "FDA dataset"), ("other", "Other")], default="other", max_length=20)),
                ("source_url", models.URLField(blank=True, default="", max_length=2000)),
                ("file_name", models.CharField(blank=True, default="", max_length=255)),
                ("content", models.TextField()),
                ("ingested_at", models.DateTimeField(default=django.utils.timezone.now)),
            ],
            options={
                "ordering": ["title", "id"],
                "constraints": [models.UniqueConstraint(fields=("title", "source_url"), name="unique_document_source")],
            },
        ),
        # Attach any existing vectors before making the relationship required.
        migrations.AddField(
            model_name="documentchunk",
            name="document",
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.CASCADE, related_name="chunks", to="chat.document"),
        ),
        migrations.RunPython(group_existing_chunks, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="documentchunk",
            name="document",
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="chunks", to="chat.document"),
        ),
    ]
