import json
import logging
import time

from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from .models import QueryLog
from .services.rag import ERROR_MESSAGE, ChatResult, answer_question

logger = logging.getLogger(__name__)

MAX_QUESTION_CHARS = 1000

def _bad_request(message):
    return JsonResponse({"status": "error", "answer": message, "sources": []}, status=400)


@require_GET
@ensure_csrf_cookie  # J: sets the csrftoken cookie that chat.js sends back
def chat_page(request):
    return render(request, "chat/index.html", {
        "topic": settings.BOT_TOPIC_DESCRIPTION,
        "example_question": settings.BOT_EXAMPLE_QUESTION,
    })


@require_POST
def chat_api(request):
    try:
        body = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return _bad_request("Something went wrong sending your question. Please try again.")

    if not isinstance(body, dict):
        return _bad_request("Invalid request format. Please send a JSON object.")

    message = str(body.get("message", "")).strip()
    if not message:
        return _bad_request("Please type a question first.")
    if len(message) > MAX_QUESTION_CHARS:
        return _bad_request("That question is a bit long. Please try asking it in fewer words.")

    started = time.monotonic()

    try:
        result = answer_question(message)
    except Exception:
        logger.exception("Unexpected chatbot failure")
        result = ChatResult(
            answer=ERROR_MESSAGE,
            status="error"
        )

    latency_ms = max(0, round((time.monotonic() - started) * 1000))

    try:
        QueryLog.objects.create(
            status=result.status,
            latency_ms=latency_ms,
        )
    except Exception:
        logger.exception("Failed to record chatbot performance")

    # J: session_id and latency_ms are here for the usage-logs story (#12).
    return JsonResponse({
        "session_id": str(body.get("session_id", "")),
        "answer": result.answer,
        "response_style": result.response_style,
        "status": result.status,
        "sources": result.sources,
        "latency_ms": result.latency_ms,
    })
