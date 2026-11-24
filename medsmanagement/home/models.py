import secrets
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import models
from django.utils.timezone import now as tz_now

from . import schedule

class PatientProfile(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="patient")
    timezone = models.CharField(max_length=64, default="America/New_York")
    pet_name = models.CharField(max_length=30, default="Pip")
    health = models.PositiveSmallIntegerField(default = schedule.HEALTH_START)
    health_checked_at = models.DateTimeField(default=tz_now)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user} ({self.timezone})"

    @property
    def tz(self):
        return ZoneInfo(self.timezone)

    @property
    def display_name(self):
        return self.user.first_name or self.user.get_username()

class Medication(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="medications")
    name = models.CharField(max_length=120)
    strength = models.CharField(max_length=60, blank=True)
    how_to_take = models.CharField(max_length=200, blank=True)
    as_needed = models.BooleanField(default=False)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    stopped_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} {self.strength}".strip()

    @property
    def label(self):
        return str(self)

    def current_times(self):
        return [t for t in self.dose_times.all() if t.active_until is None]

class DoseTime(models.Model):
    medication = models.ForeignKey(Medication, on_delete=models.CASCADE, related_name="dose_times")
    at = models.TimeField()
    weekdays = models.CharField(max_length=7, default="0123456")
    active_from = models.DateTimeField(default=tz_now)
    active_until = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["at"]

    def __str__(self):
        return f"{self.medication} at {self.at:%H:%M}"

    @property
    def weekday_set(self):
        return frozenset(int(d) for d in self.weekdays)

class DoseLog(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="dose_logs")
    medication = models.ForeignKey(Medication, on_delete=models.SET_NULL, null=True, related_name="logs")
    medication_name = models.CharField(max_length=200)
    scheduled_date = models.DateField(null=True, blank=True)
    scheduled_time = models.TimeField(null=True, blank=True)
    taken_at = models.DateTimeField()
    logged_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-taken_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "medication", "scheduled_date", "scheduled_time"], 
                condition=models.Q(scheduled_date__isnull=False), 
                name="one_log_per_scheduled_dose",
            ),
        ]

    def __str__(self):
        return f"{self.user}: {self.medication_name} at {self.taken_at:%Y-%m-%d %H:%M}"

def new_token():
    return secrets.token_urlsafe(18)

class CaregiverLink(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="caregiver_links")
    token = models.CharField(max_length=40, unique=True, default=new_token)
    created_at = models.DateTimeField(auto_now_add=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Caregiver link for {self.user}"

    @property
    def is_active(self):
        return self.revoked_at is None