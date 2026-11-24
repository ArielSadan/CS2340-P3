from django.contrib.auth import views as auth_views
from django.urls import path

from . import views

app_name = "home"

urlpatterns = [
    path("", views.hub, name="hub"),
    path("signup/", views.signup, name="signup"),
    path("login/", auth_views.LoginView.as_view(template_name="home/login.html",
                                                redirect_authenticated_user=True), name="login"),
    path("logout/", auth_views.LogoutView.as_view(next_page="home:hub"), name="logout"),
    path("settings/", views.settings_view, name="settings"),

    path("feed/", views.feed, name="feed"),
    path("fed/", views.fed, name="fed"),

    path("medications/", views.medications, name="medications"),
    path("medications/add/", views.medication_edit, name="medication_add"),
    path("medications/<int:pk>/edit/", views.medication_edit, name="medication_edit"),
    path("medications/<int:pk>/stop/", views.medication_stop, name="medication_stop"),
    path("medications/upload/", views.upload_instructions, name="upload"),
    path("medications/review/", views.review_schedule, name="review"),
    path("medications/review/discard/", views.discard_draft, name="discard_draft"),

    path("share/", views.share, name="share"),
    path("caregiver/<str:token>/", views.caregiver, name="caregiver"),
]