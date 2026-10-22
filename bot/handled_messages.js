/**
 * 已处理消息 ID 的持久化去重表。
 *
 * 为什么落盘：飞书会重投事件，进程重启后也可能收到同一批旧消息。
 * bot.js 原来用内存 Set 去重，进程一重启就清空，重启后的重投会再回一次卡片，
 * 群里看起来就是「机器人突然把刚才的问题又答了一遍」。
 *
 * 只存「处理成功」的消息 ID 和毫秒时间戳，不存消息正文、用户 ID 或任何凭证。
 *
 * 边界：
 * - 48 小时过期：飞书重投窗口远小于这个量级，留足冗余即可；
 * - 最多 2000 条：超出按时间淘汰最旧的，避免文件无界增长；
 * - 先写临时文件再 rename：重启时可能和写盘撞上，不能读到写了一半的 JSON；
 * - 解析失败直接抛错，不静默重建：静默清空等于把这 48 小时的去重记录丢掉，
 *   群里会突然重复回复，而且从日志看不出是缓存坏了。
 *
 * @author LiXinwen
 */
const fs = require('fs');
const path = require('path');

const DEFAULT_TTL_MS = 48 * 60 * 60 * 1000;
const DEFAULT_MAX_ENTRIES = 2000;

class HandledMessages {
  /**
   * @param {string} file 缓存文件路径（JSON 对象：message_id -> 毫秒时间戳）
   * @param {{ttlMs?: number, maxEntries?: number}} [options]
   */
  constructor(file, options = {}) {
    if (!file) throw new Error('handled message cache path is required');
    this.file = file;
    this.ttlMs = Number.isFinite(options.ttlMs) ? options.ttlMs : DEFAULT_TTL_MS;
    this.maxEntries = Number.isFinite(options.maxEntries)
      ? options.maxEntries
      : DEFAULT_MAX_ENTRIES;
    // 同一进程里正在处理、但还没写进文件的 ID。
    // 飞书并发重投时两条事件几乎同时到达，只靠文件挡不住。
    this.inFlight = new Set();
    this.entries = new Map();
    this.load();
  }

  /** 读盘。文件不存在视为空表；内容不合法抛错，交给调用方决定是否启动。 */
  load() {
    let raw = '';
    try {
      raw = fs.readFileSync(this.file, 'utf8');
    } catch (e) {
      if (e && e.code === 'ENOENT') {
        this.entries = new Map();
        return;
      }
      throw e;
    }

    let parsed = null;
    try {
      parsed = JSON.parse(raw);
    } catch (e) {
      throw new Error(
        `cannot parse handled message cache ${this.file}: ${e && e.message ? e.message : e}`
      );
    }
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      throw new Error(
        `cannot parse handled message cache ${this.file}: expected an object of id -> timestamp`
      );
    }

    const next = new Map();
    for (const [id, ts] of Object.entries(parsed)) {
      if (typeof ts !== 'number' || !Number.isFinite(ts)) {
        throw new Error(
          `cannot parse handled message cache ${this.file}: invalid timestamp for ${id}`
        );
      }
      next.set(id, ts);
    }
    this.entries = next;
    // 只按条数裁剪，不做时间裁剪：裁剪要用「现在」，
    // 而构造期的当前时间未必等于调用方后面传进来的时钟。
    this._trimByCount();
  }

  /** 是否已经处理过（过期的记录顺手删掉，但不必立刻回写）。 */
  has(id, now = Date.now()) {
    if (!id) return false;
    const ts = this.entries.get(id);
    if (ts === undefined) return false;
    if (now - ts >= this.ttlMs) {
      this.entries.delete(id);
      return false;
    }
    return true;
  }

  /** 记一条「已处理」，并把过期项和超出上限的旧项清掉。 */
  mark(id, now = Date.now()) {
    if (!id) return;
    this.entries.set(id, now);
    this._purgeExpired(now);
    this._trimByCount();
    this._save();
  }

  /**
   * 处理一次消息。只有 handler 明确返回 true 才记为已处理。
   *
   * 返回 false 的三种情况：已经处理过（含并发中的重复事件）、handler 返回非 true、
   * handler 抛错（异常继续往上抛，由调用方记日志）。
   * 后两种刻意不落盘：一次失败的查询不该被永久当成「答过了」，
   * 飞书重投时还要再试一次。
   */
  async processOnce(id, handler, now = Date.now()) {
    if (!id) return false;
    if (this.has(id, now)) return false;
    if (this.inFlight.has(id)) return false;

    this.inFlight.add(id);
    try {
      const ok = await handler();
      if (ok !== true) return false;
      this.mark(id, now);
      return true;
    } finally {
      this.inFlight.delete(id);
    }
  }

  _purgeExpired(now) {
    for (const [id, ts] of this.entries) {
      if (now - ts >= this.ttlMs) this.entries.delete(id);
    }
  }

  _trimByCount() {
    if (this.entries.size <= this.maxEntries) return;
    const ordered = [...this.entries.entries()].sort((a, b) => a[1] - b[1]);
    for (const [id] of ordered.slice(0, ordered.length - this.maxEntries)) {
      this.entries.delete(id);
    }
  }

  /** 原子写：临时文件 + rename，顺便把权限收成 0600（Linux 上生效）。 */
  _save() {
    fs.mkdirSync(path.dirname(this.file), { recursive: true, mode: 0o700 });
    const data = {};
    // 按时间升序写，方便人肉排查「最后几条是什么时候处理的」
    [...this.entries.entries()]
      .sort((a, b) => a[1] - b[1])
      .forEach(([id, ts]) => { data[id] = ts; });
    const tmp = `${this.file}.tmp`;
    fs.writeFileSync(tmp, JSON.stringify(data, null, 2), { encoding: 'utf8', mode: 0o600 });
    fs.renameSync(tmp, this.file);
  }
}

module.exports = { HandledMessages, DEFAULT_TTL_MS, DEFAULT_MAX_ENTRIES };
