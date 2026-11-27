
from django.urls import path
from . import views

app_name = "knowledge"

urlpatterns = [
    path("knowledge/",views.developer_knowledge,name="developer"),
    path("knowledge/clear/",views.clear_knowledge,name="clear"),
    path("knowledge/reload/",views.reload_knowledge,name="reload"),
]
