// Формулы в тексте. arithmatex (core/markup.py) уже вынул их из-под разбора markdown
// и вернул обёрнутыми в \( \) и \[ \] — автоподстановщику остаётся пройтись по .prose.
// Скрипт грузится с defer, поэтому слушатель успевает встать до DOMContentLoaded.
document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll(".prose").forEach((element) =>
    renderMathInElement(element, {
      delimiters: [
        { left: "\\(", right: "\\)", display: false },
        { left: "\\[", right: "\\]", display: true },
      ],
      // Кривая формула не должна ронять страницу — KaTeX покажет её красным как есть.
      throwOnError: false,
    })
  );
});
