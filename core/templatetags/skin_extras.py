from django import template

from core.context_processors import today

register = template.Library()


@register.simple_tag
def error_skin():
    """Скин страницы ошибки. 400 и 500 Django рисует без контекста — день считаем сами.
    На ошибки идёт только глитч."""
    theme, classes = today()
    if theme != "glitch":
        return {"theme": "default", "classes": ""}
    return {"theme": theme, "classes": classes}


@register.tag
def capture(parser, token):
    """{% capture имя %}…{% endcapture %} — отрисованное в переменную: когда блок нужен дважды."""
    bits = token.split_contents()
    if len(bits) != 2:
        raise template.TemplateSyntaxError("capture ждёт одно имя: {% capture имя %}")
    nodelist = parser.parse(("endcapture",))
    parser.delete_first_token()
    return CaptureNode(nodelist, bits[1])


class CaptureNode(template.Node):
    def __init__(self, nodelist, name):
        self.nodelist, self.name = nodelist, name

    def render(self, context):
        context[self.name] = self.nodelist.render(context).strip()
        return ""


GLYPHS = {
    "0": ["111", "101", "101", "101", "111"], "1": ["010", "110", "010", "010", "111"],
    "2": ["111", "001", "111", "100", "111"], "3": ["111", "001", "111", "001", "111"],
    "4": ["101", "101", "111", "001", "001"], "5": ["111", "100", "111", "001", "111"],
    "6": ["111", "100", "111", "101", "111"], "7": ["111", "001", "001", "010", "010"],
    "8": ["111", "101", "111", "101", "111"], "9": ["111", "101", "111", "001", "111"],
}


@register.filter
def blocks(digits):
    """«404» → цифры из блоков, пять строк: клетка — «██». Тот же шрифт, что у отсчёта
    в core/js/glitch.js. Не цифры пропускаются."""
    digits = [ch for ch in str(digits) if ch in GLYPHS]
    rows = ("  ".join(GLYPHS[ch][row] for ch in digits) for row in range(5))
    return "\n".join(row.replace("1", "██").replace("0", "  ") for row in rows)
