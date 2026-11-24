import json
from datetime import date, datetime, time, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from home import schedule as s
from home.models import CaregiverLink, DoseLog, DoseTime, Medication
from home.schedule_ai import ScheduleError, clean_draft, parse_days, parse_time
from home.services import get_profile, save_medication

NY = ZoneInfo("America/New_York")
LA = ZoneInfo("America/Los_Angeles")


def plan(med_id, name, hour, minute=0, **kw):
    return s.PlannedTime(med_id, name, time(hour, minute), **kw)


def took(med_id, day, hour, at_hour, at_minute=0, tz=NY):
    return s.LoggedDose(med_id, "x", datetime.combine(day, time(at_hour, at_minute), tz), day, time(hour))


class DoseTimingTests(SimpleTestCase):
    PLANS = [plan(1, "Lisinopril", 8), plan(2, "Metformin", 20)]

    def statuses(self, now, logs=(), tz=NY):
        return [slot.status for slot in s.today_slots(self.PLANS, list(logs), tz, now)]

    def test_due_window_is_one_hour_before_to_two_hours_after(self):
        day = date(2026, 11, 23)
        self.assertEqual(self.statuses(datetime(2026, 11, 23, 6, 59, tzinfo=NY))[0], s.UPCOMING)
        self.assertEqual(self.statuses(datetime(2026, 11, 23, 7, 0, tzinfo=NY))[0], s.DUE)
        self.assertEqual(self.statuses(datetime(2026, 11, 23, 10, 0, tzinfo=NY))[0], s.DUE)
        self.assertEqual(self.statuses(datetime(2026, 11, 23, 10, 1, tzinfo=NY))[0], s.MISSED)
        self.assertEqual(self.statuses(datetime(2026, 11, 23, 10, 1, tzinfo=NY), [took(1, day, 8, 8, 5)])[0], s.TAKEN)

    def test_logging_after_the_window_is_taken_late(self):
        day = date(2026, 11, 23)
        now = datetime(2026, 11, 23, 13, 0, tzinfo=NY)
        self.assertEqual(self.statuses(now, [took(1, day, 8, 12, 30)])[0], s.TAKEN_LATE)

    def test_each_person_uses_their_own_time_zone(self):
        instant = datetime(2026, 11, 23, 8, 30, tzinfo=NY)
        self.assertEqual(self.statuses(instant, tz=NY)[0], s.DUE)
        self.assertEqual(self.statuses(instant.astimezone(LA), tz=LA)[0], s.UPCOMING)

    def test_weekdays_and_active_period(self):
        monday = date(2026, 11, 23)
        weekly = [plan(1, "Vitamin D", 8, weekdays=frozenset({0}))]
        self.assertEqual(len(s.slots_for_day(weekly, monday, NY)), 1)
        self.assertEqual(len(s.slots_for_day(weekly, monday + timedelta(days=1), NY)), 0)
        started = [plan(1, "New", 8, active_from=datetime(2026, 11, 23, 9, tzinfo=NY))]
        self.assertEqual(s.slots_for_day(started, monday, NY), [])
        self.assertEqual(len(s.slots_for_day(started, monday + timedelta(days=1), NY)), 1)
        ended = [plan(1, "Old", 20, active_until=datetime(2026, 11, 23, 12, tzinfo=NY))]
        self.assertEqual(s.slots_for_day(ended, monday, NY), [])


class StreakAndHealthTests(SimpleTestCase):
    PLANS = [plan(1, "Lisinopril", 8), plan(2, "Metformin", 20)]

    def full_days(self, first, last):
        logs = []
        for d in s.days_between(first, last):
            logs += [took(1, d, 8, 8, 5), took(2, d, 20, 20, 5)]
        return logs

    def test_streak_counts_full_days_and_waits_for_today(self):
        now = datetime(2026, 11, 23, 9, 0, tzinfo=NY)
        logs = self.full_days(date(2026, 11, 20), date(2026, 11, 22)) + [took(1, date(2026, 11, 23), 8, 8, 5)]
        self.assertEqual(s.streak(self.PLANS, logs, NY, now, start_day=date(2026, 11, 20)), 3)

    def test_streak_includes_today_once_everything_is_taken(self):
        now = datetime(2026, 11, 23, 21, 0, tzinfo=NY)
        logs = self.full_days(date(2026, 11, 20), date(2026, 11, 23))
        self.assertEqual(s.streak(self.PLANS, logs, NY, now, start_day=date(2026, 11, 20)), 4)

    def test_a_missed_dose_ends_the_streak(self):
        now = datetime(2026, 11, 23, 9, 0, tzinfo=NY)
        logs = self.full_days(date(2026, 11, 20), date(2026, 11, 21)) + [took(1, date(2026, 11, 22), 8, 8, 5)]
        self.assertEqual(s.streak(self.PLANS, logs, NY, now, start_day=date(2026, 11, 20)), 0)

    def test_newly_missed_counts_each_dose_once(self):
        since = datetime(2026, 11, 22, 9, 0, tzinfo=NY)
        now = datetime(2026, 11, 23, 10, 1, tzinfo=NY)
        self.assertEqual(s.newly_missed(self.PLANS, [], NY, since, now), 3)
        self.assertEqual(s.newly_missed(self.PLANS, [], NY, now, now), 0)

    def test_health_rises_and_falls_gently_and_never_below_the_floor(self):
        self.assertEqual(s.health_after_doses(60, 2), 100)
        self.assertEqual(s.health_after_doses(95, 1), s.HEALTH_MAX)
        self.assertEqual(s.health_after_misses(60, 2), 40)
        self.assertEqual(s.health_after_misses(30, 10), s.HEALTH_FLOOR)
        self.assertEqual([s.mood(90), s.mood(50), s.mood(20)], ["happy", "content", "sleepy"])

    def test_week_summary_and_log_entries(self):
        now = datetime(2026, 11, 23, 21, 0, tzinfo=NY)
        start = datetime(2026, 11, 17, 0, 0, tzinfo=NY)
        plans = [plan(1, "Lisinopril", 8, active_from=start), plan(2, "Metformin", 20, active_from=start)]
        logs = self.full_days(date(2026, 11, 17), date(2026, 11, 22)) + [took(1, date(2026, 11, 23), 8, 8, 5)]
        week = s.week_summary(plans, logs, NY, now)
        self.assertEqual((week.taken, week.total), (13, 13))
        entries = s.log_entries(plans, logs, NY, now, days=1)
        self.assertEqual([e.status for e in entries], [s.TAKEN])
        later = now + timedelta(hours=2)
        self.assertEqual(s.log_entries(plans, logs, NY, later, days=1)[0].status, s.MISSED)


class CleanDraftTests(SimpleTestCase):
    def test_times_and_days(self):
        self.assertEqual(parse_time("8:00 PM"), "20:00")
        self.assertEqual(parse_time("08:30"), "08:30")
        self.assertEqual(parse_time("12 am"), "00:00")
        self.assertIsNone(parse_time("25:00"))
        self.assertIsNone(parse_time("with breakfast"))
        self.assertEqual(parse_days(["Monday", "wed"]), "02")
        self.assertEqual(parse_days([]), "0123456")

    def test_clean_draft_tidies_and_flags_problems(self):
        meds = clean_draft(json.dumps({
            "is_medication_instructions": True,
            "medications": [
                {"name": "Metformin", "strength": "500 mg", "how_to_take": "1 tablet with food",
                 "times": ["20:00", "08:00", "08:00"], "days": [], "as_needed": False,
                 "needs_check": False, "check_note": "", "source_text": "Take twice daily with meals"},
                {"name": "Lisinopril", "strength": "10 mg", "how_to_take": "", "times": ["breakfast"],
                 "days": [], "as_needed": False, "needs_check": False, "check_note": "", "source_text": ""},
                {"name": "Tylenol", "strength": "", "how_to_take": "", "times": ["08:00"], "days": [],
                 "as_needed": True, "needs_check": False, "check_note": "", "source_text": ""},
                {"name": "  ", "times": []},
            ],
        }))
        self.assertEqual([m["name"] for m in meds], ["Metformin", "Lisinopril", "Tylenol"])
        self.assertEqual(meds[0]["times"], ["08:00", "20:00"])
        self.assertFalse(meds[0]["needs_check"])
        self.assertTrue(meds[1]["needs_check"])
        self.assertIn("breakfast", meds[1]["check_note"])
        self.assertEqual(meds[2]["times"], [])

    def test_not_instructions_or_empty_reply(self):
        with self.assertRaisesRegex(ScheduleError, "doesn't look like medication instructions"):
            clean_draft({"is_medication_instructions": False, "medications": []})
        with self.assertRaisesRegex(ScheduleError, "No medicines"):
            clean_draft({"is_medication_instructions": True, "medications": []})
        with self.assertRaises(ScheduleError):
            clean_draft("not json")



FIXED_NOW = datetime(2026, 11, 23, 8, 30, tzinfo=NY)
LONG_AGO = FIXED_NOW - timedelta(days=30)


class PipTestCase(TestCase):
    def setUp(self):
        clock = mock.patch("django.utils.timezone.now", return_value=FIXED_NOW)
        clock.start()
        self.addCleanup(clock.stop)
        self.user = get_user_model().objects.create_user("jane", password="pw-12345-ok", first_name="Jane")
        self.profile = get_profile(self.user)
        self.profile.timezone = "America/New_York"
        self.profile.health_checked_at = FIXED_NOW
        self.profile.save()
        self.client.force_login(self.user)

    def add_med(self, name, *hours, as_needed=False, since=LONG_AGO):
        med = Medication.objects.create(user=self.user, name=name, as_needed=as_needed)
        for hour in hours:
            DoseTime.objects.create(medication=med, at=time(hour), active_from=since)
        return med


class HubAndAccountTests(PipTestCase):
    def test_logged_out_visitors_see_the_welcome_page(self):
        self.client.logout()
        response = self.client.get(reverse("home:hub"))
        self.assertContains(response, "Meet your medicine buddy")
        self.assertContains(response, "turtle")

    def test_signup_saves_time_zone_and_pet_name(self):
        self.client.logout()
        response = self.client.post(reverse("home:signup"), {
            "first_name": "Fabian", "username": "fabian", "password1": "a-Long-pass-123",
            "password2": "a-Long-pass-123", "timezone": "America/Los_Angeles", "pet_name": "Shelly",
        })
        self.assertRedirects(response, reverse("home:medications"))
        profile = get_profile(get_user_model().objects.get(username="fabian"))
        self.assertEqual((profile.timezone, profile.pet_name), ("America/Los_Angeles", "Shelly"))

    def test_hub_shows_todays_doses(self):
        self.add_med("Lisinopril", 8)
        self.add_med("Metformin", 20)
        response = self.client.get(reverse("home:hub"))
        self.assertContains(response, "Due now")
        self.assertContains(response, "8:00 PM")
        self.assertContains(response, "1 due now")

    def test_missed_doses_make_pip_sleepier_but_not_below_the_floor(self):
        self.add_med("Lisinopril", 8)
        self.profile.health_checked_at = LONG_AGO
        self.profile.save()
        self.client.get(reverse("home:hub"))
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.health, s.HEALTH_FLOOR)
        self.assertContains(self.client.get(reverse("home:hub")), "feeling sleepy")

    def test_pages_need_login(self):
        self.client.logout()
        for name in ["home:feed", "home:medications", "home:upload", "home:share", "home:settings"]:
            response = self.client.get(reverse(name))
            self.assertEqual(response.status_code, 302, name)
            self.assertIn(reverse("home:login"), response["Location"])


class FeedPipTests(PipTestCase):
    def test_nothing_selected_shows_the_wireframe_message(self):
        self.add_med("Lisinopril", 8)
        response = self.client.post(reverse("home:feed"), {"taken_time": "08:25"})
        self.assertContains(response, "No medication selected")
        self.assertFalse(DoseLog.objects.exists())

    def test_logging_a_due_dose_feeds_pip(self):
        med = self.add_med("Lisinopril", 8)
        key = f"{med.pk}|2026-11-23|08:00"
        response = self.client.post(reverse("home:feed"), {"doses": [key], "taken_time": "08:25"})
        self.assertRedirects(response, reverse("home:fed"), fetch_redirect_response=False)        log = DoseLog.objects.get()
        self.assertEqual((log.scheduled_date, log.scheduled_time), (date(2026, 11, 23), time(8)))
        self.assertEqual(log.taken_at, datetime(2026, 11, 23, 8, 25, tzinfo=NY))
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.health, s.HEALTH_START + s.HEALTH_PER_DOSE)
        fed = self.client.get(reverse("home:fed"))
        self.assertContains(fed, "Pip is fed!")
        self.assertContains(fed, "Health +20")

    def test_as_needed_medicine_can_be_logged(self):
        med = self.add_med("Tylenol", as_needed=True)
        self.client.post(reverse("home:feed"), {"as_needed": [str(med.pk)], "taken_time": "08:00"})
        log = DoseLog.objects.get()
        self.assertIsNone(log.scheduled_date)

    def test_logging_twice_does_not_double_count(self):
        med = self.add_med("Lisinopril", 8)
        key = f"{med.pk}|2026-11-23|08:00"
        self.client.post(reverse("home:feed"), {"doses": [key], "taken_time": "08:25"})
        response = self.client.post(reverse("home:feed"), {"doses": [key], "taken_time": "08:25"})
        self.assertEqual(DoseLog.objects.count(), 1)
        self.assertEqual(response.status_code, 200)

    def test_time_taken_cannot_be_in_the_future(self):
        med = self.add_med("Lisinopril", 8)
        response = self.client.post(reverse("home:feed"), {"doses": [f"{med.pk}|2026-11-23|08:00"], "taken_time": "11:00"})
        self.assertContains(response, "can&#x27;t be later than now")


class MedicationTests(PipTestCase):
    def test_add_by_hand(self):
        response = self.client.post(reverse("home:medication_add"), {
            "name": "Metformin", "strength": "500 mg", "how_to_take": "with food",
            "time_1": "08:00", "time_2": "20:00", "weekdays": list("0123456"),
        })
        self.assertRedirects(response, reverse("home:medications"))
        med = Medication.objects.get()
        self.assertEqual([t.at for t in med.current_times()], [time(8), time(20)])

    def test_needs_a_time_unless_as_needed(self):
        response = self.client.post(reverse("home:medication_add"), {"name": "Metformin", "weekdays": list("0123456")})
        self.assertContains(response, "Add at least one time")

    def test_changing_times_keeps_history(self):
        med, _ = save_medication(self.user, name="Metformin", times=[time(8), time(20)])
        save_medication(self.user, name="Metformin", times=[time(8), time(18)], medication=med)
        self.assertEqual([t.at for t in med.current_times()], [time(8), time(18)])
        ended = DoseTime.objects.get(medication=med, at=time(20))
        self.assertIsNotNone(ended.active_until)

    def test_stop_taking(self):
        med = self.add_med("Lisinopril", 8)
        self.client.post(reverse("home:medication_stop", args=[med.pk]))
        med.refresh_from_db()
        self.assertFalse(med.active)
        self.assertFalse(DoseTime.objects.filter(medication=med, active_until__isnull=True).exists())


DRAFT = [{
    "name": "Metformin", "strength": "500 mg", "how_to_take": "1 tablet with food", "times": ["08:00", "20:00"],
    "weekdays": "0123456", "as_needed": False, "needs_check": False, "check_note": "",
    "source_text": "Take 1 tablet by mouth twice daily with meals",
}, {
    "name": "Lisinopril", "strength": "10 mg", "how_to_take": "", "times": [], "weekdays": "0123456",
    "as_needed": False, "needs_check": True, "check_note": "No time of day was found.", "source_text": "",
}]


def review_post(rows):
    data = {"form-TOTAL_FORMS": str(len(rows)), "form-INITIAL_FORMS": str(len(rows))}
    for i, row in enumerate(rows):
        for key, value in row.items():
            data[f"form-{i}-{key}"] = value
    return data


class UploadAndReviewTests(PipTestCase):
    @mock.patch("home.views.draft_schedule", return_value=DRAFT)
    def test_upload_makes_a_draft_that_must_be_confirmed(self, fake_gemini):
        photo = SimpleUploadedFile("label.jpg", b"\xff\xd8 fake jpeg", content_type="image/jpeg")
        response = self.client.post(reverse("home:upload"), {"file": photo})
        self.assertRedirects(response, reverse("home:review"))
        self.assertEqual(fake_gemini.call_args.kwargs["filename"], "label.jpg")
        self.assertFalse(Medication.objects.exists())

        review = self.client.get(reverse("home:review"))
        self.assertContains(review, "Please check:")
        self.assertContains(review, "twice daily with meals")

        response = self.client.post(reverse("home:review"), review_post([
            {"include": "on", "name": "Metformin", "strength": "500 mg", "time_1": "08:00", "time_2": "20:00",
             "weekdays": list("0123456")},
            {"name": "Lisinopril"},
        ]))
        self.assertRedirects(response, reverse("home:medications"))
        self.assertEqual(list(Medication.objects.values_list("name", flat=True)), ["Metformin"])

    @mock.patch("home.views.draft_schedule", side_effect=ScheduleError("This doesn't look like medication instructions."))
    def test_unreadable_upload_shows_a_friendly_error(self, _):
        photo = SimpleUploadedFile("cat.png", b"\x89PNG fake", content_type="image/png")
        response = self.client.post(reverse("home:upload"), {"file": photo})
        self.assertContains(response, "doesn&#x27;t look like medication instructions")

    def test_review_without_a_draft_goes_back_to_upload(self):
        self.assertRedirects(self.client.get(reverse("home:review")), reverse("home:upload"))


class CaregiverTests(PipTestCase):
    def test_share_link_shows_the_log_read_only(self):
        lisinopril = self.add_med("Lisinopril", 8)
        DoseLog.objects.create(user=self.user, medication=lisinopril, medication_name="Lisinopril",
                               scheduled_date=date(2026, 11, 22), scheduled_time=time(8),
                               taken_at=datetime(2026, 11, 22, 8, 5, tzinfo=NY))
        self.client.post(reverse("home:share"), {"action": "create"})
        link = CaregiverLink.objects.get()
        self.client.logout()
        response = self.client.get(reverse("home:caregiver", args=[link.token]))
        self.assertContains(response, "Jane's medication log")
        self.assertContains(response, "Taken")
        self.assertContains(response, "Missed")

    def test_turning_sharing_off_breaks_the_link(self):
        self.client.post(reverse("home:share"), {"action": "create"})
        token = CaregiverLink.objects.get().token
        self.client.post(reverse("home:share"), {"action": "revoke"})
        self.assertEqual(self.client.get(reverse("home:caregiver", args=[token])).status_code, 404)

    def test_wrong_token_is_not_found(self):
        self.assertEqual(self.client.get(reverse("home:caregiver", args=["nope"])).status_code, 404)