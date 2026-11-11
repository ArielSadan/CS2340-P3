# Question -> retrieve -> off-topic check -> Gemini -> answer
#Every path returns a ChatResult; this function never raises, so the view
#can always hand the user a friendly message.

import logging
import time
from dataclasses import dataclass, field

from django.conf import settings
from google.genai import types

from . import grounding
from .gemini import get_client
from .retrieval import Chunk, RetrievalError, retrieve_chunks

logger = logging.getLogger(__name__)

NO_ANSWER_TOKEN = grounding.NO_ANSWER_TOKEN

ERROR_MESSAGE = (
    "I couldn't get an answer just now. Please wait a moment and press Send again. "
    "Your question is still in the box."
)

SYSTEM_INSTRUCTION = f"""You are a patient, friendly assistant helping older adults.
Answer ONLY using the information in the provided context.
If the context does not contain the answer, reply with exactly: {NO_ANSWER_TOKEN}
Write in plain, simple English. Use short sentences and short paragraphs.
Do not use markdown symbols such as *, #, or tables. Numbered steps are fine.
Never give medical, legal, or financial diagnoses; suggest talking to a professional when appropriate."""


@dataclass
class ChatResult:
    answer: str
    status: str  # "ok" | "off_topic" | "error"
    sources: list = field(default_factory=list)
    latency_ms: int = 0
    reason: str = ""


def off_topic_message() -> str:
    return grounding.not_in_sources_message(
        settings.BOT_TOPIC_DESCRIPTION, settings.BOT_EXAMPLE_QUESTION
    )


def build_prompt(question: str, chunks: list[Chunk]) -> str:
    #J: Story #8 (dynamic prompts) plugs in here: pick a template by question intent.
    context = grounding.format_context(chunks)
    return f"Context:\n{context}\n\nQuestion: {question}"


def _unique_sources(chunks: list[Chunk]) -> list[str]:
    seen, out = set(), []
    for c in chunks:
        if c.source not in seen:
            seen.add(c.source)
            out.append(c.source)
    return out


def answer_question(question: str) -> ChatResult:
    start = time.monotonic()

    def finish(answer, status, sources=None, reason=""):
        return ChatResult(answer, status, sources or [], int((time.monotonic() - start) * 1000), reason)

    # 1. Retrieve
    try:
        chunks = retrieve_chunks(question, settings.RAG_TOP_K)
    except RetrievalError:
        logger.exception("Retrieval failed")
        return finish(ERROR_MESSAGE, "error", reason="retrieval_error")

    # 2. Off-topic gate: nothing close enough -> don't call the LLM at all
    relevant = [c for c in chunks if c.score >= settings.RAG_MIN_SIMILARITY]
    if not relevant:
        logger.info("Off-topic (best score %.3f)", chunks[0].score if chunks else -1)
        return finish(off_topic_message(), "off_topic", reason="below_threshold" if chunks else "no_sources")

    # 3. Generate
    try:
        response = get_client().models.generate_content(
            model=settings.GEMINI_CHAT_MODEL,
            contents=build_prompt(question, relevant),
            config=types.GenerateContentConfig(
                system_instruction=grounding.with_grounding_rules(SYSTEM_INSTRUCTION),
                temperature=0.2,
            ),
        )
        text = (response.text or "").strip()
    except Exception:
        logger.exception("Gemini call failed")
        return finish(ERROR_MESSAGE, "error", reason="llm_error")

    if not text:
        return finish(ERROR_MESSAGE, "error", reason="empty_reply")

    check = grounding.check_answer(
        text, len(relevant), 
        require_citations=getattr(settings, "GROUNDING_REQUIRE_CITATIONS", True),
        )

    if not check.ok:
        logger.info("Not grounded (%s)", check.reason)
        return finish(off_topic_message(), "off_topic", reason=check.reason)
    
    used = [relevant[n - 1] for n in check.cited] or relevant
    return finish(grounding.add_disclaimer(check.text), "ok", _unique_sources(used), reason="answered")
