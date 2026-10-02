from django.urls import path

from . import views

urlpatterns = [
    path("startups/lounge/", views.lounge_feed, name="lounge_feed"),
    path("startups/lounge/<int:pk>/", views.post_detail, name="lounge_post"),
    path("startups/lounge/<int:pk>/comment/", views.add_comment, name="lounge_comment"),
    path("startups/lounge/<int:pk>/upvote/", views.toggle_upvote, name="lounge_upvote"),
    path("startups/lounge/<int:pk>/flag/", views.flag_post, name="lounge_flag"),
]
