// Глитч-скин «сайт взломан»: поведение. Грузится только в дни скина (core/base.html),
// оформление — theme/input.css.

document.addEventListener("alpine:init", () => {
  // Запертый переключатель темы (core/_theme_toggle.html): дёргается и зовёт окно отказа.
  // Класс снимаем и ставим кадром позже — иначе анимация на втором нажатии не повторится.
  Alpine.data("lockedSwitch", () => ({
    shaking: false,
    deny() {
      this.shaking = false;
      requestAnimationFrame(() => { this.shaking = true; });
      window.dispatchEvent(new CustomEvent("skin-denied"));
    },
  }));

  // Экран «взлома» уходит сам; здесь — пропуск нажатием и уборка класса.
  Alpine.data("boot", () => ({
    skip() { document.documentElement.classList.remove("glitch-boot"); },
  }));

  // Журнал взлома (quest/_journal.html): строка раз в 1,7 с, время — от открытия страницы.
  // Сданный фрагмент меняет «сверку ключа» сразу (key-restored). Начало — со случайной
  // строки, иначе подсказка была бы видна сразу на каждой странице.
  Alpine.data("journal", () => ({
    lines: unveil("glitch-log", []),
    step: 0,
    since: Date.now(),
    init() {
      this.step = Math.floor(Math.random() * this.lines.length);
      this.since -= this.step * 1700;
      window.addEventListener("key-restored", ({ detail }) => {
        this.lines = this.lines.map((line) => (line.startsWith("сверка ключа") ? `сверка ключа … ${detail.done}/${detail.total}` : line));
      });
      if (!this.lines.length || calm()) return;
      this.timer = setInterval(() => this.step++, 1700);
    },
    destroy() { clearInterval(this.timer); },
    get shown() {
      return [0, 1, 2, 3, 4, 5].map((i) => {
        const n = this.step + i;
        const at = new Date(this.since + n * 1700).toLocaleTimeString("ru-RU");
        return { key: n, text: `${at} ${this.lines[n % this.lines.length]}` };
      });
    },
  }));

  // Строка «ключ повреждён» на экране отсчёта (quest/countdown.html): сданная копия
  // открывает свою ячейку сразу.
  Alpine.data("keyline", () => ({
    restore({ slot }) {
      const cell = this.$root.querySelector(`[data-slot="${slot.number}"]`);
      if (!cell) return;
      cell.textContent = slot.value;
      cell.className = "cd-ok";
    },
  }));

  // Окна «ОТКАЗАНО»: лесенкой до пяти, текст злее с каждым нажатием, счёт идёт и дальше.
  // В последних двух смайл краснеет. Пятое окно может нести подсказку квеста.
  const REPLIES = [
    "Светлая тема отключена администратором.",
    "Повторная попытка записана в журнал.",
    "Третья попытка. Мы начинаем нервничать.",
    null,
    "…",
  ];
  Alpine.data("denied", (name) => ({
    windows: [],
    count: 0,
    _id: 0,
    add() {
      const n = Math.min(this.count++, REPLIES.length - 1);
      if (this.windows.length >= 5) return;
      const last = n === REPLIES.length - 1 ? clues().denied?.[0] : null;
      this.windows.push({ id: ++this._id, text: last || (REPLIES[n] ?? `${name || "Гость"}, хватит.`), angry: n >= 3 });
    },
    close(id) { this.windows = this.windows.filter((w) => w.id !== id); },
    // Объектом, а не строкой: строка в :style затёрла бы весь атрибут.
    place(i) {
      const wide = matchMedia("(width >= 40rem)").matches;
      return { top: `${(wide ? 68 : 64) + i * 18}px`, right: `${(wide ? 20 : 12) + i * 18}px` };
    },
  }));

  // Отсчёт до полуночи 1 декабря по Москве (quest/_pill.html, quest/countdown.html). Разница
  // часов сервера и браузера считается раз, при загрузке. since — начало скина, для полосы;
  // он есть только у экрана отсчёта, и только тот на нуле перезагружает страницу.
  const pad = (n) => String(n).padStart(2, "0");
  Alpine.data("clock", (deadline, serverNow = Date.now(), since = 0) => ({
    skew: serverNow - Date.now(),
    left: 0,
    spasm: "",
    units: ["дн", "ч", "мин", "сек"].map((u) => center(u, 14)).join("      "),
    init() {
      this.tick();
      const counting = this.left > 0;
      this.timer = setInterval(() => {
        this.tick();
        if (since && counting && this.left === 0) {
          clearInterval(this.timer);
          setTimeout(() => location.reload(), 1500);
        }
      }, 1000);
    },
    destroy() { clearInterval(this.timer); },
    tick() { this.left = Math.max(0, Math.floor((deadline - Date.now() - this.skew) / 1000)); },
    get dd() { return pad(Math.floor(this.left / 86400)); },
    get hh() { return pad(Math.floor(this.left / 3600) % 24); },
    get mm() { return pad(Math.floor(this.left / 60) % 60); },
    get ss() { return pad(this.left % 60); },
    get big() { return blocks(`${this.dd}:${this.hh}:${this.mm}:${this.ss}`); },
    // После нуля — нули, что бы ни показывали часы браузера.
    zeros: blocks("00:00:00:00"),
    get bar() {
      const done = since ? Math.min(1, Math.max(0, 1 - (this.left * 1000) / (deadline - since))) : 0;
      const k = Math.round(done * 40);
      return `[${"█".repeat(k)}${"░".repeat(40 - k)}] ${Math.round(done * 100)}%`;
    },
    get say() { return `Осталось ${+this.dd} дн ${+this.hh} ч ${+this.mm} мин`; },
    // Смех консоли: экран дёргается. Класса два, чтобы смех подряд срабатывал заново.
    shake() { this.spasm = this.spasm === "spasm-a" ? "spasm-b" : "spasm-a"; },
  }));

  // Консоль на странице отсчёта. На что своего ответа нет — спрашивает сайт (quest.views.ask):
  // подсказки лежат в базе, а не в открытом коде. Прочие команды — сайту для счёта (tell).
  // Не длиннее 60 знаков и не больше 8 команд за 20 с. После нуля (zero) в help есть unlock.
  Alpine.data("terminal", (name, url, csrf, zero = false) => ({
    lines: [],
    value: "",
    busy: false,
    waiting: false,
    history: [],
    back: 0,
    times: [],
    _id: 0,
    prompt: name ? "student@knt:~$" : "guest@knt:~$",
    init() {
      if (zero) {
        this.say("out", "> передача получена.");
        this.say("out", "> чтобы вернуть сайт, нужен ключ: unlock <ключ>");
        return;
      }
      this.say("out", "> осталось недолго.");
      this.say("out", "терминал открыт. введите help");
    },
    say(kind, text) {
      this.lines.push({ id: ++this._id, kind, text });
      if (this.lines.length > 30) this.lines.shift();
    },
    recall(step) {
      if (!this.history.length) return;
      this.back = Math.min(this.history.length, Math.max(0, this.back + step));
      this.value = this.history[this.back] ?? "";
    },
    async run() {
      const command = this.value.trim().slice(0, 60);
      this.value = "";
      if (!command || this.busy || this.waiting) return;
      this.history.push(command);
      this.back = this.history.length;
      if (command.toLowerCase() === "clear") {
        this.lines = [];
        return;
      }
      const now = Date.now();
      this.times = this.times.filter((t) => now - t < 20000);
      if (this.times.length >= 8) {
        this.say("warn", "канал перегружен. подождите 10 секунд.");
        this.busy = true;
        this.times = [];
        setTimeout(() => {
          this.busy = false;
          this.$nextTick(() => this.$root.querySelector("input").focus());
        }, 10000);
        return;
      }
      this.times.push(now);
      this.say("in", command);
      let answer = reply(command, name, zero);
      if (answer) this.tell(command);
      if (!answer) {
        this.waiting = true;
        answer = await this.ask(command);
        this.waiting = false;
        this.$nextTick(() => this.$root.querySelector("input").focus());
      }
      const [text, laugh, kind = "out"] = answer;
      this.say(kind, text);
      if (laugh) {
        this.say("laugh", "ха-ха-ха-ха-ха-ха");
        this.$dispatch("laugh");
      }
    },
    // Команда с ответом браузера — сайту только для счёта; ответа не ждём.
    tell(command) {
      fetch(url, {
        method: "POST",
        headers: { "X-CSRFToken": csrf },
        body: new URLSearchParams({ command, local: "1" }),
        keepalive: true,
      }).catch(() => {});
    },
    async ask(command) {
      try {
        const response = await fetch(url, {
          method: "POST",
          headers: { "X-CSRFToken": csrf },
          body: new URLSearchParams({ command }),
        });
        const data = await response.json();
        // Сданный фрагмент откроют строка ключа и журнал; принятый ключ — «ключ принят».
        if (data.slot) window.dispatchEvent(new CustomEvent("key-restored", { detail: data }));
        if (data.unlocked) window.dispatchEvent(new CustomEvent("key-unlocked"));
        if (data.warn) return [data.text, false, "warn"];
        if (data.text) return [data.text, data.laugh];
      } catch {
        return ["связь потеряна. попробуйте ещё раз.", false, "warn"];
      }
      return missing(command);
    },
  }));
});

const calm = () => matchMedia("(prefers-reduced-motion: reduce)").matches;
// Данные под занавеской (veil в quest_extras.py): base64 от UTF-8 JSON, XOR с ключом.
// Не шифр — чтобы подсказки не читались в коде страницы.
const VEIL = "knt";
function unveil(id, empty) {
  try {
    const raw = atob(document.getElementById(id)?.textContent.trim() || "");
    const bytes = Uint8Array.from(raw, (ch, i) => ch.charCodeAt(0) ^ VEIL.charCodeAt(i % VEIL.length));
    return JSON.parse(new TextDecoder().decode(bytes));
  } catch {
    return empty;
  }
}
const clues = () => unveil("glitch-clues", {});
const pick = (list) => list[Math.floor(Math.random() * list.length)];

// Порча текста: раз в 15–20 с заголовки и пункты меню дважды подряд рассыпаются в символы.
// Подменяем текстовые узлы и возвращаем; диктору текст отдаёт aria-label (.glitch-corrupt).
// x-text не трогаем: вернули бы старое. Иногда проступает подсказка квеста — вспышка такой
// длины, чтобы прочесть нельзя, а снять экран можно.
const NOISE = "░▒▓█#%&@<>/\\|01";
const CORRUPTIBLE = "main :is(h1, h2, h3), .site-aside .nav-link, .top-link";
const FLASH = 700;
const BLINK = 250;
const HINT_CHANCE = 0.35;
// Пробелы остаются на месте: иначе вылезли бы символы из отступов разметки.
function scramble(text, clue) {
  const chars = [...text].map((ch) => (/\s/.test(ch) || Math.random() < 0.55 ? ch : pick([...NOISE])));
  if (clue) {
    const first = chars.findIndex((ch) => !/\s/.test(ch));
    const length = text.trim().length;
    chars.splice(first + Math.max(0, Math.floor((length - clue.length) / 2)), clue.length, ...clue);
  }
  return chars.join("");
}
function corrupt() {
  if (calm()) return;
  const later = () => setTimeout(() => { hit(); later(); }, 15000 + Math.random() * 5000);
  const hit = () => {
    const targets = [...document.querySelectorAll(CORRUPTIBLE)].filter((el) => {
      const box = el.getBoundingClientRect();
      return box.width && box.bottom > 0 && box.top < innerHeight && box.right > 0 && box.left < innerWidth
        && !el.matches("[x-text], [x-html]") && !el.querySelector("[x-text], [x-html], template") && words(el).length;
    });
    if (!targets.length) return;
    // Подсказку выбираем первой и ищем ей заголовок, где она поместится, — иначе она почти
    // всегда доставалась бы короткому пункту меню и не показывалась вовсе.
    const hints = clues().card || [];
    let hint = hints.length && Math.random() < HINT_CHANCE ? pick(hints) : "";
    const roomy = hint ? targets.filter((el) => room(el) >= hint.length) : [];
    const target = pick(roomy.length ? roomy : targets);
    if (room(target) < hint.length) hint = "";
    const nodes = words(target);
    const real = nodes.map((node) => node.data);
    // Подсказке — самый длинный кусок текста.
    const longest = real.reduce((best, text, i) => (text.trim().length > real[best].trim().length ? i : best), 0);
    const label = target.getAttribute("aria-label");
    const spoken = target.textContent.replace(/\s+/g, " ").trim();
    const flash = (left) => {
      nodes.forEach((node, i) => { node.data = scramble(real[i], i === longest ? hint : ""); });
      if (label === null) target.setAttribute("aria-label", spoken);
      target.classList.add("glitch-corrupt");
      setTimeout(() => {
        nodes.forEach((node, i) => { node.data = real[i]; });
        if (label === null) target.removeAttribute("aria-label");
        target.classList.remove("glitch-corrupt");
        if (left > 1) setTimeout(() => flash(left - 1), BLINK);
      }, FLASH);
    };
    flash(2);
  };
  later();
}

// Сколько знаков подсказки поместится: длина самого длинного куска текста.
function room(el) {
  return Math.max(0, ...words(el).map((node) => node.data.trim().length));
}

// Текстовые узлы элемента или его ссылки. Счётчики в своих span не трогаем.
function words(el) {
  const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
  const found = [];
  while (walker.nextNode()) {
    const node = walker.currentNode;
    if (node.data.trim() && (node.parentElement === el || node.parentElement.tagName === "A")) found.push(node);
  }
  return found;
}

// Смайл для консоли — символами: картинку в %c Chrome не показывает. Кадр «покой» из _smiley.html.
const FACE = [
  "........########........",
  "......############......",
  "....r#**###########c....",
  "...r#*##############c...",
  "...#*###############+...",
  ".r###o###o######oo###+c.",
  ".r####o#o######*ooo##+c.",
  "r######o#######oooo##++c",
  ".#####o#o######oooo##++.",
  "r####o###o#####oooo##++c",
  "r###############oo###++c",
  "r####################++c",
  ".###oo############oo#++.",
  "r###ooo##########ooo+++c",
  "r####ooo########ooo#+++c",
  ".r####ooooo##ooooo#+++c.",
  "..######oooooooo##++++..",
  "..r##############++++c..",
  "...r############++++c...",
  "....r+########+++++c....",
  "......++++++++++++......",
  "........++++++++........",
];
const TONES = { "#": "#39ff7a", "+": "#1f8c42", "*": "#d7ffe1", o: "#03140a", r: "#ff2d5a", c: "#00d8ff" };
function face() {
  let text = "";
  const styles = [];
  for (const row of FACE) {
    for (const run of row.match(/(.)\1*/g)) {
      text += `%c${(run[0] === "." ? "  " : "██").repeat(run.length)}`;
      styles.push(`color:${TONES[run[0]] || "transparent"};font:6px/1 monospace`);
    }
    text += "\n";
  }
  console.log(text, ...styles);
}

// Пасхалка в консоли браузера: смайл, «КНТ» из блоков; подсказки — из базы.
function easterEgg() {
  const art = [
    "██  ██   ██  ██   ██████",
    "██ ██    ██  ██     ██  ",
    "████     ██████     ██  ",
    "██ ██    ██  ██     ██  ",
    "██  ██   ██  ██     ██  ",
  ].join("\n");
  const green = "color:#39ff7a;font:700 12px/1.15 monospace;text-shadow:0 0 8px rgb(57 255 122 / .5)";
  face();
  console.log(`%c${art}`, green);
  console.log("%cТы заглянул под капот. Значит, ты из наших.", "color:#39ff7a;font-size:13px");
  console.log("%cСайт сделали студенты — и сломали тоже они. Ничего не трогай, просто смотри.", "color:#9aa0a6;font-size:13px");
  console.log("%cПодсказка: в терминале на сайте есть команды, которых нет в help.", "color:#39ff7a;font-size:13px");
  for (const line of clues().devtools || []) console.log(`%c${line}`, "color:#39ff7a;font-size:13px");
}

document.addEventListener("DOMContentLoaded", () => {
  corrupt();
  easterEgg();
});

// Цифры из блоков: 3×5 клеток на знак, клетка — два символа «█».
const GLYPHS = {
  0: ["111", "101", "101", "101", "111"], 1: ["010", "110", "010", "010", "111"],
  2: ["111", "001", "111", "100", "111"], 3: ["111", "001", "111", "001", "111"],
  4: ["101", "101", "111", "001", "001"], 5: ["111", "100", "111", "001", "111"],
  6: ["111", "100", "111", "101", "111"], 7: ["111", "001", "001", "010", "010"],
  8: ["111", "101", "111", "101", "111"], 9: ["111", "101", "111", "001", "111"],
  ":": ["0", "1", "0", "1", "0"],
};
function blocks(text) {
  return [0, 1, 2, 3, 4]
    .map((row) => [...text].map((ch) => GLYPHS[ch][row].replace(/1/g, "██").replace(/0/g, "  ")).join("  "))
    .join("\n");
}
function center(text, width) {
  const left = Math.floor((width - text.length) / 2);
  return " ".repeat(left) + text + " ".repeat(width - text.length - left);
}

// Свои ответы консоли: [текст, смех]; null — спросим сайт. unlock и key/ решает только сайт.
function reply(command, name, zero) {
  const low = command.toLowerCase().split(/\s+/).join(" ");
  const [first, ...rest] = low.split(" ");
  const arg = rest.join(" ");
  if (low === "help") return [zero ? "команды: help, unlock, whoami, ls, cat, date, clear" : "команды: help, whoami, ls, cat, date, clear"];
  if (low === "whoami") return [name ? `${name}. мы знаем.` : "гость. но мы всё равно тебя видим."];
  if (low === "ls") return ["countdown.sh  takeover.log  key/  .secret/"];
  if (low === "date") return [new Date().toLocaleString("ru-RU")];
  if (first === "cat" && arg === "takeover.log") return ["03:37 вход выполнен. следы стёрты. никто не заметил."];
  if (first === "cat" && arg === "countdown.sh") return ["бинарный файл. не для твоих глаз."];
  if (first === "cat" && [".secret", ".secret/"].includes(arg)) return ["доступ запрещён. пока.", true];
  if (["kill", "pkill", "stop", "rm"].includes(first)) return ["отсчёт нельзя остановить.", true];
  if (first === "sudo") return [`${name || "guest"} не входит в список sudoers. Об этом инциденте будет доложено.`, true];
  if (["exit", "quit", "logout"].includes(first)) return ["выхода нет.", true];
  if (["laugh", "smile", "ха", "хаха", "haha"].includes(first)) return ["ха-ха-ха.", true];
  if (["theme", "light"].includes(first)) return ["нет.", true];
  return null;
}

// Ни у браузера, ни у сайта ответа нет — как ответил бы настоящий терминал.
function missing(command) {
  const [first, ...rest] = command.split(/\s+/);
  const arg = rest.join(" ");
  if (first.toLowerCase() === "cat") return [arg ? `cat: ${arg}: нет такого файла` : "cat: какой файл?"];
  if (first.toLowerCase() === "ls") return [`ls: ${arg}: отказано в доступе`];
  return [`bash: ${first}: команда не найдена`];
}
