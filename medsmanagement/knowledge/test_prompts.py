from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from chat.models import PromptStyle
from chat.services import grounding, rag
from chat.services.retrieval import Chunk


class DeveloperPromptTests(TestCase):
    def setUp(self):
        self.url = reverse("knowledge:developer")
        self.staff = get_user_model().objects.create_user("developer", is_staff=True)
        self.client.force_login(self.staff)

    def save_style(self, code, phrases, instructions):
        return self.client.post(self.url, {
            "action": "save_prompt", "style": code,
            f"{code}-phrases": phrases, f"{code}-instructions": instructions,
        })

    def test_page_shows_the_three_editable_styles_and_defaults(self):
        response = self.client.get(self.url)
        for code, title in rag.STYLE_LABELS.items():
            self.assertContains(response, title)
            self.assertContains(response, f'name="{code}-instructions"')
            self.assertContains(response, f'name="{code}-phrases"')
        self.assertEqual(PromptStyle.objects.count(), 0)

    @patch("chat.services.rag.get_client")
    def test_each_style_can_be_saved_without_gemini(self, mock_client):
        for code in rag.STYLE_LABELS:
            with self.subTest(style=code):
                response = self.save_style(code, " custom phrase ", "Use two short paragraphs.")
                self.assertRedirects(response, self.url + "#prompt-settings")
                saved = PromptStyle.objects.get(code=code)
                self.assertEqual(saved.phrases, "custom phrase")
                self.assertEqual(saved.instructions, "Use two short paragraphs.")
        mock_client.assert_not_called()

    @patch("chat.services.rag.get_client")
    @patch("chat.services.rag.retrieve_chunks")
    def test_saved_phrase_and_instructions_apply_to_the_next_chat_question(self, mock_retrieve, mock_client):
        question = "Show the bigger picture of medication lists."
        self.assertEqual(rag.detect_intent(question), "quick")
        self.save_style("details", "show the bigger picture", "Use three short paragraphs about the excerpts.")
        mock_retrieve.return_value = [Chunk("Keep a medication list.", "Medication Guide", 0.9)]
        client = MagicMock()
        client.models.generate_content.return_value.text = "Keep a medication list [1]."
        mock_client.return_value = client
        response = self.client.post(reverse("chat:api"), {"message": question},
                                    content_type="application/json")
        data = response.json()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["response_style"], "Going Into Details")
        self.assertEqual(data["sources"], [{"title": "Medication Guide", "url": ""}])
        sent = client.models.generate_content.call_args.kwargs
        self.assertIn("Use three short paragraphs about the excerpts.", sent["contents"])
        self.assertIn("reply with exactly NO_ANSWER and nothing else", sent["contents"])
        self.assertIn(grounding.GROUNDING_RULES, sent["config"].system_instruction)

    def test_saving_replaces_old_phrases_and_normalizes_new_ones(self):
        self.save_style("details", " WALK me THROUGH! \nwalk me through\n", "Give a fuller explanation.")
        self.assertEqual(PromptStyle.objects.get(code="details").phrases, "walk me through")
        self.assertEqual(rag.detect_intent("WALK ME THROUGH medication lists"), "details")
        self.assertEqual(rag.detect_intent("Explain medication lists in detail."), "explain")

    def test_invalid_instructions_do_not_overwrite_saved_settings(self):
        self.save_style("quick", "briefly", "Use one short sentence.")
        response = self.save_style("quick", "new phrase", "   ")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "This field is required.")
        saved = PromptStyle.objects.get(code="quick")
        self.assertEqual(saved.instructions, "Use one short sentence.")
        self.assertEqual(saved.phrases, "briefly")

    def test_blank_phrases_leave_quick_answer_as_the_fallback(self):
        self.save_style("quick", "", "Answer directly.")
        self.assertEqual(rag.detect_intent("What is a medication list?"), "quick")

    def test_unknown_styles_cannot_be_saved(self):
        self.assertEqual(self.save_style("unknown", "test", "Test instructions.").status_code, 400)
        self.assertEqual(PromptStyle.objects.count(), 0)

    def test_visitors_and_nonstaff_users_cannot_view_or_change_settings(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)
        self.assertEqual(self.save_style("quick", "briefly", "Short answer.").status_code, 302)
        user = get_user_model().objects.create_user("patient")
        self.client.force_login(user)
        self.assertEqual(self.client.get(self.url).status_code, 302)
        self.assertEqual(self.save_style("quick", "briefly", "Short answer.").status_code, 302)
        self.assertEqual(PromptStyle.objects.count(), 0)
