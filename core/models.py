from datetime import date

from django.conf import settings
from django.db import models
from django.utils import timezone


class Moderated(models.Model):
    """Общее для всего, что проходит проверку: книги, материалы, потом лекторий.

    Статус, а не флаг «одобрено»: без состояния «отклонено» модератору остаётся
    только молча удалить чужую работу, а автор не узнает ни причины, ни что чинить.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "на проверке"
        APPROVED = "approved", "опубликовано"
        REJECTED = "rejected", "отклонено"

    status = models.CharField(
        "статус", max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True,
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="проверил", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",  # обратная связь не нужна, а имя было бы общим на всех наследников
    )
    reviewed_at = models.DateTimeField("проверено", null=True, blank=True)
    review_note = models.CharField("причина отказа", max_length=300, blank=True)

    class Meta:
        abstract = True

    @property
    def is_published(self):
        return self.status == self.Status.APPROVED

    @property
    def is_pending(self):
        return self.status == self.Status.PENDING

    # Ни один из методов ниже НЕ сохраняет: их зовут и на объекте из формы, который
    # вот-вот запишут целиком. Кому нужна только смена статуса — save(update_fields=REVIEW_FIELDS).
    REVIEW_FIELDS = ["status", "review_note", "reviewed_by", "reviewed_at"]

    def approve(self, moderator):
        self.status = self.Status.APPROVED
        self.review_note = ""
        self._reviewed(moderator)

    def reject(self, moderator, note=""):
        self.status = self.Status.REJECTED
        self.review_note = note.strip()[:300]
        self._reviewed(moderator)

    def _reviewed(self, moderator):
        self.reviewed_by = moderator
        self.reviewed_at = timezone.now()

    def send_to_review(self):
        """Вернуть в очередь: правка не-модератором отменяет прошлое решение."""
        self.status = self.Status.PENDING
        self.reviewed_by = None
        self.reviewed_at = None

    def revise(self, editor, moderator):
        """Что делает с решением сама правка. Зовётся ДО save: новое узнаём по пустому pk.

        Не-модератор возвращает вещь в очередь — иначе одобренное можно было бы тихо
        подменить. Модератор, правя своё новое или отклонённое, публикует: замечание он
        и устраняет. «На проверке» не трогаем — решение остаётся отдельным шагом.
        """
        if not moderator:
            self.send_to_review()
        elif self.pk is None or self.status == self.Status.REJECTED:
            self.approve(editor)


class Subject(models.Model):
    name = models.CharField("название", max_length=50)
    dative = models.CharField("название (дательный падеж)", max_length=50)
    accusative = models.CharField("название (винительный падеж)", max_length=50)

    class Meta:
        verbose_name = "предмет"
        verbose_name_plural = "предметы"
        ordering = ["name"]

    def __str__(self):
        return self.name


class Term(models.Model):
    number = models.PositiveSmallIntegerField("номер")

    class Meta:
        verbose_name = "семестр"
        verbose_name_plural = "семестры"
        ordering = ["number"]

    def __str__(self):
        return f"Семестр {self.number}"


# Курс не хранится нигде: он считается от года зачисления, а «выпускник» — это уже
# не курс, а его отсутствие. Отсюда отдельное значение для фильтра (см. Team.grade_key).
ALUMNI = "alumni"


class Team(models.Model):
    STAGE_CHOICES = (
        ("bachelor", "Бакалавриат"),
        ("master", "Магистратура"),
    )
    # Служебная группа выпускников (номер 000000). Потока у них уже нет, но числиться
    # где-то надо: без группы человек не попадает ни в один чат и остаётся без подписи.
    ALUMNI_YEAR = 0

    number = models.CharField("номер", max_length=7, unique=True)
    profile = models.CharField("профиль", max_length=255)
    course_code = models.CharField("направление (код курса)", max_length=10)
    stage = models.CharField("ступень обучения", max_length=20, choices=STAGE_CHOICES)
    year_of_admission = models.PositiveSmallIntegerField("год зачисления")

    class Meta:
        verbose_name = "учебная группа"
        verbose_name_plural = "учебные группы"
        ordering = ["number"]

    def __str__(self):
        return self.number

    def get_grade_level(self):
        today = date.today()
        level = today.year - self.year_of_admission
        if today.month >= 9:
            level += 1
        return level + (4 if self.stage == "master" else 0)

    def graduation_year(self):
        return self.year_of_admission + (2 if self.stage == "master" else 6)

    def get_grade_str(self):
        # У служебной группы год нулевой, и обычный расчёт дал бы «Выпускник 6 года».
        if self.year_of_admission == self.ALUMNI_YEAR:
            return "Выпускник"
        if date.today().year > self.graduation_year():
            return f"Выпускник {self.graduation_year()} года"
        return f"Студент {self.get_grade_level()} курса"

    def grade_key(self):
        """Курс как значение фильтра: «3» или «alumni» (`ALUMNI`).

        Той же развилкой, что и `get_grade_str`, и это не совпадение: подпись в списке
        и отбор по курсу обязаны сходиться. Разъедься они — человек с подписью
        «Выпускник 2026 года» не нашёлся бы среди выпускников.
        """
        graduated = self.year_of_admission == self.ALUMNI_YEAR or date.today().year > self.graduation_year()
        return ALUMNI if graduated else str(self.get_grade_level())
