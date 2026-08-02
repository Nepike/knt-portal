from django.urls import path

from . import views

urlpatterns = [
    path("chats/", views.chat_list, name="chat_list"),
    path("chats/<int:pk>/", views.chat_detail, name="chat_detail"),
    path("chats/<int:pk>/new/", views.messages_new, name="messages_new"),
    path("chats/<int:pk>/older/", views.messages_older, name="messages_older"),
    path("chats/<int:pk>/send/", views.message_send, name="message_send"),
    path("chats/with/<int:user_id>/", views.dm_start, name="dm_start"),
    path("chats/unread/", views.unread_badge, name="unread_badge"),
    path("chats/search/", views.user_search, name="user_search"),
    path("chats/list/", views.chat_list_fragment, name="chat_list_fragment"),
    path("chats/create-group/", views.chat_create_group, name="chat_create_group"),
    path("chats/<int:pk>/members/add/", views.chat_add_members, name="chat_add_members"),
    path("chats/<int:pk>/rename/", views.chat_rename, name="chat_rename"),
    path("chats/<int:pk>/members/<int:user_id>/remove/", views.chat_remove_member, name="chat_remove_member"),
    path("chats/<int:pk>/leave/", views.chat_leave, name="chat_leave"),
    path("chats/<int:pk>/delete/", views.chat_delete, name="chat_delete"),
    path("chats/messages/<int:pk>/", views.message_card, name="message_card"),
    path("chats/messages/<int:pk>/edit/", views.message_edit, name="message_edit"),
    path("chats/messages/<int:pk>/delete/", views.message_delete, name="message_delete"),
    path("chats/messages/<int:pk>/react/", views.message_react, name="message_react"),
]
