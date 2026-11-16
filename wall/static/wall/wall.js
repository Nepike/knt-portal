// Стена: полотно, инструменты, мазки, кто поставил, картинка и штамп, модерация, таймлапс.
//
// Два холста: скрытый — пиксель на клетку; видимый — растянутая копия, перерисовка — один
// drawImage через invalidate() (склейка по кадрам). Прицел — отдельным слоем. Мазок ложится
// у себя сразу и уходит на сервер кусками; отказанные клетки возвращают прежний вид.

const PAD = 16; // поля вокруг доски, чтобы крайние клетки не липли к рамке
const GRID_FROM = 5; // с этого масштаба видна сетка — мельче она съедает рисунок
const DRAG = 5; // сдвиг в точках, после которого нажатие — перетаскивание
const HANDLE = 14; // уголок картинки, за который тянут размер
const TOP = 48; // дальше не приближаем
const TAP = 320; // мс между касаниями двойного
const NEAR = 24; // и допуск по расстоянию для него
const WIDE = 1024; // lg: инструменты колонкой, панель рядом с доской
const FLUSH = 120; // мс между кусками мазка
const TOUCH_WAIT = 90; // второй палец за это время — жест, а не мазок
const HOVER_WAIT = 250; // мс до подсказки «кто»
const RECENT = 8;
const UNDO_DEPTH = 50;
const FILM_SECONDS = 30; // на ×1 журнал проезжает не дольше — по часам, а не по кадрам экрана
const NOBODY = { id: 0, name: "" };

// Запись: столько кадров и такая задержка дают ролик секунд на десять, ×3 — картинку, которую не мылит чат.
const GIF_FRAMES = 240;
const GIF_DELAY = 4; // сотых секунды на кадр
const GIF_SCALE = 3;

// Клетки отрезка — по Брезенхэму: быстрый мазок мышью не должен рваться в точки.
function* line(from, to) {
  let { x, y } = from;
  const dx = Math.abs(to.x - x);
  const dy = -Math.abs(to.y - y);
  const sx = x < to.x ? 1 : -1;
  const sy = y < to.y ? 1 : -1;
  let error = dx + dy;
  for (;;) {
    yield { x, y };
    if (x === to.x && y === to.y) return;
    const twice = 2 * error;
    if (twice >= dy) {
      error += dy;
      x += sx;
    }
    if (twice <= dx) {
      error += dx;
      y += sy;
    }
  }
}

const bytes = (b64) => Uint8Array.from(atob(b64), (ch) => ch.charCodeAt(0));
const base64 = (array) => {
  let text = "";
  for (let at = 0; at < array.length; at += 0x8000) text += String.fromCharCode(...array.subarray(at, at + 0x8000));
  return btoa(text);
};

document.addEventListener("alpine:init", () => {
  Alpine.data("wall", (dataId) => ({
    ...JSON.parse(document.getElementById(dataId).textContent),

    cells: null, // Uint8Array: код цвета в каждой клетке
    version: 0, // последнее событие в полотне
    who: null, // Uint16Array: номер автора в people (0 — никто)
    when: null, // Uint32Array: секунды от epoch
    people: [NOBODY],
    peopleAt: null, // id → номер в people
    epoch: 0,
    ownersVersion: 0,
    loading: false,
    queue: [], // события, прилетевшие пока едет снимок

    scale: 1,
    ox: 0,
    oy: 0,
    zoomed: false,
    rect: null,
    raf: null,
    dirty: null,
    port: innerWidth,
    attempt: 0,
    busy: false,

    tool: "brush",
    spaceHand: false,
    brush: 1,
    recent: [],
    hover: null,
    sel: null,
    tip: null,
    tipWait: null,
    mini: null, // карточка «кто» на телефоне
    card: null, // история выбранной клетки
    sheet: null, // шторка на телефоне: paint | cell | more | picture | mod
    tab: "draw", // вкладка правой панели на широком экране: draw | film | mod
    modTab: "people",

    points: {},
    pinch: null,
    tap: null,
    grab: null,
    origin: null,
    last: null,
    panning: false,
    stroke: null,
    pending: null, // касание, которое станет мазком, если не придёт второй палец
    undoStack: [],
    redoStack: [],

    film: null, // журнал: { events, marks, step, start, total }
    at: 0,
    playing: false,
    speed: 1,
    reeling: false,
    saving: 0, // проценты записи; ноль — не пишем

    from: null, // углы области модератора
    to: null,
    areaDrag: false,
    areaHours: 3,
    areaNote: "",

    tpl: null, // картинка: { src, x, y, w, h, iw, ih, alpha, on, pixel }
    tplImage: null,
    tplDrag: null,
    tplCodes: null, // коды палитры для картинки в её нынешнем размере (wall/fit/)
    tplCanvas: null,
    fitWait: null,
    stampOnlyEmpty: false,

    target: null, // кого модерируем: { id, name }
    banHours: 24,
    banReason: "",
    banRollback: true,
    rollbackHours: 1,
    bansList: [],
    newTitle: "",

    init() {
      this.token = this.$el.querySelector("[name=csrfmiddlewaretoken]").value;
      this.rgbs = this.colors.map((color) => [1, 3, 5].map((at) => parseInt(color.hex.slice(at, at + 2), 16)));
      this.buffer = document.createElement("canvas");
      this.buffer.width = this.width;
      this.buffer.height = this.height;
      this.bufferCtx = this.buffer.getContext("2d");
      this.image = this.bufferCtx.createImageData(this.width, this.height);
      this.ctx = this.$refs.view.getContext("2d");
      this.aimCtx = this.$refs.aim.getContext("2d");
      this.peopleAt = new Map();
      this.recall();
      this.restoreTemplate();
      if (this.banned.until) this.tool = "hand"; // рисовать нельзя, смотреть — можно

      const forget = () => (this.rect = null);
      addEventListener("scroll", forget, { passive: true });
      addEventListener("resize", () => {
        forget();
        this.port = innerWidth;
      });
      new ResizeObserver(() => this.resize()).observe(this.$refs.view.parentElement);
      // Сокет — первым: события во время снимка ждут в очереди, иначе мазок между ними пропал бы.
      this.connect();
      this.load();
      setInterval(() => (this.banned.until && Date.parse(this.banned.until) < Date.now() ? (this.banned = { until: null }) : null), 30000);
    },

    // --- что видно ---

    get wide() {
      return this.port >= WIDE;
    },

    get tools() {
      const list = [
        { id: "brush", icon: "fa-paintbrush", label: "Кисть", key: "B", paints: true, mobile: true },
        { id: "eraser", icon: "fa-eraser", label: "Ластик", key: "E", paints: true, mobile: true },
        { id: "picker", icon: "fa-eye-dropper", label: "Пипетка", key: "I", mobile: true },
        { id: "hand", icon: "fa-hand", label: "Рука", key: "H", mobile: true },
        { id: "who", icon: "fa-user", label: "Кто", key: "W", mobile: true },
        { id: "picture", icon: "fa-image", label: "Картинка", key: "L" },
      ];
      if (this.can.moderate) list.push({ id: "select", icon: "fa-object-group", label: "Выделение", key: "M" });
      return list;
    },

    get canDraw() {
      return !this.banned.until;
    },

    get banTerm() {
      return this.term(this.banned);
    },

    term(ban) {
      if (!ban.until) return "";
      if (ban.forever) return "навсегда";
      return "до " + new Date(ban.until).toLocaleString("ru", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
    },

    // Что в правой панели: на широком экране — по вкладке (в «Рисовании» — настройки картинки,
    // когда выбран её инструмент), на телефоне — шторка.
    get pane() {
      if (!this.wide) return this.sheet;
      if (this.tab === "film") return "film";
      if (this.tab === "mod") return "mod";
      return this.tool === "picture" ? "picture" : "paint";
    },

    get paneTitle() {
      return { paint: "Палитра", cell: "Клетка", more: "Ещё", picture: "Картинка", mod: "Модерация", film: "Таймлапс" }[this.pane] || "";
    },

    // Вкладка «Таймлапс» сама запускает запись и выключает её, когда с неё уходят.
    setTab(name) {
      if (name === this.tab) return;
      if (this.tab === "film") this.stopFilm();
      this.tab = name;
      if (name !== "mod" && this.tool === "select") this.tool = "brush";
      if (name === "mod") this.loadBans();
      if (name === "film" && !this.film) this.reel();
      this.invalidate();
    },

    get zoomLabel() {
      return `×${this.scale < 10 ? this.scale.toFixed(1) : Math.round(this.scale)}`;
    },

    get spot() {
      const cell = this.hover || this.sel;
      const where = cell ? `${cell.x}, ${cell.y}` : `${this.width}×${this.height}`;
      return this.wide ? where : `${this.zoomLabel} · ${where}`;
    },

    get hoverLine() {
      if (!this.hover || !this.cells) return "";
      const owner = this.ownerAt(this.hover.x, this.hover.y);
      return owner.id ? `${owner.name} · ${this.ago(this.timeAt(this.hover.x, this.hover.y))}` : "";
    },

    get cursor() {
      if (this.pinch || (this.grab && this.panning) || this.tplDrag) return "grabbing";
      if (this.tool === "hand" || this.spaceHand) return "grab";
      if (this.tool === "picture") return "move";
      if (this.tool === "picker") return "copy";
      if (this.tool === "who") return "help";
      return "crosshair";
    },

    // Палитра: сбоку — тон строкой (8 светлот и чистый), на телефоне — светлота строкой и ряд чистых.
    get paletteCodes() {
      const codes = [];
      if (this.wide) {
        for (let hue = 0; hue < this.hues; hue++) {
          for (let tone = 0; tone < this.tones; tone++) codes.push(1 + tone * this.hues + hue);
          codes.push(this.bright_from + hue);
        }
      } else {
        for (let tone = 0; tone < this.tones; tone++) {
          for (let hue = 0; hue < this.hues; hue++) codes.push(1 + tone * this.hues + hue);
        }
        for (let hue = 0; hue < this.hues; hue++) codes.push(this.bright_from + hue);
      }
      return codes;
    },

    get greyCodes() {
      return Array.from({ length: this.bright_from - this.neutral_from }, (_, at) => this.neutral_from + at);
    },

    get swatchSize() {
      return this.wide ? 24 : Math.max(18, Math.min(30, Math.floor((this.port - 56) / this.hues) - 2));
    },

    get paletteGrid() {
      const size = `${this.swatchSize}px`;
      const columns = this.wide ? this.tones + 1 : this.hues;
      return { display: "grid", gridTemplateColumns: `repeat(${columns}, ${size})`, gridAutoRows: size, gap: "2px" };
    },

    get greyGrid() {
      const size = `${this.swatchSize}px`;
      return { display: "grid", gridTemplateColumns: `repeat(${this.greyCodes.length}, ${size})`, gridAutoRows: size, gap: "2px" };
    },

    swatch(code, edge) {
      return {
        background: this.colors[code].hex,
        boxShadow: code === this.brush ? "0 0 0 2px var(--color-accent)" : edge ? "0 0 0 1px rgba(128,132,140,.5)" : "",
      };
    },

    ago(time) {
      if (!time) return "";
      const minutes = Math.round((Date.now() - time) / 60000);
      if (minutes < 1) return "только что";
      if (minutes < 60) return `${minutes} мин назад`;
      if (minutes < 24 * 60) return `${Math.round(minutes / 60)} ч назад`;
      return new Date(time).toLocaleDateString("ru", { day: "numeric", month: "short" });
    },

    // --- инструменты ---

    use(id) {
      if (this.stroke) this.endStroke();
      if (this.film) this.wide ? this.setTab("draw") : this.stopFilm(); // по прошлому не рисуют
      this.tool = id;
      this.tip = null;
      if (id === "picture") {
        this.tab = "draw";
        this.sheet = this.wide ? null : "picture";
        this.refit();
      } else if (id === "select") {
        this.tab = "mod";
        this.modTab = "area";
        this.sheet = this.wide ? null : "mod";
        this.loadBans();
      } else if (!this.wide && (this.sheet === "more" || this.sheet === "picture")) {
        this.sheet = null;
      }
      if (id !== "who") this.mini = null;
      this.invalidate();
    },

    pick(code) {
      if (!code) return;
      this.brush = code;
      try {
        localStorage.setItem(`wall.brush.${this.board}`, code);
      } catch {}
      if (this.tool === "eraser" || this.tool === "picker" || this.tool === "who" || this.tool === "hand") this.tool = "brush";
      if (!this.wide && this.sheet === "paint") this.sheet = null;
    },

    remember(code) {
      this.recent = [code, ...this.recent.filter((one) => one !== code)].slice(0, RECENT);
      try {
        localStorage.setItem(`wall.recent.${this.board}`, JSON.stringify(this.recent));
      } catch {}
    },

    // Кисть и недавние — в браузере, с проверкой: палитра растёт, а код в памяти — нет.
    recall() {
      const fits = (code) => Number.isInteger(code) && code > 0 && code < this.colors.length;
      try {
        const saved = Number(localStorage.getItem(`wall.brush.${this.board}`));
        this.brush = fits(saved) ? saved : 1;
        this.recent = (JSON.parse(localStorage.getItem(`wall.recent.${this.board}`)) || []).filter(fits).slice(0, RECENT);
      } catch {
        this.recent = [];
      }
    },

    openSheet(kind) {
      this.sheet = kind;
    },

    toggleMod() {
      if (this.wide) return this.setTab(this.tab === "mod" ? "draw" : "mod");
      this.sheet = this.sheet === "mod" ? null : "mod";
      if (this.sheet === "mod") this.loadBans();
      else if (this.tool === "select") this.tool = "brush";
    },

    moderate(person) {
      this.target = person;
      this.modTab = "people";
      if (this.wide) this.setTab("mod");
      else {
        this.sheet = "mod";
        this.loadBans();
      }
    },

    // --- полотно ---

    async load() {
      if (this.film) return;
      this.loading = true;
      try {
        const [snapshot, owners] = await Promise.all([
          fetch(this.urls.snapshot, { headers: { "Cache-Control": "no-cache" } }),
          fetch(this.urls.owners, { headers: { "Cache-Control": "no-cache" } }).then((response) => response.json()),
        ]);
        const version = Number(snapshot.headers.get("X-Wall-Version"));
        const cells = new Uint8Array(await snapshot.arrayBuffer());
        const first = !this.cells;
        this.cells = cells;
        this.version = version;
        for (let index = 0; index < cells.length; index++) this.stain(index, cells[index]);
        this.bufferCtx.putImageData(this.image, 0, 0);
        this.who = new Uint16Array(bytes(owners.who).buffer);
        this.when = new Uint32Array(bytes(owners.when).buffer);
        this.epoch = Date.parse(owners.epoch);
        this.ownersVersion = owners.version;
        this.people = [NOBODY];
        this.peopleAt = new Map();
        for (const [id, name] of owners.people) this.person(id, name);
        if (first) this.resize();
        else this.invalidate();
      } finally {
        // Очередь распускаем, даже если снимок не доехал.
        this.loading = false;
        const waiting = this.queue;
        this.queue = [];
        for (const message of waiting) this.apply(message);
      }
    },

    // Номер автора в people; новое имя дописывается.
    person(id, name) {
      if (!id) return 0;
      let number = this.peopleAt.get(id);
      if (number === undefined) {
        number = this.people.length;
        this.people.push({ id, name: name || "…" });
        this.peopleAt.set(id, number);
      } else if (name && this.people[number].name === "…") {
        this.people[number].name = name;
      }
      return number;
    },

    ownerAt(x, y) {
      return this.who ? this.people[this.who[y * this.width + x]] || NOBODY : NOBODY;
    },

    timeAt(x, y) {
      return this.when && this.who && this.who[y * this.width + x] ? this.epoch + this.when[y * this.width + x] * 1000 : 0;
    },

    own(index, id, time, name) {
      if (!this.who) return;
      this.who[index] = this.person(id, name);
      this.when[index] = Math.max(0, Math.round((time - this.epoch) / 1000));
    },

    stain(index, code) {
      const [red, green, blue] = this.rgbs[code];
      const at = index * 4;
      this.image.data[at] = red;
      this.image.data[at + 1] = green;
      this.image.data[at + 2] = blue;
      this.image.data[at + 3] = 255;
    },

    // Правит скрытый холст без перерисовки: у пачки клеток она одна.
    set(x, y, code) {
      const index = y * this.width + x;
      if (this.cells[index] === code) return;
      this.cells[index] = code;
      this.stain(index, code);
      this.bufferCtx.putImageData(this.image, 0, 0, x, y, 1, 1);
    },

    // Одна перерисовка на кадр. "aim" — только слой прицела.
    invalidate(level = "all") {
      if (level === "all" || !this.dirty) this.dirty = level;
      if (this.raf) return;
      this.raf = requestAnimationFrame(() => {
        this.raf = null;
        const what = this.dirty;
        this.dirty = null;
        if (what === "all") this.draw();
        else this.drawAim();
      });
    },

    resize() {
      const box = this.$refs.view.parentElement;
      const ratio = window.devicePixelRatio || 1;
      this.rect = null;
      for (const canvas of [this.$refs.view, this.$refs.aim]) {
        // Физических точек больше, чем css-пикселей: без этого на ретине полотно мылится.
        canvas.width = Math.round(box.clientWidth * ratio);
        canvas.height = Math.round(box.clientHeight * ratio);
        canvas.style.width = box.clientWidth + "px";
        canvas.style.height = box.clientHeight + "px";
      }
      this.ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
      this.aimCtx.setTransform(ratio, 0, 0, ratio, 0, 0);
      // Пока масштаб не трогали — подгоняем заново: на холодной загрузке размеры меряются до css.
      if (!this.zoomed) return this.fit();
      this.scale = Math.max(this.scale, this.floor);
      this.hold();
      this.invalidate();
    },

    get view() {
      const ratio = window.devicePixelRatio || 1;
      return { w: this.$refs.view.width / ratio, h: this.$refs.view.height / ratio };
    },

    get floor() {
      const { w, h } = this.view;
      return Math.max(0.05, Math.min((w - 2 * PAD) / this.width, (h - 2 * PAD) / this.height));
    },

    fit() {
      this.scale = this.floor;
      this.zoomed = false;
      this.hold();
      this.invalidate();
    },

    // Видимый кусок доски в клетках — рисуем только его.
    get seen() {
      const { w, h } = this.view;
      const x = Math.max(0, Math.floor(-this.ox / this.scale));
      const y = Math.max(0, Math.floor(-this.oy / this.scale));
      return {
        x, y,
        w: Math.min(this.width - x, Math.ceil((w - Math.max(this.ox, 0)) / this.scale) + 1),
        h: Math.min(this.height - y, Math.ceil((h - Math.max(this.oy, 0)) / this.scale) + 1),
      };
    },

    draw() {
      if (!this.cells) return;
      const { w, h } = this.view;
      this.ctx.clearRect(0, 0, w, h);
      this.ctx.imageSmoothingEnabled = false; // иначе клетки размажутся при увеличении
      const seen = this.seen;
      if (seen.w > 0 && seen.h > 0) {
        this.ctx.drawImage(
          this.buffer, seen.x, seen.y, seen.w, seen.h,
          this.ox + seen.x * this.scale, this.oy + seen.y * this.scale, seen.w * this.scale, seen.h * this.scale,
        );
      }
      this.paintTemplate();
      // Край доски: без него бетон сливается с фоном страницы.
      this.ctx.lineWidth = 1;
      this.ctx.strokeStyle = "rgba(120,120,130,.9)";
      this.ctx.strokeRect(this.ox - 0.5, this.oy - 0.5, this.width * this.scale + 1, this.height * this.scale + 1);
      this.grid();
      this.paintAreas();
      this.drawAim();
    },

    drawAim() {
      const { w, h } = this.view;
      this.aimCtx.clearRect(0, 0, w, h);
      if (this.panning || this.pinch || this.film) return;
      // Под курсором — цвет кисти: так видно, что ляжет.
      if (this.hover && !this.same(this.hover, this.sel)) {
        const painting = this.tool === "brush" || this.tool === "eraser";
        this.mark(this.hover, painting ? this.colors[this.tool === "eraser" ? 0 : this.brush].hex : null);
      }
      if (this.sel) this.mark(this.sel, null);
    },

    same(one, other) {
      return !!one && !!other && one.x === other.x && one.y === other.y;
    },

    paintTemplate() {
      if (!this.tpl || !this.tpl.on || !this.tplImage || this.film) return;
      const box = this.tplBox();
      this.ctx.globalAlpha = this.tpl.alpha;
      this.ctx.drawImage(this.tpl.pixel && this.tplCanvas ? this.tplCanvas : this.tplImage, box.x, box.y, box.w, box.h);
      this.ctx.globalAlpha = 1;
      if (this.tool !== "picture") return;
      this.ctx.setLineDash([5, 4]);
      this.ctx.lineWidth = 2;
      this.ctx.strokeStyle = "rgba(16,185,129,.95)";
      this.ctx.strokeRect(box.x, box.y, box.w, box.h);
      this.ctx.setLineDash([]);
      this.ctx.fillStyle = "rgba(16,185,129,.95)";
      this.ctx.fillRect(box.x + box.w - HANDLE, box.y + box.h - HANDLE, HANDLE, HANDLE);
    },

    tplBox() {
      return { x: this.ox + this.tpl.x * this.scale, y: this.oy + this.tpl.y * this.scale, w: this.tpl.w * this.scale, h: this.tpl.h * this.scale };
    },

    // Закрытые участки видны всем: иначе отказ непонятен. В таймлапсе их нет.
    paintAreas() {
      if (this.film) return;
      this.ctx.setLineDash([6, 4]);
      this.ctx.lineWidth = 2;
      this.ctx.strokeStyle = "rgba(56,189,248,.95)";
      for (const area of this.areas) this.frame(area);
      if (this.rectReady) {
        this.ctx.strokeStyle = this.rectTooBig ? "rgba(244,63,94,.95)" : "rgba(245,158,11,.95)";
        this.frame({ x1: this.from.x, y1: this.from.y, x2: this.to.x, y2: this.to.y });
      }
      this.ctx.setLineDash([]);
    },

    frame(area) {
      const x1 = Math.min(area.x1, area.x2);
      const y1 = Math.min(area.y1, area.y2);
      const w = Math.abs(area.x2 - area.x1) + 1;
      const h = Math.abs(area.y2 - area.y1) + 1;
      this.ctx.strokeRect(this.ox + x1 * this.scale, this.oy + y1 * this.scale, w * this.scale, h * this.scale);
    },

    grid() {
      if (this.scale < GRID_FROM) return;
      const { w, h } = this.view;
      const seen = this.seen;
      const left = Math.max(this.ox, 0);
      const right = Math.min(this.ox + this.width * this.scale, w);
      const top = Math.max(this.oy, 0);
      const bottom = Math.min(this.oy + this.height * this.scale, h);
      this.ctx.strokeStyle = `rgba(0,0,0,${Math.min(0.28, (this.scale - GRID_FROM) / 60 + 0.06)})`;
      this.ctx.lineWidth = 1;
      this.ctx.beginPath();
      for (let x = Math.max(1, seen.x); x < Math.min(this.width, seen.x + seen.w + 1); x++) {
        const at = Math.round(this.ox + x * this.scale) + 0.5; // полпикселя — иначе линия мылится
        this.ctx.moveTo(at, top);
        this.ctx.lineTo(at, bottom);
      }
      for (let y = Math.max(1, seen.y); y < Math.min(this.height, seen.y + seen.h + 1); y++) {
        const at = Math.round(this.oy + y * this.scale) + 0.5;
        this.ctx.moveTo(left, at);
        this.ctx.lineTo(right, at);
      }
      this.ctx.stroke();
    },

    // fill — что ляжет (клетка под курсором) или null (выбранная).
    mark(cell, fill) {
      const size = this.scale;
      const x = this.ox + cell.x * size;
      const y = this.oy + cell.y * size;
      if (fill) {
        this.aimCtx.globalAlpha = 0.75;
        this.aimCtx.fillStyle = fill;
        this.aimCtx.fillRect(x, y, size, size);
        this.aimCtx.globalAlpha = 1;
      }
      // Две рамки, светлая поверх тёмной: доска из края в край по светлоте.
      const thick = Math.min(fill ? 2 : 3, Math.max(1, size / 3));
      this.aimCtx.lineWidth = thick;
      this.aimCtx.strokeStyle = "rgba(0,0,0,.7)";
      this.aimCtx.strokeRect(x - thick / 2, y - thick / 2, size + thick, size + thick);
      this.aimCtx.lineWidth = thick / 2;
      this.aimCtx.strokeStyle = "rgba(255,255,255,.95)";
      this.aimCtx.strokeRect(x - thick / 4, y - thick / 4, size + thick / 2, size + thick / 2);
    },

    // --- указатель ---

    box() {
      return this.rect || (this.rect = this.$refs.view.getBoundingClientRect());
    },

    // clamp — для рамки области: за краем доски она упирается в границу.
    cellAt(point, clamp) {
      const box = this.box();
      let x = Math.floor((point.clientX - box.left - this.ox) / this.scale);
      let y = Math.floor((point.clientY - box.top - this.oy) / this.scale);
      if (clamp) {
        x = Math.min(this.width - 1, Math.max(0, x));
        y = Math.min(this.height - 1, Math.max(0, y));
      }
      return x >= 0 && x < this.width && y >= 0 && y < this.height ? { x, y } : null;
    },

    get touches() {
      return Object.values(this.points);
    },

    get painting() {
      return (this.tool === "brush" || this.tool === "eraser") && !this.spaceHand && this.canDraw && !this.film;
    },

    onDown(event) {
      this.points[event.pointerId] = { x: event.clientX, y: event.clientY };
      // Захват указателя: иначе за краем холста движение уходит другому элементу.
      this.$refs.view.setPointerCapture(event.pointerId);
      if (this.touches.length === 2) return this.startPinch();
      if (this.touches.length > 2) return;
      this.origin = { clientX: event.clientX, clientY: event.clientY };
      this.last = this.origin;
      this.panning = false;
      this.tip = null;
      clearTimeout(this.tipWait);
      const cell = this.cellAt(event);
      const pan = event.button === 1 || this.tool === "hand" || this.spaceHand || this.film;
      if (!pan && event.button === 0 && event.altKey && cell) return this.pickAt(cell);
      if (!pan && this.tool === "picture" && this.tpl && this.onPicture(event)) return this.grabTemplate(event);
      if (!pan && this.tool === "select" && event.button === 0) return this.grabArea(event);
      if (!pan && this.painting && event.button === 0 && cell) {
        // Палец ждёт второго: щипок не должен оставлять точку.
        if (event.pointerType === "touch") {
          this.pending = { cell, timer: setTimeout(() => this.startPending(), TOUCH_WAIT) };
        } else {
          this.beginStroke(cell);
        }
        return;
      }
      this.grab = { x: event.clientX - this.ox, y: event.clientY - this.oy };
    },

    startPending() {
      if (!this.pending) return;
      const { cell } = this.pending;
      this.pending = null;
      this.beginStroke(cell);
    },

    onMove(event) {
      this.last = { clientX: event.clientX, clientY: event.clientY };
      const point = this.points[event.pointerId];
      if (point) {
        point.x = event.clientX;
        point.y = event.clientY;
      }
      if (this.pinch) return this.movePinch();
      if (this.pending && Math.hypot(event.clientX - this.origin.clientX, event.clientY - this.origin.clientY) > DRAG) {
        clearTimeout(this.pending.timer);
        this.startPending();
      }
      if (this.stroke) return this.extendStroke(this.cellAt(event));
      if (this.tplDrag) return this.moveTemplate(event);
      if (this.areaDrag) return this.moveArea(event);
      if (!this.grab) return this.hovering(event);
      if (Math.hypot(event.clientX - this.origin.clientX, event.clientY - this.origin.clientY) > DRAG) this.panning = true;
      this.ox = event.clientX - this.grab.x;
      this.oy = event.clientY - this.grab.y;
      this.hold();
      // Захват — от прижатого положения, иначе после упора в край доска не пойдёт назад.
      this.grab = { x: event.clientX - this.ox, y: event.clientY - this.oy };
      this.invalidate();
    },

    // Мышь над доской: прицел и подсказка «кто», когда курсор задержался на клетке.
    hovering(event) {
      if (event.pointerType !== "mouse") return;
      const cell = this.cellAt(event);
      if (this.same(cell, this.hover) || (!cell && !this.hover)) return;
      this.hover = cell;
      this.tip = null;
      clearTimeout(this.tipWait);
      if (cell) this.tipWait = setTimeout(() => this.showTip(cell, event), HOVER_WAIT);
      this.invalidate("aim");
    },

    showTip(cell, event) {
      if (!this.same(cell, this.hover) || this.stroke || this.film) return;
      const owner = this.ownerAt(cell.x, cell.y);
      if (!owner.id) return;
      const box = this.box();
      const { w } = this.view;
      let left = event.clientX - box.left + 14;
      if (left > w - 200) left -= 220;
      const erased = this.cells[cell.y * this.width + cell.x] === 0;
      this.tip = {
        name: owner.name,
        line: `${erased ? "стёр" : "закрасил"} ${this.ago(this.timeAt(cell.x, cell.y))} · ${cell.x}, ${cell.y}`,
        left, top: event.clientY - box.top + 16,
      };
    },

    onUp(event) {
      if (this.$refs.view.hasPointerCapture(event.pointerId)) this.$refs.view.releasePointerCapture(event.pointerId);
      delete this.points[event.pointerId];
      const left = this.touches;
      if (this.pinch) {
        // Пара пальцев сменилась — меряем заново, иначе следующее движение дёрнет масштаб.
        this.pinch = left.length >= 2 ? this.span() : null;
        // Оставшийся палец продолжает тащить доску: щипок почти всегда кончается сдвигом.
        if (!this.pinch && left.length) this.grab = { x: left[0].x - this.ox, y: left[0].y - this.oy };
      }
      if (left.length) return;
      if (this.pending) {
        clearTimeout(this.pending.timer);
        this.startPending(); // короткое касание — одна клетка
      }
      if (this.stroke) this.endStroke();
      this.grab = null;
      if (this.tplDrag) {
        this.tplDrag = null;
        this.saveTemplate();
        this.refit();
      }
      this.areaDrag = false;
      if (!this.panning && event.type === "pointerup") this.tapped(event);
      // Прицел возвращаем туда, где курсор; палец никакого прицела не оставляет.
      this.hover = event.pointerType === "mouse" ? this.cellAt(this.last || event) : null;
      this.panning = false;
      this.invalidate("aim");
    },

    onLeave() {
      if (this.grab || this.tplDrag || this.stroke || !this.hover) return;
      this.hover = null;
      this.tip = null;
      clearTimeout(this.tipWait);
      this.invalidate("aim");
    },

    // Касание без сдвига: что оно значит, решает инструмент.
    tapped(event) {
      const cell = this.cellAt(event);
      if (this.film) return;
      if (this.tool === "picker" && cell) return this.pickAt(cell);
      if (this.tool === "who" && cell) return this.selectCell(cell);
      if (this.tool === "hand" || this.tool === "who") this.doubled(event);
    },

    pickAt(cell) {
      const code = this.cells[cell.y * this.width + cell.x];
      if (!code) return this.say("info", "Клетка пустая — брать нечего");
      this.pick(code);
    },

    // Двойное касание приближает (одной рукой на телефоне).
    doubled(event) {
      const now = { x: event.clientX, y: event.clientY, when: Date.now() };
      const twice = this.tap && now.when - this.tap.when < TAP && Math.hypot(now.x - this.tap.x, now.y - this.tap.y) < NEAR;
      this.tap = twice ? null : now;
      if (!twice) return;
      const box = this.box();
      this.zoom(2, now.x - box.left, now.y - box.top);
    },

    // --- мазок ---

    beginStroke(cell) {
      if (!cell || !this.canDraw) return;
      this.stroke = { color: this.tool === "eraser" ? 0 : this.brush, cells: [], sent: 0, ids: [], jobs: [], seen: new Set(), last: cell, refused: "" };
      this.addCell(cell);
    },

    extendStroke(cell) {
      if (!cell || !this.stroke) return;
      for (const at of line(this.stroke.last, cell)) this.addCell(at);
      this.stroke.last = cell;
      this.hover = cell;
      this.invalidate("aim");
    },

    addCell({ x, y }, stroke = this.stroke, color = stroke.color) {
      const index = y * this.width + x;
      if (stroke.seen.has(index)) return;
      stroke.seen.add(index);
      const before = this.cells[index];
      if (before === color) return;
      if (!this.can.moderate) {
        if (this.frozenAt(x, y)) {
          stroke.refused ||= "этот участок закрыт от правок";
          return;
        }
        if (color === 0 && this.ownerAt(x, y).id !== this.me) {
          stroke.refused ||= "стереть можно только свой пиксель — чужой закрашивают";
          return;
        }
      }
      stroke.cells.push({ x, y, color, before, owner: this.who ? this.who[index] : 0, time: this.when ? this.when[index] : 0 });
      this.set(x, y, color);
      this.own(index, this.me, Date.now(), this.me_name);
      this.invalidate();
      if (stroke.cells.length - stroke.sent >= this.stroke_max) return this.flush(stroke);
      else if (!stroke.timer) stroke.timer = setTimeout(() => this.flush(stroke), FLUSH);
    },

    frozenAt(x, y) {
      return this.areas.some((area) => x >= Math.min(area.x1, area.x2) && x <= Math.max(area.x1, area.x2)
        && y >= Math.min(area.y1, area.y2) && y <= Math.max(area.y1, area.y2));
    },

    // Кусок мазка — на сервер. Отказ всей пачки или отдельных клеток возвращает им прежний вид.
    flush(stroke) {
      clearTimeout(stroke.timer);
      stroke.timer = null;
      while (stroke.sent < stroke.cells.length) {
        const chunk = stroke.cells.slice(stroke.sent, stroke.sent + this.stroke_max);
        stroke.sent += chunk.length;
        const job = this.postJson(this.urls.draw, { cells: chunk.map((c) => [c.x, c.y, c.color]) })
          .then((response) => response.json())
          .then((data) => {
            if (data.error) {
              this.revert(chunk);
              stroke.refused = data.error;
              return;
            }
            stroke.ids.push(...data.ids);
            if (data.rejected.length) {
              const out = new Set(data.rejected.map(([x, y]) => y * this.width + x));
              this.revert(chunk.filter((c) => out.has(c.y * this.width + c.x)));
              stroke.refused ||= data.reason;
            }
          })
          .catch(() => {
            this.revert(chunk);
            stroke.refused = "связь потеряна — мазок не сохранился";
          });
        stroke.jobs.push(job);
      }
    },

    revert(list) {
      for (const c of list) {
        c.out = true;
        const index = c.y * this.width + c.x;
        if (this.cells[index] !== c.color) continue; // поверх уже легло чужое
        this.set(c.x, c.y, c.before);
        if (this.who) {
          this.who[index] = c.owner;
          this.when[index] = c.time;
        }
      }
      this.invalidate();
    },

    endStroke() {
      const stroke = this.stroke;
      if (!stroke) return;
      this.stroke = null;
      this.flush(stroke);
      if (stroke.color && stroke.cells.length) this.remember(stroke.color);
      Promise.all(stroke.jobs).then(() => {
        if (stroke.refused) this.say("error", stroke.refused);
        if (!stroke.ids.length) return;
        this.undoStack.push(stroke);
        if (this.undoStack.length > UNDO_DEPTH) this.undoStack.shift();
        this.redoStack = [];
      });
    },

    // Отмена — на сервере: он вернёт клеткам прежних авторов. У себя возвращаем сразу.
    async undo() {
      if (this.busy || this.film) return;
      const stroke = this.undoStack.pop();
      if (!stroke) return;
      this.busy = true;
      try {
        for (const c of stroke.cells) {
          if (c.out || this.cells[c.y * this.width + c.x] !== c.color) continue;
          this.set(c.x, c.y, c.before);
          if (this.who) {
            this.who[c.y * this.width + c.x] = c.owner;
            this.when[c.y * this.width + c.x] = c.time;
          }
        }
        this.invalidate();
        const data = await (await this.postJson(this.urls.undo, { ids: stroke.ids })).json();
        if (data.error) {
          this.say("error", data.error);
          return this.load();
        }
        this.redoStack.push(stroke);
      } finally {
        this.busy = false;
      }
    },

    async redo() {
      if (this.busy || this.film || !this.canDraw) return;
      const old = this.redoStack.pop();
      if (!old) return;
      const stroke = { color: old.color, cells: [], sent: 0, ids: [], jobs: [], seen: new Set(), refused: "" };
      for (const c of old.cells) if (!c.out) this.addCell(c, stroke, c.color);
      this.flush(stroke);
      await Promise.all(stroke.jobs);
      if (stroke.refused) this.say("error", stroke.refused);
      if (stroke.ids.length) this.undoStack.push(stroke);
    },

    // --- жест двумя пальцами ---

    startPinch() {
      if (this.pending) {
        clearTimeout(this.pending.timer);
        this.pending = null;
      }
      if (this.stroke) this.endStroke();
      this.panning = true;
      this.grab = null;
      this.areaDrag = false;
      this.tplDrag = null;
      this.pinch = this.span();
      this.invalidate("aim");
    },

    span() {
      const [one, other] = this.touches;
      return { gap: Math.hypot(one.x - other.x, one.y - other.y), cx: (one.x + other.x) / 2, cy: (one.y + other.y) / 2 };
    },

    movePinch() {
      const now = this.span();
      const box = this.box();
      this.ox += now.cx - this.pinch.cx;
      this.oy += now.cy - this.pinch.cy;
      const factor = this.pinch.gap > 0 ? now.gap / this.pinch.gap : 1;
      this.pinch = now;
      this.zoom(factor, now.cx - box.left, now.cy - box.top);
      this.hold();
      this.invalidate();
    },

    onWheel(event) {
      event.preventDefault();
      const box = this.box();
      this.zoom(event.deltaY < 0 ? 1.2 : 1 / 1.2, event.clientX - box.left, event.clientY - box.top);
    },

    // Кнопки целятся в середину холста, колесо и пальцы — в точку под собой.
    zoom(factor, px, py) {
      const { w, h } = this.view;
      if (px === undefined) {
        px = w / 2;
        py = h / 2;
      }
      const next = Math.min(Math.max(this.scale * factor, this.floor), TOP);
      if (next === this.scale) return;
      this.ox = px - (px - this.ox) * (next / this.scale);
      this.oy = py - (py - this.oy) * (next / this.scale);
      this.scale = next;
      this.zoomed = next > this.floor;
      this.tip = null;
      this.hold();
      this.invalidate();
    },

    // Не за край и с округлением: на дробном сдвиге сетка дрожит.
    hold() {
      const { w, h } = this.view;
      const drawnW = this.width * this.scale;
      const drawnH = this.height * this.scale;
      this.ox = Math.round(drawnW + 2 * PAD <= w ? (w - drawnW) / 2 : Math.min(PAD, Math.max(w - drawnW - PAD, this.ox)));
      this.oy = Math.round(drawnH + 2 * PAD <= h ? (h - drawnH) / 2 : Math.min(PAD, Math.max(h - drawnH - PAD, this.oy)));
    },

    // --- клавиатура ---

    typing(event) {
      return /^(INPUT|TEXTAREA|SELECT)$/.test(event.target.tagName);
    },

    // По event.code: в русской раскладке буквы те же клавиши.
    key(event) {
      if (this.typing(event)) return;
      const mod = event.ctrlKey || event.metaKey;
      if (mod && event.code === "KeyZ") {
        event.preventDefault();
        return event.shiftKey ? this.redo() : this.undo();
      }
      if (mod && event.code === "KeyY") {
        event.preventDefault();
        return this.redo();
      }
      if (mod || event.altKey) return;
      if (event.code === "Space") {
        event.preventDefault();
        this.spaceHand = true;
        return;
      }
      const tools = { KeyB: "brush", KeyE: "eraser", KeyI: "picker", KeyH: "hand", KeyW: "who", KeyL: "picture", KeyM: "select" };
      const tool = tools[event.code];
      if (tool && (tool !== "select" || this.can.moderate)) return this.use(tool);
      if (event.key === "+" || event.key === "=") return this.zoom(1.4);
      if (event.key === "-") return this.zoom(1 / 1.4);
      if (event.key === "0") return this.fit();
      if (event.key === "Escape") return this.escape();
      const arrows = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };
      if (arrows[event.key] && this.sel) {
        event.preventDefault();
        const [dx, dy] = arrows[event.key];
        this.selectCell({
          x: Math.min(this.width - 1, Math.max(0, this.sel.x + dx)),
          y: Math.min(this.height - 1, Math.max(0, this.sel.y + dy)),
        });
      }
    },

    keyUp(event) {
      if (event.code === "Space") this.spaceHand = false;
    },

    escape() {
      if (this.sheet) return (this.sheet = null);
      if (this.film) return this.wide ? this.setTab("draw") : this.stopFilm();
      if (this.rectReady) return this.dropRect();
      if (this.tool === "picture" || this.tool === "select") return this.use("brush");
      if (this.tab !== "draw") return this.setTab("draw");
      this.sel = null;
      this.card = null;
      this.mini = null;
      this.invalidate("aim");
    },

    // --- клетка: кто её менял ---

    selectCell(cell) {
      this.sel = cell;
      const owner = this.ownerAt(cell.x, cell.y);
      const code = this.cells[cell.y * this.width + cell.x];
      this.mini = {
        name: owner.name,
        line: owner.id ? `${code ? "закрасил" : "стёр"} ${this.ago(this.timeAt(cell.x, cell.y))} · ${cell.x}, ${cell.y}` : `пустая клетка · ${cell.x}, ${cell.y}`,
        color: code,
      };
      this.invalidate("aim");
      clearTimeout(this.cardWait);
      this.cardWait = setTimeout(() => this.loadCard(cell), 150);
    },

    async loadCard(cell) {
      const data = await (await fetch(`${this.urls.pixel}?x=${cell.x}&y=${cell.y}`)).json();
      if (this.same(cell, this.sel)) this.card = data;
    },

    // --- сокет ---
    // После переподключения перечитываем полотно: пока лежали, чужие мазки шли мимо.

    connect() {
      const scheme = location.protocol === "https:" ? "wss" : "ws";
      const socket = new WebSocket(`${scheme}://${location.host}/ws/wall/`);
      socket.onopen = () => {
        if (this.attempt) this.load();
        this.attempt = 0;
      };
      socket.onmessage = (event) => this.apply(JSON.parse(event.data));
      // 1, 2, 4… до 30 с: если сервер лежит, не добиваем его переподключениями.
      socket.onclose = () => setTimeout(() => this.connect(), Math.min(1000 * 2 ** this.attempt++, 30000));
    },

    apply(message) {
      if (this.film) return; // на историю чужие мазки не кладём, при выходе перечитаем доску
      // Снимок ещё едет — придержим: иначе он ляжет поверх и мазок пропадёт.
      if (this.loading) return this.queue.push(message);
      if (!this.cells || !message.pixels) return;
      const fresh = message.id > this.version;
      const owners = message.id > this.ownersVersion;
      if (!fresh && !owners) return;
      if (fresh) this.version = message.id;
      if (owners) this.ownersVersion = message.id;
      const now = Date.now();
      const people = message.people || {};
      let touched = false;
      for (const [x, y, code, owner] of message.pixels) {
        if (fresh) this.set(x, y, code);
        if (owners) this.own(y * this.width + x, owner, now, people[owner]);
        touched ||= this.same({ x, y }, this.sel);
      }
      this.invalidate();
      if (touched && this.card) this.loadCard(this.sel);
    },

    // --- запросы ---

    post(url, values) {
      return fetch(url, {
        method: "POST",
        headers: { "X-CSRFToken": this.token, "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams(values || {}),
      });
    },

    postJson(url, value) {
      return fetch(url, {
        method: "POST",
        headers: { "X-CSRFToken": this.token, "Content-Type": "application/json" },
        body: JSON.stringify(value),
      });
    },

    say(type, text) {
      window.dispatchEvent(new CustomEvent("toast", { detail: { type, text } }));
    },

    // --- модерация ---

    get rectReady() {
      return !!(this.from && this.to);
    },

    get rectCells() {
      if (!this.rectReady) return 0;
      return (Math.abs(this.to.x - this.from.x) + 1) * (Math.abs(this.to.y - this.from.y) + 1);
    },

    get rectSize() {
      if (!this.rectReady) return "область не выделена";
      return `${Math.abs(this.to.x - this.from.x) + 1}×${Math.abs(this.to.y - this.from.y) + 1} = ${this.rectCells} кл.`;
    },

    get rectTooBig() {
      return this.rectCells > this.max_area;
    },

    grabArea(event) {
      const cell = this.cellAt(event, true);
      this.areaDrag = true;
      this.from = cell;
      this.to = cell;
      this.invalidate();
    },

    moveArea(event) {
      const cell = this.cellAt(event, true);
      if (this.same(cell, this.to)) return;
      this.to = cell;
      this.invalidate();
    },

    dropRect() {
      this.from = this.to = null;
      this.invalidate();
    },

    async area(kind, extra) {
      if (!this.rectReady || this.rectTooBig || this.busy) return;
      this.busy = true;
      try {
        const data = await (await this.post(this.urls[kind], {
          x1: this.from.x, y1: this.from.y, x2: this.to.x, y2: this.to.y, ...extra,
        })).json();
        if (data.error) return this.say("error", data.error);
        if (data.areas) {
          this.areas = data.areas;
          this.invalidate();
        }
        // Клетки приедут по сокету: на пачке не повторяем серверную логику.
        if (data.changed !== undefined) this.say("success", `Клеток изменено: ${data.changed}`);
      } finally {
        this.busy = false;
      }
    },

    async dropArea(pk) {
      const data = await (await this.post(this.urls.unprotect, { pk })).json();
      if (data.error) return this.say("error", data.error);
      this.areas = data.areas;
      this.invalidate();
    },

    async loadBans() {
      if (!this.can.moderate) return;
      const data = await (await fetch(this.urls.bans)).json();
      this.bansList = data.bans || [];
    },

    async banTarget() {
      if (!this.target || this.busy) return;
      const label = Object.fromEntries(this.ban_hours)[this.banHours];
      if (!confirm(`Закрыть Стену для «${this.target.name}»: ${label}?`)) return;
      this.busy = true;
      try {
        const data = await (await this.post(this.urls.ban, { user: this.target.id, hours: this.banHours, reason: this.banReason })).json();
        if (data.error) return this.say("error", data.error);
        if (this.banRollback) {
          const back = await (await this.post(this.urls.rollback_person, { user: this.target.id, hours: 1 })).json();
          if (back.error) this.say("error", back.error);
        }
        this.say("success", `Стена закрыта для «${data.who}»`);
        this.banReason = "";
        this.target = null;
        await this.loadBans();
      } finally {
        this.busy = false;
      }
    },

    async unban(person) {
      const data = await (await this.post(this.urls.ban, { user: person.id, hours: 0 })).json();
      if (data.error) return this.say("error", data.error);
      this.say("success", `Запрет снят с «${data.who}»`);
      await this.loadBans();
    },

    async rollbackTarget() {
      if (!this.target || this.busy) return;
      if (!confirm(`Откатить правки «${this.target.name}» за ${this.rollbackHours} ч?`)) return;
      this.busy = true;
      try {
        const data = await (await this.post(this.urls.rollback_person, { user: this.target.id, hours: this.rollbackHours })).json();
        if (data.error) return this.say("error", data.error);
        this.say("success", `Клеток возвращено: ${data.changed}`);
      } finally {
        this.busy = false;
      }
    },

    // Новый сезон: прошлая доска уходит в архив целиком, страница перечитывается с нуля.
    async newBoard(title) {
      if (this.busy || !confirm(`Закрыть «${this.title}» и открыть «${title.trim()}»?\nНынешний рисунок уйдёт в архив, на странице будет пустая доска.`)) return;
      this.busy = true;
      try {
        const data = await (await this.post(this.urls.new, { title })).json();
        if (data.error) return this.say("error", data.error);
        location.reload();
      } finally {
        this.busy = false;
      }
    },

    // --- картинка: подложка (в этом браузере) и штамп ---

    get tplKey() {
      return `wall.template.${this.board}`;
    },

    restoreTemplate() {
      try {
        const saved = localStorage.getItem(this.tplKey);
        if (saved) this.applyTemplate(JSON.parse(saved));
      } catch {}
    },

    async applyTemplate(record) {
      const image = new Image();
      try {
        await new Promise((done, fail) => {
          image.onload = done;
          image.onerror = fail;
          image.src = record.src;
        });
      } catch {
        return this.dropTemplate();
      }
      this.tplImage = image;
      this.tpl = { alpha: 0.7, on: true, pixel: false, ...record, iw: image.naturalWidth, ih: image.naturalHeight };
      if (!this.tpl.w) this.fitTemplate(true);
      this.invalidate();
      this.refit();
    },

    async pickTemplate(event) {
      const file = event.target.files[0];
      if (!file) return;
      const src = await new Promise((done) => {
        const reader = new FileReader();
        reader.onload = () => done(reader.result);
        reader.readAsDataURL(file);
      });
      await this.applyTemplate({ src, alpha: this.tpl?.alpha ?? 0.7, on: true, pixel: this.tpl?.pixel ?? false });
      event.target.value = ""; // иначе повторный выбор того же файла не даст события
    },

    // Во всю доску с сохранением пропорций; мелкую картинку не растягиваем без спроса.
    fitTemplate(small) {
      const { iw, ih } = this.tpl;
      const k = Math.min(this.width / iw, this.height / ih, small ? 1 : Infinity);
      this.tpl.w = Math.max(1, Math.round(iw * k));
      this.tpl.h = Math.max(1, Math.round(ih * k));
      this.centerTemplate();
    },

    centerTemplate() {
      this.tpl.x = Math.round((this.width - this.tpl.w) / 2);
      this.tpl.y = Math.round((this.height - this.tpl.h) / 2);
      this.saveTemplate();
      this.refit();
      this.invalidate();
    },

    sizeTemplate(cells) {
      this.tpl.w = Math.max(1, Math.min(255, Math.round(cells) || 1));
      this.tpl.h = Math.max(1, Math.min(255, Math.round((this.tpl.w * this.tpl.ih) / this.tpl.iw)));
      this.saveTemplate();
      this.refit();
      this.invalidate();
    },

    onPicture(event) {
      const box = this.tplBox();
      const view = this.box();
      const px = event.clientX - view.left;
      const py = event.clientY - view.top;
      return px >= box.x - HANDLE && px <= box.x + box.w + HANDLE && py >= box.y - HANDLE && py <= box.y + box.h + HANDLE;
    },

    // Тянем либо за уголок — тогда меняем размер, либо за саму картинку — двигаем.
    grabTemplate(event) {
      const box = this.tplBox();
      const view = this.box();
      const px = event.clientX - view.left;
      const py = event.clientY - view.top;
      const corner = px > box.x + box.w - HANDLE && px < box.x + box.w + HANDLE && py > box.y + box.h - HANDLE && py < box.y + box.h + HANDLE;
      this.tplDrag = { corner, x: this.tpl.x, y: this.tpl.y, w: this.tpl.w, sx: event.clientX, sy: event.clientY };
    },

    moveTemplate(event) {
      const dx = (event.clientX - this.tplDrag.sx) / this.scale;
      const dy = (event.clientY - this.tplDrag.sy) / this.scale;
      if (this.tplDrag.corner) {
        const w = Math.max(1, Math.min(255, Math.round(this.tplDrag.w + dx)));
        this.tpl.w = w;
        this.tpl.h = Math.max(1, Math.min(255, Math.round((w * this.tpl.ih) / this.tpl.iw)));
        this.tplCanvas = null; // старый подбор под другой размер — показываем саму картинку
      } else {
        this.tpl.x = Math.round(this.tplDrag.x + dx);
        this.tpl.y = Math.round(this.tplDrag.y + dy);
      }
      this.invalidate();
    },

    // Подложка — только в этом браузере: иначе доску заклеили бы чужими рисунками.
    saveTemplate() {
      if (!this.tpl) return;
      try {
        const { iw, ih, ...record } = this.tpl;
        localStorage.setItem(this.tplKey, JSON.stringify(record));
      } catch {
        this.say("warning", "Картинка не сохранится до следующего раза: слишком тяжёлая");
      }
    },

    dropTemplate() {
      this.tpl = null;
      this.tplImage = null;
      this.tplCodes = null;
      this.tplCanvas = null;
      try {
        localStorage.removeItem(this.tplKey);
      } catch {}
      this.invalidate();
    },

    // Цвета палитры для картинки в нынешнем размере — подбирает сервер (тот же, что у wall_draw).
    refit() {
      clearTimeout(this.fitWait);
      if (!this.tpl || !this.tplImage || (!this.tpl.pixel && !this.can.stamp)) return;
      this.fitWait = setTimeout(() => this.fitNow(), 250);
    },

    async fitNow() {
      const { w, h } = this.tpl;
      const canvas = document.createElement("canvas");
      canvas.width = w;
      canvas.height = h;
      const ctx = canvas.getContext("2d");
      ctx.imageSmoothingQuality = "high";
      ctx.drawImage(this.tplImage, 0, 0, w, h);
      const rgba = ctx.getImageData(0, 0, w, h).data;
      const response = await this.postJson(this.urls.fit, { w, h, rgba: base64(new Uint8Array(rgba.buffer)) });
      const data = await response.json();
      if (data.error) return this.say("error", data.error);
      if (!this.tpl || this.tpl.w !== w || this.tpl.h !== h) return; // пока ждали, размер сменили
      this.tplCodes = bytes(data.codes);
      const image = ctx.createImageData(w, h);
      this.tplCodes.forEach((code, index) => {
        if (code === 255) return;
        const [red, green, blue] = this.rgbs[code];
        image.data.set([red, green, blue, 255], index * 4);
      });
      ctx.putImageData(image, 0, 0);
      this.tplCanvas = canvas;
      this.invalidate();
    },

    // Что сделает штамп: клетки, цвета, чужое под ним, закрытое и то, что за краем.
    get stampStats() {
      if (!this.tpl || !this.tplCodes || !this.cells || this.tplCodes.length !== this.tpl.w * this.tpl.h) return null;
      const { x, y, w } = this.tpl;
      const stats = { cells: 0, colors: new Set(), others: 0, frozen: 0, outside: 0 };
      this.tplCodes.forEach((code, index) => {
        if (code === 255) return;
        const cx = x + (index % w);
        const cy = y + Math.floor(index / w);
        if (cx < 0 || cy < 0 || cx >= this.width || cy >= this.height) return stats.outside++;
        if (!this.can.moderate && this.frozenAt(cx, cy)) return stats.frozen++;
        const now = this.cells[cy * this.width + cx];
        if (now && now !== code && this.ownerAt(cx, cy).id !== this.me) stats.others++;
        stats.cells++;
        stats.colors.add(code);
      });
      return { ...stats, colors: stats.colors.size };
    },

    async applyStamp() {
      const stats = this.stampStats;
      if (!stats || !stats.cells || this.busy) return;
      if (!confirm(`Нанести картинку на доску: ${stats.cells} клеток?`)) return;
      // На сервер — только то, что на доске.
      const { x, y, w, h } = this.tpl;
      const left = Math.max(0, x);
      const top = Math.max(0, y);
      const right = Math.min(this.width, x + w);
      const bottom = Math.min(this.height, y + h);
      const codes = new Uint8Array((right - left) * (bottom - top));
      for (let row = top; row < bottom; row++) {
        for (let column = left; column < right; column++) {
          codes[(row - top) * (right - left) + (column - left)] = this.tplCodes[(row - y) * w + (column - x)];
        }
      }
      this.busy = true;
      try {
        const data = await (await this.postJson(this.urls.stamp, {
          x: left, y: top, w: right - left, h: bottom - top, codes: base64(codes), only_empty: this.stampOnlyEmpty,
        })).json();
        if (data.error) return this.say("error", data.error);
        this.say("success", `Нанесено клеток: ${data.changed}`);
      } finally {
        this.busy = false;
      }
    },

    // --- таймлапс ---
    //
    // Журнал — целиком (три байта на событие). Доска на момент — первые N событий на пустом полотне.

    async reel() {
      if (this.film) return this.stopFilm();
      if (this.stroke) this.endStroke();
      this.reeling = true;
      try {
        const response = await fetch(this.urls.history);
        const count = Number(response.headers.get("X-Wall-Marks"));
        const step = Number(response.headers.get("X-Wall-Step"));
        const start = Date.parse(response.headers.get("X-Wall-Start"));
        const buffer = await response.arrayBuffer();
        const events = new Uint8Array(buffer, count * 4);
        if (!events.length) {
          if (this.tab === "film") this.tab = "draw";
          return this.say("info", "На доске ещё нечего перематывать");
        }
        this.film = { marks: new Uint32Array(buffer, 0, count), events, step, start, total: events.length / 3 };
      } finally {
        this.reeling = false;
      }
      if (this.wide) this.tab = "film";
      this.sheet = null;
      this.sel = null;
      this.hover = null;
      this.tip = null;
      this.mini = null;
      this.rewind(0);
      this.play();
    },

    stopFilm() {
      this.pause();
      if (this.tab === "film") this.tab = "draw";
      this.film = null;
      this.at = 0;
      this.load(); // вернуть живую доску
    },

    // Назад не отматывается: событие не помнит, что было под ним. «Раньше» — заново с пустого полотна.
    rewind(n) {
      this.cells.fill(0);
      for (let index = 0; index < this.cells.length; index++) this.stain(index, 0);
      this.at = 0;
      this.forward(n);
    },

    forward(n) {
      const { events, total } = this.film;
      const until = Math.min(n, total);
      for (; this.at < until; this.at++) {
        const at = this.at * 3;
        const index = events[at + 1] * this.width + events[at];
        this.cells[index] = events[at + 2];
        this.stain(index, events[at + 2]);
      }
      // Одна выкладка на всю пачку: поклеточная стоила бы столько же, сколько сам показ.
      this.bufferCtx.putImageData(this.image, 0, 0);
      this.invalidate();
    },

    seek(n) {
      if (!this.film) return;
      n = Math.max(0, Math.min(Number(n), this.film.total));
      if (n < this.at) this.rewind(n);
      else this.forward(n);
    },

    // Шаг — по прошедшему времени, а не «столько-то событий за кадр»: кадров в секунду у экранов
    // разное число (60, 120, 144), и на быстром мониторе запись неслась бы в разы быстрее.
    play() {
      if (!this.film || this.playing) return;
      if (this.at >= this.film.total) this.rewind(0);
      this.playing = true;
      let last = performance.now();
      let carry = 0;
      const tick = (now) => {
        if (!this.playing || !this.film) return;
        carry += this.rate * this.speed * Math.min(0.25, (now - last) / 1000);
        last = now;
        const step = Math.floor(carry);
        carry -= step;
        if (step) this.forward(this.at + step);
        if (this.at >= this.film.total) return this.pause();
        this.reelRaf = requestAnimationFrame(tick);
      };
      this.reelRaf = requestAnimationFrame(tick);
    },

    pause() {
      this.playing = false;
      cancelAnimationFrame(this.reelRaf);
    },

    faster() {
      this.speed = this.speed >= 8 ? 1 : this.speed * 2;
    },

    // Событий в секунду на ×1: журнал проезжает за FILM_SECONDS, короткий — не медленнее 60 в секунду.
    get rate() {
      return Math.max(60, this.film.total / FILM_SECONDS);
    },

    // Время под ползунком: между отметками — линейно.
    get filmTime() {
      if (!this.film || !this.film.marks.length) return "";
      const { marks, step, start } = this.film;
      const slot = Math.min(marks.length - 1, Math.floor(this.at / step));
      const next = Math.min(marks.length - 1, slot + 1);
      const seconds = marks[slot] + (marks[next] - marks[slot]) * ((this.at % step) / step);
      return new Date(start + seconds * 1000).toLocaleString("ru", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
    },

    // Кадры — из того же полотна, что на экране. Между кадрами отдаём управление браузеру.
    async saveGif() {
      if (!this.film || this.saving) return;
      this.pause();
      this.saving = 1;
      try {
        const { total } = this.film;
        const per = Math.max(1, Math.ceil(total / GIF_FRAMES));
        const writer = gifWriter(this.width, this.height, this.rgbs, GIF_SCALE);
        this.rewind(0);
        for (let n = 0; n < total; n += per) {
          if (!this.film) return; // из таймлапса вышли на середине записи
          this.forward(n + per);
          writer.add(this.cells, GIF_DELAY);
          this.saving = Math.max(1, Math.round((n / total) * 100));
          await this.breathe();
        }
        writer.add(this.cells, 300); // последний кадр — три секунды, иначе не рассмотреть
        this.keep(writer.finish());
      } finally {
        this.saving = 0;
      }
    },

    // Пауза через сообщение себе: setTimeout в фоновой вкладке режется до секунды.
    breathe() {
      return new Promise((next) => {
        const channel = new MessageChannel();
        channel.port1.onmessage = () => next();
        channel.port2.postMessage(0);
      });
    },

    keep(blob) {
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `wall-${new Date().toISOString().slice(0, 10)}.gif`;
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 30000); // браузер ещё скачивает
      this.say("success", `Записал: ${Math.round(blob.size / 1024)} КБ`);
    },
  }));
});
