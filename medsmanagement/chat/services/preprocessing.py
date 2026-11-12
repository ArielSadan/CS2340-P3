"""Local text cleaning and paragraph-aware chunking. No API calls are made here."""
import codecs
import html
from html.parser import HTMLParser
import re
import unicodedata

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


DROP_TAGS = {"head", "script", "style", "nav", "footer", "form", "button", "iframe", "noscript", "template", "svg", "canvas"}
BLOCK_TAGS = {"p", "div", "section", "article", "main", "aside", "header", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr", "blockquote"}
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
NOISE_LABELS = {"ad", "ads", "advert", "advertisement", "advertising", "sponsored", "cookie", "cookies", "breadcrumb", "breadcrumbs", "navigation", "navbar", "social", "share", "sharing", "sidebar", "menu", "subscribe", "newsletter", "footer"}
BOILERPLATE_LINES = {"home", "menu", "skip to content", "privacy policy", "terms of use", "cookie policy", "advertisement", "advertisements", "subscribe to our newsletter"}


class ArticleTextParser(HTMLParser):
    """Skip common page furniture while keeping article headings and paragraphs."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []  # (tag, should_skip) for each open element
        self.parts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        labels = re.split(r"[\s_-]+", ((attrs.get("id") or "") + " " + (attrs.get("class") or "")).lower())
        style = re.sub(r"\s+", "", (attrs.get("style") or "").lower())
        skip = bool(self.stack and self.stack[-1][1])
        skip = skip or tag in DROP_TAGS or bool(NOISE_LABELS.intersection(labels))
        skip = skip or (attrs.get("role") or "").lower() in {"navigation", "contentinfo", "banner"}
        skip = skip or "hidden" in attrs or (attrs.get("aria-hidden") or "").lower() == "true"
        skip = skip or "display:none" in style or "visibility:hidden" in style
        if tag == "header" and not any(name in {"article", "main"} for name, _ in self.stack):
            skip = True
        if not skip:
            if tag in BLOCK_TAGS or tag == "hr":
                self.parts.append("\n\n")
            elif tag == "br":
                self.parts.append("\n")
            elif tag in {"td", "th"}:
                self.parts.append(" ")
        if tag not in VOID_TAGS:
            self.stack.append((tag, skip))

    def handle_endtag(self, tag):
        # Pop to the matching tag so a mismatched closing tag cannot expose
        # text inside an ignored script, ad, or navigation element.
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                skip = self.stack[index][1]
                del self.stack[index:]
                if tag in BLOCK_TAGS and not skip:
                    self.parts.append("\n\n")
                break

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_data(self, data):
        if not self.stack or not self.stack[-1][1]:
            self.parts.append(data)


def decode_text(data):
    """Read UTF-8/BOM, UTF-16/BOM, or older Windows-1252 text uploads."""
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252")


def clean_text(text):
    # Parse real HTML before decoding entities inside scripts. Otherwise an
    # encoded closing script tag could accidentally expose tracking code.
    if not re.search(r"</?[a-zA-Z][^>]*>", text):
        text = html.unescape(text)
    if re.search(r"</?[a-zA-Z][^>]*>", text):
        parser = ArticleTextParser()
        parser.feed(text)
        parser.close()
        text = "".join(parser.parts)

    text = unicodedata.normalize("NFC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(char for char in text if char in "\n\t" or unicodedata.category(char) != "Cc")
    for char in ["\ufeff", "\u200b", "\u2060"]:
        text = text.replace(char, "")

    lines = []
    for line in text.split("\n"):
        line = re.sub(r"\s+", " ", line).strip()
        lines.append("" if line.lower() in BOILERPLATE_LINES else line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def chunk_settings():
    size = settings.RAG_CHUNK_SIZE_CHARS
    overlap = settings.RAG_CHUNK_OVERLAP_CHARS
    minimum = settings.RAG_MIN_CHUNK_CHARS
    if not all(isinstance(value, int) for value in [size, overlap, minimum]):
        raise ImproperlyConfigured("Chunk size, overlap, and minimum must be integers.")
    if not 1 <= minimum <= size or not 0 <= overlap < size:
        raise ImproperlyConfigured("Require 1 <= minimum <= chunk size and 0 <= overlap < chunk size.")
    return size, overlap, minimum


def split_text(text):
    """Prefer paragraph/heading boundaries, then line or word boundaries."""
    size, overlap, minimum = chunk_settings()
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            # Prefer boundaries in the last 20% of the target-sized chunk.
            floor = start + size * 4 // 5
            for separator in ["\n\n", "\n", " "]:
                boundary = text.rfind(separator, floor, end)
                if boundary > start:
                    if separator == "\n\n":
                        previous = text.rfind("\n\n", floor, boundary)
                        heading = text[previous + 2:boundary].strip()
                        # Keep a short heading with the following paragraph.
                        if previous >= floor and len(heading) <= 100 and "\n" not in heading and not heading.endswith((".", "?", "!")):
                            boundary = previous
                    end = boundary
                    break
            # Keep a short final warning/sentence with this chunk rather than
            # discarding it as a near-empty tail. At most minimum extra chars.
            if len(text) - end < minimum:
                end = len(text)
        chunk = text[start:end].strip()
        if len(chunk) >= minimum and any(char.isalnum() for char in chunk):
            chunks.append(chunk)
        if end == len(text):
            break
        next_start = max(start + 1, end - overlap)
        while next_start < end and not text[next_start - 1].isspace():
            next_start += 1
        start = next_start
    return chunks


def preprocess_text(text):
    cleaned = clean_text(text)
    return cleaned, split_text(cleaned)
