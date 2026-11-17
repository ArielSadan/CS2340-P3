"""Per-document pages linked from the Developer page (/developer/knowledge/):
add pasted text, preview, re-ingest and delete."""
import logging

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_http_methods

from .forms import DocumentForm
from .models import Document
from .services.ingestion import ingest_document
from .services.preprocessing import chunk_settings, preprocess_text

logger = logging.getLogger(__name__)


@staff_member_required
@require_http_methods(["GET", "POST"])
def document_edit(request, pk=None):
    document = get_object_or_404(Document, pk=pk) if pk is not None else None
    form = DocumentForm(
        request.POST if request.method == "POST" else None,
        request.FILES if request.method == "POST" else None,
        instance=document,
    )
    preview = None
    if request.method == "POST" and form.is_valid():
        updated = form.save(commit=False)
        try:
            if request.POST.get("action") == "preview":
                cleaned, chunks = preprocess_text(updated.content)
                if not chunks:
                    raise ValueError("No usable chunks remain after cleaning. Add more document text before ingesting.")
                preview = {"cleaned_text": cleaned, "chunks": chunks, "document": updated}
                # Upload fields clear when the page reloads. Keep extracted text
                # and the file name in the form so Save can use this preview.
                form.data = form.data.copy()
                form.data["content"] = updated.content
                form.data["file_name"] = updated.file_name
            else:
                count = ingest_document(updated)
                messages.success(request, f'"{updated.title}" ingested successfully ({count} chunks).')
                return redirect("knowledge:developer")
        except ValueError as exc:
            form.add_error(None, str(exc))
        except Exception:
            logger.exception("Document ingestion failed")
            form.add_error(None, "Ingestion failed. Check the Gemini API key and try again. Existing knowledge was kept.")
    size, overlap, minimum = chunk_settings()
    return render(request, "chat/developer/document_form.html", {
        "form": form, "document": document, "preview": preview,
        "chunk_size": size, "chunk_overlap": overlap, "chunk_minimum": minimum,
    })


@staff_member_required
@require_GET
def document_preview(request, pk):
    document = get_object_or_404(Document, pk=pk)
    return render(request, "chat/developer/document_preview.html", {
        "document": document,
        "chunks": document.chunks.order_by("position", "id"),
    })


@staff_member_required
@require_http_methods(["GET", "POST"])
def document_delete(request, pk):
    document = get_object_or_404(Document, pk=pk)
    if request.method == "POST":
        title = document.title
        document.delete()  # The foreign key also deletes this document's chunks.
        messages.success(request, f'"{title}" and its chunks were deleted.')
        return redirect("knowledge:developer")
    return render(request, "chat/developer/document_confirm_delete.html", {"document": document})
