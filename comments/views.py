"""Обсуждение под материалом или лекцией.

От вида владельца тут не зависит ничего: устройство веток, голоса и перерисовка ленты
одинаковы. Разное только одно — как найти владельца, ВИДИМОГО этому человеку, и это
единственное место, где приложение комментариев смотрит наружу.
"""

from collections import defaultdict

from django.db.models import Count, Exists, OuterRef
from django.http import Http404, HttpResponseForbidden
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_POST

from attachments.uploads import drop_replaced
from economy import rewards

from .forms import CommentForm
from .models import Comment


def owners(kind, user):
    """Владельцы этого вида, видимые человеку.

    Импорт внутри функции намеренно: приложения владельцев сами зовут отсюда ленту
    для своей страницы, и на уровне модуля это был бы круг в импортах.
    """
    if kind == "material":
        from materials.views import visible

        return visible(user)
    if kind == "lecture":
        from lectorium.views import visible_lectures

        return visible_lectures(user)
    raise Http404


def field(owner):
    """Имя ключа, которым владелец привязан к комментарию. Берём у самой модели —
    ключи названы по ней (`material`, `lecture`), как и у `attachments.File`."""
    return owner._meta.model_name


def feed(user):
    """Лента со счётчиками лайков и отметкой «я уже голосовал» — одним запросом."""
    return Comment.objects.select_related("author__team").annotate(
        likes=Count("liked_users", distinct=True),
        dislikes=Count("disliked_users", distinct=True),
        liked_by_me=Exists(Comment.liked_users.through.objects.filter(comment_id=OuterRef("pk"), user_id=user.pk)),
        disliked_by_me=Exists(Comment.disliked_users.through.objects.filter(comment_id=OuterRef("pk"), user_id=user.pk)),
    )


def thread(user, owner):
    """Верхний уровень с прицепленными ответами.

    В базе дерево настоящее: parent — тот комментарий, на который отвечали. А на экране
    ветка плоская, ответы всех уровней лежат под своим корнем; кому именно отвечали,
    показывает подпись (addressee). Иначе на третьем уровне лента уезжает вправо
    и с телефона её не прочитать.
    """
    items = list(feed(user).filter(**{field(owner): owner}).order_by("created"))
    known = {comment.pk: comment for comment in items}
    answers = defaultdict(list)
    roots = []
    for comment in items:
        root = comment
        while root.parent_id in known:
            root = known[root.parent_id]
        if root is comment:
            roots.append(comment)
            continue
        # Адресат нужен, только если отвечали не самому корню — иначе и так понятно.
        comment.addressee = known[comment.parent_id] if comment.parent_id != root.pk else None
        answers[root.pk].append(comment)

    for comment in roots:
        comment.answers = answers.get(comment.pk, [])
    return roots


def context(user, owner):
    """Всё, что нужно ленте на странице владельца. Зовут отсюда материалы и лекторий."""
    roots = thread(user, owner)
    return {
        "owner": owner,
        "kind": field(owner),
        "comments": roots,
        # Счётчик считаем и здесь: раньше при первом показе страницы его не было вовсе,
        # и число появлялось только после первого добавления.
        "total": _total(roots),
        "comment_form": CommentForm(),
    }


def _total(roots):
    return sum(1 + len(root.answers) for root in roots)


def _comment(request, pk):
    """Комментарий — только если его владелец этому человеку виден: иначе через прямой
    адрес можно было бы лайкать и открывать обсуждение чужого черновика."""
    comment = get_object_or_404(Comment.objects.select_related("material", "lecture"), pk=pk)
    get_object_or_404(owners(comment.kind, request.user), pk=comment.owner.pk)
    return comment


def _may_touch(user, comment):
    return comment.author_id == user.pk or user.has_perm("comments.change_comment")


def _node(request, owner, pk):
    """Комментарий, взятый ИЗ ЛЕНТЫ — то есть с прицепленными ответами и подписью
    адресата. Собирать карточку из голого объекта нельзя: у корня не оказалось бы
    ответов, и они пропадали бы с экрана от простого лайка."""
    for root in thread(request.user, owner):
        if root.pk == pk:
            return root
        for answer in root.answers:
            if answer.pk == pk:
                return answer
    return None


def _card(request, owner, pk):
    comment = _node(request, owner, pk)
    if comment is None:
        raise Http404
    # comment_form нужен форме ответа внутри карточки — она есть у каждого комментария.
    return render(request, "comments/_comment.html", {
        "c": comment, "owner": owner, "kind": field(owner), "comment_form": CommentForm(),
    })


def _block(request, owner, form=None, parent=None):
    """Вся лента целиком. Перерисовывать её на каждое действие дешевле, чем вставлять
    ответ в нужную ветку точечно: комментариев десятки, а ошибиться местом — легко.

    Форма с ошибкой возвращается ровно туда, откуда её отправили: у ветки она приезжает
    прицепленной к своему комментарию (reply_form), иначе чужой текст оказался бы
    подставлен во все формы ответа разом.
    """
    roots = thread(request.user, owner)
    if form and parent:
        for node in (node for root in roots for node in (root, *root.answers)):
            if node.pk == parent:
                node.reply_form = form
    return render(request, "comments/_comments.html", {
        "owner": owner,
        "kind": field(owner),
        "comments": roots,
        "total": _total(roots),
        "comment_form": CommentForm() if parent else (form or CommentForm()),
    })


@require_POST
def comment_add(request, kind, pk):
    owner = get_object_or_404(owners(kind, request.user), pk=pk)
    # Владельца и автора ставим ДО проверки, а не после `save(commit=False)`: форма
    # проверяет и саму модель, а та требует, чтобы владелец был ровно один.
    form = CommentForm(request.POST, request.FILES,
                       instance=Comment(author=request.user, **{kind: owner}))
    # Храним настоящего адресата; плоской ветку делает уже thread() при выводе.
    parent = Comment.objects.filter(pk=request.POST.get("parent") or 0, **{kind: owner}).first()
    if not form.is_valid():
        return _block(request, owner, form, parent.pk if parent else None)

    comment = form.save(commit=False)
    comment.parent = parent
    comment.save()
    return _block(request, owner)


def comment_edit(request, pk):
    comment = _comment(request, pk)
    if not _may_touch(request.user, comment):
        return HttpResponseForbidden("Это чужой комментарий.")

    form = CommentForm(request.POST or None, request.FILES or None, instance=comment)
    if request.method == "POST" and form.is_valid():
        form.save()
        drop_replaced(form)
        return _card(request, comment.owner, pk)
    return render(request, "comments/_comment_form.html", {"form": form, "c": comment})


@require_POST
def comment_delete(request, pk):
    comment = _comment(request, pk)
    if not _may_touch(request.user, comment):
        return HttpResponseForbidden("Это чужой комментарий.")
    owner = comment.owner
    comment.delete()  # ответы уедут каскадом, картинку снимет post_delete
    # Отдаём ленту целиком, а не пустоту: вместе с комментарием исчезают его ответы
    # и меняется счётчик — точечным удалением узла этого не показать.
    return _block(request, owner)


@require_POST
def comment_vote(request, pk, vote):
    comment = _comment(request, pk)
    mine, other = (
        (comment.liked_users, comment.disliked_users) if vote == "like"
        else (comment.disliked_users, comment.liked_users)
    )
    if mine.filter(pk=request.user.pk).exists():
        mine.remove(request.user)  # повторный клик снимает голос
    else:
        mine.add(request.user)
        other.remove(request.user)
    rewards.sync(comment.author)
    return _card(request, comment.owner, pk)
