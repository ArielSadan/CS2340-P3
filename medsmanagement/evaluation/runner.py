
import hashlib
import json
import logging
import re
from pathlib import Path

from django.conf import settings

from chat.models import DocumentChunk
from chat.services.rag import answer_question
from .models import EvaluationRun, EvaluationResult

logger = logging.getLogger(__name__)

DEFAULT_TEST_FILE = (
    Path(settings.BASE_DIR)
    / "evaluation"
    / "golden_questions.json"
)

# how soring works: test passes when the it returns a successful answer, cites at least one source and includes all facts. For decline questions, the chatbot must return off_topic with no sources.
def normalize(text):
    """Normalize text for matching sources and key facts."""
    return " ".join(
        re.findall(r"[a-z0-9]+", str(text).casefold())
    )


def contains_fact(answer, phrase):
    """Match a complete phrase, ignoring case and punctuation."""
    answer = f" {normalize(answer)} "
    phrase = f" {normalize(phrase)} "
    return bool(normalize(phrase)) and phrase in answer


def load_cases(file_path=DEFAULT_TEST_FILE):
    """Load and validate the golden evaluation dataset."""
    with open(file_path, encoding="utf-8") as file:
        cases = json.load(file)

    if not isinstance(cases, list):
        raise ValueError("Test file must contain a JSON list.")

    if len(cases) < 20:
        raise ValueError("At least 20 test questions are required.")

    ids = set()
    decline_count = 0

    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("Each test case must be an object.")

        case_id = case.get("id")
        question = case.get("question")
        expected_status = case.get("expected_status")

        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError("Each case needs a valid ID.")

        if case_id in ids:
            raise ValueError(f"Duplicate test ID: {case_id}")

        ids.add(case_id)

        if not isinstance(question, str) or not question.strip():
            raise ValueError(f"{case_id}: Missing question.")

        if expected_status not in ("ok", "off_topic"):
            raise ValueError(
                f"{case_id}: Expected status must be ok or off_topic."
            )

        sources = case.get("expected_sources", [])
        if not isinstance(sources, list) or any(
            not isinstance(source, str) or not source.strip()
            for source in sources
        ):
            raise ValueError(f"{case_id}: Invalid expected sources.")

        if expected_status == "off_topic":
            decline_count += 1
            if sources:
                raise ValueError(
                    f"{case_id}: Decline cases should not expect sources."
                )
        else:
            if not sources:
                raise ValueError(
                    f"{case_id}: At least one expected source is required."
                )

            facts = case.get("required_facts")
            if not isinstance(facts, list) or not facts:
                raise ValueError(
                    f"{case_id}: Required facts are missing."
                )

            for alternatives in facts:
                if (
                    not isinstance(alternatives, list)
                    or not alternatives
                    or any(
                        not isinstance(fact, str) or not fact.strip()
                        for fact in alternatives
                    )
                ):
                    raise ValueError(
                        f"{case_id}: Each fact must have valid alternatives."
                    )

    if decline_count < 5:
        raise ValueError(
            "At least 5 questions must expect a decline."
        )

    return cases


def score_response(case, response):
    """Score one answer using expected facts and source titles."""
    expected_status = case["expected_status"]

    actual_sources = [
        source.get("title", "")
        for source in response.sources
        if isinstance(source, dict)
    ]

    expected_sources = case.get("expected_sources", [])

    source_hit = bool(
        {normalize(s) for s in actual_sources}
        & {normalize(s) for s in expected_sources}
    )

    if expected_status == "off_topic":
        passed = (
            response.status == "off_topic"
            and not actual_sources
        )
        return {
            "passed": passed,
            "source_hit": False,
            "facts_matched": passed,
            "reason": (
                "Correctly declined"
                if passed else "Expected decline, but chatbot did not decline"
            ),
        }

    required_facts = case["required_facts"]

    facts_matched = all(
        any(contains_fact(response.answer, phrase)
            for phrase in alternatives)
        for alternatives in required_facts
    )

    passed = (
        response.status == "ok"
        and source_hit
        and facts_matched
    )

    failures = []

    if response.status != "ok":
        failures.append(f"Unexpected status: {response.status}")
    if not source_hit:
        failures.append("Expected source not retrieved")
    if not facts_matched:
        failures.append("Required answer facts missing")

    return {
        "passed": passed,
        "source_hit": source_hit,
        "facts_matched": facts_matched,
        "reason": "Passed" if passed else "; ".join(failures),
    }


def run_evaluation(file_path=DEFAULT_TEST_FILE):
    """Run all golden questions through the real chatbot."""
    cases = load_cases(file_path)

    chunk_count = DocumentChunk.objects.count()

    if chunk_count == 0:
        raise ValueError(
            "Knowledge base is empty. Ingest documents first."
        )

    file_bytes = Path(file_path).read_bytes()
    test_hash = hashlib.sha256(file_bytes).hexdigest()

    run = EvaluationRun.objects.create(
        total=len(cases),
        model_name=settings.GEMINI_CHAT_MODEL,
        similarity_threshold=settings.RAG_MIN_SIMILARITY,
        dataset_chunks=chunk_count,
        test_set_hash=test_hash,
    )

    total_latency = 0
    source_hits = 0
    on_topic_count = 0
    passed_count = 0

    for case in cases:
        try:
            response = answer_question(case["question"])
            score = score_response(case, response)

            EvaluationResult.objects.create(
                run=run,
                case_id=case["id"],
                question=case["question"],
                expected_answer=case.get("expected_answer", ""),
                expected_status=case["expected_status"],
                expected_sources=case.get("expected_sources", []),
                required_facts=case.get("required_facts", []),
                generated_answer=response.answer,
                retrieved_sources=response.sources,
                status=response.status,
                latency_ms=response.latency_ms,
                source_hit=score["source_hit"],
                facts_matched=score["facts_matched"],
                passed=score["passed"],
                reason=score["reason"],
            )

            passed_count += int(score["passed"])
            total_latency += response.latency_ms

            if case["expected_status"] == "ok":
                on_topic_count += 1
                source_hits += int(score["source_hit"])

        except Exception:
            logger.exception("Evaluation failed for %s", case["id"])
            EvaluationResult.objects.create(
                run=run,
                case_id=case["id"],
                question=case["question"],
                expected_answer=case.get("expected_answer", ""),
                expected_status=case["expected_status"],
                expected_sources=case.get("expected_sources", []),
                required_facts=case.get("required_facts", []),
                generated_answer="",
                retrieved_sources=[],
                status="error",
                reason="Unexpected evaluation error",
            )

    run.passed = passed_count
    run.accuracy = (passed_count / len(cases)) * 100
    run.source_hit_rate = (
        (source_hits / on_topic_count) * 100
        if on_topic_count else 0
    )
    run.average_latency_ms = total_latency / len(cases)

    run.save()

    return run
