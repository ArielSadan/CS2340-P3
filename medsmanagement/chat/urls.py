from django.urls import path

from . import views

app_name = "chat"

urlpatterns = [
    path("", views.chat_page, name="page"),
    path("api/chat/", views.chat_api, name="api"),
]
