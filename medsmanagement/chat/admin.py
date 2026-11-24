from django.contrib import admin

from .models import Document, DocumentChunk, QueryLog
from .performance_views import performance_dashboard
from django.core.exceptions import PermissionDenied


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

@admin.register(QueryLog)
class QueryLogAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
    
    def has_delete_permission(self, request, obj=None):
        return False
    
    def changelist_view(self, request, extra_context=None):
        if not self.has_view_permission(request):
            raise PermissionDenied
        return performance_dashboard(request)