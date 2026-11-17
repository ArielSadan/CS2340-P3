from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from .models import Document, DocumentChunk
from .services.ingestion import ingest_document
from .services.preprocessing import clean_text, decode_text, preprocess_text, split_text
from .services.retrieval import add_chunks


SOURCE_TEXT = (
    "This is an example source paragraph for testing preprocessing and chunk "
    "metadata. It has enough readable text to remain a useful document chunk."
)


class TextCleaningTests(SimpleTestCase):
    def test_html_noise_is_removed_but_article_headings_and_warnings_remain(self):
        text = clean_text("""
            <head><title>Page furniture</title><style>body {color:red}</style></head>
            <header>Site navigation</header><nav>Home <a>Menu</a></nav>
            <article><header><h1>Source title</h1></header>
              <p>Useful &amp; readable article text.</p>
              <div class="ad-container"><p>Buy something</p></div>
              <div id="cookie-banner">Accept all cookies</div>
              <script>tracking()</script><div hidden>Hidden noise</div>
              <div role="navigation">Other pages</div>
              <p style="display: none">Invisible text</p>
              <aside><p>Important boxed warning from the source.</p></aside>
              <section id="adverse-effects"><h2>Adverse effects</h2><p>Retain this section.</p></section>
            </article><footer>Privacy policy and site links</footer>
        """)
        for useful in ["Source title", "Useful & readable", "Important boxed warning", "Adverse effects", "Retain this section."]:
            self.assertIn(useful, text)
        for noise in ["Page furniture", "Site navigation", "Buy something", "Accept all", "tracking()", "Hidden noise", "Other pages", "Invisible text", "Privacy policy", "<p>"]:
            self.assertNotIn(noise, text)
        self.assertIn("\n\n", text)

    def test_unicode_entities_whitespace_and_control_characters_are_normalized(self):
        text = clean_text("\ufeffCafe\u0301\t &amp;\u00a0 tea\r\n\r\n\r\nNext\u200b line\x00")
        self.assertEqual(text, "Café & tea\n\nNext line")

    def test_plain_navigation_lines_are_removed_without_removing_article_sentences(self):
        text = clean_text("Home\n\nActual article paragraph.\n\nPrivacy policy\n\nA sentence containing the word home remains.")
        self.assertEqual(text, "Actual article paragraph.\n\nA sentence containing the word home remains.")

    def test_null_html_attributes_and_self_closing_elements_do_not_break_cleaning(self):
        self.assertEqual(clean_text("<div class><p>Keep this text.</p><script/><br/></div>"), "Keep this text.")

    def test_common_text_encodings_are_decoded(self):
        for data in ["Café".encode("utf-8-sig"), "Café".encode("utf-16"), "Café".encode("cp1252")]:
            with self.subTest(data=data):
                self.assertEqual(decode_text(data), "Café")

    def test_encoded_closing_tag_does_not_expose_script_contents(self):
        text = clean_text('<script>const marker = "&lt;/script&gt;"; tracking();</script><p>Keep this.</p>')
        self.assertEqual(text, "Keep this.")


@override_settings(RAG_CHUNK_SIZE_CHARS=120, RAG_CHUNK_OVERLAP_CHARS=20, RAG_MIN_CHUNK_CHARS=10)
class TextChunkingTests(SimpleTestCase):
    def test_paragraph_boundary_is_preferred(self):
        paragraph = ("alpha " * 18).strip()
        chunks = split_text(paragraph + "\n\nHeading\n\n" + "beta " * 50)
        self.assertEqual(chunks[0], paragraph)
        self.assertTrue(any("Heading" in chunk for chunk in chunks))

    def test_overlap_is_retained_and_chunks_are_bounded(self):
        chunks = split_text(" ".join(f"w{index:03}" for index in range(100)))
        self.assertGreater(len(chunks), 1)
        for left, right in zip(chunks, chunks[1:]):
            overlap = ""
            for count in range(1, len(left.split())):
                if left.split()[-count:] == right.split()[:count]:
                    overlap = " ".join(right.split()[:count])
            self.assertGreaterEqual(len(overlap), 10)
            self.assertLessEqual(len(overlap), 20)
        self.assertTrue(all(10 <= len(chunk) <= 130 for chunk in chunks))

    def test_short_final_sentence_is_retained(self):
        with override_settings(RAG_CHUNK_SIZE_CHARS=100):
            chunks = split_text(("word " * 20).strip() + " STOP")
        self.assertEqual(len(chunks), 1)
        self.assertTrue(chunks[0].endswith("STOP"))

    def test_empty_noise_and_near_empty_documents_do_not_create_chunks(self):
        for text in ["", "    ", "short", "-" * 200, "<nav>Only navigation</nav>"]:
            with self.subTest(text=text):
                self.assertEqual(preprocess_text(text)[1], [])

    def test_unbroken_long_text_does_not_loop_or_disappear(self):
        chunks = split_text("x" * 500)
        self.assertEqual("".join(chunks), "x" * 500)

    def test_configuration_changes_chunking_and_invalid_overlap_is_rejected(self):
        text = "word " * 100
        small = split_text(text)
        with override_settings(RAG_CHUNK_SIZE_CHARS=240):
            self.assertLess(len(split_text(text)), len(small))
        with override_settings(RAG_CHUNK_OVERLAP_CHARS=120):
            with self.assertRaises(ImproperlyConfigured):
                split_text(text)


class PreprocessingPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = get_user_model().objects.create_user("preprocessing-developer", is_staff=True)

    def setUp(self):
        self.client.force_login(self.staff)
        self.patcher = patch("chat.services.retrieval.embed_documents", side_effect=lambda texts: [[1.0, 0.0, 0.1] for _ in texts])
        self.embed = self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def form_data(self, **changes):
        data = {"title": "Example source", "source_type": "article", "source_url": "https://example.org/source", "content": SOURCE_TEXT}
        data.update(changes)
        return data

    def test_preview_cleans_html_without_saving_or_calling_gemini(self):
        response = self.client.post(reverse("chat:document_add"), self.form_data(
            action="preview", content="<nav>Navigation noise</nav><h1>Source heading</h1><p>" + SOURCE_TEXT + "</p>",
        ))
        self.assertContains(response, "Proposed chunks")
        preview = response.context["preview"]
        self.assertNotIn("Navigation noise", preview["cleaned_text"])
        self.assertIn("Source heading", preview["cleaned_text"])
        self.assertTrue(preview["chunks"])
        self.assertFalse(Document.objects.exists())
        self.assertFalse(DocumentChunk.objects.exists())
        self.embed.assert_not_called()

    def test_uploaded_preview_can_be_saved_without_uploading_again(self):
        upload = SimpleUploadedFile("source.html", ("<p>" + SOURCE_TEXT + "</p>").encode())
        response = self.client.post(reverse("chat:document_add"), self.form_data(action="preview", content="", upload=upload))
        self.embed.assert_not_called()
        form = response.context["form"]
        self.assertEqual(form.data["file_name"], "source.html")
        data = dict(form.data.items())
        data["action"] = "ingest"
        response = self.client.post(reverse("chat:document_add"), data)
        self.assertRedirects(response, reverse("knowledge:developer"))
        document = Document.objects.get()
        self.assertEqual(document.file_name, "source.html")
        self.assertEqual(document.cleaned_content, SOURCE_TEXT)
        self.assertEqual(document.chunks.get().text, SOURCE_TEXT)

    def test_metadata_and_stored_preview_match_the_actual_embedded_chunks(self):
        document = Document(**self.form_data(content="<script>noise()</script><p>" + SOURCE_TEXT * 30 + "</p>"))
        ingest_document(document)
        chunks = list(document.chunks.order_by("position"))
        self.assertEqual([chunk.position for chunk in chunks], list(range(1, len(chunks) + 1)))
        self.assertEqual(self.embed.call_args.args[0], [chunk.text for chunk in chunks])
        for chunk in chunks:
            self.assertEqual(chunk.source, document.title)
            self.assertEqual(chunk.document.title, document.title)
            self.assertEqual(chunk.source_url, document.source_url)
            self.assertIsNotNone(chunk.created_at)
            self.assertNotIn("noise()", chunk.text)
        with patch("chat.developer_views.preprocess_text", side_effect=AssertionError("Stored preview must not recompute chunks")):
            response = self.client.get(reverse("chat:document_preview", args=[document.pk]))
        self.assertContains(response, "Stored cleaned text")
        self.assertEqual([chunk.pk for chunk in response.context["chunks"]], [chunk.pk for chunk in chunks])
        self.assertContains(response, chunks[0].text)

    def test_noise_only_reingestion_preserves_previous_data_and_explains_problem(self):
        document = Document(**self.form_data())
        ingest_document(document)
        old_id = document.chunks.get().pk
        self.embed.reset_mock()
        response = self.client.post(reverse("chat:document_reingest", args=[document.pk]), self.form_data(content="<nav>Menu</nav>"))
        self.assertContains(response, "No usable chunks remain after cleaning")
        document.refresh_from_db()
        self.assertEqual(document.content, SOURCE_TEXT)
        self.assertEqual(document.chunks.get().pk, old_id)
        self.embed.assert_not_called()

    def test_existing_document_preview_does_not_change_saved_text(self):
        document = Document(**self.form_data())
        ingest_document(document)
        self.embed.reset_mock()
        response = self.client.post(reverse("chat:document_reingest", args=[document.pk]), self.form_data(action="preview", content=SOURCE_TEXT + " Proposed update."))
        self.assertTrue(response.context["preview"])
        document.refresh_from_db()
        self.assertEqual(document.content, SOURCE_TEXT)
        self.embed.assert_not_called()

    def test_saved_preview_is_staff_only_and_read_only(self):
        document = Document(**self.form_data())
        ingest_document(document)
        url = reverse("chat:document_preview", args=[document.pk])
        self.assertEqual(self.client.post(url).status_code, 405)
        self.client.logout()
        self.assertEqual(self.client.get(url).status_code, 302)
        user = get_user_model().objects.create_user("ordinary-viewer")
        self.client.force_login(user)
        self.assertEqual(self.client.get(url).status_code, 302)

    def test_legacy_imports_get_sequential_positions_when_appended(self):
        document = Document(title="Old importer", content=SOURCE_TEXT)
        ingest_document(document)
        add_chunks([("Old importer", SOURCE_TEXT), ("Old importer", SOURCE_TEXT)])
        add_chunks([("Old importer", SOURCE_TEXT)])
        document.refresh_from_db()
        self.assertEqual(list(document.chunks.order_by("position").values_list("position", flat=True)), [1, 2, 3, 4])
        self.assertEqual(document.cleaned_content, "")


class PreprocessingMigrationTests(TransactionTestCase):
    def test_existing_vectors_are_numbered_without_reembedding(self):
        executor = MigrationExecutor(connection)
        executor.migrate([("chat", "0003_document")])
        old_apps = executor.loader.project_state([("chat", "0003_document")]).apps
        OldDocument = old_apps.get_model("chat", "Document")
        OldChunk = old_apps.get_model("chat", "DocumentChunk")
        document = OldDocument.objects.create(title="Existing source", content=SOURCE_TEXT)
        ids = [OldChunk.objects.create(document=document, source=document.title, text=SOURCE_TEXT, embedding=b"1234").pk for _ in range(2)]
        try:
            MigrationExecutor(connection).migrate([("chat", "0004_preprocessing")])
            self.assertEqual(list(DocumentChunk.objects.order_by("position").values_list("position", flat=True)), [1, 2])
            self.assertEqual(list(DocumentChunk.objects.order_by("id").values_list("id", flat=True)), ids)
            self.assertEqual(bytes(DocumentChunk.objects.first().embedding), b"1234")
            self.assertEqual(Document.objects.get().cleaned_content, "")
        finally:
            executor = MigrationExecutor(connection)
            executor.migrate(executor.loader.graph.leaf_nodes("chat"))
