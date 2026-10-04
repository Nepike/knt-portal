// Формулы в тексте. arithmatex (core/markup.py) уже вынул их из-под разбора markdown
// и вернул обёрнутыми в \( \) и \[ \] — автоподстановщику остаётся пройтись по .prose.
function renderMath(root) {
  const blocks = root.matches(".prose") ? [root] : root.querySelectorAll(".prose");
  blocks.forEach((element) =>
    renderMathInElement(element, {
      delimiters: [
        { left: "\\(", right: "\\)", display: false },
        { left: "\\[", right: "\\]", display: true },
      ],
      // Кривая формула не должна ронять страницу — KaTeX покажет её красным как есть.
      throwOnError: false,
    })
  );
}

// Скрипт грузится с defer, поэтому слушатель успевает встать до DOMContentLoaded.
document.addEventListener("DOMContentLoaded", () => renderMath(document.body));

// Комментарий, добавленный или исправленный без перезагрузки, приезжает куском через htmx.
// Саму страницу htmx тоже объявляет загруженной — её уже отрисовал слушатель выше.
document.addEventListener("htmx:load", (event) => {
  if (event.detail.elt !== document.body) renderMath(event.detail.elt);
});
