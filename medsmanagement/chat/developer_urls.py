from django.urls import path

from . import developer_views

urlpatterns = [
    path("", developer_views.documents, name="documents"),
    path("documents/add/", developer_views.document_edit, name="document_add"),
    path("documents/<int:pk>/preview/", developer_views.document_preview, name="document_preview"),
    path("documents/<int:pk>/re-ingest/", developer_views.document_edit, name="document_reingest"),
    path("documents/<int:pk>/delete/", developer_views.document_delete, name="document_delete"),
]
