
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from chat.models import Document, DocumentChunk


class KnowledgeMaintenanceTests(TestCase):

    def setUp(self):
        User = get_user_model()

        self.admin = User.objects.create_user(
            username="maintenance_admin",
            password="test-password",
            is_staff=True,
        )
        self.client.force_login(self.admin)

        self.document = Document.objects.create(
            title="Test Medication Guide",
            content="Medication reminders help older adults. " * 20,
            cleaned_content="Medication reminders help older adults. " * 20,
        )

        DocumentChunk.objects.create(
            document=self.document,
            source=self.document.title,
            text="Medication reminder information",
            embedding=b"\x00\x00\x80\x3f",
            position=1,
        )

    def test_clear_removes_chunks_but_preserves_documents(self):
        response = self.client.post(reverse("knowledge:clear"))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(DocumentChunk.objects.count(), 0)
        self.assertEqual(Document.objects.count(), 1)
        self.assertTrue(
            Document.objects.get(pk=self.document.pk).content
        )

    def test_clear_rejects_get_requests(self):
        response = self.client.get(reverse("knowledge:clear"))
        self.assertEqual(response.status_code, 405)

    @patch("knowledge.maintenance.add_chunks")
    def test_reload_uses_existing_ingestion_pipeline(self, mock_add):
        mock_add.return_value = 2

        response = self.client.post(reverse("knowledge:reload"))

        self.assertEqual(response.status_code, 302)
        mock_add.assert_called_once()

        args, kwargs = mock_add.call_args
        self.assertEqual(kwargs["document"], self.document)
        self.assertGreater(len(args[0]), 0)

    def test_non_admin_cannot_clear(self):
        User = get_user_model()
        ordinary_user = User.objects.create_user(
            username="ordinary_user",
            password="test-password",
        )

        self.client.force_login(ordinary_user)

        response = self.client.post(reverse("knowledge:clear"))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(DocumentChunk.objects.count(), 1)
