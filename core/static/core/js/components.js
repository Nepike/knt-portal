// Курсор ленты чата для hx-vals: id последнего сообщения в DOM. Читаем из DOM,
// а не держим в шаблоне — иначе после первой догрузки курсор устареет.
window.lastMessageId = () => {
  const items = document.querySelectorAll("#messages [data-id]");
  return items.length ? items[items.length - 1].dataset.id : 0;
};

// По сокету приходит ТОЛЬКО {"chat": id}: разметка у каждого получателя своя, поэтому
// её клиент забирает обычным запросом. События вешаем на <body> — оттуда их берёт htmx
// через hx-trigger="… from:body".
window.chatSocket = (() => {
  if (!document.body.hasAttribute("data-live")) return { sync() {} }; // не залогинен — некуда

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

  // Вкладка в фоне — ленту не трогаем: запрос за сообщениями пометил бы их
  // прочитанными, а человек их не видел. Копим флаг и догоняем при возврате.
  function deliver(chat) {
    if (document.hidden) {
      missed = true;
      return;
    }
    fire("chats:event", { chat });
    if (String(chat) === openChat()) fire("chats:current", { chat });
  }

  function connect() {
    socket = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/chats/`);
    socket.onopen = () => {
      const recovered = attempt > 0;
      attempt = 0;
      stopFallback();
      if (recovered) sync(); // пока лежали, события шли мимо — догоняем по курсору
    };
    socket.onmessage = (event) => deliver(JSON.parse(event.data).chat);
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

  // Общее для select и multiSelect: открытие/закрытие, фокус, клавиатура.
  // Только методы и данные — геттеры (active/filtered/...) живут в компонентах,
  // т.к. spread ...base() превратил бы геттер в статичное значение.
  const base = ({ options, search = false }) => ({
    open: false, focused: false, query: "", activeIndex: 0, options, search,
    toggle() { this.open ? this.close() : this.openMenu(); },
    openMenu() { this.open = true; this.activeIndex = 0; if (this.search) this.$nextTick(() => this.$refs.search.focus()); },
    close() { this.open = false; this.query = ""; },
    onArrow(dir) {
      if (!this.open) { this.openMenu(); return; }
      const n = this.filtered.length;
      if (!n) return;
      this.activeIndex = Math.min(Math.max(this.activeIndex + dir, 0), n - 1);
      this.$nextTick(() => this.$el.querySelector(`[data-opt="${this.activeIndex}"]`)?.scrollIntoView({ block: "nearest" }));
    },
    onEnter() { const o = this.filtered[this.activeIndex]; if (this.open && o) this.pick(o); else this.openMenu(); },
    onFocusOut(e) { if (!this.$el.contains(e.relatedTarget)) { this.focused = false; this.close(); } },
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
    get filtered() { const q = this.query.trim().toLowerCase(); return q ? this.options.filter((o) => o.label.toLowerCase().includes(q)) : this.options; },
    pick(o) { this.selectedValue = o.value; this.close(); this.changed(); },
    clear() { this.selectedValue = null; this.changed(); },
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

  Alpine.data("chat", () => ({
    replyTo: null,
    pinned: true, // держимся низа, пока человек не прокрутил вверх
    menu: null, // открытое меню сообщения: { id, author, preview, react, edit, del }
    sheet: false, // узкий экран — меню показываем шторкой снизу
    pos: null, // позиция попапа; пока null, меню держим невидимым (ещё не примерено к экрану)
    init() {
      const box = this.$refs.box;
      const pin = () => this.toBottom();
      // Высота ленты растёт уже ПОСЛЕ init: догружаются аватары и шрифты,
      // поэтому одного скролла мало — длинный диалог остался бы вверху.
      pin();
      requestAnimationFrame(pin);
      window.addEventListener("load", pin, { once: true });
      box.querySelectorAll("img").forEach((img) => {
        if (!img.complete) img.addEventListener("load", pin, { once: true });
      });

      box.addEventListener("scroll", () => {
        this.pinned = box.scrollHeight - box.scrollTop - box.clientHeight < 150;
      });

      // Подгрузка истории вставляет сообщения СВЕРХУ — запоминаем позицию,
      // иначе лента прыгнет на высоту добавленного.
      this.$el.addEventListener("htmx:beforeSwap", (e) => {
        this.loadingHistory = e.detail.target.id === "history-top";
        this.prevHeight = box.scrollHeight;
        this.prevTop = box.scrollTop;
      });
      this.$el.addEventListener("htmx:afterSwap", () => {
        if (this.loadingHistory) {
          box.scrollTop = this.prevTop + (box.scrollHeight - this.prevHeight);
          this.loadingHistory = false;
          return;
        }
        this.dedupe();
        this.toBottom();
      });
      // Не hx-on на форме: оттуда не достать Alpine-состояние (replyTo).
      this.$el.addEventListener("htmx:afterRequest", (e) => {
        if (e.detail.successful && e.detail.elt.matches?.("form.chat-send")) {
          e.detail.elt.reset();
          this.replyTo = null;
          this.toBottom(true); // своё сообщение всегда показываем
        }
      });
    },
    reply(data) {
      this.replyTo = data;
      this.$refs.input.focus();
    },
    openMenu(event) {
      const bubble = event.target.closest("[data-msg]");
      // ссылки, чипы реакций и цитата работают сами; выделение текста — тоже не вызов меню
      if (!bubble || event.target.closest("a, button") || String(getSelection())) return;
      event.preventDefault();
      const d = bubble.dataset;
      this.sheet = innerWidth < 640;
      this.pos = null;
      this.menu = { id: d.msg, author: d.author, preview: d.preview, react: d.reactUrl, edit: d.editUrl, del: d.delUrl };
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
      else if (kind === "delete" && confirm("Удалить сообщение?")) this.request("POST", m.del, m.id);
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
    // Отправка и догрузка могут разойтись и принести одно сообщение дважды.
    dedupe() {
      const seen = new Set();
      this.$refs.box.querySelectorAll("[data-id]").forEach((el) => {
        seen.has(el.dataset.id) ? el.remove() : seen.add(el.dataset.id);
      });
    },
    toBottom(force = false) {
      if (force) this.pinned = true;
      if (this.pinned) this.$refs.box.scrollTop = this.$refs.box.scrollHeight;
    },
  }));

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
    get filtered() { const q = this.query.trim().toLowerCase(); return this.options.filter((o) => !this.selected.includes(o.value) && (!q || o.label.toLowerCase().includes(q))); },
    pick(o) { this.selected.push(o.value); this.query = ""; this.activeIndex = 0; this.changed(); if (this.search) this.$refs.search.focus(); },
    remove(v) { this.selected = this.selected.filter((x) => x !== v); this.changed(); },
  }));
});
