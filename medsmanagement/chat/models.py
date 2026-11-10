from django.db import models


class DocumentChunk(models.Model):

    source = models.CharField(max_length=500)
    text = models.TextField()
    embedding = models.BinaryField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.source} #{self.pk}"
