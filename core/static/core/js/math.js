// Формулы: arithmatex (core/markup.py) уже обернул их в \( \) и \[ \] — KaTeX проходит по .prose.
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

// Куски htmx (комментарий без перезагрузки); саму страницу уже отрисовал слушатель выше.
document.addEventListener("htmx:load", (event) => {
  if (event.detail.elt !== document.body) renderMath(event.detail.elt);
});
