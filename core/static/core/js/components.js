// Края ленты чата для hx-vals — из DOM: курсор в шаблоне устарел бы после догрузки.
const feedEdges = () => document.querySelectorAll("#messages [data-id]");

// Нижний край: отсюда сервер отдаёт новое.
window.lastMessageId = () => {
  const items = feedEdges();
  return items.length ? items[items.length - 1].dataset.id : 0;
};

// Верхний край: правки за ним сервер не шлёт — их oob-замену htmx выбросил бы с ошибкой.
window.firstMessageId = () => {
  const items = feedEdges();
  return items.length ? items[0].dataset.id : 0;
};

// Размер файла для списка выбранных — пара к human_size() из attachments/models.py.
const humanSize = (bytes) => {
  let size = bytes;
  for (const unit of ["Б", "КБ", "МБ", "ГБ"]) {
    if (size < 1024) return unit === "Б" ? `${size} ${unit}` : `${size.toFixed(1)} ${unit}`;
    size /= 1024;
  }
  return `${size.toFixed(1)} ТБ`;
};

// Свой id у выбранного файла: имя правят, а ключ списка должен пережить правку.
let pickedFiles = 0;

// Уход со страницы оборвёт загрузку — браузер спросит подтверждение (свой текст нельзя).
const warnOnLeave = (event) => event.preventDefault();

// Сокет приносит повод ({chat}, для нового сообщения ещё {msg, author}). Разметку вкладка
// берёт запросом, счётчик непрочитанного считает сама. События — на <body> (hx-trigger="… from:body"):
//   chats:event   — список чатов слева;
//   chats:recount — счётчик с сервера (правка, удаление);
//   chats:current — лента открытого чата;
//   chats:sync    — всё сразу, после обрыва связи или возврата на вкладку;
//   notifications:changed — колокольчик в шапке.
window.chatSocket = (() => {
  const me = document.body.dataset.live;
  if (!me) return { sync() {} }; // не залогинен — некуда

  const fire = (name, detail) => document.body.dispatchEvent(new CustomEvent(name, { detail, bubbles: true }));
  const openChat = () => document.querySelector("[data-chat-id]")?.dataset.chatId;

  let socket = null;
  let attempt = 0;
  let fallback = null;
  let missed = false;

  function sync() {
    missed = false;
    fire("chats:sync");
  }

  // Счётчик считаем на месте, без запроса на каждое сообщение. Точное число приносит
  // chats:sync. «Без звука» читаем из списка чатов: его рисует сервер.
  const muted = (chat) => !!document.querySelector(`[data-row="${chat}"]`)?.hasAttribute("data-muted");

  function count(chat, author, reading) {
    const badge = document.getElementById("unread-badge");
    if (!badge || String(author) === me || reading || muted(chat)) return; // своё и прочитанное не считаем
    badge.textContent = Number(badge.textContent || 0) + 1;
    badge.classList.remove("hidden");
    paintTitle();
  }

  // Число — и в заголовке вкладки: фоновая вкладка иначе не покажет, что написали.
  const plainTitle = document.title;
  function paintTitle() {
    const badge = document.getElementById("unread-badge");
    const unread = badge && !badge.classList.contains("hidden") ? Number(badge.textContent) : 0;
    document.title = unread ? `(${unread}) ${plainTitle}` : plainTitle;
  }
  // Счётчик перерисовывает и сервер (chats:recount, chats:sync).
  document.body.addEventListener("htmx:afterSettle", paintTitle);
  paintTitle();

  // Собеседник дочитал до `upto` — переставляем галочки своих сообщений на месте.
  function ticks(upto, by) {
    if (String(by) === me) return; // это мы сами прочитали чужое, свои галочки ни при чём
    document.querySelectorAll("#messages [data-tick]").forEach((tick) => {
      if (Number(tick.dataset.tick) > upto) return;
      tick.classList.replace("fa-check", "fa-check-double");
      tick.classList.add("text-accent");
    });
  }

  // «Печатает…» — единственное, что вкладка шлёт в сокет; не чаще раза в 4 секунды.
  let typedAt = 0;
  function typing(chat) {
    if (!socket || socket.readyState !== WebSocket.OPEN || Date.now() - typedAt < 4000) return;
    typedAt = Date.now();
    socket.send(JSON.stringify({ typing: Number(chat) }));
  }

  function deliver({ chat, msg, author, read, by, kind, typing: typist, who, notifications }) {
    // Колокольчик: фоновая вкладка тоже переспрашивает — запрос не помечает прочитанным.
    if (notifications) {
      fire("notifications:changed");
      return;
    }
    const open = String(chat) === openChat();
    // Метка «печатает» живёт секунды: ничего не перерисовывает и на сервер не ходит
    if (typist) {
      if (String(typist) !== me) fire("chats:typing", { chat, who });
      return;
    }
    // Прочтение не трогает ни ленту, ни счётчик: только галочки открытого чата
    if (read) {
      if (open) ticks(read, by);
      return;
    }
    // Открытый чат на видимой вкладке сейчас сходит за сообщением и пометит прочитанным
    if (msg) count(chat, author, open && !document.hidden);

    // Фоновую вкладку не трогаем: запрос пометил бы невиденное прочитанным. Догоним при возврате.
    if (document.hidden) {
      missed = true;
      return;
    }
    // Обновляем только то, что могло измениться (вид — в событии, chats/events.py): реакция —
    // ни список, ни счётчик; правка — строку списка; удаление — и счётчик.
    if (!msg && kind !== "react" && kind !== "edit") fire("chats:recount");
    if (kind !== "react") fire("chats:event", { chat });
    // Своё сообщение вкладка уже показала в ответ на отправку — второй раз не ходим
    if (open && !(msg && document.getElementById(`msg-${msg}`))) fire("chats:current", { chat });
  }

  function connect() {
    socket = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/chats/`);
    socket.onopen = () => {
      const recovered = attempt > 0;
      attempt = 0;
      stopFallback();
      if (recovered) sync(); // пока лежали, события шли мимо — догоняем по курсору
    };
    socket.onmessage = (event) => deliver(JSON.parse(event.data));
    socket.onclose = () => {
      // 1, 2, 4… до 30с: если сервер лежит, не добиваем его переподключениями
      const wait = Math.min(1000 * 2 ** attempt++, 30000);
      if (attempt > 2) startFallback(); // не поднимается (прокси?) — включаем запасной опрос
      setTimeout(connect, wait);
    };
  }

  function startFallback() {
    if (!fallback) fallback = setInterval(() => !document.hidden && sync(), 10000);
  }

  function stopFallback() {
    clearInterval(fallback);
    fallback = null;
  }

  document.addEventListener("visibilitychange", () => !document.hidden && missed && sync());
  connect();
  return { sync, typing, alive: () => !!socket && socket.readyState === WebSocket.OPEN };
})();

// Кто где печатает: id чата → имя. Метка гаснет по времени: события «перестал» нет.
const typists = new Map();

// «печатает…» вместо превью в списке чатов. Список перерисовывает htmx — метку
// восстанавливаем после каждой перерисовки.
function paintTyping() {
  document.querySelectorAll("[data-row]").forEach((row) => {
    const busy = typists.has(row.dataset.row);
    row.querySelector("[data-typing]")?.classList.toggle("hidden", !busy);
    row.querySelector("[data-quiet]")?.classList.toggle("hidden", busy);
  });
}

document.body.addEventListener("chats:typing", ({ detail }) => {
  const chat = String(detail.chat);
  clearTimeout(typists.get(chat)?.timer);
  typists.set(chat, {
    who: detail.who,
    timer: setTimeout(() => {
      typists.delete(chat);
      paintTyping();
    }, 6000),
  });
  paintTyping();
});
document.body.addEventListener("htmx:afterSettle", paintTyping);

document.addEventListener("alpine:init", () => {
  // Тосты: Django messages (initial) и событие $dispatch('toast', { type, text }).
  Alpine.data("toasts", (initial = []) => ({
    items: [],
    icons: {
      success: "fa-circle-check text-emerald-500",
      error: "fa-circle-xmark text-rose-500",
      warning: "fa-triangle-exclamation text-amber-500",
      info: "fa-circle-info text-accent",
    },
    init() { initial.forEach((t) => this.add(t)); },
    add({ type, text }) {
      if (!this.icons[type]) type = "info";
      const id = ++this._id;
      this.items.push({ id, type, text });
      setTimeout(() => this.remove(id), 5000);
    },
    remove(id) { this.items = this.items.filter((t) => t.id !== id); },
    _id: 0,
  }));

  // Колокольчик. Число перерисовывает сам значок (notifications/_badge.html); панель
  // грузится при открытии, а открытая обновляется по тому же поводу.
  Alpine.data("bell", () => ({
    open: false,
    init() {
      document.body.addEventListener("notifications:changed", () => this.open && this.load());
    },
    toggle() {
      this.open = !this.open;
      if (this.open) this.load();
    },
    load() {
      htmx.ajax("GET", this.$root.dataset.panel, { target: this.$refs.panel, swap: "innerHTML" });
    },
  }));

  // Плавающий лейбл текстового поля. У date и подобных лейбл поднят всегда.
  const INTRINSIC = ["date", "time", "month", "week", "datetime-local", "color"];
  Alpine.data("field", () => ({
    focused: false, filled: false, alwaysUp: false,
    init() {
      const el = this.$el.querySelector("input, textarea");
      this.filled = !!(el && el.value);
      this.alwaysUp = !!el && INTRINSIC.includes(el.type);
    },
    get up() { return this.focused || this.filled || this.alwaysUp; },
  }));

  // Поиск по списку — по правилам core/search.py: каждое слово в подписи, порядок любой.
  const words = (query) => query.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const hits = (label, parts) => parts.every((part) => label.toLowerCase().includes(part));

  // Общее для select и multiSelect. Только методы и данные: геттеры — в компонентах,
  // spread ...base() превратил бы геттер в значение.
  const base = ({ options, search = false }) => ({
    open: false, focused: false, query: "", activeIndex: 0, options, search,
    scrolling: false, // по списку ведут пальцем — клик в конце жеста выбором не считаем
    room: 0, // чем ограничить высоту списка, когда снизу вылезла клавиатура; 0 — не мешаем

    init() {
      // Клавиатура телефона закрывает низ экрана, не двигая раскладку: это видно только
      // в visualViewport.
      if (!window.visualViewport) return;
      this.refit = () => this.open && this.fit();
      visualViewport.addEventListener("resize", this.refit);
      visualViewport.addEventListener("scroll", this.refit);
    },
    // Фильтры перерисовываются на каждый выбор — снимаем слушатель, иначе они копятся.
    destroy() {
      if (!this.refit) return;
      visualViewport.removeEventListener("resize", this.refit);
      visualViewport.removeEventListener("scroll", this.refit);
    },

    toggle() { this.open ? this.close() : this.openMenu(); },
    openMenu() {
      this.open = true;
      this.activeIndex = 0;
      this.$nextTick(() => this.focusSearch());
    },
    close() { this.open = false; this.query = ""; this.room = 0; },

    // Фокус в поиск — только при мыши: на телефоне клавиатура закрыла бы список.
    // Нажали на поле сами — высоту подгонит fit().
    focusSearch() {
      if (this.search && matchMedia("(pointer: fine)").matches) this.$refs.search.focus();
    },

    // Список не должен уезжать под клавиатуру: ужимаем его до того, что реально видно.
    fit() {
      const view = window.visualViewport;
      // Только если снизу что-то перекрыло окно; иначе высота — из разметки.
      if (!view || !this.$refs.list || view.height >= innerHeight - 1) {
        this.room = 0;
        return;
      }
      const top = this.$refs.list.getBoundingClientRect().top - view.offsetTop;
      this.room = Math.max(96, Math.round(view.height - top - 8));
    },
    onArrow(dir) {
      if (!this.open) { this.openMenu(); return; }
      const n = this.filtered.length;
      if (!n) return;
      this.activeIndex = Math.min(Math.max(this.activeIndex + dir, 0), n - 1);
      this.$nextTick(() => this.$el.querySelector(`[data-opt="${this.activeIndex}"]`)?.scrollIntoView({ block: "nearest" }));
    },
    onEnter() { const o = this.filtered[this.activeIndex]; if (this.open && o) this.pick(o); else this.openMenu(); },
    // relatedTarget === null: на телефоне так выглядит касание самого списка (mousedown во время
    // жеста не приходит), и меню закрывалось под пальцем. Уход мимо закроет @click.outside.
    onFocusOut(e) {
      if (this.$el.contains(e.relatedTarget)) return;
      this.focused = false;
      if (e.relatedTarget) this.close();
    },
    // Значение — в hidden input, правка из Alpine события не даёт: шлём change сами, в nextTick
    // (иначе слушатель прочтёт старое). С корня компонента: у мультиселекта $el — пункт,
    // который к nextTick уже убран, и событие из оторванного узла не всплыло бы.
    changed() {
      const root = this.$root;
      this.$nextTick(() => root.dispatchEvent(new Event("change", { bubbles: true })));
    },
  });

  Alpine.data("select", (cfg) => ({
    ...base(cfg),
    selectedValue: cfg.value ?? null,
    get active() { return this.open || this.focused; },
    get selectedLabel() { const o = this.options.find((o) => o.value === this.selectedValue); return o ? o.label : ""; },
    get filtered() { const parts = words(this.query); return parts.length ? this.options.filter((o) => hits(o.label, parts)) : this.options; },
    pick(o) { this.selectedValue = o.value; this.close(); this.changed(); },
    clear() { this.selectedValue = null; this.changed(); },
  }));

  // Форма с файлами: выбор, лимиты (attachments/uploads.py), прямая загрузка в R2, прогресс.
  // Многочастная — по три части разом, ссылки порциями: на 16 ГБ частей тысяча.
  const PARALLEL = 3;
  const PART_BATCH = 24;
  const PART_TRIES = 4;
  const pause = (ms) => new Promise((done) => setTimeout(done, ms));

  // Начатую загрузку помним по файлу (имя, размер, время правки): после обрыва не лить заново.
  const resumeKey = (file) => `upload:${file.name}:${file.size}:${file.lastModified}`;
  const recallUpload = (file) => {
    try { return localStorage.getItem(resumeKey(file)) || ""; } catch { return ""; }
  };
  const rememberUpload = (file, token) => {
    try { localStorage.setItem(resumeKey(file), token); } catch { /* приватный режим */ }
  };
  const forgetUpload = (file) => {
    try { localStorage.removeItem(resumeKey(file)); } catch { /* приватный режим */ }
  };

  Alpine.data("fileForm", (config = {}, saved = []) => ({
    items: [], // { file, name, size, percent, done, token } — он же и список на экране
    flying: new Set(), // XHR в полёте: по ним отменяем загрузку
    sending: new Map(), // номер части → сколько её байт уже ушло, для шкалы
    token: null, // начатая многочастная загрузка: по ней доводим отмену до хранилища
    cancelled: false,
    // marked заводим сразу: на неизвестном ключе :disabled внутри x-for ведёт себя непредсказуемо.
    saved: saved.map((file) => ({ ...file, marked: false })),
    errors: [],
    over: false,
    percent: null,
    sent: 0, // байт в уже залитых файлах — из них считается общий процент

    add(files) {
      if (this.percent !== null) return; // идёт загрузка: новые файлы прошли бы мимо прогресса
      this.errors = [];
      if (config.single) this.items = []; // форма на один файл: новый выбор заменяет прежний
      for (const file of files) {
        // Предел на пачку — из-за денег: сырьё лежит в бакете, пока до него не дойдёт пекарня.
        if (config.maxFiles && this.items.length >= config.maxFiles) {
          this.errors.push(`За раз можно сдать не больше ${config.maxFiles} файлов`);
          break;
        }
        const problem = this.problem(file);
        if (problem) {
          this.errors.push(problem);
          continue;
        }
        if (this.items.some((item) => item.file.name === file.name && item.file.size === file.size)) continue;
        this.items.push({
          id: ++pickedFiles, file, name: file.name, size: humanSize(file.size),
          // Название — по имени файла; поправить можно в строке.
          title: file.name.replace(/\.[^.]+$/, "").slice(0, 150),
          percent: null, done: false, posted: false, failed: "", token: null,
        });
      }
      this.syncInput();
    },
    get weight() {
      return humanSize(this.items.reduce((sum, item) => sum + item.file.size, 0));
    },
    // Тот же отказ, что и на сервере, но до отправки: незачем гнать гигабайт впустую.
    problem(file) {
      const extension = file.name.includes(".") ? file.name.split(".").pop().toLowerCase() : "";
      const { forbidden = [], maxSize = 0 } = config;
      if (forbidden.includes(extension)) return `«${file.name}» — такой тип файла загружать нельзя`;
      if (maxSize && file.size > maxSize) return `«${file.name}» больше ${humanSize(maxSize)}`;
      return null;
    },
    remove(index) {
      this.items.splice(index, 1);
      this.syncInput();
    },
    // В инпуте — только то, что не уехало в хранилище; залитые остаются в списке.
    syncInput() {
      const data = new DataTransfer(); // FileList только для чтения, собираем заново
      for (const item of this.items) if (!item.done) data.items.add(item.file);
      this.$refs.input.files = data.files;
    },

    // Узел двигает x-sort; приводим состояние в тот же порядок, иначе Alpine вернёт строку.
    move(list, from, position) {
      if (from === -1 || from === position) return;
      list.splice(position, 0, ...list.splice(from, 1));
    },
    reorderSaved(pk, position) {
      this.move(this.saved, this.saved.findIndex((file) => file.pk === pk), position);
    },
    // Порядок строк — это и порядок отправки.
    reorderNew(id, position) {
      this.move(this.items, this.items.findIndex((file) => file.id === id), position);
      this.syncInput();
    },

    // htmx:confirm — единственное место, где запрос можно отложить на async: сначала льём
    // файлы в хранилище, в форме остаются подписанные токены.
    async beforeSend(event) {
      if (this.percent !== null) return event.preventDefault(); // уже льём, второй раз не начинаем
      const pending = this.items.filter((item) => !item.done);
      if (!config.direct || !pending.length) return; // некуда или нечего — обычная отправка
      event.preventDefault();

      this.errors = [];
      this.cancelled = false;
      this.flying.clear();
      this.begin();
      this.sent = 0;
      const total = pending.reduce((sum, item) => sum + item.file.size, 0);
      if (config.each) return this.sendEach(pending, total);
      try {
        for (const item of pending) {
          // Отмену ловим и между файлами, и во время подписи: там abort() прерывать нечего.
          if (this.cancelled) throw new Error("Загрузка отменена");
          item.percent = 0;
          const token = await this.send(item, total);
          this.sent += item.file.size;
          // Помечаем сразу: повтор после ошибки на следующем файле не зальёт этот дважды.
          item.percent = 100;
          item.done = true;
          item.token = token; // скрытое поле рисуется из строки, уберут строку — уйдёт и токен
          this.syncInput();
        }
      } catch (error) {
        this.errors = this.cancelled ? [] : [error.message];
        for (const item of pending) if (!item.done) item.percent = null;
        this.flying.clear();
        this.end();
        return; // форму не отправляем: книга без файлов никому не нужна
      }

      // Недогруженный файл не должен уехать «молча»: форма сохранилась бы без него.
      if (this.items.some((item) => !item.done)) {
        this.end();
        return;
      }

      // Скрытые поля с токенами Alpine рисует в микрозадаче, а issueRequest собирает форму
      // синхронно: без паузы последний файл молча пропал бы.
      await this.$nextTick();
      const carried = new FormData(this.$el).getAll("uploaded");
      const lost = this.items.find((item) => !carried.includes(item.token));
      if (lost) {
        this.errors = [`«${lost.name}» не попал в форму — сохрани ещё раз`];
        this.end();
        return;
      }

      this.percent = 100;
      event.detail.issueRequest(true);
    },

    // Каждая запись — своим запросом сразу за своим файлом: упавшая не уносит остальные.
    async sendEach(pending, total) {
      for (const item of pending) {
        if (this.cancelled) break;
        item.failed = "";
        item.percent = 0;
        try {
          item.token = item.token || (await this.send(item, total));
          this.sent += item.file.size;
          const body = new FormData();
          body.append("title", item.title);
          body.append("uploaded", item.token);
          item.next = (await this.hand(config.each, body)).redirect;
          item.percent = 100;
          item.done = true;
          item.posted = true; // повтор после ошибки на соседнем файле не заведёт вторую копию
        } catch (error) {
          item.percent = null;
          item.failed = this.cancelled ? "" : error.message;
        }
      }
      this.end();
      this.syncInput();
      // Что-то не вышло — остаёмся: причина у своей строки. Ответ может сказать, куда идти
      // (у печати — страница задания).
      if (!this.items.some((item) => item.failed) && !this.cancelled) {
        const next = this.items.find((item) => item.next)?.next;
        if (next) location.assign(next);
        else location.reload();
      }
    },

    // Тот же запрос, что и подпись, но телом формы: вьюха ждёт обычные поля.
    async hand(url, body) {
      const answer = await fetch(url, {
        method: "POST",
        headers: {
          Accept: "application/json",
          "X-CSRFToken": this.$el.querySelector("[name=csrfmiddlewaretoken]").value,
        },
        body,
      });
      const data = await answer.json().catch(() => ({}));
      if (!answer.ok) throw new Error(data.error || `Сервер ответил ${answer.status}`);
      return data;
    },

    // Маленький файл — одним PUT, большой — частями. Возвращает токен для формы.
    async send(item, total) {
      if (config.partsFrom && item.file.size >= config.partsFrom) return this.sendParts(item, total);
      const { url, token, type } = await this.ask(config.signUrl, { name: item.file.name, size: item.file.size });
      if (this.cancelled) throw new Error("Загрузка отменена");
      await this.putWhole(url, item, total, type);
      return token;
    },

    async ask(url, body) {
      const response = await fetch(url, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRFToken": this.$el.querySelector("[name=csrfmiddlewaretoken]").value,
        },
        body: JSON.stringify(body),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || "Не удалось получить ссылку на загрузку");
      return data;
    },

    putWhole(url, item, total, type) {
      return this.put(url, item.file, {
        onProgress: (loaded) => {
          item.percent = Math.round((loaded / item.file.size) * 100);
          this.percent = Math.round(((this.sent + loaded) / total) * 100);
        },
        broken: `Не удалось загрузить «${item.name}»`,
        type,
      });
    },

    // Отдать кусок в хранилище. Возвращает ETag: для части это расписка для сборки объекта.
    put(url, blob, { onProgress, broken, tag = false, type = "" }) {
      return new Promise((resolve, reject) => {
        const request = new XMLHttpRequest();
        this.flying.add(request);
        const finish = (fn, value) => { this.flying.delete(request); fn(value); };
        request.open("PUT", url);
        // Тип назначает сервер, и он входит в подпись: с file.type браузера хранилище откажет.
        if (type) request.setRequestHeader("Content-Type", type);
        request.upload.onprogress = (e) => e.lengthComputable && onProgress(e.loaded);
        request.onload = () => {
          if (request.status >= 300) {
            return finish(reject, new Error(`Хранилище ответило ${request.status}`));
          }
          const etag = request.getResponseHeader("ETag");
          // ETag браузер даёт прочитать, только если бакет разрешил (ExposeHeaders: ETag в CORS).
          if (tag && !etag) {
            return finish(reject, new Error("Хранилище не отдало ETag части — в CORS бакета нужен ExposeHeaders: ETag"));
          }
          finish(resolve, etag);
        };
        request.onerror = () => finish(reject, new Error(broken));
        request.onabort = () => finish(reject, new Error("Загрузка отменена"));
        request.send(blob);
      });
    },

    // ── частями ──────────────────────────────────────────────────────────────
    async sendParts(item, total) {
      const file = item.file;
      const started = await this.ask(config.startUrl, {
        name: file.name, size: file.size, resume: recallUpload(file),
      });
      rememberUpload(file, started.token);
      this.token = started.token; // по нему отмена доводится до хранилища

      const step = started.partSize;
      const count = Math.max(1, Math.ceil(file.size / step));
      const parts = new Map(Object.entries(started.done).map(([number, tag]) => [Number(number), tag]));
      const todo = [];
      for (let number = 1; number <= count; number += 1) if (!parts.has(number)) todo.push(number);

      // Лежащее в хранилище уже отдано: шкала после обрыва не начинается с нуля.
      let settled = (count - todo.length) * step;
      this.sending.clear();
      const progress = () => {
        const inflight = [...this.sending.values()].reduce((sum, bytes) => sum + bytes, 0);
        const loaded = Math.min(file.size, settled + inflight);
        item.percent = Math.round((loaded / file.size) * 100);
        this.percent = Math.round(((this.sent + loaded) / total) * 100);
      };
      progress();

      while (todo.length) {
        if (this.cancelled) throw new Error("Загрузка отменена");
        const batch = todo.splice(0, PART_BATCH);
        const { urls } = await this.ask(config.partsUrl, { token: started.token, numbers: batch });
        await this.pool(batch, async (number) => {
          const slice = file.slice((number - 1) * step, Math.min(number * step, file.size));
          const tag = await this.putPart(urls[number], slice, number, progress);
          parts.set(number, tag);
          this.sending.delete(number);
          settled += slice.size;
          progress();
        });
      }

      const done = await this.ask(config.finishUrl, {
        token: started.token, parts: Object.fromEntries(parts),
      });
      forgetUpload(file);
      this.token = null;
      return done.token;
    },

    // Несколько частей разом, но не все: при обрыве незавершённые переливаются заново.
    async pool(numbers, work) {
      const queue = [...numbers];
      const runner = async () => {
        while (queue.length) {
          if (this.cancelled) throw new Error("Загрузка отменена");
          await work(queue.shift());
        }
      };
      await Promise.all(Array.from({ length: Math.min(PARALLEL, queue.length) }, runner));
    },

    // Часть повторяем сама по себе: за долгую отдачу одна сорвётся почти наверняка.
    async putPart(url, slice, number, progress) {
      for (let attempt = 1; ; attempt += 1) {
        try {
          this.sending.set(number, 0);
          return await this.put(url, slice, {
            onProgress: (loaded) => { this.sending.set(number, loaded); progress(); },
            broken: `Часть ${number} не доехала`,
            tag: true,
          });
        } catch (error) {
          this.sending.delete(number);
          progress();
          if (this.cancelled || attempt >= PART_TRIES) throw error;
          await pause(attempt * 1000); // каждый раз ждём дольше: сеть могла и лечь
        }
      }
    },

    // Залитые файлы остаются. Прерванная отдача может доехать — сироту убирает clean_uploads.
    // Начатую многочастную бросаем и в хранилище: её части занимают место.
    cancel() {
      this.cancelled = true;
      for (const request of this.flying) request.abort();
      this.flying.clear();
      if (this.token) {
        this.ask(config.abortUrl, { token: this.token }).catch(() => {});
        this.token = null;
      }
    },

    begin() {
      if (this.percent === null) this.percent = 0; // после прямой загрузки шкала уже на 100
      window.addEventListener("beforeunload", warnOnLeave);
    },
    // Охранника снимаем на htmx:before-on-load, не на afterRequest: HX-Redirect приходит
    // раньше, и браузер спросил бы «точно уйти?» на нашем же переходе.
    end() {
      this.percent = null;
      window.removeEventListener("beforeunload", warnOnLeave);
    },
    // Следом придёт прогресс ОТВЕТА со своим total — шкалу назад не откатываем.
    track({ detail }) {
      if (this.percent === null || !detail.lengthComputable) return;
      this.percent = Math.max(this.percent, Math.round((detail.loaded / detail.total) * 100));
    },
  }));

  // Просмотрщик картинок: крупно, стрелки, зум, перетаскивание. Своими руками: готовые тянут сборку.
  Alpine.data("lightbox", (items = []) => ({
    items,
    index: 0,
    open: false,
    scale: 1,
    x: 0,
    y: 0,
    drag: null, // { x, y } — точка захвата, пока тянем
    moved: false, // тянули или просто щёлкнули: клик приходит уже после mouseup

    show(index) {
      this.index = index;
      this.fit();
      this.open = true;
    },
    close() {
      this.open = false;
      this.drag = null;
    },
    step(delta) {
      this.index = (this.index + delta + this.items.length) % this.items.length;
      this.fit(); // соседнюю картинку показываем целиком, а не в чужом масштабе
    },
    fit() {
      this.scale = 1;
      this.x = 0;
      this.y = 0;
    },

    // Масштаб — вокруг точки под курсором: место, куда целились, остаётся на виду.
    zoomAt(factor, event) {
      const next = Math.min(Math.max(this.scale * factor, 1), 6);
      if (next === this.scale) return;
      if (next === 1) return this.fit();

      const frame = this.$refs.frame.getBoundingClientRect();
      const centerX = frame.left + frame.width / 2;
      const centerY = frame.top + frame.height / 2;
      const pointX = event ? event.clientX : centerX;
      const pointY = event ? event.clientY : centerY;
      // Точка картинки под курсором в её собственных координатах.
      const ownX = (pointX - centerX - this.x) / this.scale;
      const ownY = (pointY - centerY - this.y) / this.scale;
      this.x += (this.scale - next) * ownX;
      this.y += (this.scale - next) * ownY;
      this.scale = next;
      this.hold();
    },
    // Колесо масштабирует, а не листает страницу за спиной у оверлея.
    onWheel(event) {
      this.zoomAt(event.deltaY < 0 ? 1.25 : 1 / 1.25, event);
    },
    // Один клик, а не двойной: курсор показывает лупу, значит клика и ждут.
    onClick(event) {
      if (this.moved) return; // это был конец перетаскивания
      this.scale === 1 ? this.zoomAt(2.5, event) : this.fit();
    },

    grab(event) {
      this.moved = false;
      if (this.scale === 1) return; // нечего таскать, картинка и так целиком
      const point = event.touches ? event.touches[0] : event;
      this.drag = { x: point.clientX - this.x, y: point.clientY - this.y };
    },
    move(event) {
      if (!this.drag) return;
      const point = event.touches ? event.touches[0] : event;
      this.x = point.clientX - this.drag.x;
      this.y = point.clientY - this.drag.y;
      this.moved = true;
      this.hold();
    },
    drop() {
      this.drag = null;
    },
    // Картинку не утащить за край.
    hold() {
      const picture = this.$refs.picture;
      const frame = this.$refs.frame;
      if (!picture || !frame) return;
      const spareX = Math.max(0, (picture.offsetWidth * this.scale - frame.clientWidth) / 2);
      const spareY = Math.max(0, (picture.offsetHeight * this.scale - frame.clientHeight) / 2);
      this.x = Math.min(Math.max(this.x, -spareX), spareX);
      this.y = Math.min(Math.max(this.y, -spareY), spareY);
    },

    get current() {
      return this.items[this.index] ?? {};
    },
    get transform() {
      return `translate(${this.x}px, ${this.y}px) scale(${this.scale})`;
    },
  }));

  // Галерея материала: картинки едут обычным multipart — только выбор, порядок и удаление.
  // Типы — те, что покажет любой браузер (apng — тот же png); список сверяет attachments/tests.py.
  const PICTURE_TYPES = ["image/jpeg", "image/png", "image/apng", "image/webp", "image/gif"];
  Alpine.data("gallery", (saved = [], maxSize = 0) => ({
    saved: saved.map((image) => ({ ...image, marked: false })),
    picked: [], // { id, file, url } — url живёт до отправки формы, это objectURL
    errors: [],

    // Тот же отказ, что на сервере, но до отправки.
    add(files) {
      this.errors = [];
      for (const file of files) {
        // HEIC — и по имени: тип браузер называет не всегда. Последнее слово — за сервером
        // (attachments.uploads.check_picture).
        if (/\.hei[cf]$/i.test(file.name) || /^image\/hei[cf]/.test(file.type)) {
          this.errors.push(`«${file.name}» — формат HEIC браузеры не показывают. Сохрани как JPEG и загрузи снова`);
          continue;
        }
        if (!PICTURE_TYPES.includes(file.type)) {
          this.errors.push(`«${file.name}» — подойдут JPEG, PNG, WebP и GIF`);
          continue;
        }
        if (maxSize && file.size > maxSize) {
          this.errors.push(`«${file.name}» больше ${humanSize(maxSize)}`);
          continue;
        }
        if (this.picked.some((item) => item.file.name === file.name && item.file.size === file.size)) continue;
        this.picked.push({ id: ++pickedFiles, file, url: URL.createObjectURL(file) });
      }
      this.syncInput();
    },
    remove(index) {
      URL.revokeObjectURL(this.picked[index].url); // иначе картинка висит в памяти до перезагрузки
      this.picked.splice(index, 1);
      this.syncInput();
    },
    syncInput() {
      const data = new DataTransfer(); // FileList только для чтения, собираем заново
      for (const item of this.picked) data.items.add(item.file);
      this.$refs.images.files = data.files;
    },
    // Порядок задаётся только у сохранённых: новые всегда встают в конец.
    reorderSaved(pk, position) {
      const from = this.saved.findIndex((image) => image.pk === pk);
      if (from === -1 || from === position) return;
      this.saved.splice(position, 0, ...this.saved.splice(from, 1));
    },
  }));

  // Переключатель сортировки: значение в hidden input, change шлём сами (см. changed()).
  Alpine.data("segmented", (value) => ({
    value,
    pick(v) {
      if (v === this.value) return;
      this.value = v;
      this.$nextTick(() => this.$refs.field.dispatchEvent(new Event("change", { bubbles: true })));
    },
  }));

  // Сжатие фото перед отправкой: снимок телефона — 4000 пикселей, в ленте — триста. webp
  // держит прозрачность и текст; где его нет, canvas молча вернёт png.
  const shrink = async (file, side, quality) => {
    const bitmap = await createImageBitmap(file, { imageOrientation: "from-image" });
    const scale = Math.min(1, side / Math.max(bitmap.width, bitmap.height));
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(bitmap.width * scale);
    canvas.height = Math.round(bitmap.height * scale);
    canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    bitmap.close();
    const blob = await new Promise((done) => canvas.toBlob(done, "image/webp", quality));
    return blob;
  };

  const renamed = (blob, name, suffix) =>
    new File([blob], `${name.replace(/\.[^.]+$/, "")}${suffix}`, { type: blob.type });

  // Файлы в скрытое поле формы: DataTransfer отдаёт готовый FileList для обычного multipart.
  const carry = (input, files) => {
    const box = new DataTransfer();
    files.forEach((file) => box.items.add(file));
    input.files = box.files;
  };

  // Свайп вправо — ответ на сообщение. Первые пиксели ещё могут быть прокруткой ленты.
  const SWIPE_FROM = 12;  // с этого сдвига считаем, что тянут вбок
  const SWIPE_AT = 56;    // и с этого — что ответ
  const SWIPE_MAX = 72;

  Alpine.data("chat", (listUrl, peopleUrl, limits) => ({
    replyTo: null,
    typing: "", // «Валерий печатает…» в шапке вместо строки состояния
    swipe: null,
    actions: false, // открыто меню чата (⋮ в шапке)
    searching: false, // открыт поиск по чату: находки занимают место ленты
    people: null, // участники для подсказки упоминаний; тянем один раз, по первой собачке
    hints: [], // что показывает подсказка сейчас
    hintAt: 0, // где в тексте стоит собачка, с которой всё началось
    active: 0, // выбранная строка подсказки
    picked: [], // выбранные вложения: { id, name, file, preview?, url? }
    attachOpen: false,
    busy: false, // идёт сжатие — отправлять пока нечего
    lightbox: "",
    typed: 0, // длина набранного: по ней у самого потолка показываем остаток
    // Enter на телефоне — перенос строки: отправка там кнопкой.
    coarse: matchMedia("(pointer: coarse)").matches,
    pinned: true, // держимся низа, пока человек не прокрутил вверх
    below: 0, // сколько сообщений пришло, пока лента прокручена вверх
    readers: false, // открыто окно «кто прочитал»
    menu: null, // открытое меню сообщения: { id, author, preview, react, edit, del, readers }
    sheet: false, // узкий экран — меню показываем шторкой снизу
    pos: null, // позиция попапа; пока null, меню держим невидимым (ещё не примерено к экрану)
    init() {
      const box = this.$refs.box;
      this.draftKey = `chat-draft:${this.$el.dataset.chatId}`;
      this.restore();
      // Открываемся на черте «непрочитанные», если она есть, а не в конце.
      const start = () => {
        // Пришли из поиска — встаём на найденном.
        const found = box.querySelector("[data-at]");
        const mark = box.querySelector("[data-unread]");
        if (found) found.scrollIntoView({ block: "center" });
        else if (mark) box.scrollTop = mark.offsetTop - 12;
        else this.toBottom();
      };
      const found = box.querySelector("[data-at]");
      if (found) {
        // Подсвечиваем пузырь, а не строку во всю ширину ленты.
        const spot = found.querySelector("[data-msg]") || found;
        spot.classList.add("flash");
        setTimeout(() => spot.classList.remove("flash"), 2500);
      }
      // Высота ленты растёт после init (аватары, шрифты) — скроллим дважды.
      start();
      requestAnimationFrame(start);
      window.addEventListener("load", start, { once: true });
      box.querySelectorAll("img").forEach((img) => {
        if (!img.complete) img.addEventListener("load", start, { once: true });
      });

      box.addEventListener("scroll", () => {
        this.pinned = box.scrollHeight - box.scrollTop - box.clientHeight < 150;
        if (this.pinned) this.below = 0; // долистал сам — считать больше нечего
      });

      // История вставляется СВЕРХУ — запоминаем позицию, иначе лента прыгнет.
      this.$el.addEventListener("htmx:beforeSwap", (e) => {
        this.loadingHistory = e.detail.target.id === "history-top";
        this.prevHeight = box.scrollHeight;
        this.prevTop = box.scrollTop;
        this.prevCount = box.querySelectorAll("[data-id]").length;
      });
      this.$el.addEventListener("htmx:afterSwap", () => {
        if (this.loadingHistory) {
          box.scrollTop = this.prevTop + (box.scrollHeight - this.prevHeight);
          this.loadingHistory = false;
          return;
        }
        this.dedupe();
        // Пока лента не у низа, приехавшее вниз не видно — считаем его для кнопки
        if (!this.pinned) this.below += box.querySelectorAll("[data-id]").length - this.prevCount;
        this.toBottom();
      });
      // Не hx-on на форме: оттуда не достать Alpine-состояние (replyTo).
      this.$el.addEventListener("htmx:afterRequest", (e) => {
        if (e.detail.successful && e.detail.elt.matches?.("form.chat-send")) {
          e.detail.elt.reset();
          this.grow(); // reset очищает текст, но высоту поле держит свою, выставленную
          this.replyTo = null;
          this.clear();
          this.forget();
          this.toBottom(true); // своё сообщение всегда показываем
        }
      });

      // Чат исчез, пока страница открыта (группу удалили, из неё исключили): на 404 по
      // адресу чата уводим на список. 404 отдельного сообщения чат не закрывает.
      const own = `/chats/${this.$el.dataset.chatId}/`;
      this.$el.addEventListener("htmx:responseError", (e) => {
        const { status, responseURL } = e.detail.xhr;
        if (status === 404 && responseURL.includes(own)) location.assign(listUrl + "?gone=1");
        else if (status === 429) this.$dispatch("toast", { type: "warning", text: "Слишком часто. Подождите немного" });
        else if (status === 422) this.$dispatch("toast", { type: "warning", text: e.detail.xhr.responseText });
      });

      // «Печатает» этого чата — в шапку. Гаснет само: события «перестал» нет.
      document.body.addEventListener("chats:typing", ({ detail }) => {
        if (String(detail.chat) !== this.$el.dataset.chatId) return;
        this.typing = `${detail.who} печатает…`;
        clearTimeout(this.typingOff);
        this.typingOff = setTimeout(() => (this.typing = ""), 6000);
      });
    },
    // --- поиск по чату ---------------------------------------------------------
    openSearch() {
      this.searching = true;
      this.$nextTick(() => this.$refs.find.focus());
    },
    // Строку чистим: вернулись к переписке — прошлый запрос в поле только мешает
    closeSearch() {
      this.searching = false;
      this.$refs.find.value = "";
      this.toBottom();
    },

    // Печатаем. Пустое поле не в счёт: стёр набранное — это уже не набор.
    hint() {
      if (this.$refs.input.value.trim()) chatSocket.typing(this.$el.dataset.chatId);
      this.suggest();
    },

    // --- упоминания ------------------------------------------------------------
    // Подсказка на собачку в начале слова. Участников тянем один раз и фильтруем у себя.
    async suggest() {
      const box = this.$refs.input;
      const upto = box.value.slice(0, box.selectionStart);
      // Собачка в начале слова и набранное после; пробел в имени допускаем («@Иванов Ив»).
      const start = upto.search(/(?:^|\s)@[^\n@]{0,40}$/);
      if (start === -1) return (this.hints = []);
      this.hintAt = upto.lastIndexOf("@");
      if (this.people === null) {
        this.people = await fetch(peopleUrl).then((answer) => answer.json()).catch(() => []);
      }
      const parts = words(upto.slice(this.hintAt + 1));
      // Без запроса показываем начало списка — иначе одна собачка не даёт ничего
      this.hints = (parts.length ? this.people.filter((one) => hits(one.name, parts)) : this.people).slice(0, 6);
      this.active = 0;
    },
    // Имя целиком: по нему сервер и узнаёт, кого позвали (chats/mentions.py)
    pickMention(person) {
      const box = this.$refs.input;
      const tail = box.value.slice(box.selectionStart);
      box.value = `${box.value.slice(0, this.hintAt)}@${person.name} ${tail}`;
      const caret = this.hintAt + person.name.length + 2;
      this.hints = [];
      box.focus();
      box.setSelectionRange(caret, caret);
      this.grow();
    },
    hintMove(step, event) {
      if (!this.hints.length) return;
      event.preventDefault(); // стрелки ходят по подсказке, а не по тексту
      this.active = (this.active + step + this.hints.length) % this.hints.length;
    },
    reply(data) {
      this.replyTo = data;
      this.$refs.input.focus();
    },

    // --- вложения -------------------------------------------------------------
    addPhotos(event) {
      this.take([...event.target.files], true);
      event.target.value = ""; // иначе тот же файл второй раз не выбрать
    },
    addDocs(event) {
      this.take([...event.target.files], false);
      event.target.value = "";
    },
    // Из буфера и перетаскиванием: картинку жмём, остальное отправляем как есть
    dropped(event) {
      const files = [...(event.dataTransfer || event.clipboardData).files];
      if (!files.length) return;
      event.preventDefault();
      this.take(files.filter((f) => f.type.startsWith("image/")), true);
      this.take(files.filter((f) => !f.type.startsWith("image/")), false);
    },
    async take(files, asPhoto) {
      const room = limits.files - this.picked.length;
      if (files.length > room) {
        this.$dispatch("toast", { type: "warning", text: `Не больше ${limits.files} вложений за раз` });
      }
      this.busy = true;
      try {
        for (const file of files.slice(0, Math.max(room, 0))) {
          const item = asPhoto ? await this.asPhoto(file) : this.asDoc(file);
          if (item) this.picked.push(item);
        }
      } finally {
        this.busy = false;
        this.load();
      }
    },
    heavy(name) {
      this.$dispatch("toast", { type: "warning", text: `«${name}» слишком большой` });
      return null;
    },
    async asPhoto(file) {
      // Сжатое бывает тяжелее исходника — у уже маленькой картинки. Тогда шлём исходник.
      const small = await shrink(file, 2560, 0.85).catch(() => null);
      const body = small && small.size < file.size ? renamed(small, file.name, ".webp") : file;
      if (body.size > limits.photo) return this.heavy(file.name);
      const thumb = await shrink(file, 400, 0.7).catch(() => null);
      return {
        id: `${Date.now()}-${Math.random()}`,
        name: body.name,
        file: body,
        preview: thumb ? renamed(thumb, file.name, ".thumb.webp") : null,
        url: URL.createObjectURL(thumb || body),
      };
    },
    asDoc(file) {
      if (file.size > limits.doc) return this.heavy(file.name);
      return { id: `${Date.now()}-${Math.random()}`, name: file.name, file, preview: null, url: "" };
    },
    drop(id) {
      const gone = this.picked.find((item) => item.id === id);
      if (gone?.url) URL.revokeObjectURL(gone.url);
      this.picked = this.picked.filter((item) => item.id !== id);
      this.load();
    },
    // Раскладываем выбранное по полям. Фото и миниатюры сервер сопоставляет по порядку:
    // миниатюры либо у всех, либо ни у одной.
    load() {
      const photos = this.picked.filter((item) => item.url);
      const thumbs = photos.map((item) => item.preview);
      carry(this.$refs.photoBox, photos.map((item) => item.file));
      carry(this.$refs.previewBox, thumbs.every(Boolean) ? thumbs : []);
      carry(this.$refs.docBox, this.picked.filter((item) => !item.url).map((item) => item.file));
    },
    clear() {
      this.picked.forEach((item) => item.url && URL.revokeObjectURL(item.url));
      this.picked = [];
      this.load();
    },
    // Черновик хранится в этом браузере до отправки.
    restore() {
      try {
        this.$refs.input.value = localStorage.getItem(this.draftKey) || "";
      } catch { /* приватный режим — просто без черновиков */ }
      this.grow();
    },
    remember() {
      try {
        const text = this.$refs.input.value;
        text ? localStorage.setItem(this.draftKey, text) : localStorage.removeItem(this.draftKey);
      } catch { /* см. restore */ }
    },
    forget() {
      try { localStorage.removeItem(this.draftKey); } catch { /* см. restore */ }
    },
    // Поле растёт под текст. Сначала auto — иначе scrollHeight помнит прежнюю высоту.
    // Потолок — max-h в разметке.
    grow() {
      const box = this.$refs.input;
      box.style.height = "auto";
      // + рамка: scrollHeight её не считает, а высота поля — по border-box.
      box.style.height = `${box.scrollHeight + box.offsetHeight - box.clientHeight}px`;
      this.typed = box.value.length;
      this.remember();
    },
    send(event) {
      if (event.isComposing) return;
      // Открыта подсказка — Enter выбирает из неё, и на телефоне тоже.
      if (this.hints.length) {
        event.preventDefault();
        return this.pickMention(this.hints[this.active]);
      }
      if (event.shiftKey || this.coarse) return; // перенос строки
      event.preventDefault();
      if (this.busy) return; // фото ещё сжимается: уехал бы один текст, без него
      if (this.$refs.input.value.trim() || this.picked.length) this.$refs.input.form.requestSubmit();
    },
    // --- свайп для ответа ------------------------------------------------------
    // Свайп вправо — ответ, как в мессенджерах.
    swipeStart(event) {
      const bubble = event.target.closest("[data-msg]");
      // Внутри строки реакций свой жест вбок — там прокрутка, а не ответ
      if (!bubble || event.target.closest("[data-sideways]")) return (this.swipe = null);
      const touch = event.touches[0];
      this.swipe = { bubble, row: bubble.closest("[data-id]"), x: touch.clientX, y: touch.clientY, dx: 0, on: false };
    },
    swipeMove(event) {
      if (!this.swipe) return;
      const { clientX, clientY } = event.touches[0];
      const dx = clientX - this.swipe.x;
      const dy = clientY - this.swipe.y;
      if (!this.swipe.on) {
        // Пока не ясно, тянут вбок или листают ленту, прокрутке не мешаем
        if (Math.abs(dy) > Math.abs(dx)) return (this.swipe = null);
        if (dx < SWIPE_FROM) return;
        this.swipe.on = true;
        const arrow = this.$refs.swipe;
        const box = this.$refs.box.getBoundingClientRect();
        const row = this.swipe.row.getBoundingClientRect();
        arrow.style.top = `${row.top - box.top + row.height / 2 - 8}px`;
        arrow.classList.remove("!hidden");
      }
      event.preventDefault(); // тянем сообщение, а не ленту
      this.swipe.dx = Math.min(dx - SWIPE_FROM, SWIPE_MAX);
      this.swipe.row.style.transform = `translateX(${this.swipe.dx}px)`;
      this.$refs.swipe.style.opacity = Math.min(1, this.swipe.dx / SWIPE_AT);
      if (this.swipe.dx >= SWIPE_AT && !this.swipe.buzzed) {
        this.swipe.buzzed = true;
        navigator.vibrate?.(10); // порог взят — руке об этом лучше сказать
      }
    },
    swipeEnd() {
      const swipe = this.swipe;
      this.swipe = null;
      if (!swipe?.on) return;
      swipe.row.style.transition = "transform .15s";
      swipe.row.style.transform = "";
      setTimeout(() => (swipe.row.style.transition = ""), 200);
      this.$refs.swipe.classList.add("!hidden");
      const d = swipe.bubble.dataset;
      if (swipe.dx >= SWIPE_AT) this.reply({ id: d.msg, author: d.author, text: d.preview });
    },
    openMenu(event) {
      const bubble = event.target.closest("[data-msg]");
      if (!bubble || event.target.closest("a, button")) return; // ссылки, чипы реакций и цитата работают сами
      // Выделение текста в этом пузыре — не вызов меню.
      const picked = getSelection();
      if (picked && !picked.isCollapsed && bubble.contains(picked.anchorNode)) return;
      event.preventDefault();
      const d = bubble.dataset;
      this.sheet = innerWidth < 640;
      this.pos = null;
      this.menu = {
        id: d.msg, author: d.author, preview: d.preview,
        react: d.reactUrl, edit: d.editUrl, del: d.delUrl, readers: d.readersUrl,
      };
      if (this.sheet) return; // шторке позиция не нужна — она прижата к низу экрана
      const { clientX, clientY } = event;
      this.$nextTick(() => {
        const el = this.$refs.menu;
        const gap = 8;
        this.pos = {
          left: Math.max(gap, Math.min(clientX, innerWidth - (el ? el.offsetWidth : 224) - gap)),
          top: Math.max(gap, Math.min(clientY, innerHeight - (el ? el.offsetHeight : 220) - gap)),
        };
      });
    },
    menuAction(kind) {
      const m = this.menu;
      this.menu = null;
      if (kind === "reply") this.reply({ id: m.id, author: m.author, text: m.preview });
      else if (kind === "edit") this.request("GET", m.edit, m.id);
      else if (kind === "readers") this.showReaders(m.readers);
      else if (kind === "delete" && confirm("Удалить сообщение?")) this.request("POST", m.del, m.id);
    },
    // Список — по нажатию: рассылать живые «прочитано» всем на каждый опрос накладно.
    showReaders(url) {
      this.readers = true;
      htmx.ajax("GET", url, { source: this.$el, target: "#readers-body", swap: "innerHTML" });
    },
    react(emoji) {
      const m = this.menu;
      this.menu = null;
      this.request("POST", m.react, m.id, { emoji });
    },
    // source обязателен: по нему htmx поднимается по дереву за hx-headers с <body> — там CSRF-токен
    request(verb, url, id, values) {
      const target = "#msg-" + id;
      htmx.ajax(verb, url, { source: document.querySelector(target), target, swap: "outerHTML", values });
    },
    // Отправка и догрузка могут принести одно сообщение дважды; на стыке порций —
    // второй разделитель дня.
    dedupe() {
      const seen = new Set();
      this.$refs.box.querySelectorAll("[data-id], [data-day]").forEach((el) => {
        const key = el.dataset.id ? `m${el.dataset.id}` : `d${el.dataset.day}`;
        seen.has(key) ? el.remove() : seen.add(key);
      });
    },
    toBottom(force = false) {
      if (force) this.pinned = true;
      if (this.pinned) this.$refs.box.scrollTop = this.$refs.box.scrollHeight;
    },
  }));

  // Одна картинка в комментарии или отзыве: показываем прицепленную, даём снять.
  // `cleared` уходит в поле «<имя>-clear» — Django очищает картинку сам.
  Alpine.data("imagePick", (saved = "") => ({
    url: saved,
    name: "",
    cleared: false,
    pick(event) {
      const file = event.target.files[0];
      if (!file) return;
      this.url = URL.createObjectURL(file);
      this.name = file.name;
      this.cleared = false; // выбрали новую — снимать прежнюю не надо, она и так заменится
    },
    drop() {
      this.$refs.input.value = "";
      this.url = "";
      this.name = "";
      this.cleared = !!saved; // снимать в базе нечего, если её там и не было
    },
  }));

  // Видео в витрине — источник только когда плитка в экране: Safari на телефоне держит
  // считанные декодеры, и десяток автоплеев его роняет.
  Alpine.data("lazyVideo", () => ({
    watcher: null,

    init() {
      const video = this.$el;
      this.watcher = new IntersectionObserver(([entry]) => {
        if (!entry.isIntersecting) return video.pause();
        if (!video.src) video.src = video.dataset.src;
        video.play().catch(() => {}); // автоплей могли запретить — останется постер
      }, { rootMargin: "300px" });
      this.watcher.observe(video);
    },

    // Наблюдатель держит элемент ссылкой — без этого узел не соберётся после htmx-подмены.
    destroy() {
      this.watcher?.disconnect();
    },
  }));

  // Картинка на полке магазина — источник, когда плитку видно: loading=lazy качает с запасом
  // в два экрана. У видео — только постер, играет его полка (shelf).
  Alpine.data("lazyMedia", () => ({
    watcher: null,

    init() {
      const media = this.$el;
      this.watcher = new IntersectionObserver(([entry]) => {
        if (!entry.isIntersecting) return;
        if (media.tagName === "VIDEO") media.poster = media.dataset.poster;
        else media.src = media.dataset.src;
        this.watcher.disconnect();
      }, { rootMargin: "200px 0px" });
      this.watcher.observe(media);
    },

    destroy() {
      this.watcher?.disconnect();
    },
  }));

  // Полка магазина: листается стрелками и пальцем, подпись «1–6 из 29» следит за прокруткой.
  // Играют видео, что видны на полке (на телефоне — одно); ушедшее за край — на паузе.
  Alpine.data("shelf", () => ({
    first: 0,
    shown: 1,
    total: 0,
    seen: false,
    watcher: null,

    init() {
      const track = this.$refs.track;
      if (!track) return; // полка пустая после отбора — листать нечего
      this.total = track.children.length;
      this.watcher = new IntersectionObserver(([entry]) => {
        this.seen = entry.isIntersecting;
        this.play();
      });
      this.watcher.observe(track);
      this.measure();
    },

    destroy() {
      this.watcher?.disconnect();
    },

    get label() {
      if (this.shown >= this.total) return "";
      const last = Math.min(this.first + this.shown, this.total);
      return `${last > this.first + 1 ? `${this.first + 1}–${last}` : last} из ${this.total}`;
    },

    get atStart() {
      return this.first <= 0;
    },

    get atEnd() {
      return this.first + this.shown >= this.total;
    },

    // Шаг ленты — плитка вместе с промежутком, то есть расстояние между соседями.
    pitch() {
      const [one, two] = this.$refs.track.children;
      return two ? two.offsetLeft - one.offsetLeft : one?.offsetWidth || 1;
    },

    measure() {
      const track = this.$refs.track;
      if (!track) return;
      const pitch = this.pitch();
      this.first = Math.round(track.scrollLeft / pitch);
      this.shown = Math.max(1, Math.round(track.clientWidth / pitch));
      this.play();
    },

    step(direction) {
      this.$refs.track.scrollBy({ left: direction * this.shown * this.pitch(), behavior: "smooth" });
    },

    play() {
      [...this.$refs.track.children].forEach((tile, index) => {
        const video = tile.querySelector("video");
        if (!video) return;
        if (this.seen && index >= this.first && index < this.first + this.shown) {
          if (!video.src) video.src = video.dataset.src;
          video.play().catch(() => {}); // автоплей могли запретить — останется постер
        } else {
          video.pause();
        }
      });
    },
  }));

  // Рулетка кейса: исход решён сервером ДО анимации. Картинку выигрыша ждём не дольше 1,5 с.
  // Стрелка встаёт не ровно в середину плитки: ровная выглядит подстроенной.
  Alpine.data("roulette", () => ({
    done: false,
    track: null,
    target: 0,

    init() {
      this.track = this.$refs.track;
      const prize = this.track?.querySelector("[data-prize]");
      if (!prize) return this.finish();
      const jitter = (Math.random() - 0.5) * prize.offsetWidth * 0.7;
      this.target = prize.offsetLeft + prize.offsetWidth / 2 + jitter - this.track.parentElement.clientWidth / 2;
      const picture = prize.querySelector("img");
      const loaded = !picture || picture.complete
        ? Promise.resolve()
        : new Promise((go) => {
          picture.addEventListener("load", go, { once: true });
          picture.addEventListener("error", go, { once: true });
          setTimeout(go, 1500);
        });
      loaded.then(() => requestAnimationFrame(() => this.spin()));
    },

    spin() {
      if (this.done) return;
      if (matchMedia("(prefers-reduced-motion: reduce)").matches) return this.skip();
      this.track.style.transition = "transform 4.6s cubic-bezier(0.1, 0.75, 0.15, 1)";
      this.track.style.transform = `translateX(${-this.target}px)`;
      this.track.addEventListener("transitionend", () => this.finish(), { once: true });
    },

    // Докрутить сразу: закрыли окно или нажали на ленту — пусть увидят выпавшее.
    skip() {
      if (this.done) return;
      this.track.style.transition = "none";
      this.track.style.transform = `translateX(${-this.target}px)`;
      this.finish();
    },

    finish() {
      if (this.done) return;
      this.done = true;
      this.$el.classList.add("roulette-done");
      const coins = document.getElementById("coins");
      if (coins && this.$el.dataset.coins) coins.textContent = this.$el.dataset.coins;
    },
  }));

  // Библиотека HLS — одна на страницу: обещание запоминаем.
  let hlsLibrary = null;
  const loadHls = (src) => {
    if (window.Hls) return Promise.resolve(true);
    hlsLibrary ||= new Promise((done) => {
      const tag = document.createElement("script");
      tag.src = src;
      tag.onload = () => done(true);
      tag.onerror = () => done(false);
      document.head.append(tag);
    });
    return hlsLibrary;
  };

  // Качество помнится между записями — по высоте, а не по номеру дорожки: номера у записей разные.
  const QUALITY = "lecture-quality";
  const remembered = () => {
    try { return Number(localStorage.getItem(QUALITY)) || 0; } catch { return 0; }
  };
  const remember = (height) => {
    try { localStorage.setItem(QUALITY, height); } catch { /* приватный режим */ }
  };

  // Стрелки — на 10 секунд. После клика по полосе перемотки хром двигает стрелками её
  // ползунок на 1% длины, и перехватить это нельзя — поэтому фокус возвращаем на видео.
  const SEEK_STEP = 10;
  const keepFocus = (video) => {
    // Только если фокус уже в плеере: не выдёргиваем человека из поля комментария.
    if (document.activeElement === video) video.focus({ preventScroll: true });
  };
  const seekByKeys = (video, event) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
    if (!Number.isFinite(video.duration)) return;
    // Иначе родное управление сдвинуло бы ещё на свой шаг (в файрфоксе — по keypress).
    event.preventDefault();
    event.stopPropagation();
    const step = event.key === "ArrowRight" ? SEEK_STEP : -SEEK_STEP;
    video.currentTime = Math.min(Math.max(video.currentTime + step, 0), video.duration);
  };

  // Выбор страниц на печать: превью, поле «1-3,7» и сетка — одно и то же. Разбор — копия
  // printing.pricing.pages_of; сумма здесь — прикидка, в задание идёт расчёт сервера.
  Alpine.data("printPick", (id) => {
    const config = JSON.parse(document.getElementById(id).textContent);
    const word = (n) => (n % 10 === 1 && n % 100 !== 11 ? "страница"
      : n % 10 >= 2 && n % 10 <= 4 && !(n % 100 >= 12 && n % 100 <= 14) ? "страницы" : "страниц");
    return {
      chosen: Array(config.pages).fill(false),
      text: config.selection,
      copies: config.copies,
      duplex: config.duplex,
      printerId: config.printer,
      error: "",
      init() { this.apply(); },
      parse(text) {
        const chosen = new Set();
        const cleaned = text.trim().replace(/\s*[-–—]\s*/g, "-");
        for (const part of cleaned.split(/[,;\s]+/).filter(Boolean)) {
          const match = /^(\d+)(?:-(\d+))?$/.exec(part);
          if (!match) throw new Error(`Не понял «${part}». Номера и диапазоны пишутся через запятую, например 1-3,7`);
          const [first, last] = [Number(match[1]), Number(match[2] || match[1])].sort((a, b) => a - b);
          if (first < 1 || last > config.pages) {
            throw new Error(`Страницы ${first < 1 ? first : last} нет: в документе ${config.pages} ${word(config.pages)}`);
          }
          for (let number = first; number <= last; number += 1) chosen.add(number);
        }
        return chosen;
      },
      apply() {
        try {
          const chosen = this.parse(this.text);
          this.chosen = this.chosen.map((_, index) => chosen.has(index + 1));
          this.error = "";
        } catch (problem) {
          this.error = problem.message;
        }
      },
      toggle(number) {
        this.chosen[number - 1] = !this.chosen[number - 1];
        this.text = this.selection;
        this.error = "";
      },
      all(on) {
        this.chosen = this.chosen.map(() => on);
        this.text = this.selection;
        this.error = "";
      },
      // [1, 2, 3, 7] → «1-3,7», как pricing.selection_of
      get selection() {
        const parts = [];
        let start = null;
        this.chosen.forEach((on, index) => {
          const number = index + 1;
          if (on && start === null) start = number;
          if (start !== null && (!on || number === this.chosen.length)) {
            const end = on ? number : number - 1;
            parts.push(end > start ? `${start}-${end}` : `${start}`);
            start = null;
          }
        });
        return parts.join(",");
      },
      get count() { return this.chosen.filter(Boolean).length; },
      get copiesValid() { return Number.isInteger(this.copies) && this.copies >= 1 && this.copies <= config.maxCopies; },
      get copyCount() { return this.copiesValid ? this.copies : 0; },
      // Недоступные принтеры в настройки не попадают: выбрать их нельзя и считать незачем.
      get chosenPrinter() { return config.printers[this.printerId] || null; },
      get canDuplex() { return Boolean(this.chosenPrinter?.duplex); },
      // Одна страница с двух сторон — та же односторонняя: переключатель тогда спрятан.
      get twoSided() { return this.duplex && this.canDuplex && this.count > 1; },
      get sheets() { return this.copyCount * (this.twoSided ? Math.ceil(this.count / 2) : this.count); },
      get sides() { return this.copyCount * this.count; },
      get fits() { return config.left === null || this.sides <= config.left; },
      // Логическим, а не через «||»: пустая строка ошибки для Alpine — значение, и :disabled
      // с ней выключил бы кнопку навсегда.
      get ready() { return this.count > 0 && this.copiesValid && this.fits && !this.error && Boolean(this.chosenPrinter); },
      // Цены за единицу, копейки: лист, страница обычного текста (5%), барабан на страницу.
      get rates() {
        const c = this.chosenPrinter?.costs;
        if (!c) return { paper: 0, toner: 0, drum: 0 };
        return { paper: c.paper_sheet, toner: c.toner_price / c.toner_yield * c.toner_factor, drum: c.drum_price / c.drum_yield };
      },
      get parts() {
        const shares = config.coverage.filter((_, index) => this.chosen[index]);
        const toner = shares.reduce((sum, share) => sum + this.rates.toner * (share / 0.05), 0);
        return {
          paper: this.rates.paper * this.sheets,
          toner: this.copyCount * toner,
          drum: this.copyCount * this.count * this.rates.drum,
        };
      },
      get total() { return this.parts.paper + this.parts.toner + this.parts.drum; },
      rub(kopecks) {
        const whole = Math.round(kopecks);
        return `${Math.floor(whole / 100)},${String(whole % 100).padStart(2, "0")} ₽`;
      },
    };
  });

  Alpine.data("lecturePlayer", (src) => {
    // Плеер и счётчик — в замыкании, не в данных: Alpine оборачивает данные в Proxy, а hls.js
    // шлёт свои объекты в Worker через postMessage — прокси не клонируется.
    let player = null;
    let rescues = 0;
    let rescuedAt = 0;  // где стояло воспроизведение при последнем спасении, секунды

    return {
      problem: "",
      levels: [],   // дорожки для меню качества, сверху вниз
      choice: -1,   // выбранная дорожка; -1 — автовыбор по скорости связи
      playing: 0,   // высота дорожки, которая идёт прямо сейчас
      open: false,  // раскрыто ли меню

      async init() {
        const video = this.$refs.video;
        video.addEventListener("keydown", (event) => seekByKeys(video, event), true);
        for (const type of ["seeked", "play", "pause"]) {
          video.addEventListener(type, () => keepFocus(video));
        }

        // Библиотеку тянем, только если есть MediaSource (на айфоне его нет). Сами, а не тегом
        // в шапке: тег пришлось бы ставить до ядра Alpine на каждой странице с плеером.
        const library = "MediaSource" in window && await loadHls(this.$el.dataset.library);
        if (library && window.Hls.isSupported()) {
          player = new window.Hls();
          player.on(window.Hls.Events.ERROR, (_, data) => this.rescue(data));
          player.on(window.Hls.Events.MANIFEST_PARSED, () => this.gotLevels());
          player.on(window.Hls.Events.LEVEL_SWITCHED, (_, data) => { this.playing = this.heightOf(data.level); });
          // Счётчик — про сбои ПОДРЯД: ушли на 10 с дальше места сбоя — сеть ожила. По
          // воспроизведению, а не загрузке: битый кусок тоже грузится успешно.
          video.addEventListener("timeupdate", () => {
            if (rescues && video.currentTime > rescuedAt + 10) rescues = 0;
          });
          player.loadSource(src);
          player.attachMedia(video);
          return;
        }

        // Родной HLS — запасной путь: `canPlayType` отвечает «maybe» и без HLS, а родной плеер
        // не переживает обрыв. На айфоне эта ветка единственная.
        if (video.canPlayType("application/vnd.apple.mpegurl")) {
          video.src = src;
          return;
        }
        this.problem = "Не удалось загрузить проигрыватель. Обнови страницу.";
      },

      // Дорожки — после разбора мастер-манифеста. В меню — числа и подписи, не объекты
      // библиотеки: из Proxy Alpine их не вернуть.
      gotLevels() {
        this.levels = player.levels
          .map((one, index) => ({ index, height: one.height, label: `${one.height}p` }))
          .reverse();  // библиотека выдаёт снизу вверх, а в меню привычнее лучшее сверху
        const wanted = this.levels.find((one) => one.height === remembered());
        if (wanted) this.pick(wanted.index);
      },

      heightOf(index) {
        return player?.levels[index]?.height || 0;
      },

      // В автовыборе показываем, что идёт на самом деле.
      get label() {
        if (this.choice < 0) return this.playing ? `Авто · ${this.playing}p` : "Авто";
        return `${this.heightOf(this.choice)}p`;
      },

      pick(index) {
        player.currentLevel = index;  // -1 — вернуть автовыбор
        this.choice = index;
        this.open = false;
        remember(index < 0 ? 0 : this.heightOf(index));
      },

      // Обрыв и сбой декодера библиотека переживает по просьбе — но не бесконечно: счётчик.
      rescue(data) {
        if (!data.fatal) return;
        const kinds = window.Hls.ErrorTypes;
        const curable = data.type === kinds.NETWORK_ERROR || data.type === kinds.MEDIA_ERROR;
        if (rescues < 3 && curable) {
          rescues += 1;
          rescuedAt = this.$refs.video.currentTime;
          return data.type === kinds.NETWORK_ERROR ? player.startLoad() : player.recoverMediaError();
        }
        this.problem = "Видео оборвалось. Обнови страницу.";
        player.destroy();
        player = null;
        this.levels = [];  // выбирать качество больше не у чего
      },

      destroy() {
        player?.destroy();
      },
    };
  });

  // Аватар: человек сам выбирает квадрат из фото — мышью или пальцем, масштаб ползунком,
  // щипком или колесом. Наружу — вырезанный квадрат data-URL'ом в скрытом поле (сервер
  // всё равно пережимает, users/forms.py); значение обновляем после каждого движения.
  const AVATAR_PX = 512;
  Alpine.data("avatarPick", (saved = "", limit = 2000000) => {
    // Всё, чего не касается разметка, — здесь, не в данных: drawImage не принимает Proxy Alpine.
    let img = null;
    let x = 0, y = 0; // левый верхний угол картинки в координатах окошка
    let side = 0; // сторона окошка на экране, css-пиксели
    let grab = null; // точка захвата при перетаскивании
    const touches = new Map(); // пальцы на экране: два — это щипок
    let span = 0; // расстояние между ними на прошлом кадре

    return {
      saved,
      picked: false, // выбрали новый файл — разметке нужно только это
      whole: false, // гифка: едет файлом целиком, кадр для неё не выбирают
      cleared: false,
      scale: 1, min: 1, max: 1,

      pick(event) {
        const file = event.target.files[0];
        if (!file) return;
        // Гифку канвас сделал бы одним кадром — она уедет файлом из input.
        this.whole = file.type === "image/gif";
        if (!this.whole) event.target.value = "";
        const url = URL.createObjectURL(file);
        const next = new Image();
        next.onload = () => {
          img = next;
          this.picked = true;
          this.cleared = false;
          this.fit();
          URL.revokeObjectURL(url);
        };
        next.src = url;
      },

      drop() {
        this.$refs.input.value = "";
        img = null;
        this.picked = false;
        this.whole = false;
        this.cleared = !!saved; // снимать в базе нечего, если её там и не было
        this.commit();
      },

      // Начальный кадр: картинка целиком накрывает квадрат и стоит по центру.
      fit() {
        side = this.$refs.frame.getBoundingClientRect().width;
        this.min = Math.max(side / img.naturalWidth, side / img.naturalHeight);
        this.max = this.min * 5;
        this.scale = this.min;
        x = (side - img.naturalWidth * this.scale) / 2;
        y = (side - img.naturalHeight * this.scale) / 2;
        this.render();
        this.commit();
      },

      // Картинка обязана накрывать окошко целиком — иначе в углу кадра оказалась бы пустота.
      clamp() {
        x = Math.min(0, Math.max(side - img.naturalWidth * this.scale, x));
        y = Math.min(0, Math.max(side - img.naturalHeight * this.scale, y));
      },

      // Масштаб — вокруг точки захвата (по умолчанию центр), иначе кадр уползает.
      zoom(next, cx = side / 2, cy = side / 2) {
        if (!img) return;
        next = Math.min(this.max, Math.max(this.min, next));
        const k = next / this.scale;
        x = cx - (cx - x) * k;
        y = cy - (cy - y) * k;
        this.scale = next;
        this.clamp();
        this.render();
      },

      render() {
        const canvas = this.$refs.canvas;
        const dpr = window.devicePixelRatio || 1;
        canvas.width = canvas.height = Math.round(side * dpr);
        this.paint(canvas.getContext("2d"), canvas.width, dpr);
      },

      // k — во сколько раз холст крупнее окошка на экране.
      paint(ctx, size, k) {
        ctx.clearRect(0, 0, size, size);
        ctx.drawImage(
          img, x * k, y * k,
          img.naturalWidth * this.scale * k, img.naturalHeight * this.scale * k,
        );
      },

      // Значение поля: пусто — не трогать, clear — снять, data-URL — заменить.
      commit() {
        // Гифка уже лежит в файловом поле — строкой её дублировать нечего.
        this.$refs.out.value = this.whole ? "" : (img ? this.crop() : (this.cleared ? "clear" : ""));
      },

      crop() {
        const canvas = document.createElement("canvas");
        canvas.width = canvas.height = AVATAR_PX;
        const ctx = canvas.getContext("2d");
        this.paint(ctx, AVATAR_PX, AVATAR_PX / side);

        // jpeg вчетверо легче, но не умеет прозрачность — а её на аватарах любят.
        let value = canvas.toDataURL(this.opaque(ctx) ? "image/jpeg" : "image/png", 0.9);
        if (value.length > limit) {
          // Шумное фото в png в лимит POST не влезает. Подкладываем белое: иначе
          // при переводе в jpeg браузер зальёт прозрачные места чёрным.
          ctx.globalCompositeOperation = "destination-over";
          ctx.fillStyle = "#fff";
          ctx.fillRect(0, 0, AVATAR_PX, AVATAR_PX);
          value = canvas.toDataURL("image/jpeg", 0.85);
        }
        return value;
      },

      opaque(ctx) {
        const data = ctx.getImageData(0, 0, AVATAR_PX, AVATAR_PX).data;
        for (let i = 3; i < data.length; i += 4) if (data[i] < 255) return false;
        return true;
      },

      down(event) {
        if (!img || this.whole) return;
        this.$refs.frame.setPointerCapture(event.pointerId);
        touches.set(event.pointerId, { x: event.clientX, y: event.clientY });
        grab = { x: event.clientX - x, y: event.clientY - y };
        span = this.gap(); // ноль, пока палец один
      },

      move(event) {
        if (!img || !touches.has(event.pointerId)) return;
        touches.set(event.pointerId, { x: event.clientX, y: event.clientY });
        if (span) {
          const now = this.gap();
          const box = this.$refs.frame.getBoundingClientRect();
          const [a, b] = [...touches.values()];
          this.zoom(this.scale * (now / span), (a.x + b.x) / 2 - box.left, (a.y + b.y) / 2 - box.top);
          span = now;
          return;
        }
        x = event.clientX - grab.x;
        y = event.clientY - grab.y;
        this.clamp();
        this.render();
      },

      up(event) {
        touches.delete(event.pointerId);
        const rest = [...touches.values()][0];
        if (rest) {
          // Один палец из двух убрали — перехватываем заново, иначе кадр прыгнет на разницу.
          grab = { x: rest.x - x, y: rest.y - y };
          span = 0;
          return;
        }
        grab = null;
        this.commit();
      },

      gap() {
        const [a, b] = [...touches.values()];
        return b ? Math.hypot(a.x - b.x, a.y - b.y) : 0;
      },

      wheel(event) {
        if (!img || this.whole) return;
        const box = this.$refs.frame.getBoundingClientRect();
        this.zoom(this.scale * (event.deltaY < 0 ? 1.1 : 0.9), event.clientX - box.left, event.clientY - box.top);
        this.commit();
      },
    };
  });

  // Звёзды 1–5 для формы отзыва. Повторный клик по той же звезде снимает оценку.
  Alpine.data("stars", ({ value = null } = {}) => ({
    value,
    hover: null,
    set(n) { this.value = this.value === n ? null : n; },
    filled(n) { return n <= (this.hover ?? this.value ?? 0); },
  }));

  Alpine.data("multiSelect", (cfg) => ({
    ...base(cfg),
    selected: [...(cfg.values ?? [])],
    get active() { return this.open || this.focused; },
    get selectedOptions() { return this.options.filter((o) => this.selected.includes(o.value)); },
    get filtered() { const parts = words(this.query); return this.options.filter((o) => !this.selected.includes(o.value) && hits(o.label, parts)); },
    pick(o) { this.selected.push(o.value); this.query = ""; this.activeIndex = 0; this.changed(); this.focusSearch(); },
    remove(v) { this.selected = this.selected.filter((x) => x !== v); this.changed(); },
  }));

  // Привидение (core/halloween/_ghost.html): нажали — прячется до завтра. Помним дату
  // по местным часам («sv» даёт ГГГГ-ММ-ДД), а не срок.
  const today = () => new Date().toLocaleDateString("sv");
  Alpine.data("ghost", () => ({
    away: false, scared: false,
    init() {
      try { this.away = localStorage.getItem("ghost-away") === today(); } catch { /* приватный режим */ }
    },
    scare() {
      if (this.scared) return;
      this.scared = true;
      try { localStorage.setItem("ghost-away", today()); } catch { /* приватный режим */ }
      // 900 мс — длина skin-vanish и skin-pop в theme/input.css: даём им доиграть.
      setTimeout(() => { this.away = true; }, 900);
    },
  }));
});
