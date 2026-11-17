import io
import os
import re
from dataclasses import dataclass, field
 
from bs4 import BeautifulSoup
 
from ..conf import get_setting
from .errors import NoReadableText, PrivatePage, UnsupportedFile
 
ALLOWED_EXTENSIONS = {".pdf", ".txt", ".md", ".docx", ".csv", ".json", ".html", ".htm"}

# ---------------------------------------------------------------- pages

LOGIN_WALL_MESSAGE = (
    "This page is private or requires a login. If you have access, "
    "download it and upload it as a file instead."
)
 
# Sections of web pages that are not article content (mostly from PMC).
SKIP_SECTION_EXACT = {"notes", "footnotes", "references", "bibliography", "citations"}
SKIP_SECTION_PREFIXES = (
    "references and notes", "acknowledg", "funding", "conflict of interest",
    "conflicts of interest", "competing interest", "author contributions",
    "supplementary material", "associated data", "similar articles", "cited by",
    "article information", "related articles", "publication types", "mesh terms",
    "data availability", "abbreviations used",
)
 
# Page furniture to remove before reading the text.
REMOVE_TAGS = [
    "script", "style", "noscript", "template", "svg", "iframe", "nav", "header",
    "footer", "aside", "form", "button", "select", "dialog",
]
REMOVE_ROLES = {"navigation", "banner", "contentinfo", "search", "complementary", "dialog"}
NOISE_CLASS = re.compile(
    r"\b(cookie|consent|newsletter|subscribe|share|social|breadcrumbs?|sidebar|"
    r"advert|ads?|promo|skip-link|related)\b",
    re.I,
)
TEXT_TAGS = ["h1", "h2", "h3", "h4", "p", "li", "blockquote", "pre", "td", "dd", "dt", "figcaption"]
SECTION_TAGS = {"h1", "h2", "h3"}
 
 
@dataclass
class Extracted:
    title: str
    sections: list = field(default_factory=list)  # [(heading, text)]
 
    @property
    def word_count(self):
        return sum(len(text.split()) for _, text in self.sections)
 
 
def _pretty_name(filename):
    stem = os.path.splitext(os.path.basename(filename))[0]
    return re.sub(r"[_\-]+", " ", stem).strip() or "Untitled document"
 
 
def _clip(text, limit=300):
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
 
 
# ---------------------------------------------------------------- files
 
def extract_upload(filename, data):
    """Pick the right extractor from the file extension (checked against
    the actual bytes, so a renamed file can't sneak through)."""
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise UnsupportedFile(
            f"Unsupported file type ({ext or 'no extension'}). Use PDF, TXT, MD, DOCX, CSV, JSON or HTML."
        )
    if not data:
        raise NoReadableText("This file is empty.")
    if ext == ".pdf":
        return extract_pdf(data, filename)
    if ext == ".docx":
        return extract_docx(data, filename)
    if ext in (".html", ".htm"):
        extracted = extract_html(_decode_text(data))
        if extracted.title == "Web page":  # page had no <title> or <h1>
            extracted.title = _clip(_pretty_name(filename))
        return extracted
    return extract_text_file(data, filename, markdown=(ext == ".md"))
 
 
def extract_pdf(data, filename="document.pdf"):
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError
 
    if b"%PDF" not in data[:1024]:
        raise UnsupportedFile("This file is named .pdf but isn't a real PDF.")
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                if not reader.decrypt(""):
                    raise UnsupportedFile("This PDF is password-protected.")
            except UnsupportedFile:
                raise
            except Exception:
                raise UnsupportedFile("This PDF is password-protected.")
        sections = []
        for number, page in enumerate(reader.pages, start=1):
            text = page.extract_text() or ""
            if text.split():
                sections.append((f"Page {number}", text))
        meta_title = ""
        try:
            meta_title = (reader.metadata.title or "") if reader.metadata else ""
        except Exception:
            meta_title = ""
    except (PdfReadError, ValueError, KeyError, OSError) as exc:
        raise UnsupportedFile("Couldn't read this PDF (it may be damaged).") from exc
 
    if not sections:
        raise NoReadableText("No readable text found. This may be a scanned PDF (images only).")
    bad_titles = {"", "untitled", "microsoft word"}
    title = meta_title.strip()
    if title.lower() in bad_titles or title.lower().endswith((".doc", ".docx", ".pdf")):
        title = _pretty_name(filename)
    return Extracted(_clip(title), sections)
 
 
def _decode_text(data):
    if b"\x00" in data[:4096]:
        raise UnsupportedFile("This doesn't look like a text file.")
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")
 
 
def extract_text_file(data, filename="document.txt", markdown=False):
    text = _decode_text(data)
    if not markdown:
        if not text.split():
            raise NoReadableText("This file has no text in it.")
        return Extracted(_clip(_pretty_name(filename)), [("", text)])
 
    # Markdown: each heading starts a new section; the first "# Title" names it.
    title, sections, heading, lines = None, [], "", []
    for line in text.splitlines():
        match = re.match(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$", line)
        if match:
            if lines and " ".join(lines).split():
                sections.append((heading, "\n".join(lines)))
            lines = []
            heading = _clip(match.group(2))
            if title is None and len(match.group(1)) == 1:
                title = heading
        else:
            lines.append(line)
    if lines and " ".join(lines).split():
        sections.append((heading, "\n".join(lines)))
    if not sections:
        raise NoReadableText("This file has no text in it.")
    return Extracted(title or _clip(_pretty_name(filename)), sections)
 
 
def extract_docx(data, filename="document.docx"):
    if not data.startswith(b"PK"):
        raise UnsupportedFile("This file is named .docx but isn't a real Word document.")
    try:
        import docx  # python-docx
    except ImportError:
        raise UnsupportedFile(".docx support isn't installed on the server (pip install python-docx).")
    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as exc:
        raise UnsupportedFile("Couldn't read this Word file (it may be damaged).") from exc
 
    title, sections, heading, parts = None, [], "", []
    for paragraph in document.paragraphs:
        text = paragraph.text.strip()
        if not text:
            continue
        style = (paragraph.style.name if paragraph.style is not None else "") or ""
        if style == "Title" or style.startswith("Heading"):
            if parts:
                sections.append((heading, "\n".join(parts)))
                parts = []
            heading = _clip(text)
            if title is None:
                title = heading
        else:
            parts.append(text)
    for table in document.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    if parts:
        sections.append((heading, "\n".join(parts)))
    if not sections:
        raise NoReadableText("This Word file has no text in it.")
    return Extracted(title or _clip(_pretty_name(filename)), sections)
 
 
# ---------------------------------------------------------------- web pages
 
def _make_soup(html, encoding=None):
    for parser in ("lxml", "html.parser"):
        try:
            if isinstance(html, bytes):
                return BeautifulSoup(html, parser, from_encoding=encoding)
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    raise NoReadableText("Couldn't read this web page.")
 
 
def _page_title(soup):
    og = soup.find("meta", attrs={"property": "og:title"})
    if og and og.get("content", "").strip():
        return og["content"]
    citation = soup.find("meta", attrs={"name": "citation_title"})  # PMC and journals
    if citation and citation.get("content", "").strip():
        return citation["content"]
    if soup.title and soup.title.get_text(strip=True):
        return soup.title.get_text(" ", strip=True)
    h1 = soup.find("h1")
    return h1.get_text(" ", strip=True) if h1 else ""
 
 
def _remove_noise(soup):
    for tag in soup.find_all(REMOVE_TAGS):
        tag.decompose()
    for tag in soup.find_all(attrs={"role": True}):
        if tag.attrs is not None and str(tag.get("role", "")).lower() in REMOVE_ROLES:
            tag.decompose()
    for tag in soup.find_all(True):
        if tag.attrs is None or tag.name in ("html", "body", "main", "article"):
            continue
        labels = " ".join(tag.get("class") or []) + " " + str(tag.get("id") or "")
        if labels.strip() and NOISE_CLASS.search(labels):
            tag.decompose()
 
 
def _words(tag):
    return len(tag.get_text(" ", strip=True).split())
 
 
def _content_root(soup):
    """Prefer the <article>/<main> with the most text; fall back to <body>."""
    body = soup.body or soup
    candidates = soup.find_all(["article", "main"])
    if candidates:
        best = max(candidates, key=_words)
        if _words(best) >= 100 or _words(best) >= 0.5 * _words(body):
            return best
    return body
 
 
def _is_skipped_heading(heading):
    h = heading.lower().strip(" :.0123456789")
    return h in SKIP_SECTION_EXACT or any(h.startswith(prefix) for prefix in SKIP_SECTION_PREFIXES)
 
 
def extract_html(html, url="", encoding=None):
    soup = _make_soup(html, encoding)
    title = _page_title(soup)
    has_password_field = soup.find("input", attrs={"type": re.compile(r"^password$", re.I)}) is not None
 
    _remove_noise(soup)
    root = _content_root(soup)
 
    sections, heading, parts, skipping = [], "", [], False
    for el in root.find_all(TEXT_TAGS):
        # Skip elements nested in another text element (avoids duplicates).
        parent = el.find_parent(TEXT_TAGS)
        if parent is not None and root in parent.parents:
            continue
        text = el.get_text(" ", strip=True)
        if not text:
            continue
        if el.name in SECTION_TAGS:
            if parts and not skipping:
                sections.append((heading, "\n".join(parts)))
            parts = []
            heading = _clip(text)
            skipping = _is_skipped_heading(heading)
            continue
        if not skipping:
            parts.append(text)
    if parts and not skipping:
        sections.append((heading, "\n".join(parts)))
 
    extracted = Extracted(_clip(title or url or "Web page"), sections)
    words = extracted.word_count
 
    # A sign-in form with almost no other text = a login wall.
    if has_password_field and words < 150:
        raise PrivatePage(LOGIN_WALL_MESSAGE)
    if words < get_setting("MIN_DOCUMENT_WORDS"):
        raise NoReadableText("Couldn't find the main article text on this page.")
    return extracted
 
 
def extract_fetched(page):
    """Route a downloaded page (see fetch.py) to the right extractor."""
    ctype = page.content_type
    body = page.body
    if ctype == "application/pdf" or body[:1024].lstrip().startswith(b"%PDF"):
        name = os.path.basename(page.url.split("?")[0]) or "document.pdf"
        return extract_pdf(body, name)
    looks_html = b"<html" in body[:2048].lower() or b"<!doctype html" in body[:2048].lower()
    if ctype in ("text/html", "application/xhtml+xml") or (not ctype and looks_html):
        return extract_html(body, page.url, page.encoding)
    if ctype in ("text/plain", "text/markdown"):
        name = os.path.basename(page.url.split("?")[0]) or "page.txt"
        return extract_text_file(body, name, markdown=ctype == "text/markdown")
    raise UnsupportedFile(f"This link points to a file type we can't read ({ctype or 'unknown'}).")