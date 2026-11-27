"""Usage logs page on the website (#12): /chat/usage/

Administrators (staff with the "Can view usage log" permission; superusers have it)
see summary numbers, a queries-per-day chart and a searchable, sortable, paged log
table with a date filter. Other signed-in users get 403; signed-out users go to the
site login page.
"""
from functools import wraps

from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from .models import UsageLog
from .services.rag import STYLE_LABELS
from .services.usage import OUTCOMES, LogFilters, usage_summary

PER_PAGE = 25
# URL value -> (column label, model field). Sorting by status groups failures together.
SORT_COLUMNS = {
    "time": ("Time", "created_at"),
    "question": ("Question", "question"),
    "intent": ("Intent", "intent"),
    "response": ("Response time", "latency_ms"),
    "outcome": ("Outcome", "status"),
}
DEFAULT_SORT = "-time"


def can_view_usage_logs(user):
    return user.is_active and user.is_staff and user.has_perm("chat.view_usagelog")


def admin_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path(), settings.LOGIN_URL)
        if not can_view_usage_logs(request.user):
            raise PermissionDenied("Only administrators can view usage logs.")
        return view(request, *args, **kwargs)
    return wrapper


def _query_string(params, **changes):
    """Current query string with some keys changed (None removes the key)."""
    params = params.copy()
    for key, value in changes.items():
        if value is None:
            params.pop(key, None)
        else:
            params[key] = value
    encoded = params.urlencode()
    return f"?{encoded}" if encoded else "?"


def _sources(log):
    items = []
    for s in log.sources or []:
        if isinstance(s, dict):
            title, url = s.get("title", "").strip(), s.get("url", "").strip()
        else:
            title, url = str(s), ""
        if title or url:
            items.append({"title": title or url, "url": url})
    return items


@require_GET
@never_cache
@admin_required
def usage_logs(request):
    filters = LogFilters.from_params(request.GET, STYLE_LABELS)

    sort = request.GET.get("sort", DEFAULT_SORT)
    if sort.lstrip("-") not in SORT_COLUMNS:
        sort = DEFAULT_SORT
    descending = sort.startswith("-")
    field = SORT_COLUMNS[sort.lstrip("-")][1]
    ordering = [f"-{field}" if descending else field, "-id" if descending else "id"]

    logs = filters.apply(UsageLog.objects.all(), STYLE_LABELS).order_by(*ordering)
    page = Paginator(logs, PER_PAGE).get_page(request.GET.get("page"))

    # Column headers: clicking sorts by that column; clicking the active one flips direction.
    params = request.GET
    columns = []
    for key, (label, _) in SORT_COLUMNS.items():
        active = sort.lstrip("-") == key
        next_sort = (key if descending else f"-{key}") if active else (f"-{key}" if key in {"time", "response"} else key)
        columns.append({
            "label": label, "active": active, "descending": active and descending,
            "url": _query_string(params, sort=next_sort, page=None),
        })

    rows = [{
        "log": log,
        "intent": STYLE_LABELS.get(log.intent, log.intent),
        "sources": _sources(log),
    } for log in page.object_list]

    return render(request, "chat/usage.html", {
        "usage": usage_summary(),
        "filters": filters,
        "outcomes": OUTCOMES,
        "intents": STYLE_LABELS,
        "columns": columns,
        "rows": rows,
        "page": page,
        "sort": sort,
        "page_url": _query_string(params, page=None),
        "clear_url": _query_string(params.__class__(""), sort=sort if sort != DEFAULT_SORT else None),
    })
