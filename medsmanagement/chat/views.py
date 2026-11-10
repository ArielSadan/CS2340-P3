import json

from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from .services.rag import answer_question

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

    message = str(body.get("message", "")).strip()
    if not message:
        return _bad_request("Please type a question first.")
    if len(message) > MAX_QUESTION_CHARS:
        return _bad_request("That question is a bit long. Please try asking it in fewer words.")

    result = answer_question(message)

    # J: session_id and latency_ms are here for the usage-logs story (#12).
    return JsonResponse({
        "session_id": str(body.get("session_id", "")),
        "answer": result.answer,
        "status": result.status,
        "sources": result.sources,
        "latency_ms": result.latency_ms,
    })
