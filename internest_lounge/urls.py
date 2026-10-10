from django.urls import path

from . import messaging, views

urlpatterns = [
    path("startups/lounge/", views.lounge_feed, name="lounge_feed"),
    path("startups/lounge/<int:pk>/", views.post_detail, name="lounge_post"),
    path("startups/lounge/<int:pk>/comment/", views.add_comment, name="lounge_comment"),
    path("startups/lounge/<int:pk>/upvote/", views.toggle_upvote, name="lounge_upvote"),
    path("startups/lounge/<int:pk>/flag/", views.flag_post, name="lounge_flag"),
    path("startups/lounge/<int:pk>/edit/", views.edit_post, name="lounge_edit"),
    path("startups/lounge/<int:pk>/delete/", views.delete_post, name="lounge_delete"),
    path("startups/messages/", messaging.inbox, name="lounge_inbox"),
    path("startups/messages/<int:partner_id>/", messaging.inbox, name="lounge_thread"),
    path("startups/messages/<int:partner_id>/poll/", messaging.poll, name="lounge_thread_poll"),
]
