
from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import render, get_object_or_404

from .models import EvaluationRun


@staff_member_required
def dashboard(request, run_id=None):
    runs = EvaluationRun.objects.order_by("-created_at")[:20]

    if run_id is not None:
        selected_run = get_object_or_404(
            EvaluationRun, pk=run_id
        )
    else:
        selected_run = runs.first()

    results = (
        selected_run.results.order_by("case_id")
        if selected_run else []
    )

    return render(
        request,
        "evaluation/dashboard.html",
        {
            "runs": runs,
            "selected_run": selected_run,
            "results": results,
        },
    )
