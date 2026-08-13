// Кодировщик GIF под нашу доску.
//
// Обычно для этого берут библиотеку, но она половину работы делает впустую: квантует
// цвета, которых у нас сотня с небольшим, и заводит воркеры, чтобы это пережить. Доска
// и так хранится индексами палитры — это и есть внутреннее представление GIF, поэтому
// кодировать нечего, кроме сжатия.
//
// Кадры пишем разницей: между двумя мгновениями таймлапса меняется горстка клеток,
// остальное помечаем прозрачным индексом и не трогаем совсем. Отсюда и файл в сотни
// килобайт там, где полные кадры дали бы десятки мегабайт.
//
// Размер таблицы цветов считаем от самой палитры, а не пишем числом: палитра растёт,
// а зашитая цифра тихо переполнилась бы и перекрасила запись в мусор.

// Классический LZW переменной длины, как его требует GIF: код растёт с bits+1 до 12 бит,
// на 4096 словарь сбрасывается. Биты укладываются младшими вперёд.
function squeeze(pixels, bits) {
  const SIZE = 1 << bits;
  const CLEAR = SIZE;
  const END = CLEAR + 1;
  const out = [];
  let hold = 0;
  let held = 0;
  let size = bits + 1;
  let dict = new Map();
  let next = END + 1;

  const put = (code) => {
    hold |= code << held;
    held += size;
    while (held >= 8) {
      out.push(hold & 255);
      hold >>= 8;
      held -= 8;
    }
  };

  put(CLEAR);
  let prefix = pixels[0];
  for (let at = 1; at < pixels.length; at++) {
    const symbol = pixels[at];
    const key = prefix * SIZE + symbol; // symbol < SIZE, поэтому пара кодируется числом
    const known = dict.get(key);
    if (known !== undefined) {
      prefix = known;
      continue;
    }
    put(prefix);
    if (next === 4096) {
      put(CLEAR);
      dict = new Map();
      next = END + 1;
      size = bits + 1;
    } else {
      if (next >= 1 << size) size++;
      dict.set(key, next++);
    }
    prefix = symbol;
  }
  put(prefix);
  put(END);
  if (held) out.push(hold & 255);
  return out;
}

window.gifWriter = function (width, height, palette, scale = 1) {
  // Плюс один — место под служебный индекс «здесь не изменилось», его в палитре нет.
  const BITS = Math.max(2, Math.ceil(Math.log2(palette.length + 1)));
  const SIZE = 1 << BITS;
  const BLANK = SIZE - 1;
  const parts = [];
  const push = (...bytes) => parts.push(Uint8Array.from(bytes));
  const u16 = (value) => push(value & 255, value >> 8);
  let prev = null;

  push(...[..."GIF89a"].map((letter) => letter.charCodeAt(0)));
  u16(width * scale);
  u16(height * scale);
  push(0x80 | ((BITS - 1) << 4) | (BITS - 1), 0, 0); // своя таблица цветов, фон нулевой
  const table = new Uint8Array(SIZE * 3);
  palette.forEach(([red, green, blue], code) => table.set([red, green, blue], code * 3));
  parts.push(table);
  // Приложение-расширение Netscape: единственный способ сказать «крутить бесконечно».
  push(0x21, 0xFF, 0x0B, ...[..."NETSCAPE2.0"].map((l) => l.charCodeAt(0)), 0x03, 0x01, 0, 0, 0);

  // Данные кадра идут блоками не длиннее 255 байт, каждый со своей длиной.
  const chunks = (bytes) => {
    for (let at = 0; at < bytes.length; at += 255) {
      const piece = bytes.slice(at, at + 255);
      push(piece.length);
      parts.push(Uint8Array.from(piece));
    }
    push(0);
  };

  return {
    /** cells — индексы палитры на всю доску, delay — сколько сотых секунды держать кадр. */
    add(cells, delay) {
      let x1 = width;
      let y1 = height;
      let x2 = -1;
      let y2 = -1;
      for (let y = 0; y < height; y++) {
        for (let x = 0; x < width; x++) {
          const at = y * width + x;
          if (prev && prev[at] === cells[at]) continue;
          if (x < x1) x1 = x;
          if (x > x2) x2 = x;
          if (y < y1) y1 = y;
          if (y > y2) y2 = y;
        }
      }

      push(0x21, 0xF9, 0x04, 0x05, delay & 255, delay >> 8, BLANK, 0); // кадр не стирать, прозрачность есть
      if (x2 < 0) {
        // Ничего не изменилось — держим паузу прозрачной точкой в один пиксель.
        push(0x2C);
        u16(0);
        u16(0);
        u16(1);
        u16(1);
        push(0, BITS);
        chunks(squeeze(Uint8Array.of(BLANK), BITS));
        return;
      }

      const w = (x2 - x1 + 1) * scale;
      const h = (y2 - y1 + 1) * scale;
      push(0x2C);
      u16(x1 * scale);
      u16(y1 * scale);
      u16(w);
      u16(h);
      push(0);

      // Увеличиваем повторением клеток: доска — это пиксели, и мылить их нельзя.
      const frame = new Uint8Array(w * h);
      for (let y = y1; y <= y2; y++) {
        for (let x = x1; x <= x2; x++) {
          const at = y * width + x;
          const code = prev && prev[at] === cells[at] ? BLANK : cells[at];
          const top = (y - y1) * scale;
          const left = (x - x1) * scale;
          for (let dy = 0; dy < scale; dy++) frame.fill(code, (top + dy) * w + left, (top + dy) * w + left + scale);
        }
      }
      push(BITS);
      chunks(squeeze(frame, BITS));
      prev = cells.slice();
    },

    finish() {
      push(0x3B);
      return new Blob(parts, { type: "image/gif" });
    },
  };
};
