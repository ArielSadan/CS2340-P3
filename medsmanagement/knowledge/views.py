from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Count
from django.http import HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.urls import reverse

from chat.developer_views import start_review
from chat.models import Document, PromptStyle
from chat.services.rag import STYLE_LABELS, get_prompt_styles

from .conf import get_setting
from .forms import PromptStyleForm
from .ingestion.extract import ALLOWED_EXTENSIONS
from .ingestion.sources import ERROR, IngestResult, prepare_text, prepare_uploaded_file, prepare_url


from django.views.decorators.http import require_POST
from chat.models import DocumentChunk
from .maintenance import clear_knowledge_base, reload_knowledge_base

def _read_submission(request, max_items):
    """Read what was submitted. Returns (drafts to review, results to report)."""
    action = request.POST.get("action")
    prepared = []
    if action == "upload":
        files = request.FILES.getlist("files")
        if not files:
            prepared.append(IngestResult("—", ERROR, "Choose at least one file to upload."))
        prepared += [prepare_uploaded_file(f) for f in files[:max_items]]
        prepared += [IngestResult(f.name, ERROR, f"Skipped: at most {max_items} files per upload.")
                     for f in files[max_items:]]
    elif action == "urls":
        lines = list(dict.fromkeys(l.strip() for l in request.POST.get("urls", "").splitlines() if l.strip()))
        if not lines:
            prepared.append(IngestResult("—", ERROR, "Enter at least one URL."))
        prepared += [prepare_url(line) for line in lines[:max_items]]
        prepared += [IngestResult(line, ERROR, f"Skipped: at most {max_items} URLs at a time.")
                     for line in lines[max_items:]]
    elif action == "text":
        prepared.append(prepare_text(request.POST.get("title"), request.POST.get("text"),
                                     request.POST.get("source_url", "").strip()))
    drafts = [p for p in prepared if isinstance(p, dict)]
    results = [p for p in prepared if isinstance(p, IngestResult)]
    return drafts, results


@staff_member_required
def developer_knowledge(request):
    """Step 1 of adding knowledge: pick files, links or text. Each readable item
    then opens on the document form (step 2) to be checked before it's ingested."""
    results = []
    max_items = get_setting("KNOWLEDGE_MAX_ITEMS_PER_SUBMIT")
    styles = get_prompt_styles()
    saved_styles = {style.code: style for style in PromptStyle.objects.all()}
    prompt_cards = []
    editing_prompts = request.method == "POST" and request.POST.get("action") == "save_prompt"
    selected = request.POST.get("style")
    if editing_prompts and selected not in STYLE_LABELS:
        return HttpResponseBadRequest("Unknown response style.")
    for code, title in STYLE_LABELS.items():
        instance = saved_styles.get(code) or PromptStyle(
            code=code, phrases="\n".join(styles[code]["phrases"]),
            instructions=styles[code]["instructions"],
        )
        data = request.POST if editing_prompts and selected == code else None
        form = PromptStyleForm(data, instance=instance, prefix=code)
        prompt_cards.append({"code": code, "title": title, "form": form})
        if data is not None and form.is_valid():
            form.save()
            messages.success(request, f"{title} updated. Changes apply to the next question.")
            return redirect(reverse("knowledge:developer") + "#prompt-settings")

    if request.method == "POST" and not editing_prompts:
        drafts, results = _read_submission(request, max_items)
        if drafts:
            # Problems with the other items are shown at the top of the review form.
            for r in results:
                level = messages.WARNING if r.status == ERROR else messages.INFO
                messages.add_message(request, level, f"{r.item}: {r.message}")
            return start_review(request, drafts)

    # Documents whose chunks were cleared (#11) stay listed so they can be re-ingested.
    documents = Document.objects.annotate(chunk_total=Count("chunks")).order_by("-ingested_at")
    context = {
        "results": results,
        "prompt_cards": prompt_cards,
        "documents": documents[:100],
        "document_count": documents.count(),
        "allowed_extensions": ", ".join(sorted(ALLOWED_EXTENSIONS)),
        "accept": ",".join(sorted(ALLOWED_EXTENSIONS)),
        "max_mb": get_setting("KNOWLEDGE_MAX_UPLOAD_MB"),
        "max_items": max_items,
        "chunk_count": DocumentChunk.objects.count(),
    }
    return render(request, "knowledge/developer.html", context)

@staff_member_required
@require_POST
def clear_knowledge(request):
    """Administrator action to clear searchable knowledge."""
    count = clear_knowledge_base()

    messages.success(
        request,
        f"Knowledge base cleared successfully. "
        f"Removed {count} chunks. Saved documents were preserved."
    )

    return redirect("knowledge:developer")


@staff_member_required
@require_POST
def reload_knowledge(request):
    """Administrator action to rebuild saved knowledge."""
    summary = reload_knowledge_base()

    if summary.failures:
        messages.warning(
            request,
            f"Reload partially completed: "
            f"{summary.processed} documents rebuilt, "
            f"{summary.skipped} skipped, "
            f"{len(summary.failures)} failed."
        )

        for error in summary.failures[:10]:
            messages.error(request, error)

    elif summary.processed == 0:
        messages.warning(
            request,
            "No documents were rebuilt. Check that saved documents "
            "contain text."
        )

    else:
        messages.success(
            request,
            f"Knowledge base reloaded successfully. "
            f"{summary.processed} documents processed and "
            f"{summary.chunks_created} chunks created. "
            f"{summary.skipped} documents skipped."
        )

    return redirect("knowledge:developer")
