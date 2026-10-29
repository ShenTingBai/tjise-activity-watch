/* 刷新轮次所有权与缓存（去重、迟到轮丢弃）+ 登录存在性判据。 */
function newRoundId(prefix) {
  return prefix + "-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 8);
}

/* 终态暂存：新一轮先发、旧 owner 的终态先到时，不能直接丢——等 skipped ack
 * 接续回原 ID 后还要重放。只存结构化载荷，不含凭证；最多 16 项、130 秒过期。 */
const ROUND_CACHE_LIMIT = 16;
const ROUND_CACHE_TTL_MS = 130000;

function cacheRoundPayload(branch, id, payload) {
  const key = String(id || "");
  if (!key) return;
  /* 暂存期限是经过时长，不是墙钟时刻：系统时间前移/回拨不得让刚到的终态
   * 立刻过期或被无限延长。_monoMs 与时钟锚点同一单调域。 */
  const now = _monoMs();
  S.round.cache = (S.round.cache || []).filter((item) => now - item.at < ROUND_CACHE_TTL_MS
    && !(item.branch === branch && item.id === key));
  S.round.cache.push({ branch: branch, id: key, payload: payload, at: now });
  while (S.round.cache.length > ROUND_CACHE_LIMIT) S.round.cache.shift();
}

function takeRoundPayload(branch, id) {
  const key = String(id || "");
  if (!key) return null;
  const now = _monoMs();
  const index = (S.round.cache || []).findIndex((item) => item.branch === branch
    && item.id === key && now - item.at < ROUND_CACHE_TTL_MS);
  if (index < 0) return null;
  return S.round.cache.splice(index, 1)[0].payload;
}

/* 消费代次：refresh 每发一轮就 +1；skipped 接续回原 owner 时**不**递增——
 * 那一轮要用同一个 owner 的既有结果收口。 */
function bumpRoundGeneration(branch) {
  S.round.gen = S.round.gen || { check: 0, list: 0 };
  S.round.gen[branch] = (S.round.gen[branch] || 0) + 1;
  return S.round.gen[branch];
}

/* 同一代次里，同一请求的终态只生效一次；换一轮刷新就允许重新消费。
 * 键含代次是 FR04 的修复点：跨轮永久 applied 会让新一轮干等 100/130 秒。 */
function markRoundApplied(branch, id) {
  const key = String(id || "");
  if (!key) return true;
  const gen = (S.round.gen && S.round.gen[branch]) || 0;
  const applied = S.round.applied || (S.round.applied = {});
  const slot = branch + ":" + gen + ":" + key;
  if (applied[slot]) return false;
  applied[slot] = _monoMs();
  const keys = Object.keys(applied);
  if (keys.length > 48) {
    keys.sort((a, b) => applied[a] - applied[b])
      .slice(0, keys.length - 48)
      .forEach((old) => { delete applied[old]; });
  }
  return true;
}

/* 一次性副作用：同一请求的同一类提示只写一次，换轮重放不再刷屏 */
function firstNotice(branch, id, tag) {
  const key = String(id || "");
  if (!key) return true;
  const notified = S.round.notified || (S.round.notified = {});
  const slot = branch + ":" + key + ":" + String(tag || "");
  if (notified[slot]) return false;
  notified[slot] = _monoMs();
  const keys = Object.keys(notified);
  if (keys.length > 48) {
    keys.sort((a, b) => notified[a] - notified[b])
      .slice(0, keys.length - 48)
      .forEach((old) => { delete notified[old]; });
  }
  return true;
}

/* 登录快照里的 session_present → 三态布尔：
 * 缺字段或 null 都表示「不知道」，不能 !! 成 false（那会走「首次未登录」，
 * 把演示模式/尚未核对的状态误判成没登录）。demo 一律视为未知。 */
function loginPresent(login, isDemo) {
  if (isDemo) return undefined;
  if (!login || !("session_present" in login)) return undefined;
  if (login.session_present === null) return undefined;
  return !!login.session_present;
}
