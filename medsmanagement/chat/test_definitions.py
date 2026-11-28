
import json
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse


@override_settings(GEMINI_CHAT_MODEL="test-model")
class TermDefinitionTests(TestCase):

    def setUp(self):
        self.url = reverse("chat:define")

    def post_term(self, term, context=""):
        return self.client.post(
            self.url,
            data=json.dumps({
                "term": term,
                "context": context,
            }),
            content_type="application/json",
        )

    @patch("chat.definition_views.get_client")
    def test_returns_plain_language_definition(self, mock_client):
        mock_client.return_value.models.generate_content.return_value.text = (
            "Taking several different medicines at the same time."
        )

        response = self.post_term(
            "Polypharmacy",
            "Polypharmacy can increase medication risks."
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["term"],
            "Polypharmacy"
        )
        self.assertIn(
            "several different medicines",
            response.json()["definition"]
        )

    def test_empty_term_is_rejected(self):
        response = self.post_term("")
        self.assertEqual(response.status_code, 400)

    def test_long_term_is_rejected(self):
        response = self.post_term("medicine " * 30)
        self.assertEqual(response.status_code, 400)

    def test_invalid_json_is_rejected(self):
        response = self.client.post(
            self.url,
            data="{invalid",
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_get_request_is_rejected(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 405)

    @patch("chat.definition_views.get_client")
    def test_gemini_error_is_handled(self, mock_client):
        mock_client.return_value.models.generate_content.side_effect = (
            RuntimeError("API unavailable")
        )

        response = self.post_term("Polypharmacy")

        self.assertEqual(response.status_code, 503)
        self.assertIn("error", response.json())
