
from django.urls import path
from . import views

app_name = "evaluation"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("runs/<int:run_id>/", views.dashboard, name="run_detail"),
]
