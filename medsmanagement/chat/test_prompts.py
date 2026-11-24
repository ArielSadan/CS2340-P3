from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings

from .services import grounding, rag
from .services.retrieval import Chunk


QUESTIONS = {
    "quick": "What is a medication list?",
    "explain": "Explain why a medication list is helpful.",
    "details": "Explain medication lists in detail.",
}


class PromptSelectionTests(TestCase):
    def test_unmatched_questions_use_quick_answer(self):
        for question in ["What is a medication list?", "What does unexplained mean?", ""]:
            with self.subTest(question=question):
                self.assertEqual(rag.detect_intent(question), "quick")

    def test_phrases_select_the_response_style(self):
        cases = [
            ("A quick answer about medication lists, please.", "quick"),
            ("Briefly describe a medication list.", "quick"),
            ("Keep it short: what is a medication list?", "quick"),
            ("Explain medication lists.", "explain"),
            ("Why are medication lists useful?", "explain"),
            ("How does a medication list help?", "explain"),
            ("How do medication lists help?", "explain"),
            ("Help me understand medication lists.", "explain"),
            ("Describe medication lists with sources.", "explain"),
            ("Tell me more about medication lists.", "details"),
            ("Break it down: how does a medication list help?", "details"),
            ("Give a detailed explanation of medication lists.", "details"),
            ("Go into details about medication lists.", "details"),
            ("Explain medication lists step-by-step.", "details"),
            ("Summarize why medication lists help.", "quick"),
            ("Give me a brief summary of medication lists.", "quick"),
            ("What are the key points about medication lists?", "quick"),
            ("Explain medication lists, but get to the point.", "quick"),
            ("TL;DR: why should I keep a medication list?", "quick"),
            ("Clarify the medication list instructions.", "explain"),
            ("Describe medication lists in plain English.", "explain"),
            ("How can a medication list help?", "explain"),
            ("According to the article, when should lists be updated?", "explain"),
            ("Where does it say to update the list?", "explain"),
            ("Explain medication lists in depth.", "details"),
            ("Walk me through keeping a medication list.", "details"),
            ("Give me examples of medication list entries.", "details"),
            ("Elaborate on why the list matters.", "details"),
            ("Can you provide more information about medication lists?", "details"),
        ]
        for question, expected in cases:
            with self.subTest(question=question):
                self.assertEqual(rag.detect_intent(question), expected)

    def test_matching_ignores_case_spacing_and_punctuation(self):
        self.assertEqual(rag.detect_intent("  EXPLAIN: why   lists help?  "), "explain")
        self.assertEqual(rag.detect_intent("Tell me MORE! About medication lists."), "details")

    def test_explicit_length_requests_take_priority_over_explanations(self):
        self.assertEqual(rag.detect_intent("Explain medication lists in detail."), "details")
        self.assertEqual(rag.detect_intent("Briefly explain why lists help."), "quick")

    def test_every_style_keeps_the_question_context_and_grounding_rules(self):
        chunks = [Chunk("Keep an updated medication list.", "Medication Guide", 0.9)]
        for intent, question in QUESTIONS.items():
            with self.subTest(intent=intent):
                prompt = rag.build_prompt(question, chunks)
                self.assertIn(rag.PROMPT_TEMPLATES[intent], prompt)
                self.assertIn("[1] Source: Medication Guide", prompt)
                self.assertIn(chunks[0].text, prompt)
                self.assertIn("Question: " + question, prompt)
                self.assertIn("Use ONLY the provided context", prompt)
                self.assertIn("Cite each factual sentence", prompt)
                self.assertIn("reply with exactly NO_ANSWER and nothing else", prompt)


@override_settings(RAG_MIN_SIMILARITY=0.5, RAG_TOP_K=3,
                   GEMINI_CHAT_MODEL="test-model", GROUNDING_REQUIRE_CITATIONS=True)
class DynamicPromptRagTests(TestCase):
    @patch("chat.services.rag.get_client")
    @patch("chat.services.rag.retrieve_chunks")
    def test_each_style_reaches_gemini_and_keeps_sources(self, mock_retrieve, mock_client):
        mock_retrieve.return_value = [
            Chunk("Keep an updated medication list.", "Medication Guide", 0.9,
                  "https://example.com/guide"),
            Chunk("Unrelated gardening advice.", "Gardening", 0.1),
        ]
        for intent, question in QUESTIONS.items():
            with self.subTest(intent=intent):
                client = MagicMock()
                client.models.generate_content.return_value.text = "Keep an updated medication list [1]."
                mock_client.return_value = client
                result = rag.answer_question(question)
                client.models.generate_content.assert_called_once()
                sent = client.models.generate_content.call_args.kwargs
                self.assertIn(rag.PROMPT_TEMPLATES[intent], sent["contents"])
                self.assertNotIn("Gardening", sent["contents"])
                self.assertIn(grounding.GROUNDING_RULES, sent["config"].system_instruction)
                self.assertEqual(sent["model"], "test-model")
                self.assertEqual(result.status, "ok")
                self.assertEqual(result.response_style, rag.STYLE_LABELS[intent])
                self.assertEqual(result.sources, [
                    {"title": "Medication Guide", "url": "https://example.com/guide"},
                ])
                self.assertTrue(result.answer.endswith(grounding.DISCLAIMER))

    @patch("chat.services.rag.get_client")
    @patch("chat.services.rag.retrieve_chunks")
    def test_every_style_still_declines_missing_or_uncited_answers(self, mock_retrieve, mock_client):
        mock_retrieve.return_value = [Chunk("Some context.", "Medication Guide", 0.9)]
        for question in QUESTIONS.values():
            for reply in ["NO_ANSWER", "An answer without any citations."]:
                with self.subTest(question=question, reply=reply):
                    client = MagicMock()
                    client.models.generate_content.return_value.text = reply
                    mock_client.return_value = client
                    result = rag.answer_question(question)
                    self.assertEqual(result.status, "off_topic")
                    self.assertEqual(result.sources, [])

    @patch("chat.services.rag.get_client")
    @patch("chat.services.rag.retrieve_chunks", return_value=[])
    def test_every_style_skips_gemini_when_the_knowledge_base_is_empty(self, _, mock_client):
        for question in QUESTIONS.values():
            with self.subTest(question=question):
                self.assertEqual(rag.answer_question(question).reason, "no_sources")
        mock_client.assert_not_called()


class PromptLabelApiTests(TestCase):
    @patch("chat.views.QueryLog.objects.create")
    @patch("chat.services.rag.get_client")
    @patch("chat.services.rag.retrieve_chunks")
    def test_api_returns_the_style_used_in_the_prompt(self, mock_retrieve, mock_client, _):
        mock_retrieve.return_value = [Chunk("Use a medication list.", "Guide", 0.9)]
        client = MagicMock()
        client.models.generate_content.return_value.text = "Use a medication list [1]."
        mock_client.return_value = client
        for intent, question in QUESTIONS.items():
            with self.subTest(intent=intent):
                response = self.client.post("/chat/api/chat/", {"message": question},
                                            content_type="application/json")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["response_style"], rag.STYLE_LABELS[intent])
                prompt = client.models.generate_content.call_args.kwargs["contents"]
                self.assertIn(response.json()["response_style"], prompt)

    @patch("chat.views.QueryLog.objects.create")
    @patch("chat.services.rag.retrieve_chunks", return_value=[])
    def test_declined_answers_have_no_style_label(self, *_):
        response = self.client.post("/chat/api/chat/", {"message": "Explain this in detail."},
                                    content_type="application/json")
        self.assertEqual(response.json()["status"], "off_topic")
        self.assertEqual(response.json()["response_style"], "")

    @patch("chat.views.QueryLog.objects.create")
    @patch("chat.views.answer_question", return_value=rag.ChatResult("Try again.", "error"))
    def test_errors_have_no_style_label(self, *_):
        response = self.client.post("/chat/api/chat/", {"message": "Explain this."},
                                    content_type="application/json")
        self.assertEqual(response.json()["status"], "error")
        self.assertEqual(response.json()["response_style"], "")
