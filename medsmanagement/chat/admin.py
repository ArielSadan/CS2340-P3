from django.contrib import admin

from .models import Document, DocumentChunk


class ChunkInline(admin.TabularInline):
    model = DocumentChunk
    fields = ("position", "text")
    readonly_fields = fields
    extra = 0
    can_delete = False
    show_change_link = False


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    list_display = ("title", "source_type", "file_name", "source_url", "ingested_at")
    list_filter = ("source_type",)
    search_fields = ("title", "file_name", "source_url")
    readonly_fields = ("ingested_at", "cleaned_content")
    inlines = [ChunkInline]
