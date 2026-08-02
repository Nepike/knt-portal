// Курсор ленты чата для hx-vals: id последнего сообщения в DOM. Читаем из DOM,
// а не держим в шаблоне — иначе после первой догрузки курсор устареет.
window.lastMessageId = () => {
  const items = document.querySelectorAll("#messages [data-id]");
  return items.length ? items[items.length - 1].dataset.id : 0;
};

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
  });

  Alpine.data("select", (cfg) => ({
    ...base(cfg),
    selectedValue: cfg.value ?? null,
    get active() { return this.open || this.focused; },
    get selectedLabel() { const o = this.options.find((o) => o.value === this.selectedValue); return o ? o.label : ""; },
    get filtered() { const q = this.query.trim().toLowerCase(); return q ? this.options.filter((o) => o.label.toLowerCase().includes(q)) : this.options; },
    pick(o) { this.selectedValue = o.value; this.close(); },
    clear() { this.selectedValue = null; },
  }));

  // Лента чата: автоскролл вниз, защита от дублей (отправка и поллинг могут
  // принести одно и то же сообщение) и состояние «отвечаю на…».
  Alpine.data("chat", () => ({
    replyTo: null,
    pinned: true, // держимся низа, пока человек не прокрутил вверх
    init() {
      const box = this.$refs.box;
      const pin = () => this.toBottom();
      // Высота ленты растёт уже ПОСЛЕ init: догружаются аватары и шрифты.
      // Одного скролла в init мало — длинный диалог остаётся вверху.
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
      // Очистка после удачной отправки: hx-on не видит Alpine-состояние, поэтому тут.
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

  // Палитра реакций. У верхних сообщений раскрываем вниз — иначе её срежет
  // край ленты (у контейнера overflow-y: auto).
  Alpine.data("reactionMenu", () => ({
    open: false,
    up: true,
    toggle() {
      const box = this.$el.closest("#messages");
      const top = this.$el.getBoundingClientRect().top;
      this.up = !box || top - box.getBoundingClientRect().top > 70;
      this.open = !this.open;
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
    pick(o) { this.selected.push(o.value); this.query = ""; this.activeIndex = 0; if (this.search) this.$refs.search.focus(); },
    remove(v) { this.selected = this.selected.filter((x) => x !== v); },
  }));
});
