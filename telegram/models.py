from django.db import models


class TelegramChat(models.Model):
    """Куда бот пишет. Из админки: chat_id узнаётся у живого чата (/get_chat_id, commands/bot.py)."""

    name = models.CharField("название", max_length=30, unique=True)
    description = models.CharField("описание", max_length=150, blank=True)
    chat_id = models.BigIntegerField("chat ID")
    topic_id = models.BigIntegerField(
        "ID темы", null=True, blank=True,
        help_text="Только для групп с темами — сообщения уйдут в неё, а не в общий поток.",
    )

    class Meta:
        verbose_name = "телеграм-чат"
        verbose_name_plural = "телеграм-чаты"

    def __str__(self):
        return self.name
