from datetime import datetime, timedelta
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import schedule, services
from .forms import (FeedForm, MedicationForm, ReviewFormSet, SettingsForm, SignupForm, UploadInstructionsForm)
from .models import CaregiverLink, Medication
from .schedule_ai import ScheduleError, draft_schedule

DRAFT_SESSION_KEY = "pip_schedule_draft"
FED_SESSION_KEY = "pip_last_feed"
FUTURE_SLACK = timedelta(minutes=5)

def fmt_time(value):
    return value.strftime("%I:%M %p").lstrip("0")

def fmt_day_time(value, tz):
    local = value.astimezone(tz)
    return f"{local:%a %b} {local.day}, {fmt_time(local)}"

def _slot_key(slot):
    return f"{slot.medication_id}|{slot.day.isoformat()}|{slot.at:%H:%M}"

def _patient_state(user, now=None):
    now = now or timezone.now()
    profile = services.get_profile(user)
    plans = services.load_plans(user)
    logs = services.load_logs(user)
    services.refresh_health(profile, plans, logs, now)
    tz = profile.tz
    slots = schedule.today_slots(plans, logs, tz, now)
    start_day = profile.created_at.astimezone(tz).date()
    return {
        "profile": profile,
        "tz": tz,
        "now": now,
        "slots": slots,
        "streak": schedule.streak(plans, logs, tz, now, start_day=start_day),
    }

def hub(request):
    if not request.user.is_authenticated:
        return render(request, "home/welcome.html")
    state = _patient_state(request.user)
    profile, slots = state["profile"], state["slots"]
    for slot in slots:
        slot.time_label = fmt_time(slot.at)
        slot.taken_label = fmt_time(slot.taken_at.astimezone(state["tz"])) if slot.taken_at else ""
    meds = Medication.objects.filter(user=request.user, active=True)
    return render(request, "home/hub.html", {
        "profile": profile,
        "mood": schedule.mood(profile.health),
        "streak": state["streak"],
        "slots": slots,
        "due_count": sum(s.status == schedule.DUE for s in slots),
        "has_meds": meds.exists(),
        "as_needed_meds": meds.filter(as_needed=True),
        "caregiver_link": services.active_caregiver_link(request.user),
    })

def signup(request):
    if request.user.is_authenticated:
        return redirect("home:hub")
    form = SignupForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        profile = services.get_profile(user)
        profile.timezone = form.cleaned_data["timezone"]
        profile.pet_name = form.cleaned_data["pet_name"].strip() or "Pip"
        profile.save()
        login(request, user)
        messages.success(request, f"Welcome! Add your medicines so {profile.pet_name} knows when they're due.")
        return redirect("home:medications")
    return render(request, "home/signup.html", {"form": form})

@login_required
def settings_view(request):
    profile = services.get_profile(request.user)
    form = SettingsForm(request.POST or None, initial={
        "first_name": request.user.first_name, "timezone": profile.timezone, "pet_name": profile.pet_name,
    })
    if request.method == "POST" and form.is_valid():
        request.user.first_name = form.cleaned_data["first_name"]
        request.user.save(update_fields=["first_name"])
        profile.timezone = form.cleaned_data["timezone"]
        profile.pet_name = form.cleaned_data["pet_name"].strip() or "Pip"
        profile.save(update_fields=["timezone", "pet_name"])
        messages.success(request, "Settings saved.")
        return redirect("home:hub")
    return render(request, "home/settings.html", {"form": form})

def _feed_groups(slots):
    open_slots = [s for s in slots if s.status not in schedule.DONE]
    return {
        "due": [s for s in open_slots if s.status == schedule.DUE],
        "missed": [s for s in open_slots if s.status == schedule.MISSED],
        "later": [s for s in open_slots if s.status == schedule.UPCOMING],
    }

@login_required
def feed(request):
    state = _patient_state(request.user)
    profile, tz, now = state["profile"], state["tz"], state["now"]
    groups = _feed_groups(state["slots"])
    open_slots = {_slot_key(s): s for group in groups.values() for s in group}
    as_needed = {str(m.pk): m for m in Medication.objects.filter(user=request.user, active=True, as_needed=True)}

    def label(slot):
        return f"{slot.medication_name} ({fmt_time(slot.at)})"

    form = FeedForm(
        request.POST or None,
        dose_choices=[(k, label(s)) for k, s in open_slots.items()],
        as_needed_choices=[(k, m.label) for k, m in as_needed.items()],
        initial={"taken_time": now.astimezone(tz).time().replace(second=0, microsecond=0)},
    )
    if request.method == "POST" and form.is_valid():
        today = now.astimezone(tz).date()
        taken_at = datetime.combine(today, form.cleaned_data["taken_time"], tzinfo=tz)
        if taken_at > now + FUTURE_SLACK:
            form.add_error("taken_time", "The time taken can't be later than now.")
        else:
            chosen = [open_slots[k] for k in form.cleaned_data["doses"]]
            meds = [as_needed[k] for k in form.cleaned_data["as_needed"]]
            before = profile.health
            names = services.log_doses(profile, chosen, meds, taken_at)
            if not names:
                messages.info(request, "Those medicines were already logged.")
                return redirect("home:hub")
            request.session[FED_SESSION_KEY] = {
                "names": names,
                "time": fmt_time(taken_at),
                "gain": profile.health - before,
            }
            return redirect("home:fed")

    for group in groups.values():
        for slot in group:
            slot.form_value = _slot_key(slot)
            slot.time_label = fmt_time(slot.at)
    return render(request, "home/feed.html", {
        "form": form,
        "profile": profile,
        "groups": groups,
        "as_needed": as_needed.values(),
        "nothing_to_log": not open_slots and not as_needed,
        "selected": set(form["doses"].value() or []),
        "selected_as_needed": set(form["as_needed"].value() or []),
    })

@login_required
def fed(request):
    result = request.session.pop(FED_SESSION_KEY, None)
    if not result:
        return redirect("home:hub")
    state = _patient_state(request.user)
    profile = state["profile"]
    return render(request, "home/fed.html", {
        "profile": profile,
        "mood": schedule.mood(profile.health),
        "result": result,
        "streak": state["streak"],
        "caregiver_link": services.active_caregiver_link(request.user),
    })

@login_required
def medications(request):
    meds = Medication.objects.filter(user=request.user, active=True).prefetch_related("dose_times")
    rows = []
    for med in meds:
        times = med.current_times()
        rows.append({
            "med": med,
            "times": ", ".join(fmt_time(t.at) for t in times),
            "days": schedule.describe_weekdays(times[0].weekday_set) if times else "",
        })
    return render(request, "home/medications.html", {"rows": rows, "profile": services.get_profile(request.user)})

def _save_from_form(user, data, medication=None):
    return services.save_medication(
        user,
        name=data["name"].strip(),
        strength=data.get("strength", "").strip(),
        how_to_take=data.get("how_to_take", "").strip(),
        as_needed=data.get("as_needed", False),
        times=data["times"],
        weekdays=data["weekdays"] or "0123456",
        medication=medication,
    )

@login_required
def medication_edit(request, pk=None):
    med = get_object_or_404(Medication, pk=pk, user=request.user, active=True) if pk else None
    initial = MedicationForm.initial_from_medication(med) if med else None
    form = MedicationForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        saved, _ = _save_from_form(request.user, form.cleaned_data, med)
        messages.success(request, f"Saved {saved.label}.")
        return redirect("home:medications")
    return render(request, "home/medication_form.html", {"form": form, "med": med})

@login_required
@require_POST
def medication_stop(request, pk):
    med = get_object_or_404(Medication, pk=pk, user=request.user, active=True)
    services.stop_medication(med)
    messages.success(request, f"Stopped {med.label}. Its past doses stay in the log.")
    return redirect("home:medications")

@login_required
def upload_instructions(request):
    form = UploadInstructionsForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        upload = form.cleaned_data.get("file")
        try:
            draft = draft_schedule(
                filename=upload.name if upload else None,
                data=upload.read() if upload else None,
                pasted_text=None if upload else form.cleaned_data.get("text"),
            )
        except ScheduleError as exc:
            form.add_error(None, exc.user_message)
        else:
            request.session[DRAFT_SESSION_KEY] = draft
            return redirect("home:review")
    return render(request, "home/upload.html", {"form": form})

def _review_initial(item):
    initial = MedicationForm.initial_from_draft(item)
    initial["include"] = True
    return initial

@login_required
def review_schedule(request):
    draft = request.session.get(DRAFT_SESSION_KEY)
    if not draft:
        messages.info(request, "Upload your instructions first.")
        return redirect("home:upload")
    formset = ReviewFormSet(request.POST or None, initial=[_review_initial(d) for d in draft])
    if request.method == "POST" and formset.is_valid():
        added = updated = 0
        for form in formset:
            data = form.cleaned_data
            if not data.get("include"):
                continue
            existing = services.find_active_by_name(request.user, data["name"])
            _, created = _save_from_form(request.user, data, existing)
            added += created
            updated += not created
        request.session.pop(DRAFT_SESSION_KEY, None)
        parts = [f"{added} added"] if added else []
        parts += [f"{updated} updated"] if updated else []
        messages.success(request, "Schedule saved: " + (", ".join(parts) or "nothing changed") + ".")
        return redirect("home:medications")
    rows = []
    for form, item in zip(formset, draft):
        existing = services.find_active_by_name(request.user, item["name"])
        rows.append({"form": form, "draft": item, "existing": existing})
    return render(request, "home/review.html", {"formset": formset, "rows": rows})

@login_required
@require_POST
def discard_draft(request):
    request.session.pop(DRAFT_SESSION_KEY, None)
    return redirect("home:medications")

@login_required
def share(request):
    if request.method == "POST":
        action = request.POST.get("action")
        current = services.active_caregiver_link(request.user)
        if action in ("create", "replace"):
            if current:
                current.revoked_at = timezone.now()
                current.save(update_fields=["revoked_at"])
            CaregiverLink.objects.create(user=request.user)
            messages.success(request, "New caregiver link made. Any old link no longer works.")
        elif action == "revoke" and current:
            current.revoked_at = timezone.now()
            current.save(update_fields=["revoked_at"])
            messages.success(request, "Sharing turned off. The old link no longer works.")
        return redirect("home:share")
    link = services.active_caregiver_link(request.user)
    url = request.build_absolute_uri(reverse("home:caregiver", args=[link.token])) if link else None
    return render(request, "home/share.html", {"link": link, "url": url, "profile": services.get_profile(request.user)})

def caregiver(request, token):
    link = CaregiverLink.objects.filter(token=token, revoked_at__isnull=True).select_related("user").first()
    if link is None:
        raise Http404("This caregiver link doesn't work any more.")
    user = link.user
    profile = services.get_profile(user)
    tz, now = profile.tz, timezone.now()
    plans, logs = services.load_plans(user), services.load_logs(user)
    week = schedule.week_summary(plans, logs, tz, now)
    status_labels = {
        schedule.TAKEN: "Taken", schedule.TAKEN_LATE: "Taken late",
        schedule.MISSED: "Missed", "as_needed": "Taken (as needed)",
    }
    entries = [
        {
            "when": fmt_day_time(e.when, tz),
            "name": e.medication_name,
            "status": e.status,
            "status_label": status_labels[e.status],
            "scheduled": fmt_time(e.scheduled_at.astimezone(tz)) if e.scheduled_at else "",
        }
        for e in schedule.log_entries(plans, logs, tz, now)
    ]
    return render(request, "home/caregiver.html", {
        "profile": profile, "week": week, "entries": entries, "tz_name": profile.timezone.replace("_", " "),
    })