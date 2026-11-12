import json
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings
from django.urls import reverse

from .services.retrieval import Chunk, RetrievalError

from django.test import SimpleTestCase
 
from .services import grounding
from .services.rag import answer_question

RAG = "chat.services.rag"


@override_settings(RAG_MIN_SIMILARITY=0.5, RAG_TOP_K=3, GEMINI_CHAT_MODEL="test-model")
class ChatApiTests(TestCase):
    def ask(self, message):
        return self.client.post(
            reverse("chat:api"),
            data=json.dumps({"session_id": "s1", "message": message}),
            content_type="application/json",
        )

    def fake_llm(self, text=None, error=None):
        client = MagicMock()
        if error:
            client.models.generate_content.side_effect = error
        else:
            client.models.generate_content.return_value = MagicMock(text=text)
        return client

    def test_page_loads_with_send_button(self):
        res = self.client.get(reverse("chat:page"))
        self.assertContains(res, 'id="send-btn"')

    @patch(f"{RAG}.get_client")
    @patch(f"{RAG}.retrieve_chunks")
    def test_on_topic_question_returns_answer(self, mock_retrieve, mock_client):
        mock_retrieve.return_value = [Chunk("Banks never ask for your PIN.", "fraud_guide.pdf", 0.82)]
        mock_client.return_value = self.fake_llm("Your bank will never ask for your PIN [1].")
        data = self.ask("Will my bank ask for my PIN?").json()
        self.assertEqual(data["status"], "ok")
        self.assertIn("PIN", data["answer"])
        self.assertEqual(data["sources"], [{"title": "fraud_guide.pdf", "url": ""}],)

    @patch(f"{RAG}.get_client")
    @patch(f"{RAG}.retrieve_chunks")
    def test_low_similarity_is_off_topic_and_skips_llm(self, mock_retrieve, mock_client):
        mock_retrieve.return_value = [Chunk("unrelated", "x.pdf", 0.2)]
        data = self.ask("Who won the football game?").json()
        self.assertEqual(data["status"], "off_topic")
        mock_client.assert_not_called()

    @patch(f"{RAG}.get_client")
    @patch(f"{RAG}.retrieve_chunks")
    def test_llm_no_answer_token_is_off_topic(self, mock_retrieve, mock_client):
        mock_retrieve.return_value = [Chunk("some text", "a.pdf", 0.7)]
        mock_client.return_value = self.fake_llm("NO_ANSWER")
        self.assertEqual(self.ask("Something vague").json()["status"], "off_topic")

    @patch(f"{RAG}.retrieve_chunks", side_effect=RetrievalError("store down"))
    def test_retrieval_failure_gives_friendly_error(self, _):
        data = self.ask("Anything").json()
        self.assertEqual(data["status"], "error")
        self.assertNotIn("store down", data["answer"])

    @patch(f"{RAG}.get_client")
    @patch(f"{RAG}.retrieve_chunks")
    def test_llm_timeout_gives_friendly_error(self, mock_retrieve, mock_client):
        mock_retrieve.return_value = [Chunk("text", "a.pdf", 0.9)]
        mock_client.return_value = self.fake_llm(error=TimeoutError("timed out"))
        data = self.ask("Anything").json()
        self.assertEqual(data["status"], "error")
        self.assertNotIn("timed out", data["answer"])

    def test_empty_question_rejected(self):
        res = self.ask("   ")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["answer"], "Please type a question first.")

    @patch(f"{RAG}.get_client")
    @patch(f"{RAG}.retrieve_chunks")
    def test_sources_include_titles_urls_and_remove_duplicates(
        self, mock_retrieve, mock_client
    ):
        url = "https://medlineplus.gov/druginformation.html"

        mock_retrieve.return_value = [
            Chunk("First passage.", "Medication Guide", 0.9, url),
            Chunk("Second passage.", "Medication Guide", 0.8, url),
            Chunk("Another passage.", "Facility Handbook", 0.7, ""),
        ]
        mock_client.return_value = self.fake_llm(
            "Here is an explanation from the provided documents."
        )

        response = self.ask("Explain the medication information.")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["sources"],
            [
                {"title": "Medication Guide", "url": url},
                {"title": "Facility Handbook", "url": ""},
            ],
        )

    @patch(f"{RAG}.get_client")
    @patch(f"{RAG}.retrieve_chunks")
    def test_below_threshold_sources_are_excluded(
        self, mock_retrieve, mock_client
    ):
        mock_retrieve.return_value = [
            Chunk("Relevant passage.", "Relevant Guide", 0.9, ""),
            Chunk("Unrelated passage.", "Unrelated Guide", 0.2, ""),
        ]
        mock_client.return_value = self.fake_llm(
            "An answer from the relevant guide."
        )

        data = self.ask("Explain my medication instructions.").json()

        self.assertEqual(data["status"], "ok")
        self.assertEqual(
            data["sources"],
            [{"title": "Relevant Guide", "url": ""}],
        )

    @patch(f"{RAG}.get_client")
    @patch(f"{RAG}.retrieve_chunks")
    def test_sources_are_empty_when_llm_cannot_answer(
        self, mock_retrieve, mock_client
    ):
        mock_retrieve.return_value = [
            Chunk("Incomplete context.", "Medication Guide", 0.9, "")
        ]
        mock_client.return_value = self.fake_llm("NO_ANSWER")

        data = self.ask("A question not answered by the document.").json()

        self.assertEqual(data["status"], "off_topic")
        self.assertEqual(data["sources"], [])

    @patch(f"{RAG}.get_client")
    @patch(f"{RAG}.retrieve_chunks")
    def test_sources_are_empty_when_generation_fails(
        self, mock_retrieve, mock_client
    ):
        mock_retrieve.return_value = [
            Chunk("Some context.", "Medication Guide", 0.9, "")
        ]
        mock_client.return_value = self.fake_llm(
            error=TimeoutError("timed out")
        )

        data = self.ask("Explain this information.").json()

        self.assertEqual(data["status"], "error")
        self.assertEqual(data["sources"], [])

# SQLite retrieval

from .models import DocumentChunk
from .services import retrieval

RET = "chat.services.retrieval"


def fake_embed(text):
    """Tiny 3-d embeddings so tests are deterministic."""
    t = text.lower()
    return [1.0 if "scam" in t else 0.0, 1.0 if "pill" in t else 0.0, 0.1]


class SqliteRetrievalTests(TestCase):
    def setUp(self):
        retrieval._index["version"] = None

    @patch(f"{RET}.embed_documents", side_effect=lambda texts: [fake_embed(t) for t in texts])
    def add(self, items, _):
        return retrieval.add_chunks(items)

    @patch(f"{RET}.embed_query", side_effect=fake_embed)
    def test_returns_most_similar_first(self, _):
        self.add([("scams.pdf", "How to spot a scam call"), ("meds.pdf", "Take each pill with water")])
        results = retrieval.retrieve_chunks("Is this a scam?", k=2)
        self.assertEqual(results[0].source, "scams.pdf")
        self.assertGreater(results[0].score, results[1].score)

    @patch(f"{RET}.embed_query", side_effect=fake_embed)
    def test_empty_knowledge_base_returns_nothing(self, _):
        self.assertEqual(retrieval.retrieve_chunks("Is this a scam?", k=3), [])

    @patch(f"{RET}.embed_query", side_effect=fake_embed)
    def test_cache_picks_up_cleared_and_reloaded_data(self, _):
        self.add([("scams.pdf", "scam advice")])
        self.assertEqual(len(retrieval.retrieve_chunks("scam", k=5)), 1)
        DocumentChunk.objects.all().delete()  # admin "clear" (story #11)
        self.assertEqual(retrieval.retrieve_chunks("scam", k=5), [])
        self.add([("new.pdf", "new scam advice")])  # admin "reload"
        self.assertEqual(retrieval.retrieve_chunks("scam", k=5)[0].source, "new.pdf")

    @patch(f"{RET}.embed_query", return_value=[1.0, 0.0])  # wrong dimension
    def test_dimension_mismatch_raises_retrieval_error(self, _):
        self.add([("scams.pdf", "scam advice")])
        with self.assertRaises(RetrievalError):
            retrieval.retrieve_chunks("scam", k=1)

class GroundingHelperTests(SimpleTestCase):
    """The grounding helpers on their own (no database, no Gemini)."""
 
    def test_rules_added_once(self):
        once = grounding.with_grounding_rules("Be kind.")
        self.assertTrue(once.startswith("Be kind."))
        self.assertIn(grounding.GROUNDING_RULES, once)
        self.assertEqual(grounding.with_grounding_rules(once), once)
 
    def test_rules_require_context_only_and_no_answer(self):
        self.assertIn("ONLY the numbered context excerpts", grounding.GROUNDING_RULES)
        self.assertIn(f"reply with exactly {grounding.NO_ANSWER_TOKEN}", grounding.GROUNDING_RULES)
 
    def test_context_is_numbered(self):
        ctx = grounding.format_context([Chunk("Take with food.", "Metformin Guide", 0.9),
                                        Chunk("May cause cough.", "Lisinopril", 0.8)])
        self.assertEqual(ctx, "[1] Source: Metformin Guide\nTake with food.\n\n"
                              "[2] Source: Lisinopril\nMay cause cough.")
 
    def test_not_in_sources_message(self):
        msg = grounding.not_in_sources_message("managing your medications", "What if I miss a dose?")
        self.assertTrue(msg.startswith(grounding.NOT_IN_SOURCES))
        self.assertIn("What if I miss a dose?", msg)
 
    def test_disclaimer(self):
        self.assertEqual(grounding.add_disclaimer("Answer.  "), "Answer.\n\n" + grounding.DISCLAIMER)
 
    def test_cited_answer_passes_and_markers_are_removed(self):
        check = grounding.check_answer("Take it with meals [1]. It can upset your stomach [2].", 2)
        self.assertTrue(check.ok)
        self.assertEqual(check.text, "Take it with meals. It can upset your stomach.")
        self.assertEqual(check.cited, [1, 2])
 
    def test_keep_markers(self):
        check = grounding.check_answer("Take it with meals [1].", 2, keep_markers=True)
        self.assertEqual(check.text, "Take it with meals [1].")
 
    def test_no_answer_anywhere_is_declined(self):
        for reply in ["NO_ANSWER", "no_answer.", "The context has nothing on that. NO_ANSWER",
                      "I don’t have that information in my sources.", "   "]:
            with self.subTest(reply=reply):
                check = grounding.check_answer(reply, 3)
                self.assertFalse(check.ok)
                self.assertEqual(check.reason, "declined")
 
    def test_uncited_answer_is_rejected(self):
        check = grounding.check_answer("Metformin is used for diabetes.", 3)
        self.assertFalse(check.ok)
        self.assertEqual(check.reason, "no_valid_citation")
 
    def test_uncited_answer_allowed_when_citations_not_required(self):
        self.assertTrue(grounding.check_answer("Metformin is used for diabetes.", 3,
                                               require_citations=False).ok)
 
    def test_made_up_citation_numbers_are_dropped(self):
        check = grounding.check_answer("It lowers sugar [1, 7]. Also this [9].", 2)
        self.assertTrue(check.ok)
        self.assertEqual(check.cited, [1])
        self.assertNotIn("7", check.text)
        self.assertNotIn("[9]", check.text)
 
    def test_only_made_up_citations_is_rejected(self):
        self.assertEqual(grounding.check_answer("It lowers sugar [8].", 2).reason, "no_valid_citation")
 
    def test_cited_order_is_first_use(self):
        self.assertEqual(grounding.check_answer("A [3]. B [1]. C [3].", 3).cited, [3, 1])
 
 
def grounded_llm(text):
    client = MagicMock()
    client.models.generate_content.return_value = MagicMock(text=text)
    return client
 
 
@override_settings(RAG_MIN_SIMILARITY=0.5, RAG_TOP_K=3, GEMINI_CHAT_MODEL="test-model",
                   GROUNDING_REQUIRE_CITATIONS=True)
class GroundingApiTests(TestCase):
    """Grounding through the chat API and answer_question(), with Gemini mocked."""
 
    CHUNKS = [
        Chunk("Take metformin with meals to avoid an upset stomach.", "Metformin Guide", 0.81),
        Chunk("Lisinopril can cause a dry cough.", "Lisinopril Facts", 0.66),
        Chunk("Unrelated text about gardening.", "Garden Tips", 0.21),
    ]
 
    def ask(self, message):
        return self.client.post(
            reverse("chat:api"),
            data=json.dumps({"session_id": "s1", "message": message}),
            content_type="application/json",
        ).json()
 
    @patch(f"{RAG}.get_client")
    @patch(f"{RAG}.retrieve_chunks")
    def test_grounded_answer_has_disclaimer_and_cited_sources_only(self, mock_retrieve, mock_client):
        mock_retrieve.return_value = self.CHUNKS
        mock_client.return_value = grounded_llm("Take it with meals [1].")
        data = self.ask("How do I take metformin?")
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["answer"], "Take it with meals.\n\n" + grounding.DISCLAIMER)
        self.assertEqual(data["sources"], ["Metformin Guide"])  # [2] wasn't cited
 
    @patch(f"{RAG}.get_client")
    @patch(f"{RAG}.retrieve_chunks")
    def test_prompt_has_rules_and_only_relevant_numbered_excerpts(self, mock_retrieve, mock_client):
        mock_retrieve.return_value = self.CHUNKS
        client = grounded_llm("Take it with meals [1].")
        mock_client.return_value = client
        self.ask("How do I take metformin?")
        kwargs = client.models.generate_content.call_args.kwargs
        self.assertIn(grounding.GROUNDING_RULES, kwargs["config"].system_instruction)
        self.assertIn("[1] Source: Metformin Guide", kwargs["contents"])
        self.assertIn("[2] Source: Lisinopril Facts", kwargs["contents"])
        self.assertNotIn("Garden Tips", kwargs["contents"])  # below the threshold
 
    @patch(f"{RAG}.get_client")
    @patch(f"{RAG}.retrieve_chunks")
    def test_below_threshold_says_not_in_sources_without_calling_gemini(self, mock_retrieve, mock_client):
        mock_retrieve.return_value = [Chunk("Gardening.", "Garden Tips", 0.2)]
        data = self.ask("Who won the World Cup?")
        self.assertEqual(data["status"], "off_topic")
        self.assertTrue(data["answer"].startswith(grounding.NOT_IN_SOURCES))
        mock_client.assert_not_called()
 
    @patch(f"{RAG}.retrieve_chunks", return_value=[])
    def test_empty_knowledge_base_says_not_in_sources(self, _):
        result = answer_question("What is metformin?")
        self.assertEqual(result.reason, "no_sources")
        self.assertTrue(result.answer.startswith(grounding.NOT_IN_SOURCES))
 
    @patch(f"{RAG}.get_client")
    @patch(f"{RAG}.retrieve_chunks")
    def test_answer_not_in_dataset_is_declined(self, mock_retrieve, mock_client):
        mock_retrieve.return_value = self.CHUNKS
        mock_client.return_value = grounded_llm("The excerpts don't cover that. NO_ANSWER")
        result = answer_question("What is the dose of warfarin?")
        self.assertEqual((result.status, result.reason), ("off_topic", "declined"))
        self.assertTrue(result.answer.startswith(grounding.NOT_IN_SOURCES))
 
    @patch(f"{RAG}.get_client")
    @patch(f"{RAG}.retrieve_chunks")
    def test_answer_without_citations_is_declined(self, mock_retrieve, mock_client):
        mock_retrieve.return_value = self.CHUNKS
        mock_client.return_value = grounded_llm("Warfarin is a blood thinner.")  # outside knowledge
        result = answer_question("What is warfarin?")
        self.assertEqual((result.status, result.reason), ("off_topic", "no_valid_citation"))
        self.assertEqual(result.sources, [])
 
    @override_settings(GROUNDING_REQUIRE_CITATIONS=False)
    @patch(f"{RAG}.get_client")
    @patch(f"{RAG}.retrieve_chunks")
    def test_citation_check_can_be_turned_off(self, mock_retrieve, mock_client):
        mock_retrieve.return_value = self.CHUNKS
        mock_client.return_value = grounded_llm("Take it with meals.")
        result = answer_question("How do I take metformin?")
        self.assertEqual(result.status, "ok")
        self.assertTrue(result.answer.endswith(grounding.DISCLAIMER))
        self.assertEqual(result.sources, ["Metformin Guide", "Lisinopril Facts"])
    @patch(f"{RET}.embed_query", side_effect=fake_embed)
    def test_source_urls_survive_ingestion_and_retrieval(self, _):
        url = "https://medlineplus.gov/druginformation.html"

        self.add([
            ("Medication Guide", "Information about a pill", url),
            ("Older Document", "More pill information"),
        ])

        results = retrieval.retrieve_chunks("pill", k=2)
        sources = {
            chunk.source: chunk.source_url
            for chunk in results
        }

        self.assertEqual(
            sources,
            {
                "Medication Guide": url,
                "Older Document": "",
            },
        )
        self.assertEqual(
            DocumentChunk.objects.get(
                source="Medication Guide"
            ).source_url,
            url,
        )
