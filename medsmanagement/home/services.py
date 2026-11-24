from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from . import schedule
from .models import CaregiverLink, DoseLog, DoseTime, Medication, PatientProfile

def get_profile(user):
    profile, _ = PatientProfile.objects.get_or_create(user=user)
    return profile


def load_plans(user):
    times = DoseTime.objects.filter(medication__user=user).select_related("medication")
    return [
        schedule.PlannedTime(
            medication_id=t.medication_id,
            medication_name=t.medication.label,
            at=t.at,
            weekdays=t.weekday_set,
            active_from=t.active_from,
            active_until=t.active_until,
        )
        for t in times
    ]

def load_logs(user, since=None):
    logs = DoseLog.objects.filter(user=user)
    if since is not None:
        logs = logs.filter(taken_at__gte=since - timedelta(days=1))
    return [
        schedule.LoggedDose(l.medication_id, l.medication_name, l.taken_at, l.scheduled_date, l.scheduled_time)
        for l in logs
    ]

def refresh_health(profile, plans=None, logs=None, now=None):
    now = now or timezone.now()
    plans = load_plans(profile.user) if plans is None else plans
    logs = load_logs(profile.user, since=profile.health_checked_at) if logs is None else logs
    missed = schedule.newly_missed(plans, logs, profile.tz, profile.health_checked_at, now)
    profile.health = schedule.health_after_misses(profile.health, missed)
    profile.health_checked_at = now
    profile.save(update_fields=["health", "health_checked_at"])
    return missed

def active_caregiver_link(user):
    return CaregiverLink.objects.filter(user=user, revoked_at__isnull=True).order_by("-created_at").first()

def log_doses(profile, slots, as_needed_meds, taken_at):
    user = profile.user
    names = []
    with transaction.atomic():
        for slot in slots:
            try:
                with transaction.atomic():
                    DoseLog.objects.create(
                        user=user,
                        medication_id=slot.medication_id,
                        medication_name=slot.medication_name,
                        scheduled_date=slot.day,
                        scheduled_time=slot.at,
                        taken_at=taken_at,
                    )
                names.append(slot.medication_name)
            except IntegrityError:
                continue
        for med in as_needed_meds:
            DoseLog.objects.create(user=user, medication=med, medication_name=med.label, taken_at=taken_at)
            names.append(med.label)
        profile.health = schedule.health_after_doses(profile.health, len(names))
        profile.save(update_fields=["health"])
    return names

def save_medication(user, *, name, strength="", how_to_take="", as_needed=False,
                    times=(), weekdays="0123456", medication=None):
    now = timezone.now()
    created = medication is None
    with transaction.atomic():
        if medication is None:
            medication = Medication(user=user)
        medication.name = name
        medication.strength = strength
        medication.how_to_take = how_to_take
        medication.as_needed = as_needed
        medication.active = True
        medication.stopped_at = None
        medication.save()

        wanted = set() if as_needed else {(t, weekdays) for t in times}
        for dose_time in medication.current_times() if not created else []:
            key = (dose_time.at, dose_time.weekdays)
            if key in wanted:
                wanted.discard(key)
            else:
                dose_time.active_until = now
                dose_time.save(update_fields=["active_until"])
        DoseTime.objects.bulk_create(
            DoseTime(medication=medication, at=at, weekdays=days, active_from=now) for at, days in sorted(wanted)
        )
    return medication, created

def find_active_by_name(user, name):
    return Medication.objects.filter(user=user, active=True, name__iexact=name.strip()).first()

def stop_medication(medication):
    now = timezone.now()
    with transaction.atomic():
        medication.active = False
        medication.stopped_at = now
        medication.save(update_fields=["active", "stopped_at"])
        medication.dose_times.filter(active_until__isnull=True).update(active_until=now)