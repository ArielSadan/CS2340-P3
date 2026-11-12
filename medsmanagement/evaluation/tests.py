
from types import SimpleNamespace

from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from .runner import score_response, contains_fact, load_cases


class EvaluationScoringTests(SimpleTestCase):

    def setUp(self):
        self.case = {
            "id": "Q01",
            "question": "Test question",
            "expected_status": "ok",
            "expected_answer": "A fact from the document",
            "required_facts": [["blood sugar", "glucose"]],
            "expected_sources": ["Medication Guide"],
        }

    def test_correct_answer_passes(self):
        response = SimpleNamespace(
            answer="This helps manage blood sugar.",
            status="ok",
            sources=[{"title": "Medication Guide", "url": ""}],
            latency_ms=100,
        )

        score = score_response(self.case, response)
        self.assertTrue(score["passed"])

    def test_missing_source_fails(self):
        response = SimpleNamespace(
            answer="This helps manage blood sugar.",
            status="ok",
            sources=[{"title": "Different Guide", "url": ""}],
            latency_ms=100,
        )

        score = score_response(self.case, response)
        self.assertFalse(score["passed"])

    def test_missing_fact_fails(self):
        response = SimpleNamespace(
            answer="This is a medication.",
            status="ok",
            sources=[{"title": "Medication Guide", "url": ""}],
            latency_ms=100,
        )

        score = score_response(self.case, response)
        self.assertFalse(score["passed"])

    def test_correct_decline_passes(self):
        case = {
            "expected_status": "off_topic",
            "expected_sources": [],
        }

        response = SimpleNamespace(
            answer="I cannot answer that.",
            status="off_topic",
            sources=[],
            latency_ms=20,
        )

        score = score_response(case, response)
        self.assertTrue(score["passed"])

    def test_error_does_not_count_as_decline(self):
        case = {
            "expected_status": "off_topic",
            "expected_sources": [],
        }

        response = SimpleNamespace(
            answer="Service unavailable",
            status="error",
            sources=[],
            latency_ms=20,
        )

        self.assertFalse(score_response(case, response)["passed"])

    def test_fact_matching_ignores_case(self):
        self.assertTrue(
            contains_fact("BLOOD SUGAR", "blood sugar")
        )

    def test_incomplete_test_set_is_rejected(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path

        with TemporaryDirectory() as folder:
            file = Path(folder) / "cases.json"
            file.write_text("[]", encoding="utf-8")

            with self.assertRaises(ValueError):
                load_cases(file)


class EvaluationPageTests(TestCase):

    def test_dashboard_requires_staff_login(self):
        response = self.client.get(
            reverse("evaluation:dashboard")
        )
        self.assertEqual(response.status_code, 302)
