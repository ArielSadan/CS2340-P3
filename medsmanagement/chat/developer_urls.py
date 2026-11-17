from django.urls import path
from django.views.generic import RedirectView

from . import developer_views

urlpatterns = [
    # The document list lives on the knowledge Developer page now.
    path("", RedirectView.as_view(pattern_name="knowledge:developer")),
    path("documents/add/", developer_views.document_edit, name="document_add"),
    path("documents/review/", developer_views.document_review, name="document_review"),
    path("documents/<int:pk>/preview/", developer_views.document_preview, name="document_preview"),
    path("documents/<int:pk>/re-ingest/", developer_views.document_edit, name="document_reingest"),
    path("documents/<int:pk>/delete/", developer_views.document_delete, name="document_delete"),
]
