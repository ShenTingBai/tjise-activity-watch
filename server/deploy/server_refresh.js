/**
 * 在服务器上续期 token。
 *
 * 原理：站点把登录态放在 localStorage 和 cookie 里，页面加载时可能会续期。
 * 一次性模式打开页面后退出；常驻模式（--daemon）保持一个页面长期运行，
 * 给 SPA 自己的刷新计时器持续工作的机会，并定期保存完整 storageState。
 * 兼容旧版只有 localStorage 的快照；首次成功续期后自动补齐 cookie。
 *
 * 输出：更新 /root/tjise-activity-watch/data/session.json
 * 退出码：0 正常；2 拿不到 token；3 token 拿到但接口不认；4 会话文件写入失败
 */
const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');
const { atomicWriteJson } = require('./atomic_json');

const APP = '/root/tjise-activity-watch';
const STATE_FILE = path.join(__dirname, 'storage_state.json');
const SESSION_FILE = path.join(APP, 'data', 'session.json');
const DAEMON = process.argv.includes('--daemon');
const LOOP_INTERVAL_MS = 60 * 1000;
const VALIDATE_INTERVAL_MS = 2 * 60 * 60 * 1000;
const NO_TOKEN_LOG_INTERVAL_MS = 10 * 60 * 1000;

// 学校站点 origin（含 scheme）。换学校时改这里，或在服务器环境里设 SCHOOL_ORIGIN。
// 默认值保持当前学校；/dr/api 这类端点路径是站点路由，不随学校变化。
const SCHOOL_ORIGIN = (process.env.SCHOOL_ORIGIN || 'https://xg.tjise.edu.cn')
  .replace(/\/+$/, '');
const PAGE_URL = `${SCHOOL_ORIGIN}/stu/`;
const API = `${SCHOOL_ORIGIN}/dr/api/rjyHd/listActivity`;
const REFERER = `${SCHOOL_ORIGIN}/stu/`;

function clock() {
  return new Date().toLocaleString('sv-SE').replace('T', ' ');
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function stateVersion() {
  try {
    const stat = fs.statSync(STATE_FILE);
    return `${stat.mtimeMs}:${stat.size}`;
  } catch (e) {
    return '';
  }
}

function readSavedState() {
  if (!fs.existsSync(STATE_FILE)) {
    throw new Error('缺少 storage_state.json');
  }
  return JSON.parse(fs.readFileSync(STATE_FILE, 'utf8'));
}

function isFullStorageState(savedState) {
  return savedState &&
    (Array.isArray(savedState.cookies) || Array.isArray(savedState.origins));
}

function browserOptions(savedState) {
  const options = {
    viewport: { width: 1280, height: 860 },
    userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ' +
      '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
  };
  if (isFullStorageState(savedState)) options.storageState = savedState;
  return options;
}

async function readStore(page) {
  return page.evaluate(() => {
    const get = (k) => {
      try { return localStorage.getItem(k) || ''; } catch (e) { return ''; }
    };
    let user = {};
    try { user = JSON.parse(get('user') || '{}'); } catch (e) { user = {}; }
    const store = {};
    try {
      for (const k of Object.keys(localStorage)) store[k] = localStorage.getItem(k);
    } catch (e) { /* 忽略 */ }
    return { token: get('xgUniToken') || get('token'), user, store };
  });
}

async function tokenWorks(token) {
  try {
    const res = await fetch(API, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Accept: 'application/json, text/plain, */*',
        token,
        Referer: REFERER,
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ' +
          '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
      },
      // 字段要和 fetch.py 一致，少字段接口会直接拒绝
      body: JSON.stringify({
        pageSize: 1, pageNum: 1, hdmc: '', hdzt: null, jssj: '', kssj: '',
      }),
    });
    const data = await res.json();
    if (data.code !== 1) {
      console.log('    接口返回:', JSON.stringify(data).slice(0, 120));
    }
    return data.code === 1;
  } catch (e) {
    console.log('    校验异常:', e.message);
    return false;
  }
}

async function openRuntime(browser, savedState) {
  const fullState = isFullStorageState(savedState);
  const ctx = await browser.newContext(browserOptions(savedState));
  const page = await ctx.newPage();

  await page.goto(PAGE_URL, { waitUntil: 'domcontentloaded', timeout: 60000 });
  if (!fullState) {
    await page.evaluate((s) => {
      for (const k in s) localStorage.setItem(k, s[k]);
    }, savedState);
    await page.reload({ waitUntil: 'domcontentloaded' });
  }
  await page.waitForTimeout(6000);
  return { ctx, page };
}

async function closeRuntime(runtime) {
  if (!runtime) return;
  try { await runtime.ctx.close(); } catch (e) { /* 关闭失败不覆盖原错误 */ }
}

async function captureRuntime(runtime) {
  return {
    got: await readStore(runtime.page),
    state: await runtime.ctx.storageState(),
  };
}

async function validateAndPersist(got, refreshedState) {
  if (!(await tokenWorks(got.token))) {
    console.log('[x] token 拿到了但接口不认');
    return 3;
  }

  let oldToken = '';
  try {
    oldToken = JSON.parse(fs.readFileSync(SESSION_FILE, 'utf8')).token || '';
  } catch (e) { /* 首次 */ }

  const session = {
    token: got.token,
    saved_at: clock(),
    name: (got.user && got.user.xm) || '',
    student_no: (got.user && got.user.xh) || '',
    gender: (got.user && got.user.xb) || '',
  };

  // 两个文件都放到接口校验通过之后再写，而且用原子写：
  // 以前是先写 state 再验证 token，token 无效时也会把 localStorage 覆盖掉，
  // 留给下一轮的「上一份可用状态」就没了；writeFileSync 半途失败还会留下坏 JSON。
  try {
    // 页面可能刷新了 localStorage，也可能下发续期所需的 cookie；两者一起回写。
    atomicWriteJson(STATE_FILE, refreshedState);
    atomicWriteJson(SESSION_FILE, session);
  } catch (e) {
    // 写失败不能当成功：cron 会以为已经续期，实际下一轮还在用旧 token
    console.log('[x] 会话文件写入失败：' +
      (e && e.code ? e.code + ' ' : '') + (e && e.message ? e.message : e));
    return 4;
  }

  const changed = oldToken !== session.token;
  // 日志只写状态和时间：token 片段和姓名一旦进 cron.log，
  // 就等于把凭据和个人信息复制到了另一个长期保存的文件里。
  console.log(
    '[' + (changed ? '✓ 已更新' : '· 无变化') + '] ' + session.saved_at
  );
  return 0;
}

async function runOnce() {
  let browser = null;
  let runtime = null;
  try {
    const savedState = readSavedState();
    browser = await chromium.launch({ args: ['--no-sandbox'] });
    runtime = await openRuntime(browser, savedState);
    const captured = await captureRuntime(runtime);
    await closeRuntime(runtime);
    runtime = null;
    await browser.close();
    browser = null;

    if (!captured.got.token) {
      console.log('[x] 页面没有给出 token');
      return 2;
    }
    return await validateAndPersist(captured.got, captured.state);
  } catch (e) {
    console.log('[x] 续期异常：' + (e && e.message ? e.message : e));
    return 3;
  } finally {
    await closeRuntime(runtime);
    if (browser) {
      try { await browser.close(); } catch (e) { /* 退出路径 */ }
    }
  }
}

async function runDaemon() {
  let browser = null;
  let runtime = null;
  let loadedVersion = '';
  let lastToken = '';
  let lastValidatedAt = 0;
  let lastStateDigest = '';
  let lastNoTokenLog = 0;
  let stopping = false;

  const stop = () => { stopping = true; };
  process.once('SIGTERM', stop);
  process.once('SIGINT', stop);

  console.log('[i] 常驻续期模式已启动');
  while (!stopping) {
    try {
      const currentVersion = stateVersion();
      if (!currentVersion) {
        if (Date.now() - lastNoTokenLog >= NO_TOKEN_LOG_INTERVAL_MS) {
          console.log('[x] 缺少 storage_state.json，等待登录态更新');
          lastNoTokenLog = Date.now();
        }
        await sleep(LOOP_INTERVAL_MS);
        continue;
      }

      if (!runtime || currentVersion !== loadedVersion) {
        await closeRuntime(runtime);
        runtime = null;
        if (!browser) browser = await chromium.launch({ args: ['--no-sandbox'] });
        runtime = await openRuntime(browser, readSavedState());
        loadedVersion = stateVersion();
        lastToken = '';
        lastValidatedAt = 0;
        lastStateDigest = '';
        console.log('[i] 已加载登录态，常驻页面继续运行');
      }

      const captured = await captureRuntime(runtime);
      if (!captured.got.token) {
        if (Date.now() - lastNoTokenLog >= NO_TOKEN_LOG_INTERVAL_MS) {
          console.log('[x] 页面没有给出 token，等待重新登录');
          lastNoTokenLog = Date.now();
        }
      } else {
        const digest = JSON.stringify(captured.state);
        const tokenChanged = captured.got.token !== lastToken;
        const validationDue = Date.now() - lastValidatedAt >= VALIDATE_INTERVAL_MS;

        if (tokenChanged || validationDue) {
          const result = await validateAndPersist(captured.got, captured.state);
          lastToken = captured.got.token;
          lastValidatedAt = Date.now();
          if (result === 0) {
            lastStateDigest = digest;
            loadedVersion = stateVersion();
          }
        } else if (digest !== lastStateDigest) {
          // token 仍使用最近一次已验证的值，只更新页面新产生的 cookie/localStorage。
          atomicWriteJson(STATE_FILE, captured.state);
          lastStateDigest = digest;
          loadedVersion = stateVersion();
        }
      }
    } catch (e) {
      console.log('[x] 常驻续期异常：' + (e && e.message ? e.message : e));
      await closeRuntime(runtime);
      runtime = null;
      loadedVersion = '';
    }
    await sleep(LOOP_INTERVAL_MS);
  }

  await closeRuntime(runtime);
  if (browser) {
    try { await browser.close(); } catch (e) { /* 退出路径 */ }
  }
  console.log('[i] 常驻续期模式已停止');
}

(async () => {
  if (DAEMON) {
    await runDaemon();
    return;
  }
  process.exitCode = await runOnce();
})().catch((e) => {
  console.log('[x] 续期进程异常：' + (e && e.message ? e.message : e));
  process.exitCode = 3;
});
