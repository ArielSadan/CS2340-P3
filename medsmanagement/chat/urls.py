from django.urls import include, path

from . import views

app_name = "chat"

urlpatterns = [
    path("developer/", include("chat.developer_urls")),
    path("", views.chat_page, name="page"),
    path("api/chat/", views.chat_api, name="api"),
]
