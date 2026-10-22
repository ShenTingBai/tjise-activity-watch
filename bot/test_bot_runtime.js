/**
 * bot.js 的离线集成测试。
 *
 * 做法：把 bot.js 源码读进 vm 沙箱执行，只放行真实的 fs/path、本地
 * handled_messages 模块，child_process 和 Lark SDK 全部换成假实现。
 * 这样能走到实际的 dispatcher 回调，又不会连网、不会读写真实配置或用户表。
 *
 * 关键边界：
 * - __dirname 指向任务临时目录，bot.js 的 data/users.json 与去重缓存都落在里面；
 * - 假 execFile 记录每次调用的 view/format，假 message.create 记录回复内容；
 * - 假 WSClient.start 只计数，不建立任何连接；
 * - 启动是异步 IIFE，必须等它跑完再断言，不能用 setImmediate 猜。
 *
 * @author LiXinwen
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const vm = require('node:vm');

const BOT_JS = path.join(__dirname, 'bot.js');
const BOT_SOURCE = fs.readFileSync(BOT_JS, 'utf8');
const PREFERRED_ROOT = 'D:/@Temp/tjise-runtime-hardening-20261020';

const CARD_FIXTURE = {
  config: { wide_screen_mode: true },
  header: { template: 'blue', title: { tag: 'plain_text', content: '待开放报名 1 项' } },
  elements: [{ tag: 'div', text: { tag: 'lark_md', content: 'fixture' } }],
};
const TEXT_FIXTURE = '▸ 10-21 19:00 开放\n　测试活动\n　其他';

const OWNER = 'ou_owner_fixture';
const BOT = 'ou_bot_fixture';

/** 临时根：优先用计划指定的 D:/@Temp 目录，非 Windows 或不可写时退回系统临时目录。 */
function resolveRoot() {
  if (process.env.TJISE_TEST_ROOT) {
    fs.mkdirSync(process.env.TJISE_TEST_ROOT, { recursive: true });
    return process.env.TJISE_TEST_ROOT;
  }
  const preferred = process.platform === 'win32'
    ? PREFERRED_ROOT
    : path.join(os.tmpdir(), 'tjise-runtime-hardening');
  try {
    fs.mkdirSync(preferred, { recursive: true });
    return preferred;
  } catch (e) {
    return os.tmpdir();
  }
}

const TEST_ROOT = resolveRoot();

function makeDir(label) {
  return fs.mkdtempSync(path.join(TEST_ROOT, `bot-${label}-`));
}

/** 一个 bot.js 沙箱实例。options 可覆盖 execFile 与 message.create 的行为。 */
function createHarness(options = {}) {
  // options.dir 用于模拟「同一个部署目录里的下一个进程」
  const dir = options.dir || makeDir(options.label || 'case');
  const state = {
    dir,
    logs: [],
    replies: [],
    cards: [],
    execCalls: [],
    exits: [],
    startCalls: 0,
    handlers: null,
    clientRequests: 0,
    execMode: options.execMode || 'ok',
    sendMode: options.sendMode || 'ok',
    botInfoFails: Boolean(options.botInfoFails),
    startThrows: Boolean(options.startThrows),
  };

  const fakeProcess = {
    env: Object.assign({
      FEISHU_APP_ID: 'cli_fixture',
      FEISHU_APP_SECRET: 'secret_fixture',
      BOT_OPEN_ID: BOT,
      PRIVATE_OWNER_OPEN_ID: OWNER,
      APP_DIR: dir,
      PY_BIN: 'python3',
    }, options.env || {}),
    argv: options.argv || ['node', 'bot.js'],
    exitCode: undefined,
    exit(code) {
      state.exits.push(code);
    },
  };

  function execFile(bin, args, opts, callback) {
    state.execCalls.push({ bin, args: args.slice(), opts });
    const isCard = args.includes('card');
    const mode = state.execMode;
    const ok = (stdout, stderr) => callback(null, stdout, stderr || '');
    const fail = (message, extra) => {
      const err = new Error(message);
      Object.assign(err, extra || {});
      callback(err, '', '');
    };

    switch (mode) {
      case 'nonzero':
        fail('Command failed: query.py exited 1');
        return;
      case 'timeout':
        fail('Command failed: query.py timed out', { killed: true, signal: 'SIGTERM' });
        return;
      case 'empty':
        ok('', '');
        return;
      case 'stderr':
        ok(isCard ? JSON.stringify(CARD_FIXTURE) : TEXT_FIXTURE, 'deprecation warning');
        return;
      case 'badjson':
        ok('{not-json', '');
        return;
      case 'badcardjson':
        // 卡片这一步返回坏 JSON，文本这一步正常：验证退回文本的路径
        ok(isCard ? '{not-json' : TEXT_FIXTURE, '');
        return;
      case 'throw':
        throw new Error('spawn failed');
      default:
        ok(isCard ? JSON.stringify(CARD_FIXTURE) : TEXT_FIXTURE, '');
    }
  }

  async function createMessage(payload) {
    const mode = state.sendMode;
    if (mode === 'throw') throw new Error('network down');
    if (mode === 'bizfail') return { code: 230001, msg: 'invalid receive_id' };
    if (payload && payload.data && payload.data.msg_type === 'interactive') {
      state.cards.push({ chatId: payload.data.receive_id, card: payload.data.content });
    } else {
      state.replies.push({
        chatId: payload && payload.data ? payload.data.receive_id : '?',
        text: payload && payload.data ? payload.data.content : '',
      });
    }
    return { code: 0, msg: 'ok' };
  }

  const lark = {
    AppType: { SelfBuild: 'SelfBuild' },
    Domain: { Feishu: 'Feishu' },
    LoggerLevel: { info: 'info', debug: 'debug' },
    Client: function FakeClient(cfg) {
      this.cfg = cfg;
      this.request = async (req) => {
        state.clientRequests += 1;
        state.requestCalls = state.requestCalls || [];
        state.requestCalls.push(req);
        if (state.botInfoFails) throw new Error('bot info unavailable');
        return { code: 0, bot: { open_id: BOT } };
      };
      this.im = { message: { create: createMessage } };
    },
    EventDispatcher: function FakeEventDispatcher() {
      this.register = (handlers) => {
        state.handlers = handlers;
        return handlers;
      };
    },
    WSClient: function FakeWSClient(cfg) {
      this.cfg = cfg;
      this.start = async (opts) => {
        state.startCalls += 1;
        state.startOpts = opts;
        if (state.startThrows) throw new Error('ws start failed');
      };
    },
  };

  const realRequire = require;
  function sandboxRequire(id) {
    switch (id) {
      case 'fs':
        return fs;
      case 'path':
        return path;
      case 'child_process':
        return { execFile };
      case '@larksuiteoapi/node-sdk':
        return lark;
      case './handled_messages':
        return realRequire('./handled_messages');
      default:
        // 未知模块直接炸掉：测试里不允许出现意料之外的真实依赖
        throw new Error(`unexpected require in bot.js: ${id}`);
    }
  }

  const sandbox = {
    console: {
      log: (...args) => state.logs.push(args.join(' ')),
      error: (...args) => state.logs.push(args.join(' ')),
    },
    process: fakeProcess,
    require: sandboxRequire,
    __dirname: dir,
    __filename: path.join(dir, 'bot.js'),
    module: { exports: {} },
    exports: {},
    Buffer,
    URL,
    setTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
  };
  vm.createContext(sandbox);
  vm.runInContext(BOT_SOURCE, sandbox, { filename: BOT_JS });

  return state;
}

/** 等启动 IIFE 跑完：start 被调用或 process.exit 被调用即视为稳定。 */
async function waitForStartup(state) {
  for (let i = 0; i < 100; i += 1) {
    if (state.startCalls > 0 || state.exits.length > 0) return state;
    await new Promise((resolve) => setImmediate(resolve));
  }
  throw new Error('bot.js 启动既没有调用 ws.start，也没有退出');
}

function messagePayload(overrides = {}) {
  const message = Object.assign({
    chat_id: 'oc_fixture',
    chat_type: 'p2p',
    message_id: 'om_fixture_1',
    message_type: 'text',
    content: JSON.stringify({ text: '我的' }),
  }, overrides.message || {});
  const sender = Object.assign({
    sender_type: 'user',
    sender_id: { open_id: OWNER },
  }, overrides.sender || {});
  return { sender, message };
}

async function deliver(state, payload) {
  assert.ok(state.handlers, 'bot.js 没有注册 dispatcher');
  const handler = state.handlers['im.message.receive_v1'];
  assert.equal(typeof handler, 'function', 'im.message.receive_v1 未注册');
  return handler(payload);
}

function replyTexts(state) {
  return state.replies.map((r) => JSON.parse(r.text).text);
}

function mineCalls(state) {
  return state.execCalls.filter((c) => c.args[1] === 'mine');
}

function usersFile(state) {
  return path.join(state.dir, 'data', 'users.json');
}

function readUsers(state) {
  const file = usersFile(state);
  if (!fs.existsSync(file)) return {};
  return JSON.parse(fs.readFileSync(file, 'utf8'));
}

function cleanup(state) {
  fs.rmSync(state.dir, { recursive: true, force: true });
}

async function withHarness(options, body) {
  const state = createHarness(options);
  try {
    await waitForStartup(state);
    await body(state);
  } finally {
    cleanup(state);
  }
}

const tests = [];
function test(name, fn) {
  tests.push({ name, fn });
}

test('owner 私聊可以查 mine', async () => {
  await withHarness({}, async (state) => {
    await deliver(state, messagePayload());
    assert.equal(mineCalls(state).length, 1, 'owner 私聊应当执行一次 mine 查询');
    assert.equal(state.cards.length, 1, '应该发出卡片');
    assert.equal(replyTexts(state).length, 0, '卡片成功时不应再发纯文本');
  });
});

test('陌生人私聊被拒绝且不查 mine', async () => {
  await withHarness({}, async (state) => {
    await deliver(state, messagePayload({ sender: { sender_id: { open_id: 'ou_stranger_fixture' } } }));
    assert.equal(mineCalls(state).length, 0, '陌生人不能触发 mine 查询');
    assert.equal(state.execCalls.length, 0, '被拒绝时不应跑任何查询');
    const texts = replyTexts(state);
    assert.equal(texts.length, 1, '应该回复一条拒绝文案');
    assert.match(texts[0], /只对账号主人/, '拒绝文案必须是固定说明');
    assert.doesNotMatch(texts[0], /ou_|oc_|open_id/, '拒绝文案不能泄露任何 ID');
  });
});

test('发送者 open_id 缺失时拒绝', async () => {
  await withHarness({}, async (state) => {
    await deliver(state, messagePayload({ sender: { sender_id: {} } }));
    assert.equal(mineCalls(state).length, 0);
    assert.equal(replyTexts(state).length, 1);
  });
});

test('未配置 owner 时私人查询关闭，公开查询仍可用', async () => {
  await withHarness({ env: { PRIVATE_OWNER_OPEN_ID: '' } }, async (state) => {
    await deliver(state, messagePayload());
    assert.equal(mineCalls(state).length, 0, '没有 owner 配置时必须关闭 mine');
    assert.equal(replyTexts(state).length, 1);

    // 公开查询不受影响
    await deliver(state, messagePayload({
      message: { message_id: 'om_fixture_public', content: JSON.stringify({ text: '报名中' }) },
    }));
    assert.equal(state.execCalls.length, 1, '公开查询应当照常执行一次');
    assert.equal(state.execCalls[0].args[1], 'open', '公开查询应走 open 视图');
    assert.equal(state.cards.length, 1, '卡片发出即结束，不再补发纯文本');
  });
});

test('群聊里的「我的」不查 mine，也不引导私聊', async () => {
  await withHarness({}, async (state) => {
    await deliver(state, messagePayload({
      message: {
        chat_type: 'group',
        mentions: [{ id: { open_id: BOT } }],
      },
    }));
    assert.equal(mineCalls(state).length, 0);
    const texts = replyTexts(state);
    assert.equal(texts.length, 1);
    assert.match(texts[0], /不在群里展示/);
    assert.doesNotMatch(texts[0], /私聊/, '群聊拒绝文案不能引导私聊');
  });
});

test('群里没 @ 机器人时不响应', async () => {
  await withHarness({}, async (state) => {
    await deliver(state, messagePayload({
      message: { chat_type: 'group', mentions: [] },
    }));
    assert.equal(state.execCalls.length, 0, '未 @ 时不查');
    assert.equal(state.replies.length + state.cards.length, 0, '未 @ 时不回');
  });
});

test('非真人消息直接跳过', async () => {
  await withHarness({}, async (state) => {
    await deliver(state, messagePayload({ sender: { sender_type: 'bot' } }));
    assert.equal(state.execCalls.length, 0);
    assert.equal(state.replies.length + state.cards.length, 0);
    assert.ok(state.logs.some((line) => line.includes('skip non-user message')));
  });
});

test('owner 私聊记录不会被后续群消息覆盖', async () => {
  await withHarness({}, async (state) => {
    await deliver(state, messagePayload());
    const before = readUsers(state);
    assert.equal(before[OWNER].chat_type, 'p2p', '私聊后应记录 p2p');
    assert.equal(before[OWNER].open_id, OWNER);

    await deliver(state, messagePayload({
      message: {
        chat_type: 'group',
        message_id: 'om_fixture_group',
        mentions: [{ id: { open_id: BOT } }],
        content: JSON.stringify({ text: '待报名' }),
      },
    }));
    const after = readUsers(state);
    assert.equal(after[OWNER].chat_type, 'p2p', '群消息不能把 p2p 记录覆盖成 group');
    assert.equal(after[OWNER].chat_id, before[OWNER].chat_id);
  });
});

test('启动失败时不留下长连接', async () => {
  await withHarness({ env: { BOT_OPEN_ID: '' }, botInfoFails: true }, async (state) => {
    await waitForStartup(state);
    assert.deepEqual(state.exits, [1], '拿不到自身 open_id 时必须 exit(1)');
    assert.equal(state.startCalls, 0, '身份未知时不能建立长连接');
  });
});

function readStore(state) {
  const file = path.join(state.dir, 'data', 'handled_messages.json');
  if (!fs.existsSync(file)) return {};
  return JSON.parse(fs.readFileSync(file, 'utf8'));
}

const QUERY_FAILED_TEXT = '查询失败，请稍后重新发送';

test('类型查询走纯文本通道', async () => {
  await withHarness({}, async (state) => {
    await deliver(state, messagePayload({
      message: { content: JSON.stringify({ text: '类型' }) },
    }));
    assert.equal(state.execCalls.length, 1);
    assert.equal(state.execCalls[0].args[1], 'types');
    assert.equal(state.cards.length, 0, 'types 视图只发文本');
    assert.deepEqual(replyTexts(state), [TEXT_FIXTURE]);
    assert.equal(typeof readStore(state)['om_fixture_1'], 'number', '文本发成功才落盘');
  });
});

test('查询超时回复固定文案且不落盘，重投可以重试', async () => {
  await withHarness({ execMode: 'timeout' }, async (state) => {
    await deliver(state, messagePayload());
    assert.equal(state.cards.length, 0, '超时不能发卡片');
    assert.deepEqual(replyTexts(state), [QUERY_FAILED_TEXT], '只回固定失败文案');
    assert.deepEqual(readStore(state), {}, '失败不能记为已处理');
    assert.ok(state.logs.some((line) => line.includes('query failed (card)')));

    // 飞书重投时状态已恢复，应当正常回答
    state.execMode = 'ok';
    await deliver(state, messagePayload());
    assert.equal(state.cards.length, 1);
    assert.equal(typeof readStore(state)['om_fixture_1'], 'number');
  });
});

test('非零退出与空输出都算失败', async () => {
  for (const mode of ['nonzero', 'empty']) {
    await withHarness({ execMode: mode }, async (state) => {
      await deliver(state, messagePayload({ message: { message_id: 'om_' + mode } }));
      assert.deepEqual(replyTexts(state), [QUERY_FAILED_TEXT], mode + ' 应当回失败文案');
      assert.equal(state.cards.length, 0);
      assert.deepEqual(readStore(state), {}, mode + ' 不应落盘');
    });
  }
});

test('stderr 只作为诊断，不影响卡片内容', async () => {
  await withHarness({ execMode: 'stderr' }, async (state) => {
    await deliver(state, messagePayload());
    assert.equal(state.cards.length, 1);
    assert.equal(replyTexts(state).length, 0, '卡片成功就不再补发文本');
    assert.ok(state.logs.some((line) => line.includes('query stderr (card):')));
    // 诊断信息不能混进卡片 JSON
    const card = JSON.parse(state.cards[0].card);
    assert.equal(card.header.title.content, '待开放报名 1 项');
  });
});

test('卡片 JSON 解析失败时退回文本查询', async () => {
  await withHarness({ execMode: 'badcardjson' }, async (state) => {
    await deliver(state, messagePayload({
      message: { content: JSON.stringify({ text: '待报名' }) },
    }));
    assert.equal(state.execCalls.length, 2, '卡片失败后应再跑一次文本查询');
    assert.equal(state.cards.length, 0);
    assert.deepEqual(replyTexts(state), [TEXT_FIXTURE]);
    assert.ok(state.logs.some((line) => line.includes('card json invalid')));
  });
});

test('回复失败不落盘，飞书重投时可以重试', async () => {
  await withHarness({ sendMode: 'bizfail' }, async (state) => {
    await deliver(state, messagePayload());
    assert.equal(state.cards.length, 0);
    assert.equal(state.replies.length, 0);
    assert.deepEqual(readStore(state), {}, '回复失败不能记为已处理');
    assert.ok(state.logs.some((line) => line.includes('reply rejected')));

    state.sendMode = 'ok';
    await deliver(state, messagePayload());
    assert.equal(state.cards.length, 1, '状态恢复后重投应当正常回复');
    assert.equal(typeof readStore(state)['om_fixture_1'], 'number');
  });
});

test('回复抛异常时同样不落盘', async () => {
  await withHarness({ sendMode: 'throw' }, async (state) => {
    await deliver(state, messagePayload());
    assert.deepEqual(readStore(state), {});
    assert.ok(state.logs.some((line) => line.includes('reply failed')));
  });
});

test('成功后重启进程仍然去重', async () => {
  const dir = makeDir('restart');
  try {
    const first = createHarness({ dir });
    await waitForStartup(first);
    await deliver(first, messagePayload());
    assert.equal(typeof readStore(first)['om_fixture_1'], 'number');

    // 同一个部署目录里的下一个进程：磁盘上的去重表必须继续生效
    const second = createHarness({ dir });
    await waitForStartup(second);
    await deliver(second, messagePayload());
    assert.equal(second.execCalls.length, 0, '重启后同一 message_id 不应再执行');
    assert.equal(second.replies.length + second.cards.length, 0);
    assert.ok(second.logs.some((line) => line.includes('skip duplicate or in-flight')));
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('并发重投只执行一次', async () => {
  await withHarness({}, async (state) => {
    await Promise.all([deliver(state, messagePayload()), deliver(state, messagePayload())]);
    assert.equal(state.execCalls.length, 1, '同一 message_id 并发到达只能查一次');
    assert.equal(state.cards.length, 1);
  });
});

test('首次启动允许没有缓存文件', async () => {
  await withHarness({}, async (state) => {
    assert.equal(state.startCalls, 1, '没有缓存文件是正常的首次启动');
    assert.equal(fs.existsSync(path.join(state.dir, 'data', 'handled_messages.json')), false);
  });
});

test('缓存损坏时启动即退出，不建立长连接', async () => {
  const dir = makeDir('corrupt');
  try {
    const dataDir = path.join(dir, 'data');
    fs.mkdirSync(dataDir, { recursive: true });
    const file = path.join(dataDir, 'handled_messages.json');
    fs.writeFileSync(file, '{invalid', 'utf8');

    const state = createHarness({ dir });
    await waitForStartup(state);
    assert.deepEqual(state.exits, [1], '坏缓存必须在启动阶段就退出');
    assert.equal(state.startCalls, 0, '不能带着坏缓存建立长连接');
    assert.ok(state.logs.some((line) => line.includes('去重缓存无法加载')));
    assert.equal(fs.readFileSync(file, 'utf8'), '{invalid', '坏缓存不能被静默重建');
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('已有缓存正常读取后照常启动并去重', async () => {
  const dir = makeDir('preloaded');
  try {
    const dataDir = path.join(dir, 'data');
    fs.mkdirSync(dataDir, { recursive: true });
    fs.writeFileSync(
      path.join(dataDir, 'handled_messages.json'),
      JSON.stringify({ om_fixture_1: Date.now() }),
      'utf8'
    );

    const state = createHarness({ dir });
    await waitForStartup(state);
    assert.equal(state.startCalls, 1);
    await deliver(state, messagePayload());
    assert.equal(state.execCalls.length, 0, '磁盘里已有的 ID 不能再查一遍');
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

(async () => {
  let failed = 0;
  for (const item of tests) {
    try {
      await item.fn();
      console.log('  ✓ ' + item.name);
    } catch (error) {
      failed += 1;
      console.log('  ✗ ' + item.name);
      console.log('    ' + (error && error.message ? error.message : error));
    }
  }
  console.log(`\n共 ${tests.length} 项，失败 ${failed} 项`);
  process.exitCode = failed === 0 ? 0 : 1;
})();
