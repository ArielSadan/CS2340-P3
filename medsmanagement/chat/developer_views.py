"""Document form and per-document pages for the Developer page (/developer/knowledge/).

Adding knowledge is two steps:
  1. /developer/knowledge/ reads uploaded files, links or pasted text into drafts.
  2. document_review opens each draft on the document form, filled in, so the
     developer can check it (and preview the chunks) before it's ingested.
"""
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

REVIEW_QUEUE = "document_review_queue"
REVIEW_TOTAL = "document_review_total"
DRAFT_FIELDS = ["title", "source_type", "source_url", "content", "file_name"]


def _document_form(request, document=None, *, initial=None, review=None):
    """Show the document form, or preview/ingest what was submitted.
    Returns a redirect after a successful ingest, otherwise the rendered form."""
    form = DocumentForm(
        request.POST if request.method == "POST" else None,
        request.FILES if request.method == "POST" else None,
        instance=document,
        initial=initial,
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
                return _next_review(request) if review else redirect("knowledge:developer")
        except ValueError as exc:
            form.add_error(None, str(exc))
        except Exception:
            logger.exception("Document ingestion failed")
            form.add_error(None, "Ingestion failed. Check the Gemini API key and try again. Existing knowledge was kept.")
    size, overlap, minimum = chunk_settings()
    return render(request, "chat/developer/document_form.html", {
        "form": form, "document": document, "preview": preview, "review": review,
        "chunk_size": size, "chunk_overlap": overlap, "chunk_minimum": minimum,
    })


@staff_member_required
@require_http_methods(["GET", "POST"])
def document_edit(request, pk=None):
    document = get_object_or_404(Document, pk=pk) if pk is not None else None
    return _document_form(request, document)


# ------------------------------------------------------------ review queue

def start_review(request, drafts):
    """Called by the Developer page with the drafts it read. Each draft is a dict
    from knowledge.ingestion.sources (keys: item, document_id + DRAFT_FIELDS)."""
    request.session[REVIEW_QUEUE] = drafts
    request.session[REVIEW_TOTAL] = len(drafts)
    return redirect("chat:document_review")


def _next_review(request):
    """Drop the draft that was just handled and open the next one, if any."""
    queue = request.session.get(REVIEW_QUEUE) or []
    request.session[REVIEW_QUEUE] = queue[1:]
    if queue[1:]:
        return redirect("chat:document_review")
    request.session.pop(REVIEW_QUEUE, None)
    request.session.pop(REVIEW_TOTAL, None)
    return redirect("knowledge:developer")


@staff_member_required
@require_http_methods(["GET", "POST"])
def document_review(request):
    queue = request.session.get(REVIEW_QUEUE) or []
    if not queue:
        return redirect("knowledge:developer")
    draft = queue[0]

    action = request.POST.get("action") if request.method == "POST" else None
    if action == "skip":
        messages.info(request, f'Skipped "{draft["item"]}". Nothing was saved for it.')
        return _next_review(request)
    if action == "cancel":
        remaining = len(queue)
        request.session.pop(REVIEW_QUEUE, None)
        request.session.pop(REVIEW_TOTAL, None)
        messages.info(request, f"Cancelled. {remaining} item{'s' if remaining != 1 else ''} not added.")
        return redirect("knowledge:developer")

    # A new version of a document that's already in the knowledge base is
    # reviewed as a re-ingest, so saving replaces its old chunks.
    document = Document.objects.filter(pk=draft.get("document_id")).first() if draft.get("document_id") else None
    total = request.session.get(REVIEW_TOTAL) or len(queue)
    review = {
        "item": draft["item"],
        "position": total - len(queue) + 1,
        "total": total,
        "replacing": document,
    }
    initial = {field: draft[field] for field in DRAFT_FIELDS}
    return _document_form(request, document, initial=initial, review=review)


# ------------------------------------------------------------ other document pages

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
