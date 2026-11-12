
from django.db import models


class EvaluationRun(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    model_name = models.CharField(max_length=150, blank=True)
    similarity_threshold = models.FloatField(default=0)
    dataset_chunks = models.PositiveIntegerField(default=0)
    test_set_hash = models.CharField(max_length=64, blank=True)

    total = models.PositiveIntegerField(default=0)
    passed = models.PositiveIntegerField(default=0)
    accuracy = models.FloatField(default=0)
    source_hit_rate = models.FloatField(default=0)
    average_latency_ms = models.FloatField(default=0)

    def __str__(self):
        return f"Run {self.id}: {self.accuracy:.1f}%"


class EvaluationResult(models.Model):
    run = models.ForeignKey(
        EvaluationRun,
        on_delete=models.CASCADE,
        related_name="results",
    )

    case_id = models.CharField(max_length=30)
    question = models.TextField()
    expected_answer = models.TextField(blank=True)
    expected_status = models.CharField(max_length=20)
    expected_sources = models.JSONField(default=list)
    required_facts = models.JSONField(default=list)

    generated_answer = models.TextField(blank=True)
    retrieved_sources = models.JSONField(default=list)
    status = models.CharField(max_length=30)
    latency_ms = models.PositiveIntegerField(default=0)

    source_hit = models.BooleanField(default=False)
    facts_matched = models.BooleanField(default=False)
    passed = models.BooleanField(default=False)
    reason = models.TextField(blank=True)

