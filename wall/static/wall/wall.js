// Полотно Стены: рисование, зум, живые обновления, подложка и инструменты модератора.
//
// Держим два холста. Скрытый — размером с доску, один пиксель на клетку: в него кладём
// снимок и точечно правим прилетающие события. Видимый — растянутая копия скрытого.
// Так перерисовка любого масштаба стоит одного drawImage, а не обхода клеток. Узлов DOM
// на клетки нет и быть не может: их девять тысяч.
//
// Перерисовку не зовём напрямую из обработчиков — только через invalidate(): мышь шлёт
// события чаще, чем экран успевает обновиться, и без склейки по кадрам курсор на доске
// начинает заметно отставать.
//
// Указатели считаем поимённо, а не «нажато/отпущено»: пальцев на холсте бывает два,
// и щипок — единственный способ увеличить доску на телефоне.

const PAD = 16; // поля вокруг доски, чтобы крайние клетки не липли к рамке холста
const GRID_FROM = 5; // с этого масштаба рисуем сетку — мельче она съедает сам рисунок
const DRAG = 5; // сдвиг в точках, после которого нажатие считается перетаскиванием
const HANDLE = 14; // сторона уголка, за который тянут размер подложки
const TOP = 40; // дальше не приближаем: клетка и так во весь палец
const TAP = 320; // мс между касаниями, которые считаем двойным
const NEAR = 24; // и допуск по расстоянию для него — палец не попадает в ту же точку
const CATCHUP = 15000; // как часто спрашиваем, не отстала ли доска от сервера
const SWATCH = 20; // сторона образца в палитре
const SIDEWAYS = 1024; // с этой ширины палитра и панель стоят рядом с доской (lg в разметке)

// Запись: столько кадров и такая задержка дают ролик секунд на десять, а тройное
// увеличение — картинку 384×216, которую не мылит ни один чат.
const GIF_FRAMES = 240;
const GIF_DELAY = 4; // сотых секунды на кадр
const GIF_SCALE = 3;

document.addEventListener("alpine:init", () => {
  Alpine.data("wall", (dataId) => ({
    ...JSON.parse(document.getElementById(dataId).textContent),

    cells: null, // Uint8Array: код цвета в каждой клетке
    version: 0, // номер последнего учтённого события
    loading: false,
    queue: [], // события, прилетевшие пока едет снимок
    scale: 1,
    ox: 0, // сдвиг полотна внутри видимого холста, в css-пикселях
    oy: 0,
    sel: null, // выбранная клетка { x, y }
    hover: null, // клетка под курсором — её подсвечиваем цветом кисти
    mine: false, // выбранную клетку дают стереть
    owner: null, // чей пиксель выбран — по нему модератор закрывает доску
    ownerName: "",
    zoomed: false, // человек трогал масштаб — больше не подгоняем доску сами
    busy: false,
    now: Date.now(), // тикает раз в секунду, из него считается таймер зарядов

    panel: null, // открытая панель: cell | tpl | tools

    points: {}, // указатели, лежащие сейчас на холсте, по id
    pinch: null, // жест двумя пальцами: расстояние и середина
    tap: null, // прошлое касание — из него узнаём двойное
    grab: null,
    origin: null,
    last: null, // последняя точка указателя — по ней возвращаем прицел после перетаскивания
    panning: false,
    raf: null,
    dirty: null, // что перерисовать на ближайшем кадре: "all" или "aim"
    rect: null, // запомненное положение холста на странице, null — пересчитать
    port: innerWidth, // ширина окна: от неё зависит, где стоит палитра и где панель
    attempt: 0, // подряд неудачных подключений — из него растёт пауза

    film: null, // журнал доски целиком: { events, marks, step, start, total }
    at: 0, // сколько событий журнала уже наложено на полотно
    playing: false,
    speed: 1,
    reeling: false, // журнал ещё едет

    artist: false, // режим художника: модератор кладёт любой цвет и без зарядов
    brush: null, // выбранный цвет кисти, null — свой
    from: null, // углы области для инструментов модератора
    to: null,
    areaEdit: false, // область выделяют мышью — доска на это время не таскается
    areaDrag: false,
    tpl: null, // подложка: { src, x, y, w, h, iw, ih, alpha, on }
    tplImage: null,
    tplEdit: false,
    tplDrag: null,

    init() {
      // Токен забираем здесь и запоминаем: в методе, вызванном с кнопки, $el — это сама
      // кнопка, а не корень компонента, и искать в ней скрытое поле формы бесполезно.
      this.token = this.$el.querySelector("[name=csrfmiddlewaretoken]").value;
      // Разбираем палитру один раз: иначе каждый снимок — это 27 тысяч parseInt.
      this.rgbs = this.colors.map((color) => [1, 3, 5].map((at) => parseInt(color.hex.slice(at, at + 2), 16)));
      this.buffer = document.createElement("canvas");
      this.buffer.width = this.width;
      this.buffer.height = this.height;
      this.bufferCtx = this.buffer.getContext("2d");
      this.image = this.bufferCtx.createImageData(this.width, this.height);
      this.ctx = this.$refs.view.getContext("2d");
      this.aimCtx = this.$refs.aim.getContext("2d");
      this.next = this.next ? Date.parse(this.next) : null;
      // Кисть помним между заходами: художник дорисовывает начатое, а не ищет цвет заново.
      // С проверкой по списку: палитра меняется, а запомненный код — нет, и на цвете,
      // которого уже нет, споткнулась бы первая же отрисовка.
      const saved = Number(localStorage.getItem(`wall.brush.${this.board}`));
      this.brush = saved > 0 && saved < this.colors.length ? saved : this.color;
      this.restoreTemplate();

      // Положение холста держим наготове: спрашивать его у браузера на каждое движение
      // мыши — значит заставлять страницу пересчитывать раскладку сотню раз в секунду.
      const forget = () => (this.rect = null);
      addEventListener("scroll", forget, { passive: true });
      addEventListener("resize", () => {
        forget();
        this.port = innerWidth;
      });
      new ResizeObserver(() => this.resize()).observe(this.$refs.view.parentElement);
      setInterval(() => this.tick(), 1000);
      setInterval(() => this.catchUp(), CATCHUP);
      // Вкладку, на которую вернулись, догоняем сразу: пока она лежала свёрнутой, опрос
      // не шёл, и доска на ней ровно та, какой её оставили.
      addEventListener("visibilitychange", () => this.catchUp());
      // Сокет открываем первым: события, пришедшие пока едет снимок, подождут в очереди.
      // Наоборот было бы дырой — чужой мазок между снимком и подпиской пропал бы совсем.
      this.connect();
      this.load();

      // Карточка клетки приезжает фрагментом с сервера — он же решает, моя ли она
      // и кого показывать модератору как автора.
      this.$el.addEventListener("htmx:afterSwap", () => {
        const card = this.$refs.card.querySelector("[data-mine]");
        if (!card) return; // приехал список носителей цвета, а не карточка
        this.mine = card.dataset.mine === "1";
        this.owner = card.dataset.owner ? Number(card.dataset.owner) : null;
        this.ownerName = card.dataset.ownerName || "";
      });
    },

    // --- что видно на панелях ---

    get colorHex() {
      return this.colors[this.color].hex;
    },

    get colorName() {
      return this.colors[this.color].name;
    },

    // Каким цветом ляжет следующий мазок. Пока цвет закреплён за аккаунтом, выбирать
    // нечего — кроме режима художника, где модератору можно любой.
    get brushCode() {
      return this.own_color && !this.artist ? this.color : this.brush;
    },

    pick(code) {
      if (this.own_color && !this.artist) return; // цвет закреплён, кисть не меняется
      this.brush = code;
      localStorage.setItem(`wall.brush.${this.board}`, code);
    },

    // Палитра стоит слева от доски, а под доску уходит только на узком экране. Отсюда
    // и разворот: сбоку лесенка тона идёт строкой, снизу — колонкой. Порядок кнопок
    // один и тот же, меняется только раскладка сетки.
    get paletteGrid() {
      const ladder = `repeat(${this.hues}, ${SWATCH}px)`;
      const size = `${SWATCH}px`;
      return this.port >= SIDEWAYS
        ? { display: "grid", gridAutoFlow: "column", gridTemplateRows: ladder, gridAutoColumns: size, gap: "2px" }
        : { display: "grid", gridTemplateColumns: ladder, gridAutoRows: size, gap: "2px" };
    },

    get greyGrid() {
      const many = `repeat(${this.greys.length}, ${SWATCH}px)`;
      return {
        display: "grid",
        gridTemplateColumns: this.port >= SIDEWAYS ? many : `${SWATCH}px`,
        gridAutoRows: `${SWATCH}px`,
        gap: "2px",
      };
    },

    // Палитру рисуем из тех же данных, что и полотно: своей разметки на сервере для неё
    // не надо, список цветов там уже есть. Нейтральные идут отдельной группой.
    get tones() {
      return this.colors
        .map((color, code) => ({ ...color, code }))
        .filter((color) => color.code && color.code < this.neutral_from);
    },

    get greys() {
      return this.colors.map((color, code) => ({ ...color, code })).slice(this.neutral_from);
    },

    get zoomLabel() {
      return `×${this.scale.toFixed(1)}`;
    },

    get spot() {
      const cell = this.hover || this.sel;
      return cell ? `${cell.x}, ${cell.y}` : `${this.width}×${this.height}`;
    },

    // Тот же порог, что у lg в разметке: за ним панель стоит рядом с доской, до него — шторкой.
    get wide() {
      return this.port >= SIDEWAYS;
    },

    get panelTitle() {
      return { cell: "Клетка", tpl: "Подложка", tools: "Инструменты" }[this.panel] || "";
    },

    get rectReady() {
      return !!(this.from && this.to);
    },

    get rectCells() {
      if (!this.rectReady) return 0;
      return (Math.abs(this.to.x - this.from.x) + 1) * (Math.abs(this.to.y - this.from.y) + 1);
    },

    get rectSize() {
      if (!this.rectReady) return "область не выделена";
      const w = Math.abs(this.to.x - this.from.x) + 1;
      const h = Math.abs(this.to.y - this.from.y) + 1;
      return `${w}×${h} = ${this.rectCells} кл.`;
    },

    get rectTooBig() {
      return this.rectCells > this.max_area;
    },

    show(name) {
      this.panel = this.panel === name ? null : name;
      if (this.panel !== "tpl") this.tplEdit = false;
      if (this.panel !== "tools") this.areaEdit = false;
    },

    // --- полотно ---

    async load() {
      if (this.film) return; // идёт таймлапс — живая доска подождёт до выхода
      this.loading = true;
      try {
        const response = await fetch(this.urls.snapshot, { headers: { "Cache-Control": "no-cache" } });
        const version = Number(response.headers.get("X-Wall-Version"));
        const cells = new Uint8Array(await response.arrayBuffer());
        const first = !this.cells;
        this.cells = cells;
        this.version = version;
        for (let index = 0; index < cells.length; index++) this.stain(index, cells[index]);
        this.bufferCtx.putImageData(this.image, 0, 0);
        // Масштаб подгоняем только на первом снимке: перекачка после обрыва связи
        // не должна выбрасывать человека из того места доски, куда он смотрел.
        if (first) this.resize();
        else this.invalidate();
      } finally {
        // Очередь распускаем даже если снимок не доехал: иначе события копятся без конца,
        // а доска так и стоит.
        this.loading = false;
        const waiting = this.queue;
        this.queue = [];
        for (const message of waiting) this.apply(message);
      }
    },

    stain(index, code) {
      const [red, green, blue] = this.rgbs[code];
      const at = index * 4;
      this.image.data[at] = red;
      this.image.data[at + 1] = green;
      this.image.data[at + 2] = blue;
      this.image.data[at + 3] = 255;
    },

    // Правит скрытый холст, но не перерисовывает видимый: у пачки клеток перерисовка
    // должна быть одна на всех, а не на каждую.
    set(x, y, code) {
      const index = y * this.width + x;
      if (this.cells[index] === code) return;
      this.cells[index] = code;
      this.stain(index, code);
      this.bufferCtx.putImageData(this.image, 0, 0, x, y, 1, 1);
    },

    put(x, y, code) {
      this.set(x, y, code);
      this.invalidate();
    },

    // Одна перерисовка на кадр, сколько бы событий ни пришло. Уровень "aim" трогает
    // только слой прицела: при простом движении мыши доска не меняется вовсе.
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
      // Пока масштаб не трогали руками — подгоняем заново. Без этого доска остаётся
      // в масштабе, посчитанном до применения стилей: на холодной загрузке размеры
      // контейнера успевают померяться раньше, чем приедет css, и доска выходит вдвое мельче.
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
      // Не даём уйти в ноль и минус: на совсем низком окне поля съедают всю высоту,
      // и отрицательный масштаб потом не выправить ничем.
      return Math.max(0.05, Math.min((w - 2 * PAD) / this.width, (h - 2 * PAD) / this.height));
    },

    fit() {
      this.scale = this.floor;
      this.zoomed = false;
      this.hold();
      this.invalidate();
    },

    // Видимый кусок доски в её собственных клетках: всё остальное рисовать незачем,
    // а при сильном увеличении «всё остальное» — это почти вся доска.
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
      const drawnW = this.width * this.scale;
      const drawnH = this.height * this.scale;
      this.ctx.clearRect(0, 0, w, h);
      this.ctx.imageSmoothingEnabled = false; // иначе клетки размажутся при увеличении
      const seen = this.seen;
      if (seen.w > 0 && seen.h > 0) {
        this.ctx.drawImage(
          this.buffer, seen.x, seen.y, seen.w, seen.h,
          this.ox + seen.x * this.scale, this.oy + seen.y * this.scale,
          seen.w * this.scale, seen.h * this.scale,
        );
      }
      this.paintTemplate();

      // Край доски: без него бетон сливается с фоном страницы и не понять, где она кончается.
      this.ctx.lineWidth = 1;
      this.ctx.strokeStyle = "rgba(120,120,130,.9)";
      this.ctx.strokeRect(this.ox - 0.5, this.oy - 0.5, drawnW + 1, drawnH + 1);

      this.grid();
      this.paintAreas();
      this.drawAim();
    },

    // Отдельный слой: движение мыши перерисовывает только его. Раньше вместе с ним
    // заново рисовались полотно и сетка, и квадратик заметно отставал от курсора.
    drawAim() {
      const { w, h } = this.view;
      this.aimCtx.clearRect(0, 0, w, h);
      // Пока доску тащат, метки убираем: висящий на месте прицел выглядит поломкой,
      // а клетка под курсором всё равно уже не та, над которой он был при нажатии.
      if (this.panning || this.tplEdit || this.film) return;
      // Курсор-перекрестье теряется на бетоне, поэтому клетку под ним показываем сами —
      // и сразу тем цветом, который встанет, если нажать «Закрасить».
      if (this.hover && !this.same(this.hover, this.sel)) this.mark(this.hover, this.colors[this.brushCode].hex);
      if (this.sel) this.mark(this.sel, null);
    },

    same(one, other) {
      return !!one && !!other && one.x === other.x && one.y === other.y;
    },

    paintTemplate() {
      if (!this.tpl || !this.tpl.on || !this.tplImage || this.film) return;
      const box = this.tplBox();
      this.ctx.globalAlpha = this.tpl.alpha;
      this.ctx.drawImage(this.tplImage, box.x, box.y, box.w, box.h);
      this.ctx.globalAlpha = 1;
      if (!this.tplEdit) return;

      this.ctx.setLineDash([5, 4]);
      this.ctx.lineWidth = 2;
      this.ctx.strokeStyle = "rgba(16,185,129,.95)";
      this.ctx.strokeRect(box.x, box.y, box.w, box.h);
      this.ctx.setLineDash([]);
      this.ctx.fillStyle = "rgba(16,185,129,.95)";
      this.ctx.fillRect(box.x + box.w - HANDLE, box.y + box.h - HANDLE, HANDLE, HANDLE);
    },

    tplBox() {
      return {
        x: this.ox + this.tpl.x * this.scale,
        y: this.oy + this.tpl.y * this.scale,
        w: this.tpl.w * this.scale,
        h: this.tpl.h * this.scale,
      };
    },

    // Закрытые участки видны всем, а не только модератору: иначе человек тратит заряд
    // и получает отказ, не понимая, за что. В таймлапсе их нет: рамки сегодняшнего дня
    // поверх прошлогоднего рисунка — это не история, а помеха.
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
      this.ctx.strokeRect(
        this.ox + x1 * this.scale, this.oy + y1 * this.scale, w * this.scale, h * this.scale,
      );
    },

    grid() {
      if (this.scale < GRID_FROM) return;
      const { w, h } = this.view;
      const seen = this.seen;
      // Рисуем только видимые линии и только на видимую длину: при увеличении в сорок
      // раз доска шире экрана впятеро, и остальное уходило бы в никуда каждый кадр.
      const left = Math.max(this.ox, 0);
      const right = Math.min(this.ox + this.width * this.scale, w);
      const top = Math.max(this.oy, 0);
      const bottom = Math.min(this.oy + this.height * this.scale, h);
      // Чем крупнее клетки, тем заметнее сетка: у самой границы видимости она должна
      // едва проступать, иначе рисунок читается как клетчатая бумага, а не как рисунок.
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

    // fill — заливка-предпросмотр (для клетки под курсором) либо null для выбранной.
    // Рисует по слою прицела, а не по доске: он чистится и обновляется отдельно.
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
      // Две рамки, светлая поверх тёмной: одним цветом выделение теряется то на туши,
      // то на бумаге, а доска у нас как раз из края в край по светлоте.
      // Толщину привязываем к клетке: на общем плане рамка в три точки вокруг клетки
      // в полторы превращается в кляксу и закрывает то, что показывает.
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

    // clamp — для протяжки области: палец ушёл за край доски, а прямоугольник должен
    // дотянуться до границы, а не потеряться.
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

    onDown(event) {
      this.points[event.pointerId] = { x: event.clientX, y: event.clientY };
      // Захват указателя: без него движение за краем холста уходит другому элементу,
      // и доска замирает на полпути, хотя кнопку мыши никто не отпускал.
      this.$refs.view.setPointerCapture(event.pointerId);
      if (this.touches.length === 2) return this.startPinch();
      if (this.touches.length > 2) return;
      this.origin = { clientX: event.clientX, clientY: event.clientY };
      this.last = this.origin;
      this.panning = false;
      if (this.tplEdit && this.tpl) return this.grabTemplate(event);
      if (this.areaEdit) return this.grabArea(event);
      this.grab = { x: event.clientX - this.ox, y: event.clientY - this.oy };
    },

    onMove(event) {
      this.last = { clientX: event.clientX, clientY: event.clientY };
      const point = this.points[event.pointerId];
      if (point) {
        point.x = event.clientX;
        point.y = event.clientY;
      }
      if (this.pinch) return this.movePinch();
      if (this.tplDrag) return this.moveTemplate(event);
      if (this.areaDrag) return this.moveArea(event);
      if (!this.grab) {
        // Палец прицел не рисует: он и так стоит ровно там, где смотрят, а после
        // касания квадратик остался бы висеть на доске сам по себе.
        if (event.pointerType !== "mouse") return;
        const cell = this.cellAt(event);
        if (this.same(cell, this.hover) || (!cell && !this.hover)) return; // клетка та же — рисовать нечего
        this.hover = cell;
        return this.invalidate("aim");
      }
      // Считаем по пути указателя, а не по тому, сдвинулась ли доска: на вписанной
      // доске двигать нечего, и без этого отпускание кнопки выбирало бы клетку,
      // хотя человек тянул, а не целился.
      if (Math.hypot(event.clientX - this.origin.clientX, event.clientY - this.origin.clientY) > DRAG) {
        this.panning = true;
      }
      this.ox = event.clientX - this.grab.x;
      this.oy = event.clientY - this.grab.y;
      this.hold();
      // Захват пересчитываем от уже прижатого положения, иначе после упора в край доска
      // не тронется назад, пока курсор не вернётся ровно в ту же точку.
      this.grab = { x: event.clientX - this.ox, y: event.clientY - this.oy };
      this.invalidate();
    },

    onUp(event) {
      if (this.$refs.view.hasPointerCapture(event.pointerId)) {
        this.$refs.view.releasePointerCapture(event.pointerId);
      }
      delete this.points[event.pointerId];
      const left = this.touches;
      if (this.pinch) {
        // Пара пальцев сменилась — меряем заново, иначе следующее движение дёрнет масштаб.
        this.pinch = left.length >= 2 ? this.span() : null;
        // Оставшийся палец продолжает тащить доску: щипок почти всегда кончается сдвигом.
        if (!this.pinch && left.length) this.grab = { x: left[0].x - this.ox, y: left[0].y - this.oy };
      }
      if (left.length) return;

      this.grab = null;
      if (this.tplDrag) {
        this.tplDrag = null;
        this.saveTemplate();
      }
      this.areaDrag = false;
      if (this.panning) {
        // Прицел возвращаем туда, где курсор; палец никакого прицела не оставляет.
        this.hover = event.pointerType === "mouse" ? this.cellAt(this.last) : null;
      } else {
        this.doubled(event);
      }
      this.invalidate("aim");
    },

    onLeave() {
      if (this.grab || this.tplDrag || !this.hover) return; // тянем с захватом — курсор за краем это норма
      this.hover = null;
      this.invalidate("aim");
    },

    onClick(event) {
      const dragged = this.panning;
      this.panning = false;
      if (this.film) return; // в таймлапсе доска только смотрится, зум и перетаскивание работают
      if (dragged || this.tplEdit || this.areaEdit) return this.invalidate("aim"); // это было перетаскивание
      const cell = this.cellAt(event);
      if (!cell) return;
      this.sel = cell;
      this.mine = false; // до ответа сервера считаем чужой: кнопку лучше не дразнить
      this.invalidate("aim");
      // Художнику подтверждать нечего: цена ошибки — один клик, который её и исправит,
      // а рисовать, нажимая кнопку на каждую клетку, попросту невозможно.
      if (this.artist) return this.quickPaint(cell);
      // На узком экране панель — шторка поверх доски: открывать её на каждое касание
      // значит закрывать то, по чему только что ткнули. Там её зовут кнопкой.
      if (!this.panel && this.wide) this.panel = "cell";
      if (this.panel === "cell") this.showCard();
    },

    async quickPaint(cell) {
      // Без блокировки busy: художник щёлкает быстрее, чем идут ответы, и пропускать
      // клики нельзя. Клетку рисуем сразу, а расходимся с сервером только при отказе.
      this.put(cell.x, cell.y, this.brushCode);
      this.mine = true;
      const data = await (await this.post(this.urls.paint, {
        x: cell.x, y: cell.y, color: this.brushCode, free: 1,
      })).json();
      if (!data.error) return;
      this.say("error", data.error);
      this.load(); // вернуть то, что на доске на самом деле
    },

    // Двойное касание приближает: на телефоне это единственный способ увеличить одной
    // рукой. В режиме художника не работает — там два быстрых клика подряд это два мазка.
    doubled(event) {
      if (this.artist || this.tplEdit || this.areaEdit) return;
      const now = { x: event.clientX, y: event.clientY, when: Date.now() };
      const twice = this.tap && now.when - this.tap.when < TAP
        && Math.hypot(now.x - this.tap.x, now.y - this.tap.y) < NEAR;
      this.tap = twice ? null : now;
      if (!twice) return;
      const box = this.box();
      this.zoom(2, now.x - box.left, now.y - box.top);
      this.panning = true; // чтобы клик следом не выбрал клетку под пальцем
    },

    // --- жест двумя пальцами ---

    startPinch() {
      this.panning = true; // это жест, а не выбор клетки
      this.grab = null;
      this.areaDrag = false;
      this.tplDrag = null;
      this.pinch = this.span();
      this.invalidate("aim");
    },

    span() {
      const [one, other] = this.touches;
      return {
        gap: Math.hypot(one.x - other.x, one.y - other.y),
        cx: (one.x + other.x) / 2,
        cy: (one.y + other.y) / 2,
      };
    },

    // Расстояние между пальцами задаёт масштаб, их середина — куда тащить доску.
    movePinch() {
      const now = this.span();
      const box = this.box();
      this.ox += now.cx - this.pinch.cx;
      this.oy += now.cy - this.pinch.cy;
      const factor = this.pinch.gap > 0 ? now.gap / this.pinch.gap : 1;
      this.pinch = now;
      this.zoom(factor, now.cx - box.left, now.cy - box.top);
      this.hold(); // zoom мог ничего не менять на упоре — сдвиг всё равно надо прижать
      this.invalidate();
    },

    onWheel(event) {
      event.preventDefault();
      const box = this.box();
      this.zoom(event.deltaY < 0 ? 1.2 : 1 / 1.2, event.clientX - box.left, event.clientY - box.top);
    },

    // Кнопки масштаба целятся в середину холста, колесо и пальцы — в точку под собой.
    zoom(factor, px, py) {
      const { w, h } = this.view;
      if (px === undefined) {
        px = w / 2;
        py = h / 2;
      }
      const next = Math.min(Math.max(this.scale * factor, this.floor), TOP);
      if (next === this.scale) return;
      // Держим под прицелом ту же клетку — иначе место, куда человек целился, уезжает.
      this.ox = px - (px - this.ox) * (next / this.scale);
      this.oy = py - (py - this.oy) * (next / this.scale);
      this.scale = next;
      this.zoomed = next > this.floor;
      this.hold();
      this.invalidate();
    },

    // Не даём утащить доску за край: дальше пустота, и возвращать её оттуда неприятно.
    // Заодно округляем — на дробном сдвиге сетка и рамка выделения начинают дрожать.
    hold() {
      const { w, h } = this.view;
      const drawnW = this.width * this.scale;
      const drawnH = this.height * this.scale;
      this.ox = Math.round(drawnW + 2 * PAD <= w
        ? (w - drawnW) / 2
        : Math.min(PAD, Math.max(w - drawnW - PAD, this.ox)));
      this.oy = Math.round(drawnH + 2 * PAD <= h
        ? (h - drawnH) / 2
        : Math.min(PAD, Math.max(h - drawnH - PAD, this.oy)));
    },

    // --- клавиатура ---

    typing(event) {
      return /^(INPUT|TEXTAREA|SELECT)$/.test(event.target.tagName);
    },

    // Стрелками двигают выбранную клетку: попасть мышью в клетку в полторы точки
    // на общем плане невозможно, а поправить рисунок ровно на одну клетку хочется.
    nudge(dx, dy, event) {
      if (!this.sel || this.typing(event)) return;
      event.preventDefault();
      this.sel = {
        x: Math.min(this.width - 1, Math.max(0, this.sel.x + dx)),
        y: Math.min(this.height - 1, Math.max(0, this.sel.y + dy)),
      };
      this.invalidate("aim");
      if (this.panel !== "cell") return;
      // Карточку не дёргаем на каждое нажатие: по клавише зажатой стрелки их два десятка.
      clearTimeout(this.cardWait);
      this.cardWait = setTimeout(() => this.showCard(), 250);
    },

    escape() {
      if (this.tplEdit) return (this.tplEdit = false);
      if (this.areaEdit) return (this.areaEdit = false);
      if (this.panel) return (this.panel = null);
      this.sel = null;
      this.invalidate("aim");
    },

    // --- сокет ---

    connect() {
      const scheme = location.protocol === "https:" ? "wss" : "ws";
      const socket = new WebSocket(`${scheme}://${location.host}/ws/wall/`);
      socket.onopen = () => {
        // Пока лежали, чужие мазки шли мимо — проще перекачать снимок, он девять килобайт.
        if (this.attempt) this.load();
        this.attempt = 0;
      };
      socket.onmessage = (event) => this.apply(JSON.parse(event.data));
      // 1, 2, 4… до 30с: если сервер лежит, не добиваем его переподключениями.
      socket.onclose = () => setTimeout(() => this.connect(), Math.min(1000 * 2 ** this.attempt++, 30000));
    },

    // Сокет — не гарантия доставки. Пиксель, положенный мимо этого процесса (командой
    // из консоли, соседним воркером), уходит через общий слой сообщений, и если слой
    // занят или, как в разработке, живёт в памяти одного процесса, — на странице его
    // нет и не будет до перезагрузки. Спросить номер версии стоит два десятка байт,
    // и снимок перекачиваем, только если он и правда отстал.
    async catchUp() {
      if (this.film || this.loading || !this.cells || document.hidden) return;
      const response = await fetch(this.urls.version, { headers: { "Cache-Control": "no-cache" } });
      if (!response.ok) return;
      const { version } = await response.json();
      if (version > this.version) this.load();
    },

    apply(message) {
      if (this.film) return; // на историю чужие мазки не кладём, при выходе перечитаем доску
      // Снимок ещё едет — придержим: иначе он ляжет поверх и мазок пропадёт до перезагрузки.
      if (this.loading) return this.queue.push(message);
      if (!this.cells || message.id <= this.version) return; // доски ещё нет либо это уже в снимке
      this.version = message.id;
      if (message.pixels) {
        for (const [x, y, code] of message.pixels) this.set(x, y, code);
        this.invalidate();
      } else {
        this.put(message.x, message.y, message.color);
      }
    },

    // --- действия ---

    showCard() {
      if (!this.sel) return;
      htmx.ajax("GET", `${this.urls.pixel}?x=${this.sel.x}&y=${this.sel.y}`, {
        source: this.$refs.card, target: this.$refs.card,
      });
    },

    async act(kind) {
      if (!this.sel || this.busy || this.film) return;
      this.busy = true;
      try {
        const body = { x: this.sel.x, y: this.sel.y };
        if (kind === "paint") {
          body.color = this.brushCode;
          if (this.artist) body.free = 1; // без заряда — право на это проверит сервер
        }
        const data = await (await this.post(this.urls[kind], body)).json();
        this.charges = data.charges;
        this.next = data.next ? Date.parse(data.next) : null;
        if (data.error) return this.say("error", data.error);
        // Рисуем не дожидаясь сокета: своё действие должно отзываться сразу.
        this.put(this.sel.x, this.sel.y, kind === "paint" ? this.brushCode : 0);
        if (this.panel === "cell") this.showCard();
      } finally {
        this.busy = false;
      }
    },

    async reroll() {
      if (this.busy || !confirm(`Сменить цвет за ${this.price}? Новый выпадет случайно.`)) return;
      this.busy = true;
      try {
        const data = await (await this.post(this.urls.reroll)).json();
        if (data.error) return this.say("error", data.error);
        this.color = data.color;
        this.balance = data.balance;
        this.say("info", `Теперь у тебя ${data.name}`);
        this.invalidate();
      } finally {
        this.busy = false;
      }
    },

    // Новый сезон: прошлая доска уходит в архив целиком, страница перечитывается с нуля.
    async newBoard(title) {
      if (this.busy || !confirm(`Закрыть «${this.title}» и открыть «${title.trim()}»?\n`
          + "Нынешний рисунок уйдёт в архив, на странице будет пустая доска.")) return;
      this.busy = true;
      try {
        const data = await (await this.post(this.urls.newboard, { title })).json();
        if (data.error) return this.say("error", data.error);
        location.reload(); // сменилось всё: доска, снимок, журнал, подложка
      } finally {
        this.busy = false;
      }
    },

    post(url, values) {
      return fetch(url, {
        method: "POST",
        headers: { "X-CSRFToken": this.token, "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams(values || {}),
      });
    },

    say(type, text) {
      window.dispatchEvent(new CustomEvent("toast", { detail: { type, text } }));
    },

    // --- инструменты модератора ---

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
        // Клетки приедут по сокету — своих в обход не рисуем, иначе на пачке пришлось
        // бы повторять здесь всю серверную логику «что именно изменилось».
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

    async banOwner(days) {
      if (!this.owner) return;
      if (days && !confirm(`Закрыть Стену для «${this.ownerName}» на ${days} дн.?`)) return;
      const data = await (await this.post(this.urls.ban, { user: this.owner, days })).json();
      if (data.error) return this.say("error", data.error);
      this.say("success", days ? `Стена закрыта для «${data.who}»` : `Запрет снят с «${data.who}»`);
    },

    // --- таймлапс ---
    //
    // Журнал качаем целиком: три байта на событие, вся жизнь доски — десятки килобайт.
    // Доска на любой момент — это первые N событий, наложенные на пустое полотно, так
    // что перемотка не требует ни одного запроса.

    async reel() {
      if (this.film) return this.stopFilm();
      this.reeling = true;
      try {
        const response = await fetch(this.urls.history);
        const count = Number(response.headers.get("X-Wall-Marks"));
        const step = Number(response.headers.get("X-Wall-Step"));
        const start = Date.parse(response.headers.get("X-Wall-Start"));
        const buffer = await response.arrayBuffer();
        const events = new Uint8Array(buffer, count * 4);
        if (!events.length) return this.say("info", "На доске ещё нечего перематывать");
        this.film = {
          marks: new Uint32Array(buffer, 0, count),
          events, step, start, total: events.length / 3,
        };
      } finally {
        this.reeling = false;
      }
      this.panel = null;
      this.sel = null;
      this.hover = null;
      this.rewind(0);
      this.play();
    },

    stopFilm() {
      this.pause();
      this.film = null;
      this.at = 0;
      this.load(); // вернуть живую доску
    },

    // Назад доска не отматывается: событие не помнит, что было под ним. Поэтому «раньше» —
    // это заново с пустого полотна; пятнадцать тысяч байт проходятся быстрее, чем кадр.
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

    play() {
      if (!this.film || this.playing) return;
      if (this.at >= this.film.total) this.rewind(0);
      this.playing = true;
      const tick = () => {
        if (!this.playing || !this.film) return;
        this.forward(this.at + this.pace);
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

    // Сколько событий проглатываем за кадр. Считаем от их числа, а не от времени: доска
    // должна проезжать примерно за полминуты и на тысяче мазков, и на сотне тысяч.
    get pace() {
      return Math.max(1, Math.round(this.film.total / (60 * 30))) * this.speed;
    },

    // Время под ползунком. Отметки стоят через step событий, между ними тянем линейно:
    // на подпись «12 авг, 14:30» этого с запасом.
    get filmTime() {
      if (!this.film || !this.film.marks.length) return "";
      const { marks, step, start } = this.film;
      const slot = Math.min(marks.length - 1, Math.floor(this.at / step));
      const next = Math.min(marks.length - 1, slot + 1);
      const seconds = marks[slot] + (marks[next] - marks[slot]) * ((this.at % step) / step);
      return new Date(start + seconds * 1000).toLocaleString("ru", {
        day: "numeric", month: "short", hour: "2-digit", minute: "2-digit",
      });
    },

    // --- запись таймлапса ---

    saving: 0, // проценты; ноль — не пишем

    // Кадры собираем из того же полотна, которое крутится на экране: человек видит,
    // что именно записывается. Между кадрами отдаём управление браузеру, иначе вкладка
    // на полминуты замирает, а прогресс никто не увидит.
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
        writer.add(this.cells, 300); // последний кадр держим три секунды, иначе не рассмотреть
        this.keep(writer.finish());
      } finally {
        this.saving = 0;
      }
    },

    // Пауза на один оборот цикла событий. Не setTimeout: тот приколочен к четырём
    // миллисекундам, а в фоновой вкладке — к секунде, и запись в отвёрнутой вкладке
    // растянулась бы на минуты. Сообщение самому себе ни тем, ни другим не режется.
    breathe() {
      return new Promise((next) => {
        const line = new MessageChannel();
        line.port1.onmessage = () => next();
        line.port2.postMessage(0);
      });
    },

    keep(blob) {
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `wall-${new Date().toISOString().slice(0, 10)}.gif`;
      link.click();
      // Ссылку освобождаем не сразу: браузер ещё скачивает по ней, и отобрать её
      // в тот же миг местами значит отменить скачивание.
      setTimeout(() => URL.revokeObjectURL(url), 30000);
      this.say("success", `Записал: ${Math.round(blob.size / 1024)} КБ`);
    },

    // --- подложка (только в этом браузере) ---

    get tplKey() {
      return `wall.template.${this.board}`;
    },

    restoreTemplate() {
      const saved = localStorage.getItem(this.tplKey);
      if (saved) this.applyTemplate(JSON.parse(saved));
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
      this.tpl = {
        ...record,
        iw: image.naturalWidth,
        ih: image.naturalHeight,
        // Размер на доске задаётся отдельно от размера файла: эскиз рисуют крупно,
        // а на доску он должен лечь ровно теми клетками, которые люди будут ставить.
        w: record.w || image.naturalWidth,
        h: record.h || image.naturalHeight,
      };
      this.invalidate();
    },

    async pickTemplate(event) {
      const file = event.target.files[0];
      if (!file) return;
      const src = await new Promise((done) => {
        const reader = new FileReader();
        reader.onload = () => done(reader.result);
        reader.readAsDataURL(file);
      });
      const at = this.sel || { x: 0, y: 0 };
      await this.applyTemplate({ src, x: at.x, y: at.y, alpha: this.tpl?.alpha ?? 0.7, on: true });
      this.tplEdit = true;
      this.saveTemplate();
      event.target.value = ""; // иначе повторный выбор того же файла не даст события
    },

    // Тянем либо за уголок — тогда меняем размер, либо за саму картинку — двигаем.
    grabTemplate(event) {
      const box = this.tplBox();
      const view = this.box();
      const px = event.clientX - view.left;
      const py = event.clientY - view.top;
      const corner = px > box.x + box.w - HANDLE && px < box.x + box.w + HANDLE
        && py > box.y + box.h - HANDLE && py < box.y + box.h + HANDLE;
      this.tplDrag = {
        corner, x: this.tpl.x, y: this.tpl.y, w: this.tpl.w,
        sx: event.clientX, sy: event.clientY,
      };
    },

    moveTemplate(event) {
      const dx = (event.clientX - this.tplDrag.sx) / this.scale;
      const dy = (event.clientY - this.tplDrag.sy) / this.scale;
      if (this.tplDrag.corner) {
        // Пропорции держим: растянутый по одной стороне эскиз перестаёт совпадать
        // с тем, что по нему рисуют.
        const w = Math.max(1, Math.round(this.tplDrag.w + dx));
        this.tpl.w = w;
        this.tpl.h = Math.max(1, Math.round((w * this.tpl.ih) / this.tpl.iw));
      } else {
        this.tpl.x = Math.round(this.tplDrag.x + dx);
        this.tpl.y = Math.round(this.tplDrag.y + dy);
      }
      this.invalidate();
    },

    sizeTemplate(cells) {
      this.tpl.w = Math.max(1, cells);
      this.tpl.h = Math.max(1, Math.round((this.tpl.w * this.tpl.ih) / this.tpl.iw));
      this.saveTemplate();
      this.invalidate();
    },

    // Картинка никуда не уходит: она лежит в этом браузере и видна только тебе.
    // Иначе доску можно было бы бесплатно заклеить чужими рисунками.
    saveTemplate() {
      try {
        localStorage.setItem(this.tplKey, JSON.stringify(this.tpl));
      } catch {
        this.say("warning", "Подложка не сохранится до следующего раза: картинка слишком тяжёлая");
      }
    },

    dropTemplate() {
      this.tpl = null;
      this.tplImage = null;
      this.tplEdit = false;
      localStorage.removeItem(this.tplKey);
      this.invalidate();
    },

    // --- заряды ---

    tick() {
      this.now = Date.now();
      if (!this.next || this.now < this.next) return;
      this.charges = Math.min(this.charges + 1, this.max_charges);
      this.next = this.charges >= this.max_charges ? null : this.next + this.interval * 1000;
    },

    get timer() {
      if (!this.next) return "";
      const left = Math.max(0, Math.round((this.next - this.now) / 1000));
      return `${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`;
    },
  }));
});
