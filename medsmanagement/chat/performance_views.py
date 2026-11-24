from datetime import timedelta

from django.conf import settings
from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Avg, Count, Max, Q
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from .models import QueryLog

@staff_member_required
@never_cache
@require_GET
def performance_dashboard(request):
    if not request.user.has_perm("chat.view_querylog"):
        raise PermissionDenied("You do not have permission to view this page.")
    
    hours = {"24": 24, "168": 168, "720": 720}.get(request.GET.get("hours", "24"), 24)
    threshold_ms = getattr(settings, "CHATBOT_PERFORMANCE_THRESHOLD_MS", 5000)
    logs = QueryLog.objects.filter(created_at__gte=timezone.now() - timedelta(hours=hours))
    metrics = logs.aggregate(
        total=Count("id"),
        answered=Count("id", filter=Q(status="ok")),
        off_topic=Count("id", filter=Q(status="off_topic")),
        failures=Count("id", filter=Q(status="error")),
        slow=Count("id", filter=Q(latency_ms__gt=threshold_ms)),
        avg_ms=Avg("latency_ms"),
        max_ms=Max("latency_ms"),
    )
    metrics["failure_rate"] = 100 * metrics["failures"] / metrics["total"] if metrics["total"] else 0
    return render(request, "chat/performance.html", {
        **admin.site.each_context(request),
        "title": "Chatbot Performance",
        "metrics": metrics, "recent": logs[:50], "hours": hours,
        "threshold_ms": threshold_ms, "updated_at": timezone.now(),
    })