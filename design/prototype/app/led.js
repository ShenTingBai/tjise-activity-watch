/* 点阵屏：车站 LED 大屏那种 5×7 点阵数字。
 *
 * 只画倒计时用得到的字：0-9、冒号、横杠、空格，外加 ✓ / ✕ 两个结果图标。
 * 灭着的点也画出来（暗色），这样数字变化时不会跳版，看上去才像一块真的屏。
 *
 *   const led = LED.mount(el);    // el 里会插入一个 <svg>
 *   led.set("02:14:37", "amber"); // 色调：amber / dim / red / green / off
 */
(function (root) {
  "use strict";

  const NS = "http://www.w3.org/2000/svg";
  const GLYPHS = {
    "0": ["01110", "10001", "10011", "10101", "11001", "10001", "01110"],
    "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
    "2": ["01110", "10001", "00001", "00010", "00100", "01000", "11111"],
    "3": ["11111", "00010", "00100", "00010", "00001", "10001", "01110"],
    "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
    "5": ["11111", "10000", "11110", "00001", "00001", "10001", "01110"],
    "6": ["00110", "01000", "10000", "11110", "10001", "10001", "01110"],
    "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
    "8": ["01110", "10001", "10001", "01110", "10001", "10001", "01110"],
    "9": ["01110", "10001", "10001", "01111", "00001", "00010", "01100"],
    "-": ["00000", "00000", "00000", "11111", "00000", "00000", "00000"],
    " ": ["00000", "00000", "00000", "00000", "00000", "00000", "00000"],
    ":": ["0", "0", "1", "0", "1", "0", "0"],
    "✓": ["0000000", "0000001", "0000011", "1000110", "1101100", "0111000", "0010000"],
    "✕": ["1000001", "0100010", "0010100", "0001000", "0010100", "0100010", "1000001"],
  };
  const R = 0.36;          // 点半径（以点距为 1）
  const GAP = 1;           // 字与字之间空一列

  function glyph(ch) { return GLYPHS[ch] || GLYPHS[" "]; }

  function shapeOf(chars) { return chars.map((c) => glyph(c)[0].length).join(","); }

  function build(svg, chars) {
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    const cells = [];
    let x = 0;
    chars.forEach((ch, i) => {
      const g = glyph(ch);
      const w = g[0].length;
      const group = document.createElementNS(NS, "g");
      const dots = [];
      for (let row = 0; row < 7; row++) {
        for (let col = 0; col < w; col++) {
          const c = document.createElementNS(NS, "circle");
          c.setAttribute("cx", String(x + col + 0.5));
          c.setAttribute("cy", String(row + 0.5));
          c.setAttribute("r", String(R));
          group.appendChild(c);
          dots.push(c);
        }
      }
      svg.appendChild(group);
      cells.push({ ch: null, w, dots });
      x += w + (i < chars.length - 1 ? GAP : 0);
    });
    svg.setAttribute("viewBox", `0 0 ${x} 7`);
    return cells;
  }

  function paint(cell, ch) {
    if (cell.ch === ch) return;
    const g = glyph(ch);
    let k = 0;
    for (let row = 0; row < 7; row++) {
      for (let col = 0; col < cell.w; col++) {
        cell.dots[k++].classList.toggle("on", g[row][col] === "1");
      }
    }
    cell.ch = ch;
  }

  function mount(el) {
    const svg = document.createElementNS(NS, "svg");
    svg.setAttribute("class", "led");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    el.appendChild(svg);
    let cells = [];
    let shape = "";
    let text = null;
    return {
      set(next, tone) {
        if (tone && el.dataset.tone !== tone) el.dataset.tone = tone;
        if (next === text) return;
        const chars = Array.from(String(next));
        const s = shapeOf(chars);
        if (s !== shape) { cells = build(svg, chars); shape = s; }
        chars.forEach((ch, i) => paint(cells[i], ch));
        text = next;
        el.setAttribute("aria-label", String(next).replace("✓", "成功").replace("✕", "失败"));
      },
      /* 点亮的那一下：只在「到点自动报名」生效时用一次 */
      flicker() {
        if (root.matchMedia && root.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
        el.classList.remove("is-flicker");
        void el.offsetWidth;
        el.classList.add("is-flicker");
      },
    };
  }

  root.LED = { mount, GLYPHS };
})(window);
