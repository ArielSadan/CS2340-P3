import zoneinfo
from datetime import datetime

from django import forms
from django.contrib.auth.forms import UserCreationForm

from . import schedule
from .schedule_ai import ALLOWED_EXTENSIONS, MAX_TIMES

COMMON_TIMEZONES = [
    ("America/New_York", "Eastern (New York, Atlanta)"),
    ("America/Chicago", "Central (Chicago)"),
    ("America/Denver", "Mountain (Denver)"),
    ("America/Phoenix", "Arizona (Phoenix)"),
    ("America/Los_Angeles", "Pacific (Los Angeles)"),
    ("America/Anchorage", "Alaska"),
    ("Pacific/Honolulu", "Hawaii"),
]

def timezone_choices():
    common = {value for value, _ in COMMON_TIMEZONES}
    others = sorted(
        tz for tz in zoneinfo.available_timezones()
        if "/" in tz and tz not in common and not tz.startswith(("Etc/", "SystemV/", "posix/", "right/"))
    )
    return [("Common", COMMON_TIMEZONES), ("Everywhere else", [(tz, tz.replace("_", " ")) for tz in others])]

class TimezoneField(forms.ChoiceField):
    def __init__(self, **kwargs):
        kwargs.setdefault("label", "Your time zone")
        kwargs.setdefault("help_text", "Pill times use this time zone. It was filled in from your device.")
        super().__init__(choices=timezone_choices, **kwargs)
        self.widget.attrs["data-detect-timezone"] = "true"

class SignupForm(UserCreationForm):
    first_name = forms.CharField(label="What should we call you?", max_length=150)
    timezone = TimezoneField(initial="America/New_York")
    pet_name = forms.CharField(label="Name your buddy", max_length=30, initial="Pip")

    field_order = ["first_name", "username", "password1", "password2", "timezone", "pet_name"]

    class Meta(UserCreationForm.Meta):
        fields = ("first_name", "username")

class SettingsForm(forms.Form):
    first_name = forms.CharField(label="Your name", max_length=150)
    timezone = TimezoneField()
    pet_name = forms.CharField(label="Your buddy's name", max_length=30)


WEEKDAY_CHOICES = [(str(i), name) for i, name in enumerate(schedule.WEEKDAY_NAMES)]
TIME_WIDGET = forms.TimeInput(attrs={"type": "time"}, format="%H:%M")

class MedicationForm(forms.Form):
    name = forms.CharField(label="Medicine", max_length=120)
    strength = forms.CharField(label="Strength", max_length=60, required=False,
                               widget=forms.TextInput(attrs={"placeholder": "e.g. 500 mg"}))
    how_to_take = forms.CharField(label="How to take it", max_length=200, required=False,
                                  widget=forms.TextInput(attrs={"placeholder": "e.g. 1 tablet with food"}))
    as_needed = forms.BooleanField(label="Only when needed (no set times)", required=False)
    weekdays = forms.MultipleChoiceField(label="Days", choices=WEEKDAY_CHOICES, initial=[c for c, _ in WEEKDAY_CHOICES],
                                         widget=forms.CheckboxSelectMultiple, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for i in range(1, MAX_TIMES + 1):
            self.fields[f"time_{i}"] = forms.TimeField(label=f"Time {i}", required=False, widget=TIME_WIDGET)

    def time_fields(self):
        return [self[f"time_{i}"] for i in range(1, MAX_TIMES + 1)]

    def clean(self):
        data = super().clean()
        times = sorted({data[f"time_{i}"] for i in range(1, MAX_TIMES + 1) if data.get(f"time_{i}")})
        data["times"] = times
        data["weekdays"] = "".join(sorted(data.get("weekdays") or []))
        if not data.get("as_needed"):
            if not times:
                raise forms.ValidationError("Add at least one time, or tick \"Only when needed\".")
            if not data["weekdays"]:
                raise forms.ValidationError("Choose at least one day.")
        return data

    @staticmethod
    def initial_from_medication(med):
        times = med.current_times()
        initial = {
            "name": med.name, "strength": med.strength, "how_to_take": med.how_to_take,
            "as_needed": med.as_needed,
            "weekdays": list(times[0].weekdays) if times else [c for c, _ in WEEKDAY_CHOICES],
        }
        for i, t in enumerate(times[:MAX_TIMES], start=1):
            initial[f"time_{i}"] = t.at
        return initial

    @staticmethod
    def initial_from_draft(draft):
        initial = {
            "name": draft["name"], "strength": draft["strength"], "how_to_take": draft["how_to_take"],
            "as_needed": draft["as_needed"], "weekdays": list(draft["weekdays"]),
        }
        for i, value in enumerate(draft["times"][:MAX_TIMES], start=1):
            initial[f"time_{i}"] = datetime.strptime(value, "%H:%M").time()
        return initial

class ReviewMedicationForm(MedicationForm):
    include = forms.BooleanField(label="Add this medicine", required=False, initial=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["name"].required = False   # checked in clean(), only for rows being added

    def clean(self):
        if not self.cleaned_data.get("include"):
            self.cleaned_data["times"] = []
            return self.cleaned_data       # skipped rows aren't checked
        if not (self.cleaned_data.get("name") or "").strip():
            raise forms.ValidationError("Give this medicine a name, or untick \"Add this medicine\".")
        return super().clean()


ReviewFormSet = forms.formset_factory(ReviewMedicationForm, extra=0)

class UploadInstructionsForm(forms.Form):
    file = forms.FileField(
        label="Photo or file of the instructions", required=False,
        widget=forms.ClearableFileInput(attrs={"accept": ",".join(ALLOWED_EXTENSIONS) + ",image/*"}),
    )
    text = forms.CharField(label="Or paste the instructions", required=False,
                           widget=forms.Textarea(attrs={"rows": 5}))

    def clean(self):
        data = super().clean()
        if not data.get("file") and not (data.get("text") or "").strip():
            raise forms.ValidationError("Choose a photo or file, or paste the instructions.")
        return data

class FeedForm(forms.Form):
    doses = forms.MultipleChoiceField(required=False, widget=forms.CheckboxSelectMultiple)
    as_needed = forms.MultipleChoiceField(required=False, widget=forms.CheckboxSelectMultiple)
    taken_time = forms.TimeField(label="Time taken", widget=TIME_WIDGET)

    def __init__(self, *args, dose_choices=(), as_needed_choices=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["doses"].choices = dose_choices
        self.fields["as_needed"].choices = as_needed_choices

    def clean(self):
        data = super().clean()
        if not data.get("doses") and not data.get("as_needed"):
            raise forms.ValidationError("No medication selected. Select at least one medication to feed your buddy.")
        return data