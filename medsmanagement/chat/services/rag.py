# Question -> retrieve -> off-topic check -> Gemini -> answer
#Every path returns a ChatResult; this function never raises, so the view
#can always hand the user a friendly message.

import logging
import re
import time
from dataclasses import dataclass, field

from django.conf import settings
from google.genai import types

from chat.models import PromptStyle

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
    response_style: str = ""


def off_topic_message() -> str:
    return grounding.not_in_sources_message(
        settings.BOT_TOPIC_DESCRIPTION, settings.BOT_EXAMPLE_QUESTION
    )


# Check detailed requests first, then explicit short answers, then explanations.
INTENT_PHRASES = {
    "details": (
        "in detail", "in details", "more detail", "more details", "tell me more",
        "break it down", "break down", "go into detail", "go into details",
        "going into details", "detailed explanation", "detailed answer", "step by step",
        "in depth", "full explanation", "full details", "all the details",
        "more information", "more info", "walk me through", "explain further",
        "elaborate", "expand on", "give me examples", "with examples",
        "be specific", "thorough explanation",
    ),
    "quick": (
        "quick answer", "briefly", "keep it short", "short answer",
        "in a few words", "in one sentence", "short version", "brief answer",
        "brief summary", "quick summary", "summarize", "sum it up",
        "to the point", "just the basics", "main point", "key points",
        "short and simple", "a few sentences", "tldr", "tl dr",
    ),
    "explain": (
        "explain", "explanation", "why", "how does", "how do",
        "help me understand", "with sources", "clarify", "how can", "how should",
        "how would", "how is", "how are", "what does this mean",
        "what does that mean", "in simple terms", "in plain english",
        "help me make sense", "which source", "where does it say",
        "according to the article", "according to the document",
    ),
}

STYLE_LABELS = {
    "quick": "Quick Answer",
    "explain": "Explain with Sources",
    "details": "Going Into Details",
}

PROMPT_TEMPLATES = {
    "quick": (
        "Answer the question directly in 1 to 3 short sentences. "
        "Focus on the main point and leave out extra background."
    ),
    "explain": (
        "Give a short explanation in plain language. Explain the reason or connection "
        "the user is asking about. Mention the supporting source titles when helpful."
    ),
    "details": (
        "Give a fuller explanation in short, readable paragraphs. Include relevant "
        "details and use numbered steps or examples only when the context supports them. "
        "Keep the answer short if the context contains little information; do not pad it."
    ),
}


def get_prompt_styles():
    styles = {
        code: {"phrases": list(INTENT_PHRASES[code]), "instructions": PROMPT_TEMPLATES[code]}
        for code in STYLE_LABELS
    }
    for saved in PromptStyle.objects.filter(code__in=STYLE_LABELS):
        styles[saved.code] = {
            "phrases": saved.phrases.splitlines(),
            "instructions": saved.instructions,
        }
    return styles


def detect_intent(question: str, styles=None) -> str:
    styles = get_prompt_styles() if styles is None else styles
    words = " " + " ".join(re.findall(r"\w+", question.lower())) + " "
    for intent in INTENT_PHRASES:
        for phrase in styles[intent]["phrases"]:
            phrase = " ".join(re.findall(r"\w+", phrase.lower()))
            if phrase and " " + phrase + " " in words:
                return intent
    return "quick"


def build_prompt(question: str, chunks: list[Chunk], styles=None) -> str:
    styles = get_prompt_styles() if styles is None else styles
    intent = detect_intent(question, styles)
    context = grounding.format_context(chunks)
    rules = (
        "Use ONLY the provided context for every response style. "
        "Cite each factual sentence with its numbered excerpt, such as [1]. "
        f"If the context does not contain the answer, reply with exactly {NO_ANSWER_TOKEN} "
        "and nothing else."
    )
    return (
        f"Response style: {STYLE_LABELS[intent]}.\n{styles[intent]['instructions']}\n\n"
        f"{rules}\n\nContext:\n{context}\n\nQuestion: {question}"
    )


def _unique_sources(chunks: list[Chunk]) -> list[str]:
    seen = set()
    sources = []

    for chunk in chunks:
        title = chunk.source.strip()
        url = chunk.source_url.strip()
        identifier = (title, url)

        if identifier not in seen:
            seen.add(identifier)
            sources.append({
                "title": title,
                "url": url
            })

    return sources


def answer_question(question: str) -> ChatResult:
    start = time.monotonic()
    intent = "quick"

    def finish(answer, status, sources=None, reason=""):
        return ChatResult(
            answer=answer, status=status, sources=sources or [],
            latency_ms=int((time.monotonic() - start) * 1000), reason=reason,
            response_style=STYLE_LABELS[intent] if status == "ok" else "",
        )

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
        styles = get_prompt_styles()
        intent = detect_intent(question, styles)
        response = get_client().models.generate_content(
            model=settings.GEMINI_CHAT_MODEL,
            contents=build_prompt(question, relevant, styles),
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
