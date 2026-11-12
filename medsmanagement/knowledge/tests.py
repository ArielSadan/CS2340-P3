import hashlib
import io
import re
from unittest import mock
 
import requests
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
 
from chat.models import DocumentChunk
from chat.services import retrieval
from knowledge.chunking import chunk_section, chunk_sections, split_sentences
from knowledge.ingestion import extract, fetch
from knowledge.ingestion.errors import (
    BlockedByHost, FetchFailed, InvalidURL, NoReadableText, PrivatePage, UnsafeURL, UnsupportedFile,
)
from knowledge.ingestion.fetch import FetchedPage
from knowledge.ingestion.sources import ingest_uploaded_file, ingest_url
from knowledge.models import Document
 
 
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
 
 
# ------------------------------------------------------------ chunking
 
class ChunkingTests(SimpleTestCase):
    def test_splits_sentences(self):
        self.assertEqual(split_sentences("One. Two!  Three?"), ["One.", "Two!", "Three?"])
 
    def test_chunks_respect_size_and_overlap(self):
        text = " ".join(f"Sentence number {i} has six words." for i in range(100))
        chunks = chunk_section(text, "Body", max_words=60, overlap_words=12)
        self.assertGreater(len(chunks), 5)
        for c in chunks:
            self.assertLessEqual(len(c.text.split()), 60)
            self.assertEqual(c.section, "Body")
        # overlap: the last sentence of one chunk is repeated at the start of the next
        last_sentence = split_sentences(chunks[0].text)[-1]
        self.assertIn(last_sentence, chunks[1].text[:80])
 
    def test_very_long_sentence_is_hard_split(self):
        chunks = chunk_section("word " * 700, max_words=300, overlap_words=0)
        self.assertEqual([len(c.text.split()) for c in chunks], [300, 300, 100])
 
    def test_sections_keep_headings(self):
        chunks = chunk_sections([("Uses", "Lowers sugar."), ("Side effects", "Upset stomach.")])
        self.assertEqual([c.section for c in chunks], ["Uses", "Side effects"])
        self.assertEqual(chunks[1].text_for_embedding("Metformin"), "Metformin — Side effects: Upset stomach.")
 
 
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
        self.assertEqual(doc.source_key, "file:metformin_guide.txt")
        chunk = DocumentChunk.objects.get()
        self.assertEqual(chunk.document, doc)
        self.assertEqual(chunk.source, "metformin guide")  # readable title, not a file path
        self.assertTrue(chunk.text.startswith("metformin guide: "))
 
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
        self.assertEqual(doc.source_key, self.URL)  # normalized
        self.assertEqual(doc.source_url, self.URL)
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
 
    def test_upload_reports_each_item(self):
        self.client.force_login(self.staff)
        response = self.client.post(self.url, {
            "action": "upload",
            "files": [upload("metformin.txt", METFORMIN), SimpleUploadedFile("scan.exe", b"MZ")],
        })
        self.assertContains(response, "✓ Added")
        self.assertContains(response, "Unsupported file type (.exe)")
        self.assertContains(response, "In the knowledge base (1 document)")
 
    def test_urls_report_each_item(self):
        self.client.force_login(self.staff)
        response = self.client.post(self.url, {"action": "urls", "urls": "http://127.0.0.1/\nftp://x.org/a"})
        self.assertContains(response, "Private or internal network addresses")
        self.assertContains(response, "Only http:// and https://")
        self.assertContains(response, "✗ Error", count=2)