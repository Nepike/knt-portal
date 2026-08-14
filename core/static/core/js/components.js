// Курсор ленты чата для hx-vals: id последнего сообщения в DOM. Читаем из DOM,
// а не держим в шаблоне — иначе после первой догрузки курсор устареет.
window.lastMessageId = () => {
  const items = document.querySelectorAll("#messages [data-id]");
  return items.length ? items[items.length - 1].dataset.id : 0;
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
    scrolling: false, // по списку ведут пальцем — клик в конце жеста выбором не считаем
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
    get filtered() { const q = this.query.trim().toLowerCase(); return q ? this.options.filter((o) => o.label.toLowerCase().includes(q)) : this.options; },
    pick(o) { this.selectedValue = o.value; this.close(); this.changed(); },
    clear() { this.selectedValue = null; this.changed(); },
  }));

  // Форма с файлами: выбор, лимиты, прямая загрузка в R2 и общий прогресс. Висит на самой
  // форме — оттуда видно и htmx-события, и инпут. Лимиты приезжают из attachments/uploads.py.
  Alpine.data("fileForm", ({ maxSize = 0, forbidden = [], direct = false, signUrl = "" } = {}, saved = []) => ({
    items: [], // { file, name, size, percent, done, token } — он же и список на экране
    request: null, // текущий XHR: по нему отменяем загрузку
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
      if (!direct || !pending.length) return; // некуда или нечего — обычная отправка
      event.preventDefault();

      this.errors = [];
      this.cancelled = false;
      this.request = null;
      this.begin();
      this.sent = 0;
      const total = pending.reduce((sum, item) => sum + item.file.size, 0);
      try {
        for (const item of pending) {
          // Отмену ловим и между файлами, и во время подписи: там abort() нечего прерывать,
          // а без проверки загрузка поехала бы дальше как ни в чём не бывало.
          if (this.cancelled) throw new Error("Загрузка отменена");
          item.percent = 0;
          const { url, token } = await this.sign(item.file);
          if (this.cancelled) throw new Error("Загрузка отменена");
          await this.put(url, item, total);
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
        this.request = null;
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

    async sign(file) {
      const response = await fetch(signUrl, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRFToken": this.$el.querySelector("[name=csrfmiddlewaretoken]").value,
        },
        body: JSON.stringify({ name: file.name, size: file.size }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || "Не удалось получить ссылку на загрузку");
      return data;
    },

    put(url, item, total) {
      return new Promise((resolve, reject) => {
        const request = new XMLHttpRequest();
        this.request = request;
        request.open("PUT", url);
        request.upload.onprogress = (e) => {
          if (!e.lengthComputable) return;
          item.percent = Math.round((e.loaded / e.total) * 100);
          this.percent = Math.round(((this.sent + e.loaded) / total) * 100);
        };
        request.onload = () =>
          request.status < 300 ? resolve() : reject(new Error(`Хранилище ответило ${request.status}`));
        request.onerror = () => reject(new Error(`Не удалось загрузить «${item.name}»`));
        request.onabort = () => reject(new Error("Загрузка отменена"));
        request.send(item.file);
      });
    },

    // Залитые файлы остаются: они уже в хранилище. Прерванная отдача может доехать
    // и всё равно (байты браузер отдал) — такую сироту убирает clean_uploads.
    cancel() {
      this.cancelled = true;
      this.request?.abort();
      this.request = null;
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
