/* 天软活动预报名工具：5×7 LED 点阵渲染。
 *
 * 以 design/prototype/app/led.js 的字库与点距为参考的纯本地实现：
 * 只画倒计时用得到的字（数字、冒号、横杠、空格，外加 ✓ / ✕），
 * 灭着的点也画出来，数字变化不跳版。不产生任何业务事件、不发请求；
 * 不支持的字形按空格渲染，长文案由普通文本承载（见 app.js 的文本层）。
 *
 *   LED_UI.renderLed(el, "02:14:37", "amber");  // tone: amber/dim/red/green/off
 *   LED_UI.clearLed(el);
 */
(function (root) {
  "use strict";

  var NS = "http://www.w3.org/2000/svg";
  var GLYPHS = {
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
  var R = 0.36;   /* 点半径（以点距为 1） */
  var GAP = 1;    /* 字与字之间空一列 */
  var instances = new WeakMap();

  function glyph(ch) { return GLYPHS[ch] || GLYPHS[" "]; }

  function shapeOf(chars) { return chars.map(function (c) { return glyph(c)[0].length; }).join(","); }

  function build(svg, chars) {
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    var cells = [];
    var x = 0;
    chars.forEach(function (ch, i) {
      var g = glyph(ch);
      var w = g[0].length;
      var group = document.createElementNS(NS, "g");
      var dots = [];
      for (var row = 0; row < 7; row++) {
        for (var col = 0; col < w; col++) {
          var c = document.createElementNS(NS, "circle");
          c.setAttribute("cx", String(x + col + 0.5));
          c.setAttribute("cy", String(row + 0.5));
          c.setAttribute("r", String(R));
          group.appendChild(c);
          dots.push(c);
        }
      }
      svg.appendChild(group);
      cells.push({ ch: null, w: w, dots: dots });
      x += w + (i < chars.length - 1 ? GAP : 0);
    });
    svg.setAttribute("viewBox", "0 0 " + x + " 7");
    return cells;
  }

  function paint(cell, ch) {
    if (cell.ch === ch) return;
    var g = glyph(ch);
    var k = 0;
    for (var row = 0; row < 7; row++) {
      for (var col = 0; col < cell.w; col++) {
        cell.dots[k++].classList.toggle("on", g[row][col] === "1");
      }
    }
    cell.ch = ch;
  }

  /* 渲染/更新一个点阵容器；同一元素重复调用只更新亮灭，不重建 DOM */
  function renderLed(el, text, tone) {
    if (!el) return;
    var inst = instances.get(el);
    if (!inst) {
      var svg = document.createElementNS(NS, "svg");
      svg.setAttribute("class", "led");
      svg.setAttribute("aria-hidden", "true");
      svg.setAttribute("focusable", "false");
      el.appendChild(svg);
      inst = { svg: svg, cells: [], shape: "", text: null };
      instances.set(el, inst);
    }
    if (tone && el.dataset.tone !== tone) el.dataset.tone = tone;
    if (text === inst.text) return;
    var chars = Array.from(String(text == null ? "" : text));
    var s = shapeOf(chars);
    if (s !== inst.shape) {
      inst.cells = build(inst.svg, chars);
      inst.shape = s;
    }
    chars.forEach(function (ch, i) { paint(inst.cells[i], ch); });
    inst.text = text;
    el.setAttribute("aria-label", String(text).replace("✓", "成功").replace("✕", "失败"));
  }

  /* 清空点阵并移除容器内容；再次 renderLed 会重新挂载 */
  function clearLed(el) {
    if (!el) return;
    var inst = instances.get(el);
    if (inst) {
      instances.delete(el);
    }
    while (el.firstChild) el.removeChild(el.firstChild);
    el.removeAttribute("aria-label");
    el.removeAttribute("data-tone");
  }

  root.LED_UI = { renderLed: renderLed, clearLed: clearLed, GLYPHS: GLYPHS };
})(window);
