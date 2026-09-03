import secrets
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import permission_required
from django.contrib.auth.forms import PasswordResetForm
from django.contrib.auth.views import (
    PasswordChangeView as BasePasswordChangeView,
    PasswordResetConfirmView as BasePasswordResetConfirmView,
    PasswordResetView as BasePasswordResetView,
)
from django.contrib.sessions.models import Session
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.db.models.functions import Coalesce, Lower
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from cosmetics.models import CosmeticItem
from cosmetics.services import inventory, outfit
from core.models import ALUMNI, Moderated, Team
from core.search import by_name
from core.throttle import client_ip, throttled
from library.models import Book
from materials.models import Material

from .forms import MAX_AVATAR_DATA, ProfileForm, RegisterUserForm, StudentFilterForm
from .models import User
from .roster import RosterForm
from .sessions import alive


def _by_term(materials):
    """Материалы по семестрам. Материал с двумя семестрами попадает в оба — так и задумано:
    это разбивка «когда пригодится», а не деление одной работы на части.

    order_by() перед values() обязателен: у Material в Meta стоит сортировка по дате,
    а сортировка попадает в GROUP BY — без сброса каждый материал стал бы своей группой.
    """
    rows = list(
        materials.order_by().values("terms__number").annotate(count=Count("id")).order_by("terms__number")
    )
    top = max((row["count"] for row in rows), default=0)
    return [
        {
            "label": f"{row['terms__number']} семестр" if row["terms__number"] else "Без семестра",
            "count": row["count"],
            "share": round(row["count"] * 100 / top),
        }
        for row in rows
    ]


def _contributions(person, full):
    """Вклад человека в сайт. Считаем только опубликованное: материал на проверке ещё
    может и не выйти, а в профиле он выглядел бы как заслуга.

    full — свой профиль. В чужом анонимные работы не показываем даже числом: у того,
    у кого такая работа одна, счётчик и есть та самая подпись, которую он снял.
    """
    approved = Moderated.Status.APPROVED
    materials = Material.objects.filter(uploader=person, status=approved)
    books = Book.objects.filter(uploader=person, status=approved)
    reviews = person.teacher_reviews.all()
    comments = person.comments.all()
    if not full:
        materials = materials.filter(hide_uploader=False)
        books = books.filter(hide_uploader=False)
        reviews = reviews.filter(hide_author=False)
        comments = comments.filter(hide_author=False)

    return {
        "materials": materials.count(),
        "books": books.count(),
        "reviews": reviews.count(),
        "comments": comments.count(),
        "by_term": _by_term(materials),
    }


PAGE_SIZE = 24  # человек в порции
# Как сортировать список. По алфавиту — по умолчанию: раздел заведён, чтобы НАЙТИ
# человека, а не посмотреть, кто первый.
SORTS = {"name": ("surname", "name"), "contribution": ("-earned", "surname", "name")}
SORT_LABELS = {"name": "По алфавиту", "contribution": "По вкладу"}


def _people():
    """Все живые люди со счётчиком заработанного.

    Считаем ЗАРАБОТАННОЕ (плюсы журнала), а не баланс. Баланс — это заработанное минус
    потраченное, и порядок по нему поднимал бы наверх тех, кто ничего не покупает: купил
    рамку — уехал вниз. А ещё баланс — дело личное (чужой кошелёк в профиле не
    показывается), тогда как заработанное складывается из того, что и так на виду:
    материалов, книг, отзывов, клеток на Стене.
    """
    earned = Sum("wallet__entries__amount", filter=Q(wallet__entries__amount__gt=0))
    return (
        User.objects.filter(is_active=True)
        .select_related("team")
        .annotate(earned=Coalesce(earned, 0))
    )


def _courses():
    """{значение фильтра: (подпись, [номера групп])} — по нынешнему составу групп.

    Список курсов не зашит: набор групп меняется каждый год, и «6 курс», за которым
    никого нет, — предложение, ведущее в пустоту.

    Считаем в Python и по ГРУППАМ: курс нигде не хранится, он выводится из года
    зачисления (`Team.grade_key`), и вторая его реализация на SQL разошлась бы с первой
    в первый же сентябрь. Групп два десятка — это один запрос, а не арифметика
    по трёмстам людям.
    """
    buckets = {}
    for team in Team.objects.all():
        key = team.grade_key()
        label = "Выпускники" if key == ALUMNI else f"{key} курс"
        buckets.setdefault(key, (label, []))[1].append(team.pk)
    # Курсы по возрастанию, выпускники последними: они не курс, а его отсутствие.
    return dict(sorted(buckets.items(), key=lambda pair: (pair[0] == ALUMNI, pair[0])))


def student_list(request):
    q = request.GET.get("q", "").strip()
    sort = request.GET.get("sort", "")
    if sort not in SORTS:
        sort = "name"

    courses = _courses()
    course = request.GET.get("course", "")
    if course not in courses:
        course = ""

    people = _people()
    if course:
        people = people.filter(team__in=courses[course][1])
    if q:
        people = by_name(people, q)
    # Порядок поиска сохраняем ПЕРВЫМ ключом: он ставит вперёд тех, у кого слово стоит
    # в начале имени, — по «Иван» это Иван и Иванов, а не десять Ивановых (`core.search`).
    # Выбранная сортировка идёт после него, иначе поиск перестал бы попадать в цель.
    people = people.order_by(*(("name_rank",) if q else ()), *SORTS[sort])

    page = Paginator(people, PAGE_SIZE).get_page(request.GET.get("page"))
    # Пусто под выбранным курсом — это не «на сайте нет людей», и говорить об этом
    # надо по-разному.
    context = {"page": page, "people": page.object_list, "q": q, "sort": sort, "course": course}

    if not request.headers.get("HX-Request"):
        # Форма несвязанная, со значением УЖЕ разобранным: селект показывает ровно то,
        # что применено, а не то, что прислали.
        form = StudentFilterForm(
            courses=[(key, label) for key, (label, _) in courses.items()],
            initial={"course": course},
        )
        return render(request, "users/students.html", {
            **context, "form": form, "sorts": SORT_LABELS.items(),
        })

    response = render(request, "users/_student_list.html", context)
    # Порция — это про место в списке, а не про подбор: адрес она не трогает.
    if not request.GET.get("page"):
        response["HX-Push-Url"] = _picked_url(request)
    return response


def _picked_url(request):
    """Адрес списка с выбранным подбором: F5 не сбрасывает фильтры, а ссылку можно
    переслать. Пустые параметры выбрасываем, `page` — тоже: он про порцию, а не про подбор."""
    query = urlencode({key: value for key, value in request.GET.items() if value and key != "page"})
    return f"{request.path}?{query}" if query else request.path


RECENT = 7  # операций в кошельке на странице профиля, дальше — «вся история»


def profile(request, pk):
    # TODO (M5): значки — когда будет что рисовать
    person = get_object_or_404(
        User.objects.select_related("team", "wallet"), pk=pk, is_active=True,
    )
    own = person.pk == request.user.pk
    wallet = getattr(person, "wallet", None)
    on = outfit(person)
    # Берём на одну больше, чем покажем: так видно, есть ли что смотреть дальше,
    # и это тот же запрос, а не второй ради count().
    entries = list(wallet.entries.all()[: RECENT + 1]) if wallet and own else []
    return render(request, "users/profile.html", {
        "person": person,
        "own": own,
        "stats": _contributions(person, full=own),
        # История трат и список устройств — только свои: по ним видно и что человек
        # покупал, и откуда он заходит.
        "entries": entries[:RECENT],
        "more": len(entries) > RECENT,
        "devices": _devices(request) if own else [],
        "worn": on.get(CosmeticItem.Kind.AVATAR_FRAME),
        "header": on.get(CosmeticItem.Kind.PROFILE_HEADER),
        "background": on.get(CosmeticItem.Kind.PROFILE_BACKGROUND),
        # Инвентарь — только свой: чужой сундук это витрина «вот чего у тебя нет».
        "items": inventory(person) if own else [],
    })


def _devices(request):
    """Сессии для страницы. Ключ сессии наружу не отдаём даже своему хозяину: это пароль
    на предъявителя, и попавший на скриншот действует до конца срока. Наружу — id строки."""
    mine = request.session.session_key
    return [(row, row.session_id == mine) for row in alive(request.user)]


@require_POST
def session_end(request):
    """Закрыть сессию: своя строка по id в теле запроса, без него — все, кроме текущей.

    Id в теле, а не в адресе: адреса оседают в логах и в Referer, а тут закрывается доступ.
    Текущую не трогаем — это просто выход, и он рядом, в меню аккаунта.
    """
    doomed = alive(request.user).exclude(session_id=request.session.session_key)
    pk = request.POST.get("id", "")
    if pk:
        # Не число — значит, запрос пришёл не с нашей страницы. Молча закрывать всё
        # в таком случае нельзя, а падать пятисоткой на кривом поле незачем.
        if not pk.isdigit():
            return redirect("profile", pk=request.user.pk)
        doomed = doomed.filter(pk=pk)

    # Удаляем сами сессии — записи уедут за ними каскадом.
    closed = Session.objects.filter(pk__in=doomed.values("session_id")).delete()[0]
    messages.success(request, f"Закрыто сессий: {closed}" if closed else "Закрывать нечего")
    return redirect("profile", pk=request.user.pk)


def profile_edit(request):
    form = ProfileForm(request.POST or None, request.FILES or None, instance=request.user)
    was = request.user.photo
    if request.method == "POST" and form.is_valid():
        form.save()
        # Прежний блоб больше ничей: запись жива, а добраться до него потом неоткуда.
        if was and str(was) != str(request.user.photo):
            was.storage.delete(str(was))
        messages.success(request, "Профиль обновлён")
        return redirect("profile", pk=request.user.pk)

    return render(request, "users/profile_edit.html", {"form": form, "avatar_limit": MAX_AVATAR_DATA})


def _clear_must_change_password(user):
    if user.must_change_password:
        user.must_change_password = False
        user.save(update_fields=["must_change_password"])


class PasswordChangeView(BasePasswordChangeView):
    template_name = "users/password_change.html"
    success_url = settings.LOGIN_REDIRECT_URL

    def form_valid(self, form):
        response = super().form_valid(form)
        _clear_must_change_password(self.request.user)
        messages.success(self.request, "Пароль обновлён")
        return response


class PasswordResetView(BasePasswordResetView):
    template_name = "users/password_reset.html"
    email_template_name = "users/password_reset_email.txt"
    subject_template_name = "users/password_reset_subject.txt"

    def form_valid(self, form):
        # Защита от спама письмами: при превышении лимита молча уходим на done-страницу.
        ip = client_ip(self.request)
        email = form.cleaned_data["email"].lower()
        if throttled(f"pwreset:ip:{ip}", 5) or throttled(f"pwreset:email:{email}", 3):
            return HttpResponseRedirect(self.get_success_url())
        return super().form_valid(form)


class PasswordResetConfirmView(BasePasswordResetConfirmView):
    template_name = "users/password_reset_confirm.html"

    def form_valid(self, form):
        response = super().form_valid(form)
        _clear_must_change_password(form.user)
        return response


def _passwordless(user):
    """Аккаунт без пароля: случайный не знает никто, свой человек задаст по ссылке."""
    user.set_password(secrets.token_urlsafe(16))
    user.must_change_password = False
    return user


def _welcome(request, user):
    """Приглашение — то же письмо восстановления пароля, только другим текстом."""
    mail = PasswordResetForm({"email": user.email})
    mail.is_valid()
    mail.save(
        request=request,
        email_template_name="users/welcome_email.txt",
        subject_template_name="users/welcome_subject.txt",
    )


def _register_all(request, people):
    """Завести всю ведомость разом.

    Знакомые почты пропускаем: выгрузку формы носят целиком и не по одному разу —
    новых ответов с прошлой загрузки прибавилось, а старые никуда не делись, и вычищать
    их руками перед каждой загрузкой никто не станет. Заодно это делает безопасной
    повторную загрузку после исправления файла.
    """
    wanted = [person.email.lower() for person in people]
    known = set(
        User.objects.annotate(lowered=Lower("email"))
        .filter(lowered__in=wanted)
        .values_list("lowered", flat=True)
    )
    fresh = [person for person in people if person.email.lower() not in known]

    # Заводим одной транзакцией, а письма шлём после неё: приглашение, ушедшее в
    # откатившуюся базу, ведёт по ссылке, за которой никого нет.
    with transaction.atomic():
        for person in fresh:
            _passwordless(person).save()
    for person in fresh:
        _welcome(request, person)

    if not fresh:
        messages.info(request, "Все из файла уже зарегистрированы")
    else:
        was = len(people) - len(fresh)
        messages.success(request, f"Заведено аккаунтов: {len(fresh)}, письма отправлены"
                                  + (f". Уже были на сайте: {was}" if was else ""))
    return redirect("user_new")


@permission_required("users.add_user", raise_exception=True)
def user_new(request):
    """Регистрация: по одному человеку руками или курсом сразу — файлом ответов формы.

    Обе формы на одной странице, и различает их имя кнопки: заводят людей в одном
    и том же месте, а вторая страница только пряталась бы.
    """
    posted = request.method == "POST"
    bulk = posted and "roster" in request.POST

    form = RegisterUserForm(request.POST if posted and not bulk else None, creator=request.user)
    roster = RosterForm(request.POST, request.FILES) if bulk else RosterForm()

    if bulk and roster.is_valid():
        return _register_all(request, roster.people)
    if posted and not bulk and form.is_valid():
        user = _passwordless(form.save(commit=False))
        user.save()
        form.save_m2m()
        _welcome(request, user)
        messages.success(request, f"Аккаунт создан, письмо отправлено на {user.email}")
        return redirect("user_new")

    return render(request, "users/user_new.html", {"form": form, "roster": roster})
