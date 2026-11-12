from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, connection
from django.db.migrations.executor import MigrationExecutor
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from .forms import MAX_DOCUMENT_CHARS, MAX_UPLOAD_BYTES
from .models import Document, DocumentChunk
from .services import retrieval
from .services.ingestion import ingest_document


def fake_embeddings(texts):
    return [[1.0, 0.0, 0.1] if "pill" in text.lower() else [0.0, 1.0, 0.1] for text in texts]


@override_settings(RAG_MIN_CHUNK_CHARS=1)
class DeveloperDocumentTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.staff = User.objects.create_user("developer", is_staff=True)
        cls.regular = User.objects.create_user("visitor")

    def setUp(self):
        self.client.force_login(self.staff)
        retrieval._index["version"] = None
        self.embeddings = patch("chat.services.retrieval.embed_documents", side_effect=fake_embeddings)
        self.mock_embed = self.embeddings.start()
        self.addCleanup(self.embeddings.stop)

    def make_document(self, title="Medication guide", text="Read the pill label."):
        document = Document(title=title, source_type="article", content=text)
        ingest_document(document)
        return document

    def form_data(self, **changes):
        data = {
            "title": "Medication guide", "source_type": "article",
            "source_url": "https://example.org/medication-guide", "content": "Read the pill label.",
        }
        data.update(changes)
        return data

    def test_anonymous_and_regular_users_cannot_access_document_actions(self):
        document = self.make_document()
        endpoints = [
            reverse("chat:documents"), reverse("chat:document_add"),
            reverse("chat:document_reingest", args=[document.pk]),
            reverse("chat:document_delete", args=[document.pk]),
        ]
        for user in [None, self.regular]:
            self.client.logout()
            if user:
                self.client.force_login(user)
            for url in endpoints:
                for method in [self.client.get, self.client.post]:
                    with self.subTest(user=user, url=url, method=method.__name__):
                        response = method(url)
                        self.assertEqual(response.status_code, 302)
                        self.assertTrue(response.url.startswith(reverse("admin:login")))
        self.assertEqual(document.chunks.count(), 1)

    def test_document_posts_require_csrf_token(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.staff)
        self.assertEqual(client.post(reverse("chat:document_add"), self.form_data()).status_code, 403)
        self.assertFalse(Document.objects.exists())

    def test_staff_sees_document_metadata_and_chat_navigation(self):
        document = self.make_document()
        document.source_url = "https://example.org/guide"
        document.file_name = "guide.txt"
        document.save()
        response = self.client.get(reverse("chat:documents"))
        for text in ["Medication guide", "Article", "https://example.org/guide", "guide.txt", "Date added", "Re-ingest", "Delete"]:
            self.assertContains(response, text)
        self.assertEqual(response.context["documents"][0].chunk_count, 1)
        self.assertContains(self.client.get(reverse("chat:page")), "Developer tools")

    def test_add_text_document_creates_linked_chunks_and_citations(self):
        response = self.client.post(reverse("chat:document_add"), self.form_data())
        self.assertRedirects(response, reverse("chat:documents"))
        document = Document.objects.get()
        chunk = document.chunks.get()
        self.assertEqual(document.source_type, "article")
        self.assertEqual(chunk.source, document.title)
        self.assertEqual(chunk.source_url, document.source_url)
        self.assertEqual(chunk.text, document.content)

    def test_uploads_replace_pasted_content(self):
        for extension in ["txt", "csv", "json"]:
            with self.subTest(extension=extension):
                upload = SimpleUploadedFile(f"fda.{extension}", b"FDA pill information")
                response = self.client.post(reverse("chat:document_add"), self.form_data(
                    title=f"FDA {extension}", source_type="fda", upload=upload,
                ))
                self.assertRedirects(response, reverse("chat:documents"))
                document = Document.objects.get(title=f"FDA {extension}")
                self.assertEqual(document.content, "FDA pill information")
                self.assertEqual(document.file_name, f"fda.{extension}")
                self.assertEqual(document.chunks.get().text, "FDA pill information")

    @patch("chat.forms.PdfReader")
    def test_pdf_extracted_text_is_ingested(self, mock_reader):
        mock_reader.return_value.is_encrypted = False
        mock_reader.return_value.pages = [MagicMock(extract_text=lambda: "FDA pill information")]
        upload = SimpleUploadedFile("guide.pdf", b"pdf test input")
        response = self.client.post(reverse("chat:document_add"), self.form_data(content="", upload=upload))
        self.assertRedirects(response, reverse("chat:documents"))
        self.assertEqual(Document.objects.get().content, "FDA pill information")

    def test_missing_content_and_unsafe_citation_url_are_rejected(self):
        for changes in [{"content": "  "}, {"source_url": "javascript:alert(1)"}]:
            with self.subTest(changes=changes):
                response = self.client.post(reverse("chat:document_add"), self.form_data(**changes))
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context["form"].errors)
        self.assertFalse(Document.objects.exists())
        self.mock_embed.assert_not_called()

    def test_duplicate_source_is_rejected_before_embedding(self):
        self.client.post(reverse("chat:document_add"), self.form_data())
        self.mock_embed.reset_mock()
        response = self.client.post(reverse("chat:document_add"), self.form_data())
        self.assertTrue(response.context["form"].errors)
        self.assertEqual(Document.objects.count(), 1)
        self.mock_embed.assert_not_called()

    def test_invalid_unreadable_and_oversized_files_are_rejected(self):
        for name, content in [
            ("guide.exe", b"not a document"), ("guide.txt", b"\xff\xfe"),
            ("guide.pdf", b"invalid PDF"), ("guide.txt", b" " * (MAX_UPLOAD_BYTES + 1)),
        ]:
            with self.subTest(name=name, size=len(content)):
                response = self.client.post(reverse("chat:document_add"), self.form_data(
                    content="", upload=SimpleUploadedFile(name, content),
                ))
                self.assertTrue(response.context["form"].errors)
        self.assertFalse(Document.objects.exists())
        self.mock_embed.assert_not_called()

    def test_excessively_long_document_is_rejected(self):
        response = self.client.post(reverse("chat:document_add"), self.form_data(content="x" * (MAX_DOCUMENT_CHARS + 1)))
        self.assertTrue(response.context["form"].errors)
        self.mock_embed.assert_not_called()

    def test_reingestion_replaces_only_selected_document_and_refreshes_cache(self):
        document = self.make_document()
        other = self.make_document("Another guide", "More pill information.")
        old_id = document.chunks.get().pk
        old_date = document.ingested_at
        with patch("chat.services.retrieval.embed_query", return_value=[1.0, 0.0, 0.1]):
            retrieval.retrieve_chunks("pill", k=5)  # Warm the existing index.
        response = self.client.post(reverse("chat:document_reingest", args=[document.pk]), self.form_data(
            title="Updated guide", source_url="https://example.org/updated", content="Updated scam information.",
        ))
        self.assertRedirects(response, reverse("chat:documents"))
        document.refresh_from_db()
        self.assertFalse(DocumentChunk.objects.filter(pk=old_id).exists())
        self.assertEqual(document.chunks.count(), 1)
        self.assertEqual(other.chunks.count(), 1)
        self.assertGreater(document.ingested_at, old_date)
        with patch("chat.services.retrieval.embed_query", return_value=[0.0, 1.0, 0.1]):
            result = retrieval.retrieve_chunks("scam", k=1)[0]
        self.assertEqual(result.text, "Updated scam information.")
        self.assertEqual(result.source, "Updated guide")
        self.assertEqual(result.source_url, "https://example.org/updated")

    def test_reingestion_can_rebuild_saved_text_without_upload(self):
        document = self.make_document()
        response = self.client.get(reverse("chat:document_reingest", args=[document.pk]))
        self.assertEqual(response.context["form"].initial["content"], document.content)
        response = self.client.post(reverse("chat:document_reingest", args=[document.pk]), self.form_data(source_url=""))
        self.assertRedirects(response, reverse("chat:documents"))
        self.assertEqual(document.chunks.count(), 1)

    def test_failed_embedding_preserves_old_document_and_chunks(self):
        document = self.make_document()
        original_id = document.chunks.get().pk
        self.mock_embed.side_effect = TimeoutError("embedding service down")
        with self.assertLogs("chat.developer_views", level="ERROR"):
            response = self.client.post(reverse("chat:document_reingest", args=[document.pk]), self.form_data(
                title="Changed title", content="Changed text",
            ))
        self.assertContains(response, "Existing knowledge was kept.")
        document.refresh_from_db()
        self.assertEqual(document.title, "Medication guide")
        self.assertEqual(document.content, "Read the pill label.")
        self.assertEqual(document.chunks.get().pk, original_id)

    def test_failed_database_write_rolls_back_replacement(self):
        document = self.make_document()
        original_id = document.chunks.get().pk
        document.content = "Replacement text"
        with patch("chat.services.retrieval.DocumentChunk.objects.bulk_create", side_effect=IntegrityError("write failed")):
            with self.assertRaises(IntegrityError):
                ingest_document(document)
        document.refresh_from_db()
        self.assertEqual(document.content, "Read the pill label.")
        self.assertEqual(document.chunks.get().pk, original_id)

    def test_failed_new_ingestion_leaves_no_partial_document(self):
        self.mock_embed.side_effect = TimeoutError("embedding service down")
        with self.assertLogs("chat.developer_views", level="ERROR"):
            self.client.post(reverse("chat:document_add"), self.form_data())
        self.assertFalse(Document.objects.exists())
        self.assertFalse(DocumentChunk.objects.exists())

    def test_delete_requires_confirmation_and_removes_only_selected_chunks(self):
        document = self.make_document()
        other = self.make_document("Other", "Other pill information")
        url = reverse("chat:document_delete", args=[document.pk])
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertTrue(Document.objects.filter(pk=document.pk).exists())
        with patch("chat.services.retrieval.embed_query", return_value=[1.0, 0.0, 0.1]):
            retrieval.retrieve_chunks("pill", k=5)
            response = self.client.post(url)
            results = retrieval.retrieve_chunks("pill", k=5)
        self.assertRedirects(response, reverse("chat:documents"))
        self.assertFalse(Document.objects.filter(pk=document.pk).exists())
        self.assertEqual(DocumentChunk.objects.count(), 1)
        self.assertEqual([chunk.source for chunk in results], [other.title])

    def test_legacy_add_chunks_imports_also_appear_on_developer_page(self):
        retrieval.add_chunks([("Old source", "First pill passage."), ("Old source", "Second pill passage.")])
        document = Document.objects.get(title="Old source")
        self.assertEqual(document.chunks.count(), 2)
        self.assertIn("First pill passage.", document.content)
        self.assertIn("Second pill passage.", document.content)
        self.assertContains(self.client.get(reverse("chat:documents")), "Old source")


class DocumentMigrationTests(TransactionTestCase):
    def test_existing_chunks_are_preserved_and_grouped_by_source_and_url(self):
        executor = MigrationExecutor(connection)
        executor.migrate([("chat", "0002_documentchunk_source_url")])
        old_apps = executor.loader.project_state([("chat", "0002_documentchunk_source_url")]).apps
        OldChunk = old_apps.get_model("chat", "DocumentChunk")
        first = OldChunk.objects.create(source="Guide", source_url="https://example.org/a", text="First", embedding=b"1234")
        OldChunk.objects.create(source="Guide", source_url="https://example.org/a", text="Second", embedding=b"5678")
        OldChunk.objects.create(source="Guide", source_url="https://example.org/b", text="Different URL", embedding=b"9012")
        try:
            executor = MigrationExecutor(connection)
            executor.migrate([("chat", "0003_document")])
            migrated_apps = executor.loader.project_state([("chat", "0003_document")]).apps
            MigratedDocument = migrated_apps.get_model("chat", "Document")
            self.assertEqual(MigratedDocument.objects.count(), 2)
            document = MigratedDocument.objects.get(source_url="https://example.org/a")
            self.assertEqual(document.content, "First\n\nSecond")
            self.assertEqual(document.chunks.count(), 2)
            self.assertEqual(bytes(document.chunks.get(pk=first.pk).embedding), b"1234")
        finally:
            executor = MigrationExecutor(connection)
            executor.migrate(executor.loader.graph.leaf_nodes("chat"))
