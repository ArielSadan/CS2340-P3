import json
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings
from django.urls import reverse

from .services.retrieval import Chunk, RetrievalError

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
        mock_client.return_value = self.fake_llm("Your bank will never ask for your PIN.")
        data = self.ask("Will my bank ask for my PIN?").json()
        self.assertEqual(data["status"], "ok")
        self.assertIn("PIN", data["answer"])
        self.assertEqual(data["sources"], ["fraud_guide.pdf"])

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
