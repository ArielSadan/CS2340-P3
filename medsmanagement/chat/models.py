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

class QueryLog(models.Model):
    """One row per chatbot question. Shared by Performance (#10) and Usage logs (#12).

    Privacy: only the question text is stored. No user, session id, IP address
    or answer text is kept, so a row can't be traced back to a person.
    """

    STATUS_CHOICES = [
        ("ok", "answered"),
        ("off_topic", "off-topic"),
        ("error", "failed")
    ]

    status = models.CharField(max_length=16, choices=STATUS_CHOICES)
    latency_ms = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    # Usage-log fields (#12). Blank for rows recorded before they existed.
    question = models.TextField(blank=True, default="")
    intent = models.CharField(max_length=20, blank=True, default="")
    sources = models.JSONField(blank=True, default=list)  # [{"title": ..., "url": ...}]

    class Meta:
        verbose_name = "Chatbot Performance"
        verbose_name_plural = "Chatbot Performance"
        ordering = ["-created_at", "-id"]

    @property
    def succeeded(self):
        """Off-topic replies are a correct outcome; only errors count as failures."""
        return self.status != "error"


class UsageLog(QueryLog):
    """Same rows as QueryLog, shown on its own admin page (Usage logs)."""

    class Meta:
        proxy = True
        verbose_name = "Usage log"
        verbose_name_plural = "Usage logs"
        ordering = ["-created_at", "-id"]

class PromptStyle(models.Model):
    code = models.CharField(max_length=20, unique=True, choices=[
        ("quick", "Quick Answer"),
        ("explain", "Explain with Sources"),
        ("details", "Going Into Details"),
    ])
    phrases = models.TextField(blank=True, max_length=2000)
    instructions = models.TextField(max_length=2000)

    def __str__(self):
        return self.get_code_display()
