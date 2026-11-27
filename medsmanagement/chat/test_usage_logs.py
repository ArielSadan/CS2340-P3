"""Tests for the usage logs story (#12)."""
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import Permission, User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import QueryLog, UsageLog
from .services import rag
from .services.retrieval import Chunk
from .services.usage import usage_summary

API = "/chat/api/chat/"
RAG = "chat.services.rag"


def make_log(question="q", status="ok", latency_ms=100, intent="quick", sources=None, ago=timedelta()):
    log = QueryLog.objects.create(question=question, status=status, latency_ms=latency_ms,
                                  intent=intent, sources=sources or [])
    if ago:
        QueryLog.objects.filter(pk=log.pk).update(created_at=timezone.now() - ago)
    return log


class UsageLoggingApiTests(TestCase):
    def ask(self, message, **extra):
        return self.client.post(API, {"message": message, **extra}, content_type="application/json")

    @patch(f"{RAG}.get_client")
    @patch(f"{RAG}.retrieve_chunks")
    def test_answered_question_is_logged_with_all_fields(self, mock_retrieve, mock_client):
        mock_retrieve.return_value = [Chunk("Take it with food.", "Metformin Guide", 0.9, "https://example.org/m")]
        client = MagicMock()
        client.models.generate_content.return_value.text = "Take it with food [1]."
        mock_client.return_value = client

        response = self.ask("Explain why I take metformin with food.", session_id="abc-123")
        self.assertEqual(response.status_code, 200)

        log = QueryLog.objects.get()
        self.assertEqual(log.question, "Explain why I take metformin with food.")
        self.assertEqual(log.intent, "explain")
        self.assertEqual(log.status, "ok")
        self.assertTrue(log.succeeded)
        self.assertEqual(log.sources, [{"title": "Metformin Guide", "url": "https://example.org/m"}])
        self.assertGreaterEqual(log.latency_ms, 0)
        self.assertIsNotNone(log.created_at)

    @patch(f"{RAG}.retrieve_chunks", return_value=[])
    def test_off_topic_question_is_logged_as_success_with_intent(self, _):
        self.ask("Tell me more about football.")
        log = QueryLog.objects.get()
        self.assertEqual((log.status, log.intent, log.sources), ("off_topic", "details", []))
        self.assertTrue(log.succeeded)

    @patch("chat.views.answer_question", side_effect=RuntimeError("boom"))
    def test_unexpected_failure_is_logged_as_failure(self, _):
        response = self.ask("What is a medication list?")
        self.assertEqual(response.json()["status"], "error")
        log = QueryLog.objects.get()
        self.assertEqual(log.status, "error")
        self.assertFalse(log.succeeded)
        self.assertEqual(log.question, "What is a medication list?")

    def test_invalid_requests_are_not_logged(self):
        self.ask("")
        self.client.post(API, "not json", content_type="application/json")
        self.assertFalse(QueryLog.objects.exists())

    @patch("chat.views.QueryLog.objects.create", side_effect=Exception("db down"))
    @patch("chat.views.answer_question", return_value=rag.ChatResult("Hi", "ok", intent="quick"))
    def test_logging_failure_does_not_break_the_chat(self, *_):
        self.assertEqual(self.ask("Hello?").status_code, 200)

    def test_no_personal_identifiers_are_stored(self):
        fields = {f.name for f in QueryLog._meta.get_fields()}
        self.assertEqual(fields, {"id", "status", "latency_ms", "created_at", "question", "intent", "sources"})

    def test_chat_page_tells_users_questions_are_logged(self):
        response = self.client.get(reverse("chat:page"))
        self.assertContains(response, 'id="log-notice"')
        self.assertContains(response, "Your questions are saved")


class UsageSummaryTests(TestCase):
    def test_counts_today_week_and_chart(self):
        make_log("today a")
        make_log("today b", status="error")
        make_log("three days ago", ago=timedelta(days=3))
        make_log("ten days ago", ago=timedelta(days=10))
        make_log("old", ago=timedelta(days=40))

        summary = usage_summary()
        totals = summary["totals"]
        self.assertEqual(totals["all_time"], 5)
        self.assertEqual(totals["today"], 2)
        self.assertEqual(totals["week"], 3)
        self.assertAlmostEqual(totals["week_success_rate"], 100 * 2 / 3)
        self.assertEqual(len(summary["chart"]), 14)
        self.assertEqual(summary["chart"][-1]["count"], 2)  # today is the last bar
        self.assertEqual(sum(d["count"] for d in summary["chart"]), 4)  # 40-day-old row excluded
        self.assertEqual(summary["chart"][-1]["pct"], 100)

    def test_empty_summary(self):
        summary = usage_summary()
        self.assertEqual(summary["totals"]["week"], 0)
        self.assertIsNone(summary["totals"]["week_success_rate"])
        self.assertTrue(all(d["pct"] == 0 for d in summary["chart"]))


class UsageDjangoAdminTests(TestCase):
    """The same data is also available in Django admin."""
    url = reverse("admin:chat_usagelog_changelist")

    def setUp(self):
        self.admin = User.objects.create_superuser("admin", "a@example.org", "pw")
        self.client.force_login(self.admin)

    def test_requires_staff(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)
        User.objects.create_user("plain", password="pw")
        self.client.login(username="plain", password="pw")
        self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_staff_with_view_permission_can_see_logs(self):
        staff = User.objects.create_user("staff", password="pw", is_staff=True)
        staff.user_permissions.add(Permission.objects.get(codename="view_usagelog"))
        self.client.force_login(staff)
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_page_shows_summary_chart_and_table(self):
        make_log("How do I store insulin?", intent="explain",
                 sources=[{"title": "Insulin Storage", "url": ""}], latency_ms=1234)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Queries today")
        self.assertContains(response, "Queries, last 7 days")
        self.assertContains(response, "Queries per day")
        self.assertContains(response, "How do I store insulin?")
        self.assertContains(response, "Explain with Sources")
        self.assertContains(response, "Insulin Storage")
        self.assertContains(response, "1,234 ms")

    def test_search_by_question(self):
        make_log("missed dose of metformin")
        make_log("storing insulin")
        response = self.client.get(self.url, {"q": "insulin"})
        self.assertContains(response, "storing insulin")
        self.assertNotContains(response, "missed dose of metformin")

    def test_date_range_filter(self):
        make_log("recent question")
        make_log("older question", ago=timedelta(days=20))
        today = timezone.localdate()
        start = (today - timedelta(days=2)).isoformat()
        response = self.client.get(self.url, {"created_at_from": start, "created_at_to": today.isoformat()})
        self.assertContains(response, "recent question")
        self.assertNotContains(response, "older question")

        old_day = (today - timedelta(days=20)).isoformat()
        response = self.client.get(self.url, {"created_at_from": old_day, "created_at_to": old_day})
        self.assertContains(response, "older question")
        self.assertNotContains(response, "recent question")

    def test_invalid_dates_are_ignored(self):
        make_log("any question")
        response = self.client.get(self.url, {"created_at_from": "not-a-date"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "any question")

    def test_outcome_and_intent_filters(self):
        make_log("good one", status="ok", intent="quick")
        make_log("broken one", status="error", intent="details")
        response = self.client.get(self.url, {"outcome": "error"})
        self.assertContains(response, "broken one")
        self.assertNotContains(response, "good one")
        response = self.client.get(self.url, {"intent": "quick"})
        self.assertContains(response, "good one")
        self.assertNotContains(response, "broken one")

    def test_sortable_by_response_time(self):
        make_log("slow question", latency_ms=9000)
        make_log("fast question", latency_ms=10)
        col = str(self.client.get(self.url).context["cl"].list_display.index("latency_ms_display"))
        response = self.client.get(self.url, {"o": col})
        body = response.content.decode()
        self.assertLess(body.index("fast question"), body.index("slow question"))
        response = self.client.get(self.url, {"o": "-" + col})
        body = response.content.decode()
        self.assertLess(body.index("slow question"), body.index("fast question"))

    def test_paging(self):
        for i in range(55):
            make_log(f"question number {i:02d}")
        page1 = self.client.get(self.url)
        self.assertEqual(len(page1.context["cl"].result_list), 50)
        page2 = self.client.get(self.url, {"p": "2"})
        self.assertEqual(len(page2.context["cl"].result_list), 5)

    def test_logs_are_read_only(self):
        log = make_log("do not change me")
        self.assertEqual(self.client.get(reverse("admin:chat_usagelog_add")).status_code, 403)
        detail = self.client.get(reverse("admin:chat_usagelog_change", args=[log.pk]))
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, "do not change me")
        self.assertNotContains(detail, 'name="_save"')

    def test_logs_persist_in_database(self):
        make_log("persisted question")
        # A fresh query (as after a restart) reads the row back from the database.
        self.assertTrue(UsageLog.objects.filter(question="persisted question").exists())


class UsageSitePageTests(TestCase):
    """The Usage logs page on the website itself (/chat/usage/)."""
    url = "/chat/usage/"

    def setUp(self):
        self.admin = User.objects.create_superuser("admin", "a@example.org", "pw")
        self.client.force_login(self.admin)

    def rows(self, response):
        return [row["log"].question for row in response.context["rows"]]

    def test_url_name(self):
        self.assertEqual(reverse("chat:usage"), self.url)

    def test_signed_out_users_go_to_the_site_login(self):
        self.client.logout()
        response = self.client.get(self.url)
        self.assertRedirects(response, f"{reverse('home:login')}?next={self.url}", fetch_redirect_response=False)

    def test_regular_users_and_staff_without_permission_are_refused(self):
        self.client.force_login(User.objects.create_user("patient", password="pw"))
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.client.force_login(User.objects.create_user("staff", password="pw", is_staff=True))
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_staff_with_permission_can_view(self):
        staff = User.objects.create_user("staff", password="pw", is_staff=True)
        staff.user_permissions.add(Permission.objects.get(codename="view_usagelog"))
        self.client.force_login(staff)
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_page_uses_site_layout_and_shows_summary_chart_and_table(self):
        make_log("How do I store insulin?", intent="explain", latency_ms=1234,
                 sources=[{"title": "Insulin Storage", "url": "https://example.org/insulin"}])
        make_log("broken", status="error", ago=timedelta(days=2))
        response = self.client.get(self.url)
        self.assertTemplateUsed(response, "home/base.html")
        self.assertTemplateNotUsed(response, "admin/base.html")
        for text in ["Queries today", "Queries, last 7 days", "Queries per day", "Show as table",
                     "How do I store insulin?", "Explain with Sources", "1234 ms", "Success", "Failure"]:
            self.assertContains(response, text)
        self.assertContains(response, 'href="https://example.org/insulin"')
        totals = response.context["usage"]["totals"]
        self.assertEqual((totals["today"], totals["week"]), (1, 2))

    def test_nav_link_only_for_administrators(self):
        self.assertContains(self.client.get(reverse("home:settings")), 'href="/chat/usage/"')
        self.client.force_login(User.objects.create_user("patient", password="pw"))
        self.assertNotContains(self.client.get(reverse("home:settings")), 'href="/chat/usage/"')

    def test_empty_state(self):
        self.assertContains(self.client.get(self.url), "No queries have been logged yet")

    def test_search(self):
        make_log("missed dose of metformin")
        make_log("storing insulin")
        response = self.client.get(self.url, {"q": "INSULIN"})
        self.assertEqual(self.rows(response), ["storing insulin"])

    def test_date_filter(self):
        make_log("recent question")
        make_log("older question", ago=timedelta(days=20))
        today = timezone.localdate()
        response = self.client.get(self.url, {"from": (today - timedelta(days=2)).isoformat(),
                                              "to": today.isoformat()})
        self.assertEqual(self.rows(response), ["recent question"])
        old = (today - timedelta(days=20)).isoformat()
        self.assertEqual(self.rows(self.client.get(self.url, {"from": old, "to": old})), ["older question"])
        self.assertEqual(self.rows(self.client.get(self.url, {"to": old})), ["older question"])

    def test_invalid_filter_values_are_ignored(self):
        make_log("any question")
        response = self.client.get(self.url, {"from": "nope", "outcome": "x", "intent": "y",
                                              "sort": "bogus", "page": "999"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.rows(response), ["any question"])

    def test_outcome_and_intent_filters(self):
        make_log("good one", status="ok", intent="quick")
        make_log("off one", status="off_topic", intent="explain")
        make_log("broken one", status="error", intent="details")
        self.assertEqual(self.rows(self.client.get(self.url, {"outcome": "error"})), ["broken one"])
        self.assertCountEqual(self.rows(self.client.get(self.url, {"outcome": "success"})), ["good one", "off one"])
        self.assertEqual(self.rows(self.client.get(self.url, {"intent": "quick"})), ["good one"])

    def test_default_order_is_newest_first(self):
        make_log("old", ago=timedelta(hours=2))
        make_log("new")
        self.assertEqual(self.rows(self.client.get(self.url)), ["new", "old"])

    def test_sort_by_columns(self):
        make_log("slow", latency_ms=9000)
        make_log("fast", latency_ms=10)
        self.assertEqual(self.rows(self.client.get(self.url, {"sort": "response"})), ["fast", "slow"])
        self.assertEqual(self.rows(self.client.get(self.url, {"sort": "-response"})), ["slow", "fast"])
        self.assertEqual(self.rows(self.client.get(self.url, {"sort": "question"})), ["fast", "slow"])
        response = self.client.get(self.url, {"sort": "response"})
        self.assertContains(response, 'aria-sort="ascending"')
        # Clicking the active column flips it.
        column = next(c for c in response.context["columns"] if c["active"])
        self.assertIn("sort=-response", column["url"])

    def test_paging_keeps_filters_and_sort(self):
        for i in range(30):
            make_log(f"insulin question {i:02d}", latency_ms=i)
        make_log("other")
        page1 = self.client.get(self.url, {"q": "insulin", "sort": "response"})
        self.assertEqual(len(page1.context["rows"]), 25)
        self.assertContains(page1, "Showing 1–25 of 30 queries matching your filters")
        self.assertContains(page1, "Page 1 of 2")
        self.assertContains(page1, "q=insulin&amp;sort=response&amp;page=2")
        page2 = self.client.get(self.url, {"q": "insulin", "sort": "response", "page": "2"})
        self.assertEqual(self.rows(page2), [f"insulin question {i:02d}" for i in range(25, 30)])

    def test_post_not_allowed(self):
        self.assertEqual(self.client.post(self.url).status_code, 405)
