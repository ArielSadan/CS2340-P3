from django.urls import path
 
from . import views
 
app_name = "knowledge"
 
urlpatterns = [
    path("knowledge/", views.developer_knowledge, name="developer"),
]