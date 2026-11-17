from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Count
from django.shortcuts import render

from chat.models import Document

from .conf import get_setting
from .ingestion.extract import ALLOWED_EXTENSIONS
from .ingestion.sources import ERROR, IngestResult, ingest_uploaded_file, ingest_url


@staff_member_required
def developer_knowledge(request):
    results = []
    max_items = get_setting("KNOWLEDGE_MAX_ITEMS_PER_SUBMIT")

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "upload":
            files = request.FILES.getlist("files")
            if not files:
                results.append(IngestResult("—", ERROR, "Choose at least one file to upload."))
            for f in files[:max_items]:
                results.append(ingest_uploaded_file(f))
            for f in files[max_items:]:
                results.append(IngestResult(f.name, ERROR, f"Skipped: at most {max_items} files per upload."))
        elif action == "urls":
            lines = [l.strip() for l in request.POST.get("urls", "").splitlines() if l.strip()]
            if not lines:
                results.append(IngestResult("—", ERROR, "Enter at least one URL."))
            seen = set()
            for line in lines[:max_items]:
                if line in seen:
                    continue
                seen.add(line)
                results.append(ingest_url(line))
            for line in lines[max_items:]:
                results.append(IngestResult(line, ERROR, f"Skipped: at most {max_items} URLs at a time."))

    # Documents whose chunks were cleared (#11) stay listed so they can be re-ingested.
    documents = Document.objects.annotate(chunk_total=Count("chunks")).order_by("-ingested_at")
    context = {
        "results": results,
        "documents": documents[:100],
        "document_count": documents.count(),
        "allowed_extensions": ", ".join(sorted(ALLOWED_EXTENSIONS)),
        "accept": ",".join(sorted(ALLOWED_EXTENSIONS)),
        "max_mb": get_setting("KNOWLEDGE_MAX_UPLOAD_MB"),
        "max_items": max_items,
    }
    return render(request, "knowledge/developer.html", context)
