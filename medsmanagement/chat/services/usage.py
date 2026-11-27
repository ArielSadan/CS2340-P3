"""Usage logs (#12): filtering and summary numbers, shared by the site page and the admin page."""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from django.db.models import Avg, Count, Q
from django.db.models.functions import TruncDate
from django.utils import timezone

from chat.models import UsageLog

CHART_DAYS = 14
OUTCOMES = {
    "success": "Success (answered or off-topic)",
    "ok": "Answered",
    "off_topic": "Off-topic",
    "error": "Failure",
}


def parse_date(value):
    try:
        return date.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        return None


def day_start(day):
    """Start of a local calendar day as an aware datetime."""
    return timezone.make_aware(datetime.combine(day, time.min))


def filter_by_dates(queryset, date_from=None, date_to=None):
    """Inclusive local-date range. Either end may be None."""
    if date_from:
        queryset = queryset.filter(created_at__gte=day_start(date_from))
    if date_to:
        queryset = queryset.filter(created_at__lt=day_start(date_to + timedelta(days=1)))
    return queryset


def filter_by_outcome(queryset, outcome):
    if outcome == "success":
        return queryset.exclude(status="error")
    if outcome in {"ok", "off_topic", "error"}:
        return queryset.filter(status=outcome)
    return queryset


def filter_by_intent(queryset, intent, known_intents):
    if intent == "none":
        return queryset.filter(intent="")
    if intent in known_intents:
        return queryset.filter(intent=intent)
    return queryset


@dataclass
class LogFilters:
    q: str = ""
    date_from: date | None = None
    date_to: date | None = None
    outcome: str = ""
    intent: str = ""

    @classmethod
    def from_params(cls, params, known_intents):
        outcome = params.get("outcome", "")
        intent = params.get("intent", "")
        return cls(
            q=params.get("q", "").strip()[:200],
            date_from=parse_date(params.get("from")),
            date_to=parse_date(params.get("to")),
            outcome=outcome if outcome in OUTCOMES else "",
            intent=intent if intent in known_intents or intent == "none" else "",
        )

    @property
    def active(self):
        return any([self.q, self.date_from, self.date_to, self.outcome, self.intent])

    def apply(self, queryset, known_intents):
        if self.q:
            queryset = queryset.filter(question__icontains=self.q)
        queryset = filter_by_dates(queryset, self.date_from, self.date_to)
        queryset = filter_by_outcome(queryset, self.outcome)
        return filter_by_intent(queryset, self.intent, known_intents)


def usage_summary(now=None):
    """Headline numbers and the daily counts for the queries-over-time chart."""
    now = now or timezone.now()
    today = timezone.localdate(now)
    today_start = day_start(today)
    week_start = day_start(today - timedelta(days=6))  # today + previous 6 days
    chart_start_day = today - timedelta(days=CHART_DAYS - 1)

    totals = UsageLog.objects.aggregate(
        all_time=Count("id"),
        today=Count("id", filter=Q(created_at__gte=today_start)),
        week=Count("id", filter=Q(created_at__gte=week_start)),
        week_failures=Count("id", filter=Q(created_at__gte=week_start, status="error")),
        week_avg_ms=Avg("latency_ms", filter=Q(created_at__gte=week_start)),
    )
    week = totals["week"]
    totals["week_success_rate"] = 100 * (week - totals["week_failures"]) / week if week else None

    per_day = dict(
        UsageLog.objects.filter(created_at__gte=day_start(chart_start_day))
        .annotate(day=TruncDate("created_at", tzinfo=timezone.get_current_timezone()))
        .values_list("day")
        .annotate(n=Count("id"))
        .values_list("day", "n")
    )
    days = [chart_start_day + timedelta(days=i) for i in range(CHART_DAYS)]
    peak = max([per_day.get(d, 0) for d in days] + [0])
    chart = [
        {
            "day": d,
            "count": per_day.get(d, 0),
            # Bar height as a percentage of the busiest day; non-zero days stay visible.
            "pct": (max(4, round(100 * per_day.get(d, 0) / peak)) if per_day.get(d, 0) else 0),
        }
        for d in days
    ]
    return {"totals": totals, "chart": chart, "chart_peak": peak, "chart_days": CHART_DAYS}
