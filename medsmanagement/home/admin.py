from django.contrib import admin

from .models import CaregiverLink, DoseLog, DoseTime, Medication, PatientProfile

@admin.register(PatientProfile)
class PatientProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "timezone", "pet_name", "health", "created_at")

class DoseTimeInline(admin.TabularInline):
    model = DoseTime
    extra = 0

@admin.register(Medication)
class MedicationAdmin(admin.ModelAdmin):
    list_display = ("name", "strength", "user", "as_needed", "active")
    list_filter = ("active", "as_needed")
    inlines = [DoseTimeInline]

@admin.register(DoseLog)
class DoseLogAdmin(admin.ModelAdmin):
    list_display = ("user", "medication_name", "scheduled_date", "scheduled_time", "taken_at")
    list_filter = ("user",)

@admin.register(CaregiverLink)
class CaregiverLinkAdmin(admin.ModelAdmin):
    list_display = ("user", "created_at", "revoked_at")