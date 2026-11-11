from django.contrib import admin
from chat.models import DocumentChunk
from .models import Document
 
class ChunkInline(admin.TabularInline):
    model = DocumentChunk
    fields = ("source", "text")
    readonly_fields = fields
    extra = 0
    can_delete = False
    show_change_link = False
 
 
@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    list_display = ("title", "source_type", "word_count", "updated_at")
    list_filter = ("source_type",)
    search_fields = ("title", "source_key")
    readonly_fields = ("content_hash", "created_at", "updated_at")
    inlines = [ChunkInline]