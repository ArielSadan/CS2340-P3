
from django.urls import include, path
from django.views.generic import RedirectView

from . import definition_views, usage_views, views

app_name = "chat"

urlpatterns = [
    path(
        "performance/",
        RedirectView.as_view(
            pattern_name="admin:chat_querylog_changelist",
            query_string=True,
            permanent=False,
        ),
        name="perofrmance",
    ),
    path("usage/", usage_views.usage_logs, name="usage"),
    path("developer/", include("chat.developer_urls")),
    path("", views.chat_page, name="page"),
    path("api/chat/", views.chat_api, name="api"),
    path("api/define/", definition_views.define_term, name="define"),
]
