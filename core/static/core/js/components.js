// Края ленты чата для hx-vals. Читаем из DOM, а не держим в шаблоне — иначе после
// первой же догрузки курсор устареет.
const feedEdges = () => document.querySelectorAll("#messages [data-id]");

// Нижний край: докуда мы дочитали, отсюда сервер отдаёт новое.
window.lastMessageId = () => {
  const items = feedEdges();
  return items.length ? items[items.length - 1].dataset.id : 0;
};

// Верхний: правку сообщения, которого у нас на экране нет, htmx выбросил бы с ошибкой
// в консоль — за этот край сервер oob-замены не шлёт.
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

// Свой id у каждого выбранного файла: имя можно править, а ключ списка и сортировки
// должен пережить правку — иначе строка пересоздастся прямо во время набора.
let pickedFiles = 0;

// Уход со страницы во время отправки оборвёт загрузку — спрашиваем подтверждение.
// Текст задаёт браузер, свой показать нельзя.
const warnOnLeave = (event) => event.preventDefault();

// По сокету приходит {chat} и, если появилось новое сообщение, ещё {msg, author}.
// Разметка у каждого получателя своя, поэтому её клиент забирает обычным запросом —
// а вот счётчик непрочитанных считает сам. События вешаем на <body>, оттуда их берёт
// htmx через hx-trigger="… from:body":
//   chats:event   — список чатов слева;
//   chats:recount — счётчик у сервера (правка, удаление: сколько их, тут не вычислить);
//   chats:current — лента открытого чата;
//   chats:sync    — всё сразу, после обрыва связи или возвращения на вкладку.
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

  // Счётчик держим у себя. Раньше за каждым чужим сообщением каждая вкладка каждого
  // участника шла на сервер за одним числом, и в чате курса это сотня запросов на
  // одно сообщение — при том, что по сокету уже пришло всё, чтобы прибавить единицу.
  // Точка правды прежняя: chats:sync и любая перезагрузка берут число у сервера.
  function count(chat, author, reading) {
    const badge = document.getElementById("unread-badge");
    if (!badge || String(author) === me || reading) return; // своё и прочитанное не считаем
    badge.textContent = Number(badge.textContent || 0) + 1;
    badge.classList.remove("hidden");
    paintTitle();
  }

  // Число и в заголовке вкладки: фоновая вкладка иначе никак не показывает, что написали,
  // — её не видно целиком, а бейдж живёт внутри страницы.
  const plainTitle = document.title;
  function paintTitle() {
    const badge = document.getElementById("unread-badge");
    const unread = badge && !badge.classList.contains("hidden") ? Number(badge.textContent) : 0;
    document.title = unread ? `(${unread}) ${plainTitle}` : plainTitle;
  }
  // Счётчик перерисовывает и сервер (chats:recount, chats:sync). Отличать его подмену
  // от прочих незачем: перерисовка заголовка — это чтение одного узла.
  document.body.addEventListener("htmx:afterSettle", paintTitle);
  paintTitle();

  // Собеседник дочитал до `upto` — переставляем галочки у своих сообщений. Разметку за
  // этим не перезапрашиваем: состояние тут двоичное, и класс на месте меняется дешевле.
  function ticks(upto, by) {
    if (String(by) === me) return; // это мы сами прочитали чужое, свои галочки ни при чём
    document.querySelectorAll("#messages [data-tick]").forEach((tick) => {
      if (Number(tick.dataset.tick) > upto) return;
      tick.classList.replace("fa-check", "fa-check-double");
      tick.classList.add("text-accent");
    });
  }

  function deliver({ chat, msg, author, read, by, kind }) {
    const open = String(chat) === openChat();
    // Прочтение не трогает ни ленту, ни счётчик: только галочки открытого чата
    if (read) {
      if (open) ticks(read, by);
      return;
    }
    // Открытый чат на видимой вкладке сейчас сходит за сообщением и пометит прочитанным
    if (msg) count(chat, author, open && !document.hidden);

    // Вкладка в фоне — ленту не трогаем: запрос за сообщениями пометил бы их
    // прочитанными, а человек их не видел. Копим флаг и догоняем при возврате.
    if (document.hidden) {
      missed = true;
      return;
    }
    // Обновляем ровно то, что могло измениться (вид изменения приходит в событии,
    // см. chats/events.py). Реакция не трогает ни счётчик, ни список чатов; правка
    // меняет строку в списке, но не счётчик; удаление — и то и другое.
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
  return { sync, alive: () => !!socket && socket.readyState === WebSocket.OPEN };
})();

document.addEventListener("alpine:init", () => {
  // Тосты. Источники: Django messages (initial при рендере) и событие window
  // $dispatch('toast', { type, text }) — для действий без перезагрузки (HTMX и т.п.).
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

  // Обёртка текстового поля: плавающий лейбл по состоянию (как active у селектов).
  // Типы вроде date всегда держат лейбл поднятым — определяем по el.type при init.
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

  // Поиск по списку — по тем же правилам, что и на сервере (core/search.py): запрос
  // режем на слова, каждое должно найтись в подписи, порядок слов любой. Иначе
  // «Иван Иванов» не находил Иванова Ивана — подпись начинается с фамилии.
  const words = (query) => query.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const hits = (label, parts) => parts.every((part) => label.toLowerCase().includes(part));

  // Общее для select и multiSelect: открытие/закрытие, фокус, клавиатура.
  // Только методы и данные — геттеры (active/filtered/...) живут в компонентах,
  // т.к. spread ...base() превратил бы геттер в статичное значение.
  const base = ({ options, search = false }) => ({
    open: false, focused: false, query: "", activeIndex: 0, options, search,
    scrolling: false, // по списку ведут пальцем — клик в конце жеста выбором не считаем
    room: 0, // чем ограничить высоту списка, когда снизу вылезла клавиатура; 0 — не мешаем

    init() {
      // Клавиатура телефона НЕ двигает раскладку — она просто закрывает нижнюю часть
      // экрана. Обычные размеры окна об этом ничего не знают, знает только visualViewport.
      if (!window.visualViewport) return;
      const refit = () => this.open && this.fit();
      visualViewport.addEventListener("resize", refit);
      visualViewport.addEventListener("scroll", refit);
    },

    toggle() { this.open ? this.close() : this.openMenu(); },
    openMenu() {
      this.open = true;
      this.activeIndex = 0;
      this.$nextTick(() => this.focusSearch());
    },
    close() { this.open = false; this.query = ""; this.room = 0; },

    // Фокус в поиск ставим только там, где есть мышь. На телефоне он поднимает клавиатуру,
    // а она закрывает собой тот самый список, который человек и пришёл листать: замерено
    // на экране 375×812 — из 224px списка над клавиатурой остаётся 34, меньше одной строки.
    // Кому нужен поиск, тот нажмёт на поле сам — и тогда за высотой следит fit().
    focusSearch() {
      if (this.search && matchMedia("(pointer: fine)").matches) this.$refs.search.focus();
    },

    // Список не должен уезжать под клавиатуру: ужимаем его до того, что реально видно.
    fit() {
      const view = window.visualViewport;
      // Ограничиваем ТОЛЬКО когда поверх страницы снизу что-то появилось. В обычном
      // состоянии высота задана в разметке, и лезть в неё незачем.
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
    // relatedTarget === null — фокус ушёл «в никуда». На телефоне так выглядит касание
    // самого списка: браузер снимает фокус с селекта, а mousedown, которым мы его держим
    // на десктопе, во время жеста не приходит вовсе — и меню закрывалось под пальцем.
    // Настоящий уход мимо селекта всё равно закроет @click.outside.
    onFocusOut(e) {
      if (this.$el.contains(e.relatedTarget)) return;
      this.focused = false;
      if (e.relatedTarget) this.close();
    },
    // Значение живёт в hidden input и меняется из Alpine, а такая правка события не даёт.
    // Шлём change сами (в nextTick — иначе слушатель прочитает ещё старое значение),
    // чтобы htmx-фильтры и любые формы работали с нашими селектами как с обычными.
    changed() { this.$nextTick(() => this.$el.dispatchEvent(new Event("change", { bubbles: true }))); },
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

  // Форма с файлами: выбор, лимиты, прямая загрузка в R2 и общий прогресс. Висит на самой
  // форме — оттуда видно и htmx-события, и инпут. Лимиты приезжают из attachments/uploads.py.
  // Многочастная загрузка. Три части разом: одна не выбирает канал целиком, а больше —
  // это лишние повторы при обрыве. Ссылки берём порциями: на 16 ГБ частей тысяча,
  // и все разом — полмегабайта ответа и лишний риск, что они протухнут по дороге.
  const PARALLEL = 3;
  const PART_BATCH = 24;
  const PART_TRIES = 4;
  const pause = (ms) => new Promise((done) => setTimeout(done, ms));

  // Помним начатую загрузку, чтобы после обрыва не лить гигабайты заново. Ключ — по
  // самому файлу: имя, размер и время правки. У разных файлов эта тройка совпасть может,
  // но правка файла меняет время, так что на практике этого хватает — на том же держатся
  // и готовые библиотеки возобновляемой загрузки.
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
      for (const file of files) {
        const problem = this.problem(file);
        if (problem) {
          this.errors.push(problem);
          continue;
        }
        if (this.items.some((item) => item.file.name === file.name && item.file.size === file.size)) continue;
        this.items.push({
          id: ++pickedFiles, file, name: file.name, size: humanSize(file.size),
          percent: null, done: false, token: null,
        });
      }
      this.syncInput();
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
    // В инпуте только то, что ещё не уехало в хранилище: залитые файлы остаются
    // в списке (иначе кажется, будто они отвалились), но второй раз не отправляются.
    syncInput() {
      const data = new DataTransfer(); // FileList только для чтения, собираем заново
      for (const item of this.items) if (!item.done) data.items.add(item.file);
      this.$refs.input.files = data.files;
    },

    // Перестановку рисует плагин x-sort (он же двигает узел), нам остаётся привести
    // состояние в тот же порядок — иначе Alpine на следующем рендере вернёт строку назад.
    move(list, from, position) {
      if (from === -1 || from === position) return;
      list.splice(position, 0, ...list.splice(from, 1));
    },
    reorderSaved(pk, position) {
      this.move(this.saved, this.saved.findIndex((file) => file.pk === pk), position);
    },
    // Порядок строк задаёт и порядок отправки: из этого же списка собираются
    // файловый инпут и скрытые поля с токенами и именами.
    reorderNew(id, position) {
      this.move(this.items, this.items.findIndex((file) => file.id === id), position);
      this.syncInput();
    },

    // htmx:confirm — единственное место, где запрос можно отложить и сделать что-то async.
    // Сначала льём файлы прямо в хранилище, в форме остаются только подписанные токены.
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
      try {
        for (const item of pending) {
          // Отмену ловим и между файлами, и во время подписи: там abort() нечего прерывать,
          // а без проверки загрузка поехала бы дальше как ни в чём не бывало.
          if (this.cancelled) throw new Error("Загрузка отменена");
          item.percent = 0;
          const token = await this.send(item, total);
          this.sent += item.file.size;
          // Помечаем сразу: повторная отправка после ошибки на следующем файле
          // не должна залить этот вторым экземпляром.
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

      // Скрытые поля с токенами рисует Alpine, а он правит DOM в микрозадаче.
      // issueRequest собирает форму СИНХРОННО, поэтому без этой паузы поле последнего
      // файла ещё не существует и книга сохраняется без него — молча.
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

    // Маленький файл — одним PUT, большой — частями. Возвращает токен для формы.
    async send(item, total) {
      if (config.partsFrom && item.file.size >= config.partsFrom) return this.sendParts(item, total);
      const { url, token } = await this.ask(config.signUrl, { name: item.file.name, size: item.file.size });
      if (this.cancelled) throw new Error("Загрузка отменена");
      await this.putWhole(url, item, total);
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

    putWhole(url, item, total) {
      return this.put(url, item.file, {
        onProgress: (loaded) => {
          item.percent = Math.round((loaded / item.file.size) * 100);
          this.percent = Math.round(((this.sent + loaded) / total) * 100);
        },
        broken: `Не удалось загрузить «${item.name}»`,
      });
    },

    // Общая отдача куска в хранилище. Возвращает ETag — для части он и есть расписка,
    // без которой объект потом не собрать.
    put(url, blob, { onProgress, broken, tag = false }) {
      return new Promise((resolve, reject) => {
        const request = new XMLHttpRequest();
        this.flying.add(request);
        const finish = (fn, value) => { this.flying.delete(request); fn(value); };
        request.open("PUT", url);
        request.upload.onprogress = (e) => e.lengthComputable && onProgress(e.loaded);
        request.onload = () => {
          if (request.status >= 300) {
            return finish(reject, new Error(`Хранилище ответило ${request.status}`));
          }
          const etag = request.getResponseHeader("ETag");
          // Без ETag часть не пришить к объекту. Заголовок приходит всегда, а вот ЧИТАТЬ
          // его браузер даёт, только если бакет разрешил (ExposeHeaders: ETag в CORS).
          // Молчать тут нельзя: иначе загрузка гигабайтов кончается непонятно чем.
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

      // Уже лежащее в хранилище — это уже отданные байты, и шкала не должна начинаться
      // с нуля: человек, продолживший после обрыва, решил бы, что всё зря.
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

    // Несколько частей разом, но не все: канал один, а каждая незавершённая часть
    // при обрыве переливается заново.
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

    // Часть повторяем сама по себе: на сорока минутах отдачи одна сорвётся почти
    // наверняка, и ронять из-за неё весь файл — значит не иметь возобновления вовсе.
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

    // Залитые файлы остаются: они уже в хранилище. Прерванная отдача может доехать
    // и всё равно (байты браузер отдал) — такую сироту убирает clean_uploads.
    //
    // Начатую многочастную бросаем НА СТОРОНЕ ХРАНИЛИЩА: её части занимают место
    // и стоят денег, а продолжать её человек уже не собирается — он нажал «отмена».
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
    // Снимаем охранника, КАК ТОЛЬКО пришёл ответ (htmx:before-on-load), а не по
    // afterRequest: htmx обрабатывает HX-Redirect раньше, и браузер спрашивал бы
    // «точно уйти?» на нашем же переходе к книге.
    end() {
      this.percent = null;
      window.removeEventListener("beforeunload", warnOnLeave);
    },
    // htmx вешает слушателей и на xhr, и на xhr.upload, поэтому следом придёт прогресс
    // ОТВЕТА со своим total — шкалу назад не откатываем.
    track({ detail }) {
      if (this.percent === null || !detail.lengthComputable) return;
      this.percent = Math.max(this.percent, Math.round((detail.loaded / detail.total) * 100));
    },
  }));

  // Просмотрщик картинок: превью маленькие, по клику — крупно, со стрелками,
  // зумом и перетаскиванием. Своими руками, а не библиотекой: нужного тут строк на сто,
  // а любая готовая тянет за собой сборку, которой у нас нет.
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

    // Масштабируем ВОКРУГ ТОЧКИ под курсором, а не вокруг центра: иначе то место,
    // куда человек целился, уезжает из виду и приходится его догонять.
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
    // Не даём утащить картинку за край: дальше начинается пустота, и обратно
    // её ловить неприятно.
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

  // Галерея материала. Проще fileForm: картинки маленькие и едут обычным multipart,
  // поэтому ни подписанных ссылок, ни прогресса — только выбор, порядок и удаление.
  Alpine.data("gallery", (saved = [], maxSize = 0) => ({
    saved: saved.map((image) => ({ ...image, marked: false })),
    picked: [], // { id, file, url } — url живёт до отправки формы, это objectURL
    errors: [],

    // Тот же отказ, что и на сервере, но до отправки: картинки едут обычным multipart,
    // и отказ по размеру приходил бы уже после того, как всё уехало.
    add(files) {
      this.errors = [];
      for (const file of files) {
        if (!file.type.startsWith("image/")) {
          this.errors.push(`«${file.name}» — это не картинка`);
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

  // Переключатель сортировки списков. Значение живёт в hidden input формы,
  // change шлём вручную — по той же причине, что и в селектах (см. changed()).
  Alpine.data("segmented", (value) => ({
    value,
    pick(v) {
      if (v === this.value) return;
      this.value = v;
      this.$nextTick(() => this.$refs.field.dispatchEvent(new Event("change", { bubbles: true })));
    },
  }));

  // Сжатие фото перед отправкой. Снимок с телефона — это 4000 пикселей и мегабайты,
  // а в ленте он показывается в триста: остальное едет впустую, через мобильный интернет
  // студента и в наш бакет, за который платят. Жмём в webp — он держит и прозрачность
  // (скриншоты), и текст лучше jpeg; где его нет, canvas молча вернёт png.
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

  // Сложить файлы в скрытое поле формы: своей записи у files нет, но DataTransfer
  // отдаёт готовый FileList — так браузер и отправит их обычным multipart.
  const carry = (input, files) => {
    const box = new DataTransfer();
    files.forEach((file) => box.items.add(file));
    input.files = box.files;
  };

  Alpine.data("chat", (listUrl, limits) => ({
    replyTo: null,
    picked: [], // выбранные вложения: { id, name, file, preview?, url? }
    attachOpen: false,
    busy: false, // идёт сжатие — отправлять пока нечего
    lightbox: "",
    typed: 0, // длина набранного: по ней у самого потолка показываем остаток
    // Enter на телефоне — перенос строки: там он на экранной клавиатуре, и отправлять
    // им значит рассылать обрывки фраз. Отправка кнопкой, она рядом с полем.
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
      // Открываемся не на конце, а на черте «непрочитанные», если она есть: иначе после
      // ночи в чате курса человек попадает в конец и листает назад, гадая, где остановился.
      const start = () => {
        const mark = box.querySelector("[data-unread]");
        if (mark) box.scrollTop = mark.offsetTop - 12;
        else this.toBottom();
      };
      // Высота ленты растёт уже ПОСЛЕ init: догружаются аватары и шрифты,
      // поэтому одного скролла мало — длинный диалог остался бы вверху.
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

      // Подгрузка истории вставляет сообщения СВЕРХУ — запоминаем позицию,
      // иначе лента прыгнет на высоту добавленного.
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

      // Чат мог исчезнуть, пока страница открыта: группу удалили, из неё исключили.
      // Слева он из списка пропадёт сам, а эта половина осталась бы жить с открытой
      // перепиской и молча ловить 404 на каждую догрузку — htmx на них не подменяет
      // ничего. Уводим на список, а он уже скажет, что произошло.
      //
      // Сверяемся с адресом: 404 прилетает и на отдельное сообщение, если его снесли
      // из админки, — выгонять из-за него живого человека из живого чата незачем.
      const own = `/chats/${this.$el.dataset.chatId}/`;
      this.$el.addEventListener("htmx:responseError", (e) => {
        const { status, responseURL } = e.detail.xhr;
        if (status === 404 && responseURL.includes(own)) location.assign(listUrl + "?gone=1");
        else if (status === 429) this.$dispatch("toast", { type: "warning", text: "Слишком часто. Подождите немного" });
        else if (status === 422) this.$dispatch("toast", { type: "warning", text: e.detail.xhr.responseText });
      });
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
    // Раскладываем выбранное по полям-носителям. Фото и миниатюры сервер сопоставляет
    // по порядку, поэтому либо миниатюры есть у всех, либо не шлём ни одной: пропусти
    // одна (не собрался canvas) — и дальше каждое фото получило бы чужую.
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
    // Поле ввода растёт под текст. Сначала auto — иначе scrollHeight помнит прежнюю
    // высоту и поле умеет только расти. Потолок задан в разметке (max-h), оттуда же
    // берётся и прокрутка: JS про число строк ничего не знает и знать не должен.
    // Черновик переживает уход со страницы: набрал длинное, отвлёкся на другой чат —
    // и всё пропадало. Хранится у этого браузера и только до отправки.
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
    grow() {
      const box = this.$refs.input;
      box.style.height = "auto";
      // Рамку добавляем отдельно: scrollHeight её не считает, а высота у поля меряется
      // по внешнему краю (border-box). Без этих двух пикселей текст никогда не влезает
      // целиком, и поле показывает полосу прокрутки уже на второй строке.
      box.style.height = `${box.scrollHeight + box.offsetHeight - box.clientHeight}px`;
      this.typed = box.value.length;
      this.remember();
    },
    send(event) {
      if (event.isComposing || event.shiftKey || this.coarse) return; // перенос строки
      event.preventDefault();
      if (this.busy) return; // фото ещё сжимается: уехал бы один текст, без него
      if (this.$refs.input.value.trim() || this.picked.length) this.$refs.input.form.requestSubmit();
    },
    openMenu(event) {
      const bubble = event.target.closest("[data-msg]");
      if (!bubble || event.target.closest("a, button")) return; // ссылки, чипы реакций и цитата работают сами
      // Выделение текста — не вызов меню. Но именно ЭТОГО пузыря: раньше сверялись
      // с выделением где угодно на странице, и одна давняя выделенная строчка в шапке
      // запирала меню совсем — до клика по пустому месту.
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
    // Список считается по нажатию и на этот момент: живые галочки «прочитано» пришлось бы
    // рассылать всем на каждый чужой опрос, а в чате курса это лавина ради двух значков.
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
    // Отправка и догрузка могут разойтись и принести одно сообщение дважды. Заодно
    // разделители дня: у одного дня в ленте он ровно один, а порции считают его каждая
    // сама и на стыке ставят второй.
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

  // Одна картинка в комментарии или отзыве: показываем ту, что уже прицеплена (иначе при
  // правке не понять, есть она вообще или нет), и даём снять её, не трогая сам комментарий.
  // `cleared` уезжает в поле «<имя>-clear», по которому Django очищает картинку сам.
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

  // Аватар: человек сам решает, каким куском фото станет миниатюра. Снимок с телефона
  // бывает 4000×3000 и вертикальный, и в квадрате от него оставался бы случайный кусок.
  // Кадр двигают мышью или пальцем, масштаб — ползунком, щипком или колесом.
  //
  // Наружу уходит не файл, а уже вырезанный квадрат data-URL'ом в скрытом поле: сервер
  // всё равно перерисовывает картинку своим Pillow (users/forms.py), а так не нужен ни
  // второй запрос, ни разбор исходника на сервере. Значение обновляем после каждого
  // движения, чтобы не ловить отправку формы посреди асинхронной работы канваса.
  const AVATAR_PX = 512;
  // Видео вещи в витрине: источник подставляем, только когда плитка попала в экран.
  // Не ради трафика — Safari на телефоне держит считанные видеодекодеры разом, и
  // десяток автоплеев его роняет. До подстановки видно постер, ждать ничего не надо.
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

  // Библиотека HLS общая на страницу и грузится один раз: обещание запоминаем, иначе
  // два плеера на одной странице потянули бы её дважды.
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

  // Выбранное качество помнится между записями: человек с медленным каналом иначе
  // переставлял бы его на каждой лекции заново. Помним ВЫСОТУ, а не номер дорожки:
  // номера у разных записей значат разное — у снятой на 720p дорожка всего одна.
  const QUALITY = "lecture-quality";
  const remembered = () => {
    try { return Number(localStorage.getItem(QUALITY)) || 0; } catch { return 0; }
  };
  const remember = (height) => {
    try { localStorage.setItem(QUALITY, height); } catch { /* приватный режим */ }
  };

  Alpine.data("lecturePlayer", (src) => {
    // Плеер и счётчик — ЗДЕСЬ, в замыкании, а не в данных компонента. Alpine оборачивает
    // данные в Proxy, а hls.js разбирает поток в Worker и шлёт туда свои объекты через
    // postMessage — прокси структурно не клонируется, и разбор падает сразу же:
    // «Failed to execute 'postMessage' on 'Worker': #<Object> could not be cloned».
    // Видно это только в браузере: тесты и статический разбор такое пропускают.
    let player = null;
    let rescues = 0;

    return {
      problem: "",
      levels: [],   // дорожки для меню качества, сверху вниз
      choice: -1,   // выбранная дорожка; -1 — автовыбор по скорости связи
      playing: 0,   // высота дорожки, которая идёт прямо сейчас
      open: false,  // раскрыто ли меню

      async init() {
        const video = this.$refs.video;

        // Библиотеку тянем, только если её есть на чём запускать: на айфоне MediaSource
        // нет вовсе (там любой браузер — Safari внутри), hls.js бесполезен, и полмегабайта
        // телефон качал бы впустую. Проверяем ДО загрузки, а не после.
        //
        // Догружаем сами, а не тегом в шапке: тег пришлось бы ставить ДО ядра Alpine
        // (иначе к init() библиотеки ещё нет), и каждая страница с плеером была бы
        // обязана про это помнить. Забыли — чёрный прямоугольник без объяснений.
        const library = "MediaSource" in window && await loadHls(this.$el.dataset.library);
        if (library && window.Hls.isSupported()) {
          player = new window.Hls();
          player.on(window.Hls.Events.ERROR, (_, data) => this.rescue(data));
          player.on(window.Hls.Events.MANIFEST_PARSED, () => this.gotLevels());
          player.on(window.Hls.Events.LEVEL_SWITCHED, (_, data) => { this.playing = this.heightOf(data.level); });
          player.loadSource(src);
          player.attachMedia(video);
          return;
        }

        // Родной HLS — запасной путь, а не первый. Так советует и сама библиотека:
        // `canPlayType` отвечает «maybe» даже там, где своего HLS на деле нет (поймали
        // на движке хрома), а родной плеер не умеет ни пережить обрыв, ни рассказать
        // о нём. На айфоне же выбора нет, и там эта ветка единственная рабочая.
        if (video.canPlayType("application/vnd.apple.mpegurl")) {
          video.src = src;
          return;
        }
        this.problem = "Не удалось загрузить проигрыватель. Обнови страницу.";
      },

      // Дорожки известны только после разбора мастер-манифеста: до него их нет вовсе.
      // В меню кладём не сами объекты библиотеки, а числа и подписи — данные компонента
      // Alpine оборачивает в Proxy, и такой объект нельзя вернуть библиотеке обратно.
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

      // Подпись на кнопке. В автовыборе показываем, что идёт НА САМОМ ДЕЛЕ: одно слово
      // «Авто» не отвечает на вопрос, ради которого сюда и смотрят.
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

      // Лекцию смотрят час с лишним, и за это время сеть моргнёт наверняка. Обрыв и сбой
      // декодера библиотека умеет пережить, если её попросить, — но не бесконечно: без
      // счётчика безнадёжный случай крутился бы в цикле, добивая и сеть, и батарею.
      rescue(data) {
        if (!data.fatal) return;
        const kinds = window.Hls.ErrorTypes;
        if (rescues < 3 && data.type === kinds.NETWORK_ERROR) {
          rescues += 1;
          return player.startLoad();
        }
        if (rescues < 3 && data.type === kinds.MEDIA_ERROR) {
          rescues += 1;
          return player.recoverMediaError();
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

  Alpine.data("avatarPick", (saved = "", limit = 2000000) => {
    // Всё, чего не касается разметка, держим ЗДЕСЬ, а не в данных компонента: Alpine
    // оборачивает свои данные в Proxy, а drawImage подсунутый вместо Image прокси
    // не принимает. Заодно перетаскивание не дёргает реактивность на каждый кадр.
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
        // Из гифки канвас берёт один кадр, и она перестала бы двигаться. Такую
        // оставляем в самом input — уедет файлом, а сервер сохранит как есть.
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

      // Масштаб меняем относительно точки, за которую тянут (по умолчанию — центр окошка):
      // иначе кадр уползает вбок при каждом движении ползунка.
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

      // Готовое значение поля. Три состояния: пусто — не трогать, clear — снять,
      // data-URL — заменить. Пишем после каждого движения, чтобы отправка формы
      // не пришлась на середину работы канваса.
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
});
