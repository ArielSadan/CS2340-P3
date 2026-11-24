from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

DUE_BEFORE = timedelta(hours=1)
LATE_AFTER = timedelta(hours=2)

HEALTH_START = 60
HEALTH_MAX = 100
HEALTH_FLOOR = 20
HEALTH_PER_DOSE = 20
HEALTH_PER_MISS = 10

STREAK_LOOKBACK_DAYS = 365

TAKEN, TAKEN_LATE, MISSED, DUE, UPCOMING = "taken", "taken_late", "missed", "due", "upcoming"
DONE = {TAKEN, TAKEN_LATE}
SETTLED = {TAKEN, TAKEN_LATE, MISSED}

WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
EVERY_DAY = frozenset(range(7))

@dataclass(frozen=True)
class PlannedTime:
    medication_id: int
    medication_name: str
    at: time
    weekdays: frozenset = EVERY_DAY
    active_from: datetime | None = None
    active_until: datetime | None = None

@dataclass(frozen=True)
class LoggedDose:
    medication_id: int | None
    medication_name: str
    taken_at: datetime
    scheduled_date: date | None = None
    scheduled_time: time | None = None

@dataclass
class Slot:
    medication_id: int
    medication_name: str
    day: date
    at: time
    due_at: datetime
    status: str = UPCOMING
    taken_at: datetime | None = None

    @property
    def window_start(self):
        return self.due_at - DUE_BEFORE

    @property
    def window_end(self):
        return self.due_at + LATE_AFTER

    @property
    def key(self):
        return (self.medication_id, self.day, self.at)

@dataclass
class DaySummary:
    day: date
    slots: list = field(default_factory=list)

    @property
    def all_done(self):
        return bool(self.slots) and all(s.status in DONE for s in self.slots)

    @property
    def has_missed(self):
        return any(s.status == MISSED for s in self.slots)


def _exists(plan, due_at):
    if plan.active_from is not None and due_at < plan.active_from:
        return False
    return plan.active_until is None or due_at < plan.active_until

def slots_for_day(plans, day, tz):
    slots = []
    for plan in plans:
        if day.weekday() not in plan.weekdays:
            continue
        due_at = datetime.combine(day, plan.at, tzinfo=tz)
        if _exists(plan, due_at):
            slots.append(Slot(plan.medication_id, plan.medication_name, day, plan.at, due_at))
    slots.sort(key=lambda s: (s.at, s.medication_name.lower()))
    return slots

def _log_index(logs):
    return {
        (log.medication_id, log.scheduled_date, log.scheduled_time): log
        for log in logs
        if log.scheduled_date is not None
    }

def classify(slots, logs, now):
    index = logs if isinstance(logs, dict) else _log_index(logs)
    for slot in slots:
        log = index.get(slot.key)
        if log is not None:
            slot.taken_at = log.taken_at
            slot.status = TAKEN_LATE if log.taken_at > slot.window_end else TAKEN
        elif now < slot.window_start:
            slot.status = UPCOMING
        elif now <= slot.window_end:
            slot.status = DUE
        else:
            slot.status = MISSED
    return slots

def days_between(first, last):
    day = first
    while day <= last:
        yield day
        day += timedelta(days=1)

def day_summaries(plans, logs, first_day, last_day, tz, now):
    index = _log_index(logs)
    return [
        DaySummary(day, classify(slots_for_day(plans, day, tz), index, now))
        for day in days_between(first_day, last_day)
    ]

def today_slots(plans, logs, tz, now):
    return day_summaries(plans, logs, now.astimezone(tz).date(), now.astimezone(tz).date(), tz, now)[0].slots


def streak(plans, logs, tz, now, start_day=None):
    today = now.astimezone(tz).date()
    earliest = today - timedelta(days=STREAK_LOOKBACK_DAYS)
    if start_day is not None:
        earliest = max(earliest, start_day)
    if earliest > today:
        return 0
    count = 0
    for summary in reversed(day_summaries(plans, logs, earliest, today, tz, now)):
        if summary.has_missed:
            break
        if summary.all_done:
            count += 1
    return count


def clamp_health(value):
    return max(HEALTH_FLOOR, min(HEALTH_MAX, value))

def newly_missed(plans, logs, tz, since, now):
    if since >= now:
        return 0
    first = since.astimezone(tz).date() - timedelta(days=1)
    last = now.astimezone(tz).date()
    count = 0
    for summary in day_summaries(plans, logs, first, last, tz, now):
        for slot in summary.slots:
            if slot.status == MISSED and since < slot.window_end <= now:
                count += 1
    return count

def health_after_misses(health, missed_count):
    return clamp_health(health - HEALTH_PER_MISS * missed_count)


def health_after_doses(health, dose_count):
    return clamp_health(health + HEALTH_PER_DOSE * dose_count)


def mood(health):
    """'happy', 'content' or 'sleepy' (never sad or sick)."""
    if health >= 70:
        return "happy"
    if health >= 40:
        return "content"
    return "sleepy"


@dataclass
class WeekSummary:
    taken: int
    total: int

    @property
    def percent(self):
        return round(100 * self.taken / self.total) if self.total else None

def week_summary(plans, logs, tz, now, days=7):
    today = now.astimezone(tz).date()
    taken = total = 0
    for summary in day_summaries(plans, logs, today - timedelta(days=days - 1), today, tz, now):
        for slot in summary.slots:
            if slot.status in SETTLED:
                total += 1
                taken += slot.status in DONE
    return WeekSummary(taken, total)

@dataclass
class LogEntry:
    when: datetime
    medication_name: str
    status: str
    scheduled_at: datetime | None = None

def log_entries(plans, logs, tz, now, days=14):
    today = now.astimezone(tz).date()
    first_day = today - timedelta(days=days - 1)
    entries = []
    for summary in day_summaries(plans, logs, first_day, today, tz, now):
        for slot in summary.slots:
            if slot.status in DONE:
                entries.append(LogEntry(slot.taken_at, slot.medication_name, slot.status, slot.due_at))
            elif slot.status == MISSED:
                entries.append(LogEntry(slot.due_at, slot.medication_name, MISSED, slot.due_at))
    window_start = datetime.combine(first_day, time.min, tzinfo=tz)
    for log in logs:
        if log.scheduled_date is None and log.taken_at >= window_start:
            entries.append(LogEntry(log.taken_at, log.medication_name, "as_needed"))
    entries.sort(key=lambda e: e.when, reverse=True)
    return entries

def describe_weekdays(weekdays):
    weekdays = frozenset(weekdays)
    if weekdays == EVERY_DAY:
        return "Every day"
    if weekdays == frozenset(range(5)):
        return "Weekdays"
    if weekdays == frozenset({5, 6}):
        return "Weekends"
    return ", ".join(WEEKDAY_NAMES[d] for d in sorted(weekdays))