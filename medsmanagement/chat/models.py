from django.db import models
from django.utils import timezone


class Document(models.Model):
    SOURCE_TYPES = [
        ("article", "Article"),
        ("fda", "FDA dataset"),
        ("other", "Other"),
    ]

    title = models.CharField(max_length=500)
    source_type = models.CharField(max_length=20, choices=SOURCE_TYPES, default="other")
    source_url = models.URLField(max_length=2000, blank=True, default="")
    file_name = models.CharField(max_length=255, blank=True, default="")
    # Keep the text so developers can edit and re-ingest without the original file.
    content = models.TextField()
    cleaned_content = models.TextField(blank=True, default="")
    ingested_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["title", "id"]
        constraints = [
            models.UniqueConstraint(fields=["title", "source_url"], name="unique_document_source"),
        ]

    def __str__(self):
        return self.title


class DocumentChunk(models.Model):
    document = models.ForeignKey(
        Document, null=True, blank=True, on_delete=models.CASCADE, related_name="chunks",
    )
    source = models.CharField(max_length=500)
    source_url = models.URLField(max_length=2000, blank=True, default="")
    text = models.TextField()
    position = models.PositiveIntegerField(default=1)
    embedding = models.BinaryField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.source} #{self.pk}"
