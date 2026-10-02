from django.urls import path

from . import views

urlpatterns = [
    path("universities/vote/", views.campus_vote, name="campus_vote"),
]
