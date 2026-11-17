import hashlib
import io
import re
from unittest import mock
 
import requests
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
 
from chat.models import Document, DocumentChunk
from chat.services import retrieval
from knowledge.ingestion import extract, fetch
from knowledge.ingestion.errors import (
    BlockedByHost, FetchFailed, InvalidURL, NoReadableText, PrivatePage, UnsafeURL, UnsupportedFile,
)
from knowledge.ingestion.fetch import FetchedPage
from knowledge.ingestion.sources import ingest_uploaded_file, ingest_url
 
 
# ============================================================ fakes for Gemini
 
FAKE_DIM = 512
 
 
def fake_vector(text, dim=FAKE_DIM):
    """Bag-of-words hashing: texts that share words get similar vectors.
    Returns a plain list, like chat.services.gemini.embed_documents does."""
    v = [0.0] * dim
    for word in re.findall(r"[a-z]{3,}", text.lower()):
        v[int(hashlib.md5(word.encode()).hexdigest(), 16) % dim] += 1.0
    return v
 
 
def fake_embed_documents(texts):
    return [fake_vector(t) for t in texts]
 
 
class FakeRateLimit(Exception):
    """Looks like the Gemini SDK's 429 error."""
    code = 429
 
 
# ============================================================ Part 1: no database
 
ARTICLE = "<p>" + " ".join(
    f"Sentence {i} explains how older adults manage medicines safely." for i in range(40)
) + "</p>"
 
 
# ------------------------------------------------------------ URLs
 
class NormalizeUrlTests(SimpleTestCase):
    def test_normalizes_for_duplicate_detection(self):
        self.assertEqual(
            fetch.normalize_url("  HTTPS://WWW.NIA.NIH.GOV:443/health/meds/?utm_source=x&b=2#top "),
            "https://www.nia.nih.gov/health/meds?b=2",
        )
 
    def test_adds_scheme(self):
        self.assertEqual(fetch.normalize_url("nia.nih.gov/health"), "https://nia.nih.gov/health")
 
    def test_rejects_bad_urls(self):
        for bad in ["", "ftp://example.com/a", "file:///etc/passwd", "https://user:pw@example.com",
                    "javascript:alert(1)"]:
            with self.subTest(bad=bad), self.assertRaises(InvalidURL):
                fetch.normalize_url(bad)
 
 
class PrivateAddressTests(SimpleTestCase):
    def test_internal_addresses_rejected(self):
        for host in ["localhost", "127.0.0.1", "10.1.2.3", "192.168.0.10", "172.16.5.4",
                     "169.254.169.254", "::1", "0.0.0.0", "printer.local", "intranet"]:
            with self.subTest(host=host), self.assertRaises(UnsafeURL):
                fetch.ensure_public_host(host, resolver=lambda *a: [])
 
    def test_name_resolving_to_private_ip_rejected(self):
        resolver = lambda *a: [(2, 1, 6, "", ("10.0.0.7", 0))]
        with self.assertRaises(UnsafeURL):
            fetch.ensure_public_host("sneaky.example.com", resolver=resolver)
 
    def test_public_host_allowed(self):
        resolver = lambda *a: [(2, 1, 6, "", ("156.40.212.209", 0))]
        fetch.ensure_public_host("www.nia.nih.gov", resolver=resolver)  # no error
 
    def test_unresolvable_name_left_to_the_request(self):
        def resolver(*a):
            import socket
            raise socket.gaierror("no dns")
        fetch.ensure_public_host("www.nia.nih.gov", resolver=resolver)  # no error
 
 
class LoginUrlTests(SimpleTestCase):
    def test_login_urls(self):
        for url in ["https://accounts.google.com/v3/signin/identifier?continue=x",
                    "https://example.com/login", "https://example.com/users/sign_in",
                    "https://example.com/account/login?next=/a", "https://gatech.okta.com/app"]:
            with self.subTest(url=url):
                self.assertTrue(fetch.looks_like_login_url(url))
 
    def test_normal_urls(self):
        for url in ["https://pmc.ncbi.nlm.nih.gov/articles/PMC123/",
                    "https://example.com/authors/smith", "https://example.com/blog/logging-tips"]:
            with self.subTest(url=url):
                self.assertFalse(fetch.looks_like_login_url(url))
 
 
class FakeResponse:
    def __init__(self, status=200, body=b"", headers=None):
        self.status_code = status
        self.headers = headers or {"Content-Type": "text/html; charset=utf-8"}
        self._body = body
        self.encoding = "utf-8"
        self.raw = io.BytesIO(body)
        self.raw.read = lambda n=-1, decode_content=True, _b=io.BytesIO(body): _b.read(n)
 
    def iter_content(self, size):
        for i in range(0, len(self._body), size):
            yield self._body[i:i + size]
 
    def close(self):
        pass
 
 
class FakeSession:
    """Serves canned responses by URL; records requested URLs."""
 
    def __init__(self, routes):
        self.routes = routes
        self.requested = []
 
    def get(self, url, **kwargs):
        self.requested.append(url)
        route = self.routes[url]
        if isinstance(route, Exception):
            raise route
        return route
 
 
PUBLIC = lambda *a: [(2, 1, 6, "", ("156.40.212.209", 0))]
 
 
class FetchTests(SimpleTestCase):
    URL = "https://www.nia.nih.gov/health/meds"
 
    def fetch(self, routes):
        return fetch.fetch_url(self.URL, session=FakeSession(routes), resolver=PUBLIC)
 
    def test_success(self):
        page = self.fetch({self.URL: FakeResponse(200, b"<html>hi</html>")})
        self.assertEqual(page.body, b"<html>hi</html>")
        self.assertEqual(page.content_type, "text/html")
 
    def test_redirect_to_sign_in_page_is_private(self):
        routes = {self.URL: FakeResponse(302, headers={"Location": "https://accounts.google.com/ServiceLogin?x=1"})}
        with self.assertRaises(PrivatePage):
            self.fetch(routes)
 
    def test_redirect_to_internal_address_is_blocked(self):
        routes = {self.URL: FakeResponse(302, headers={"Location": "http://127.0.0.1/admin"})}
        with self.assertRaises(UnsafeURL):
            self.fetch(routes)
 
    def test_401_is_private(self):
        with self.assertRaises(PrivatePage):
            self.fetch({self.URL: FakeResponse(401)})
 
    def test_403_is_private(self):
        with self.assertRaises(PrivatePage):
            self.fetch({self.URL: FakeResponse(403, b"<h1>Forbidden</h1>")})
 
    def test_pythonanywhere_proxy_page_is_blocked_not_private(self):
        body = b"<html>Access to arbitrary websites is not available from free accounts; " \
               b"you can only access sites on our whitelist. PythonAnywhere</html>"
        with self.assertRaises(BlockedByHost):
            self.fetch({self.URL: FakeResponse(403, body)})
 
    def test_https_proxy_refusal_is_blocked(self):
        with self.assertRaises(BlockedByHost):
            self.fetch({self.URL: requests.exceptions.ProxyError("Tunnel connection failed: 403")})
 
    def test_404_and_timeout(self):
        with self.assertRaisesRegex(FetchFailed, "not found"):
            self.fetch({self.URL: FakeResponse(404)})
        with self.assertRaisesRegex(FetchFailed, "too long"):
            self.fetch({self.URL: requests.exceptions.ReadTimeout()})
 
    def test_redirect_loop(self):
        routes = {self.URL: FakeResponse(301, headers={"Location": self.URL})}
        with self.assertRaisesRegex(FetchFailed, "redirects"):
            self.fetch(routes)
 
 
# ------------------------------------------------------------ extraction
 
class HtmlExtractionTests(SimpleTestCase):
    def test_main_text_sections_and_noise_removed(self):
        html = f"""<html><head><title>Safe Use of Medicines | NIA</title></head><body>
            <nav><a href="/">Home</a> <a href="/x">Topics</a></nav>
            <main><h1>Safe use of medicines</h1>{ARTICLE}
              <h2>Side effects</h2><p>Some medicines cause dizziness. Tell your doctor.</p>
              <h2>References</h2><p>Smith J. et al. 2020. Journal of Things.</p>
            </main><footer>Contact us · Privacy policy</footer></body></html>"""
        result = extract.extract_html(html)
        self.assertEqual(result.title, "Safe Use of Medicines | NIA")
        headings = [h for h, _ in result.sections]
        self.assertIn("Side effects", headings)
        self.assertNotIn("References", headings)
        all_text = " ".join(t for _, t in result.sections)
        self.assertNotIn("Privacy policy", all_text)
        self.assertNotIn("Topics", all_text)
 
    def test_login_wall_detected(self):
        html = """<html><head><title>Sign in</title></head><body>
            <h1>Sign in to continue</h1><p>Use your account to view this document.</p>
            <form><input type="email" name="u"><input type="password" name="p"></form></body></html>"""
        with self.assertRaises(PrivatePage):
            extract.extract_html(html)
 
    def test_article_with_login_box_is_not_a_login_wall(self):
        html = f"""<html><body><div class="login"><input type="password"></div>
            <article><h1>Metformin</h1>{ARTICLE}</article></body></html>"""
        self.assertGreater(extract.extract_html(html).word_count, 150)
 
    def test_empty_page(self):
        with self.assertRaises(NoReadableText):
            extract.extract_html("<html><body><p>Hi</p></body></html>")
 
 
class FileExtractionTests(SimpleTestCase):
    def test_unsupported_type(self):
        with self.assertRaisesRegex(UnsupportedFile, r"\.exe"):
            extract.extract_upload("virus.exe", b"MZ...")
 
    def test_fake_pdf(self):
        with self.assertRaisesRegex(UnsupportedFile, "isn't a real PDF"):
            extract.extract_upload("notes.pdf", b"just text")
 
    def test_txt(self):
        result = extract.extract_upload("metformin_notes.txt", "Take with food. Café".encode("utf-8"))
        self.assertEqual(result.title, "metformin notes")
        self.assertIn("Café", result.sections[0][1])
 
    def test_markdown_headings_become_sections(self):
        md = b"# Metformin guide\n\nIntro text.\n\n## Side effects\n\nUpset stomach.\n"
        result = extract.extract_upload("guide.md", md)
        self.assertEqual(result.title, "Metformin guide")
        self.assertEqual([h for h, _ in result.sections], ["Metformin guide", "Side effects"])
 
    def test_binary_txt_rejected(self):
        with self.assertRaises(UnsupportedFile):
            extract.extract_upload("data.txt", b"\x00\x01\x02binary")
 
    def test_docx(self):
        try:
            import docx
        except ImportError:
            self.skipTest("python-docx not installed")
        doc = docx.Document()
        doc.add_heading("Lisinopril", level=1)
        doc.add_paragraph("Lisinopril lowers blood pressure.")
        buf = io.BytesIO()
        doc.save(buf)
        result = extract.extract_upload("lisinopril.docx", buf.getvalue())
        self.assertEqual(result.title, "Lisinopril")
        self.assertIn("blood pressure", result.sections[0][1])
 
    def test_pdf_pages_become_sections(self):
        try:
            from reportlab.pdfgen import canvas
        except ImportError:
            self.skipTest("reportlab not installed (only needed to build a test PDF)")
        buf = io.BytesIO()
        c = canvas.Canvas(buf)
        c.drawString(72, 720, "Page one: metformin lowers blood sugar.")
        c.showPage()
        c.drawString(72, 720, "Page two: take it with meals.")
        c.save()
        result = extract.extract_upload("Metformin_Guide.pdf", buf.getvalue())
        self.assertEqual([h for h, _ in result.sections], ["Page 1", "Page 2"])
        self.assertEqual(result.title, "Metformin Guide")
 
 
# ============================================================ Part 2: database
 
RET = "chat.services.retrieval"
 
METFORMIN = (
    "Metformin is a medicine that helps control blood sugar in people with type 2 diabetes. "
    "Take metformin with meals to reduce an upset stomach. "
    "Common side effects include nausea, diarrhea and a metal taste in the mouth. "
    "Tell your doctor if you feel very weak or have trouble breathing."
)
LISINOPRIL = (
    "Lisinopril lowers high blood pressure and helps the heart. "
    "It can cause a dry cough or dizziness when standing up quickly. "
    "Do not use salt substitutes containing potassium without asking your doctor."
)
 
 
def upload(name, text):
    return SimpleUploadedFile(name, text.encode("utf-8"), content_type="text/plain")
 
 
@override_settings(GEMINI_API_KEY="test-key", MIN_DOCUMENT_WORDS=10, EMBED_RETRY_DELAYS=[0, 0])
class KnowledgeTestCase(TestCase):
    def setUp(self):
        patcher = mock.patch(f"{RET}.embed_documents", side_effect=fake_embed_documents)
        self.embed = patcher.start()
        self.addCleanup(patcher.stop)
        query = mock.patch(f"{RET}.embed_query", side_effect=fake_vector)
        query.start()
        self.addCleanup(query.stop)
        retrieval._index["version"] = None  # fresh in-memory index per test
 
 
class FileIngestionTests(KnowledgeTestCase):
    def test_upload_is_stored_in_the_chat_chunk_table(self):
        result = ingest_uploaded_file(upload("metformin_guide.txt", METFORMIN))
        self.assertEqual(result.status, "added", result.message)
        doc = Document.objects.get()
        self.assertEqual(doc.file_name, "metformin_guide.txt")
        self.assertEqual(doc.content, METFORMIN)  # kept for preview and re-ingest
        chunk = DocumentChunk.objects.get()
        self.assertEqual(chunk.document, doc)
        self.assertEqual(chunk.source, "metformin guide")  # readable title, not a file path
        self.assertEqual(chunk.text, METFORMIN)  # chat pipeline: cleaned text, no title prefix

    def test_html_upload_is_cleaned(self):
        html = f"<html><head><title>Metformin</title></head><body><nav>Home</nav><main><p>{METFORMIN}</p></main></body></html>"
        result = ingest_uploaded_file(upload("metformin.html", html))
        self.assertEqual(result.status, "added", result.message)
        text = DocumentChunk.objects.get().text
        self.assertNotIn("<p>", text)
        self.assertNotIn("Home", text)
        self.assertEqual(Document.objects.get().title, "Metformin")
 
    def test_chat_retrieval_finds_ingested_text(self):
        ingest_uploaded_file(upload("metformin.txt", METFORMIN))
        ingest_uploaded_file(upload("lisinopril.txt", LISINOPRIL))
        best = retrieval.retrieve_chunks("What are metformin side effects with meals?", k=2)[0]
        self.assertEqual(best.source, "metformin")
 
    def test_same_file_twice_is_skipped(self):
        ingest_uploaded_file(upload("metformin.txt", METFORMIN))
        result = ingest_uploaded_file(upload("metformin.txt", METFORMIN))
        self.assertEqual(result.status, "skipped")
        self.assertEqual(Document.objects.count(), 1)
 
    def test_same_content_under_another_name_is_skipped(self):
        ingest_uploaded_file(upload("metformin.txt", METFORMIN))
        self.assertEqual(ingest_uploaded_file(upload("copy.txt", METFORMIN)).status, "skipped")
 
    def test_changed_file_replaces_old_version(self):
        ingest_uploaded_file(upload("metformin.txt", METFORMIN))
        result = ingest_uploaded_file(upload("metformin.txt", METFORMIN + " New warning added this year."))
        self.assertEqual(result.status, "replaced")
        self.assertEqual(Document.objects.count(), 1)
        texts = " ".join(DocumentChunk.objects.values_list("text", flat=True))
        self.assertIn("New warning", texts)
 
    def test_reingest_after_admin_clears_chunks(self):
        ingest_uploaded_file(upload("metformin.txt", METFORMIN))
        DocumentChunk.objects.all().delete()  # story #11 "clear"
        result = ingest_uploaded_file(upload("metformin.txt", METFORMIN))
        self.assertEqual(result.status, "added", result.message)
        self.assertTrue(DocumentChunk.objects.exists())
        self.assertEqual(Document.objects.count(), 1)
 
    def test_unsupported_file_reports_readable_error(self):
        result = ingest_uploaded_file(SimpleUploadedFile("photo.png", b"\x89PNG..."))
        self.assertEqual(result.status, "error")
        self.assertIn("Unsupported file type (.png)", result.message)
        self.assertFalse(Document.objects.exists())
 
    def test_rate_limit_is_retried(self):
        self.embed.side_effect = [FakeRateLimit("429 RESOURCE_EXHAUSTED"), fake_embed_documents([METFORMIN])]
        result = ingest_uploaded_file(upload("metformin.txt", METFORMIN))
        self.assertEqual(result.status, "added", result.message)
        self.assertEqual(self.embed.call_count, 2)
 
    def test_gemini_failure_saves_nothing_and_keeps_old_version(self):
        ingest_uploaded_file(upload("metformin.txt", METFORMIN))
        self.embed.side_effect = RuntimeError("Gemini is down")
        result = ingest_uploaded_file(upload("metformin.txt", METFORMIN + " Changed."))
        self.assertEqual(result.status, "error")
        self.assertIn("Couldn't reach Gemini", result.message)
        self.assertNotIn("Changed", " ".join(DocumentChunk.objects.values_list("text", flat=True)))
 
    @override_settings(GEMINI_API_KEY="")
    def test_missing_api_key_is_explained(self):
        result = ingest_uploaded_file(upload("metformin.txt", METFORMIN))
        self.assertIn("GEMINI_API_KEY", result.message)
 
 
class UrlIngestionTests(KnowledgeTestCase):
    URL = "https://www.nia.nih.gov/health/medicines/lisinopril"
 
    def page(self, *args, **kwargs):
        html = f"<html><head><title>Lisinopril</title></head><body><main><p>{LISINOPRIL}</p></main></body></html>"
        return FetchedPage(self.URL, "text/html", html.encode(), "utf-8")
 
    def test_url_is_ingested_with_its_link(self):
        with mock.patch("knowledge.ingestion.sources.fetch_url", self.page):
            result = ingest_url(self.URL + "/?utm_source=newsletter#top")
        self.assertEqual(result.status, "added", result.message)
        doc = Document.objects.get()
        self.assertEqual(doc.source_url, self.URL)  # normalized
        self.assertEqual(doc.source_type, "article")
        self.assertEqual(DocumentChunk.objects.get().source, "Lisinopril")
 
    def test_same_url_twice_is_skipped(self):
        with mock.patch("knowledge.ingestion.sources.fetch_url", self.page):
            ingest_url(self.URL)
            self.assertEqual(ingest_url(self.URL + "/").status, "skipped")
 
    def test_private_page_reports_error(self):
        def private(*a, **k):
            raise PrivatePage("This page is private or requires a login.")
        with mock.patch("knowledge.ingestion.sources.fetch_url", private):
            result = ingest_url("https://docs.google.com/document/d/abc/edit")
        self.assertEqual(result.status, "error")
        self.assertIn("private", result.message)
 
    def test_internal_address_rejected_without_fetching(self):
        result = ingest_url("http://localhost:8000/admin")
        self.assertEqual(result.status, "error")
        self.assertIn("Private or internal", result.message)
 
 
class DeveloperPageTests(KnowledgeTestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("knowledge:developer")
        users = get_user_model()
        self.staff = users.objects.create_user("dev", password="pw", is_staff=True)
        self.regular = users.objects.create_user("user", password="pw")
 
    def test_only_staff_can_open_it(self):
        self.assertEqual(self.client.get(self.url).status_code, 302)
        self.client.force_login(self.regular)
        self.assertEqual(self.client.get(self.url).status_code, 302)
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(self.url).status_code, 200)
 
    def review_data(self, **changes):
        data = {"action": "ingest", "title": "metformin", "source_type": "other",
                "source_url": "", "content": METFORMIN, "file_name": "metformin.txt"}
        data.update(changes)
        return data

    def test_upload_opens_the_review_form_filled_in(self):
        self.client.force_login(self.staff)
        response = self.client.post(self.url, {
            "action": "upload",
            "files": [upload("metformin.txt", METFORMIN), SimpleUploadedFile("scan.exe", b"MZ")],
        }, follow=True)
        self.assertRedirects(response, reverse("chat:document_review"))
        self.assertContains(response, "Review document 1 of 1")
        self.assertContains(response, "Unsupported file type (.exe)")  # the other item's problem
        form = response.context["form"]
        self.assertEqual(form["title"].value(), "metformin")
        self.assertEqual(form["content"].value(), METFORMIN)
        self.assertEqual(form["file_name"].value(), "metformin.txt")
        self.assertFalse(Document.objects.exists())  # nothing saved before review
        self.embed.assert_not_called()

    def test_reviewed_draft_is_ingested_with_the_developers_edits(self):
        self.client.force_login(self.staff)
        self.client.post(self.url, {"action": "upload", "files": [upload("metformin.txt", METFORMIN)]})
        response = self.client.post(reverse("chat:document_review"),
                                    self.review_data(title="Metformin guide", source_type="fda"), follow=True)
        self.assertRedirects(response, self.url)
        self.assertContains(response, "ingested successfully")
        doc = Document.objects.get()
        self.assertEqual((doc.title, doc.source_type, doc.file_name), ("Metformin guide", "fda", "metformin.txt"))
        self.assertEqual(DocumentChunk.objects.get().source, "Metformin guide")

    def test_preview_on_review_saves_nothing(self):
        self.client.force_login(self.staff)
        self.client.post(self.url, {"action": "upload", "files": [upload("metformin.txt", METFORMIN)]})
        response = self.client.post(reverse("chat:document_review"), self.review_data(action="preview"))
        self.assertContains(response, "Proposed chunks")
        self.assertFalse(Document.objects.exists())

    def test_several_items_are_reviewed_one_after_another(self):
        self.client.force_login(self.staff)
        self.client.post(self.url, {"action": "upload", "files": [
            upload("metformin.txt", METFORMIN), upload("lisinopril.txt", LISINOPRIL)]})
        review = reverse("chat:document_review")
        self.assertContains(self.client.get(review), "Review document 1 of 2")
        response = self.client.post(review, self.review_data())
        self.assertRedirects(response, review)
        response = self.client.get(review)
        self.assertContains(response, "Review document 2 of 2")
        self.assertEqual(response.context["form"]["title"].value(), "lisinopril")
        response = self.client.post(review, {"action": "skip"})
        self.assertRedirects(response, self.url)
        self.assertEqual(list(Document.objects.values_list("title", flat=True)), ["metformin"])

    def test_cancel_drops_the_remaining_items(self):
        self.client.force_login(self.staff)
        self.client.post(self.url, {"action": "upload", "files": [
            upload("metformin.txt", METFORMIN), upload("lisinopril.txt", LISINOPRIL)]})
        response = self.client.post(reverse("chat:document_review"), {"action": "cancel"})
        self.assertRedirects(response, self.url)
        self.assertRedirects(self.client.get(reverse("chat:document_review")), self.url)
        self.assertFalse(Document.objects.exists())

    def test_new_version_of_a_file_is_reviewed_as_a_replacement(self):
        ingest_uploaded_file(upload("metformin.txt", METFORMIN))
        old = Document.objects.get()
        self.client.force_login(self.staff)
        self.client.post(self.url, {"action": "upload", "files": [upload("metformin.txt", METFORMIN + " New warning added.")]})
        response = self.client.get(reverse("chat:document_review"))
        self.assertContains(response, "This replaces")
        self.client.post(reverse("chat:document_review"), self.review_data(content=METFORMIN + " New warning added."))
        self.assertEqual(Document.objects.get().pk, old.pk)
        self.assertIn("New warning", " ".join(DocumentChunk.objects.values_list("text", flat=True)))

    def test_identical_content_is_skipped_without_review(self):
        ingest_uploaded_file(upload("metformin.txt", METFORMIN))
        self.client.force_login(self.staff)
        response = self.client.post(self.url, {"action": "upload", "files": [upload("copy.txt", METFORMIN)]})
        self.assertEqual(response.status_code, 200)  # stays on the page
        self.assertContains(response, "⏭ Skipped")

    def test_pasted_text_opens_the_same_review_form(self):
        self.client.force_login(self.staff)
        response = self.client.post(self.url, {"action": "text", "title": "Lisinopril facts",
                                               "source_url": "https://example.org/l", "text": LISINOPRIL}, follow=True)
        self.assertContains(response, "Review document 1 of 1")
        form = response.context["form"]
        self.assertEqual(form["title"].value(), "Lisinopril facts")
        self.assertEqual(form["source_url"].value(), "https://example.org/l")
        self.assertEqual(form["content"].value(), LISINOPRIL)

    def test_urls_report_each_item(self):
        self.client.force_login(self.staff)
        response = self.client.post(self.url, {"action": "urls", "urls": "http://127.0.0.1/\nftp://x.org/a"})
        self.assertContains(response, "Private or internal network addresses")
        self.assertContains(response, "Only http:// and https://")
        self.assertContains(response, "✗ Error", count=2)