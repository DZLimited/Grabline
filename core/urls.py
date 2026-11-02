from django.urls import path
from . import views

urlpatterns = [
    path("", views.index, name="index"),
    path("api/resolve", views.resolve_link, name="resolve_link"),
    path("api/download", views.download_proxy, name="download_proxy"),
]
