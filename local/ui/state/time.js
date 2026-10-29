/* 学校时间（UTC+8）文本与换算纯函数。 */
const SCHOOL_TZ = "Asia/Shanghai";
const SCHOOL_OFFSET_S = 8 * 3600;
const SCHOOL_FMT = new Intl.DateTimeFormat("zh-CN", {
  timeZone: SCHOOL_TZ, year: "numeric", month: "2-digit", day: "2-digit",
  hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
});

/* 学校时区的年月日时分秒（整数字段，避免 toLocaleString 的本地化文本） */
function schoolParts(ms) {
  const out = {};
  SCHOOL_FMT.formatToParts(new Date(ms)).forEach((part) => {
    if (part.type !== "literal") out[part.type] = part.value;
  });
  return { y: Number(out.year), mo: Number(out.month), d: Number(out.day),
           h: Number(out.hour), mi: Number(out.minute), s: Number(out.second) };
}

/* 学校时间文本 → epoch 秒；非法返回 null，绝不滚成另一个日期（R14）。
 * new Date(y, m, d, ...) 会把 2026-02-30 悄悄变成 3 月 2 日——那等于替用户
 * 编了一个不存在的报名时间。这里用 Date.UTC 算，再逐字段回验。 */
function toTs(value) {
  if (!value) return null;
  const match = String(value).match(/^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})$/);
  if (!match) return null;
  const [y, mo, d, h, mi, s] = match.slice(1).map(Number);
  if (mo < 1 || mo > 12 || d < 1 || d > 31 || h > 23 || mi > 59 || s > 59) return null;
  const utc = Date.UTC(y, mo - 1, d, h, mi, s);
  const check = new Date(utc);
  if (check.getUTCFullYear() !== y || check.getUTCMonth() !== mo - 1 ||
      check.getUTCDate() !== d || check.getUTCHours() !== h ||
      check.getUTCMinutes() !== mi || check.getUTCSeconds() !== s) return null;
  return utc / 1000 - SCHOOL_OFFSET_S;
}

function fmt(seconds) {
  let value = Math.max(0, Math.floor(seconds));
  const days = Math.floor(value / 86400);
  const hours = Math.floor((value % 86400) / 3600);
  const minutes = Math.floor((value % 3600) / 60);
  const secs = value % 60;
  const pad = (number) => String(number).padStart(2, "0");
  if (days > 0) return days + "天 " + pad(hours) + ":" + pad(minutes);
  return pad(hours) + ":" + pad(minutes) + ":" + pad(secs);
}

/* 行内距开放：7 分 16 秒 / 2 时 53 分 / 1 天 3 时 */
function durText(seconds) {
  const value = Math.max(0, Math.floor(seconds));
  const days = Math.floor(value / 86400);
  const hours = Math.floor((value % 86400) / 3600);
  const minutes = Math.floor((value % 3600) / 60);
  const secs = value % 60;
  if (days) return days + " 天 " + hours + " 时";
  if (hours) return hours + " 时 " + String(minutes).padStart(2, "0") + " 分";
  if (minutes) return minutes + " 分 " + String(secs).padStart(2, "0") + " 秒";
  return secs + " 秒";
}

function shortTime(value) {
  const match = String(value || "").match(/^\d{4}-(\d{2})-(\d{2}) (\d{2}):(\d{2})/);
  return match ? match[1] + "-" + match[2] + " " + match[3] + ":" + match[4] : "";
}

function hm(ms) {
  const p = schoolParts(ms);
  const pad = (n) => String(n).padStart(2, "0");
  return pad(p.h) + ":" + pad(p.mi);
}

/* 学校口径的「当天零点」：只用来算跨天差，不做本地时区换算 */
function startOfDay(ms) {
  const p = schoolParts(ms);
  return Date.UTC(p.y, p.mo - 1, p.d);
}

function relDayText(ms) {
  const bnMs = businessNowMs();
  const diff = bnMs == null ? NaN : Math.round((startOfDay(ms) - startOfDay(bnMs)) / 86400000);
  if (diff === 0) return "今天";
  if (diff === 1) return "明天";
  if (diff === 2) return "后天";
  if (diff === -1) return "昨天";
  const p = schoolParts(ms);
  const weekday = WEEK[new Date(Date.UTC(p.y, p.mo - 1, p.d)).getUTCDay()];
  return p.mo + "月" + p.d + "日 " + weekday;
}

function dayLabel(ms) {
  return relDayText(ms);
}

/* 完整时刻：今天只写时分，别的天带日期星期（一律学校时区） */
function whenText(ms) {
  const bnMs = businessNowMs();
  const diff = bnMs == null ? NaN : Math.round((startOfDay(ms) - startOfDay(bnMs)) / 86400000);
  const p = schoolParts(ms);
  const pad = (n) => String(n).padStart(2, "0");
  const time = pad(p.h) + ":" + pad(p.mi) + (p.s ? ":" + pad(p.s) : "");
  if (diff === 0) return "今天 " + time;
  if (diff === 1) return "明天 " + time;
  if (diff === 2) return "后天 " + time;
  return p.mo + "月" + p.d + "日 " + time;
}

function esc(value) {
  return String(value == null ? "" : value).replace(/[&<>\"]/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;" }[character]));
}

function cssEscape(value) {
  return String(value).replace(/(["\\])/g, "\\$1");
}

/* ---------------------------------------------------------------- 六步可重播引导 */
/* 引导「可见」= 遮罩在场且没被计划运行暂停：只有可见时才约束键盘焦点 */
