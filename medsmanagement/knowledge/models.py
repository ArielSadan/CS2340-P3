from django.db import models
 
 
class Document(models.Model):
    SOURCE_FILE = "file"
    SOURCE_URL = "url"
    SOURCE_CHOICES = [(SOURCE_FILE, "File upload"), (SOURCE_URL, "Web page")]
 
    # Shown to users as the citation (copied into DocumentChunk.source).
    title = models.CharField(max_length=300)
    source_type = models.CharField(max_length=10, choices=SOURCE_CHOICES)
    # What counts as "the same source" for replace-on-update:
    #   files -> "file:<filename lowercased>",  URLs -> the normalized URL
    source_key = models.CharField(max_length=1000, unique=True)
    # Public link to the page (empty for uploads). The sources story (#4) can show it.
    source_url = models.URLField(max_length=1000, blank=True)
    # SHA-256 of the cleaned text: catches identical content under any name.
    content_hash = models.CharField(max_length=64, unique=True)
    word_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
 
    class Meta:
        ordering = ["-updated_at"]
 
    def __str__(self):
        return self.title
 