"""Split cleaned text into overlapping chunks at sentence boundaries."""
 
import re
from dataclasses import dataclass
 
from .conf import get_setting
 
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[\"'(\[A-Z0-9])")
 
 
@dataclass
class TextChunk:
    section: str
    text: str
 
    def text_for_embedding(self, title):
        # Prefixing the title and section tells the embedding model what the
        # chunk is about ("Metformin guide — Side effects: ...").
        label = f"{title} — {self.section}" if self.section else title
        return f"{label}: {self.text}"
 
 
def split_sentences(text):
    text = " ".join(text.split())  # collapse whitespace and newlines
    if not text:
        return []
    return [s for s in _SENTENCE_END.split(text) if s]
 
 
def _word_count(text):
    return len(text.split())
 
 
def chunk_section(text, section="", max_words=None, overlap_words=None):
    """Chunk one section's text. Sentences are kept whole where possible;
    a sentence longer than max_words is hard-split by words."""
    max_words = max_words or get_setting("CHUNK_MAX_WORDS")
    overlap_words = get_setting("CHUNK_OVERLAP_WORDS") if overlap_words is None else overlap_words
 
    pieces = []
    for sentence in split_sentences(text):
        words = sentence.split()
        if len(words) <= max_words:
            pieces.append(sentence)
        else:
            for i in range(0, len(words), max_words):
                pieces.append(" ".join(words[i:i + max_words]))
 
    chunks, current, current_words = [], [], 0
    for piece in pieces:
        n = _word_count(piece)
        if current and current_words + n > max_words:
            chunks.append(TextChunk(section, " ".join(current)))
            # carry the last sentences over as overlap
            overlap, kept = [], 0
            for prev in reversed(current):
                w = _word_count(prev)
                if kept + w > overlap_words:
                    break
                overlap.insert(0, prev)
                kept += w
            current, current_words = overlap, kept
        current.append(piece)
        current_words += n
    if current:
        chunks.append(TextChunk(section, " ".join(current)))
    return chunks
 
 
def chunk_sections(sections, **kwargs):
    """sections: list of (heading, text). Returns a flat list of TextChunk."""
    result = []
    for heading, text in sections:
        result.extend(chunk_section(text, heading, **kwargs))
    return result
 