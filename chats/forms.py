from django import forms
from django.db.models import Q

from core.widgets import AccentSelectMultiple
from users.models import User


class MemberField(forms.ModelMultipleChoiceField):
    def label_from_instance(self, user):
        return f"{user.name} {user.surname}"


class GroupChatForm(forms.Form):
    title = forms.CharField(label="Название группы", max_length=100)
    members = MemberField(label="Участники", queryset=User.objects.none(), widget=AccentSelectMultiple(search=True))

    def __init__(self, *args, creator=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["members"].queryset = (
            User.objects.filter(is_active=True).exclude(pk=creator.pk).order_by("surname", "name")
        )


class AddMembersForm(forms.Form):
    members = MemberField(label="Кого добавить", queryset=User.objects.none(), widget=AccentSelectMultiple(search=True))

    def __init__(self, *args, chat=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["members"].queryset = (
            User.objects.filter(is_active=True)
            .exclude(chat_memberships__chat=chat)
            .order_by("surname", "name")
        )


class CuratorAddForm(forms.Form):
    """В чат учебной группы можно добавить только людей с правом куратора."""

    members = MemberField(label="Добавить куратора", queryset=User.objects.none(), widget=AccentSelectMultiple(search=True))

    def __init__(self, *args, chat=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["members"].queryset = (
            User.objects.filter(is_active=True)
            .filter(
                Q(is_superuser=True)
                | Q(groups__permissions__codename="curate_team_chats")
                | Q(user_permissions__codename="curate_team_chats")
            )
            .exclude(chat_memberships__chat=chat)
            .distinct()
            .order_by("surname", "name")
        )
