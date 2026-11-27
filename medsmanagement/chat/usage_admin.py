"""Usage logs in Django admin (#12). The main page for administrators is the site page
at /chat/usage/ (chat/usage_views.py); this admin view is the same data for the console.
UsageLog is a proxy of QueryLog so it gets its own admin page beside Performance.
"""
from django.contrib import admin
from django.utils.html import format_html, format_html_join
from django.utils.text import Truncator

from .models import UsageLog
from .services.rag import STYLE_LABELS
from .services.usage import OUTCOMES, filter_by_dates, filter_by_intent, filter_by_outcome, parse_date, usage_summary


class DateRangeFilter(admin.FieldListFilter):
    """From / to date inputs (inclusive, local dates). Invalid dates are ignored."""

    template = "admin/chat/usagelog/date_range_filter.html"

    def __init__(self, field, request, params, model, model_admin, field_path):
        self.param_from = f"{field_path}_from"
        self.param_to = f"{field_path}_to"
        self.request = request
        super().__init__(field, request, params, model, model_admin, field_path)
        self.title = "date range"

    def _value(self, name):
        value = self.used_parameters.get(name)
        if isinstance(value, list):  # Django 5 passes lists of values
            value = value[-1] if value else None
        return parse_date(value)

    @property
    def date_from(self):
        return self._value(self.param_from)

    @property
    def date_to(self):
        return self._value(self.param_to)

    def expected_parameters(self):
        return [self.param_from, self.param_to]

    def has_output(self):
        return True

    def queryset(self, request, queryset):
        return filter_by_dates(queryset, self.date_from, self.date_to)

    def choices(self, changelist):
        # Keep search, sort and other filters when the date form is submitted; reset paging.
        skip = {self.param_from, self.param_to, "p"}
        hidden = [
            (key, value)
            for key, values in self.request.GET.lists()
            if key not in skip
            for value in values
        ]
        yield {
            "param_from": self.param_from,
            "param_to": self.param_to,
            "value_from": self.date_from.isoformat() if self.date_from else "",
            "value_to": self.date_to.isoformat() if self.date_to else "",
            "hidden": hidden,
            "clear_url": changelist.get_query_string(remove=[self.param_from, self.param_to, "p"]),
            "active": bool(self.date_from or self.date_to),
        }


class IntentFilter(admin.SimpleListFilter):
    title = "detected intent"
    parameter_name = "intent"

    def lookups(self, request, model_admin):
        return [(code, label) for code, label in STYLE_LABELS.items()] + [("none", "Not recorded")]

    def queryset(self, request, queryset):
        return filter_by_intent(queryset, self.value(), STYLE_LABELS)


class OutcomeFilter(admin.SimpleListFilter):
    title = "outcome"
    parameter_name = "outcome"

    def lookups(self, request, model_admin):
        return list(OUTCOMES.items())

    def queryset(self, request, queryset):
        return filter_by_outcome(queryset, self.value())


@admin.register(UsageLog)
class UsageLogAdmin(admin.ModelAdmin):
    change_list_template = "admin/chat/usagelog/change_list.html"
    list_display = ("created_at", "short_question", "intent_label", "source_list",
                    "latency_ms_display", "success", "status")
    list_display_links = ("created_at", "short_question")
    list_filter = (("created_at", DateRangeFilter), OutcomeFilter, IntentFilter)
    search_fields = ("question",)
    search_help_text = "Search question text."
    list_per_page = 50
    list_max_show_all = 500
    ordering = ("-created_at", "-id")
    show_full_result_count = True

    fields = ("created_at", "question", "intent_label", "sources_detail",
              "latency_ms_display", "success", "status")
    readonly_fields = fields

    # Logs are a read-only record: no adding, editing or deleting from the admin.
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description="Question", ordering="question")
    def short_question(self, obj):
        return Truncator(obj.question).chars(90) if obj.question else "(not recorded)"

    @admin.display(description="Intent", ordering="intent")
    def intent_label(self, obj):
        return STYLE_LABELS.get(obj.intent, obj.intent) or "-"

    @admin.display(description="Sources")
    def source_list(self, obj):
        titles = [s.get("title", "") if isinstance(s, dict) else str(s) for s in obj.sources or []]
        titles = [t for t in titles if t]
        if not titles:
            return "-"
        text = Truncator(", ".join(titles)).chars(80)
        return format_html('<span title="{}">{} ({})</span>', "\n".join(titles), text, len(titles))

    @admin.display(description="Sources returned")
    def sources_detail(self, obj):
        items = []
        for s in obj.sources or []:
            title, url = (s.get("title", ""), s.get("url", "")) if isinstance(s, dict) else (str(s), "")
            items.append((url, title or url) if url else ("", title))
        if not items:
            return "None"
        return format_html(
            "<ul>{}</ul>",
            format_html_join(
                "", "<li>{}</li>",
                ((format_html('<a href="{}" rel="noopener noreferrer" target="_blank">{}</a>', url, t)
                  if url else t,) for url, t in items),
            ),
        )

    @admin.display(description="Response time", ordering="latency_ms")
    def latency_ms_display(self, obj):
        return f"{obj.latency_ms:,} ms"

    @admin.display(description="Success", boolean=True, ordering="status")
    def success(self, obj):
        return obj.succeeded

    def changelist_view(self, request, extra_context=None):
        extra_context = {"title": "Usage logs", **(extra_context or {}), "usage": usage_summary()}
        return super().changelist_view(request, extra_context=extra_context)
