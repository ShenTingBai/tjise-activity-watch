/**
 * 持久化去重表的离线测试。
 *
 * 只验证 handled_messages.js 的行为：跨进程重启、过期回收、容量上限、
 * 失败可重试、损坏文件，以及临时目录的生命周期。
 * 不加载 bot.js、不读 config.json / data/、不调用任何子进程或网络。
 *
 * 用法：node bot/test_handled_messages.js
 *
 * @author LiXinwen
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { HandledMessages } = require('./handled_messages');

// 与其他离线测试共用同一个临时根，便于统一清理
const PREFERRED_ROOT = path.join('D:', '@Temp', 'tjise-runtime-hardening-20261020');

function testRoot() {
  const preferred = process.env.TJISE_TEST_ROOT
    || (process.platform === 'win32' ? PREFERRED_ROOT : os.tmpdir());
  try {
    fs.mkdirSync(preferred, { recursive: true });
    return preferred;
  } catch (e) {
    return os.tmpdir();
  }
}

/**
 * 建一个独立临时目录跑用例，用例（含断言失败）结束后才删除。
 *
 * 这里必须 await test()：以前 finally 在 await 之前执行，异步用例其实是在
 * 已经删掉的目录上继续读写，失败一次就再也复现不出来。
 */
async function withStore(test) {
  const root = fs.mkdtempSync(path.join(testRoot(), 'handled-'));
  try {
    await test(path.join(root, 'handled.json'), root);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
}

async function testSuccessfulMessagePersistsAcrossRestarts() {
  await withStore(async (file) => {
    const first = new HandledMessages(file);
    let calls = 0;
    assert.equal(await first.processOnce('om_1', async () => {
      calls += 1;
      return true;
    }, 1000), true);

    // 模拟进程重启：新实例从磁盘读回同一个 ID，不能再执行一次
    const restarted = new HandledMessages(file);
    assert.equal(await restarted.processOnce('om_1', async () => {
      calls += 1;
      return true;
    }, 1001), false);
    assert.equal(calls, 1);
  });
}

async function testFailedHandlerCanRetry() {
  await withStore(async (file) => {
    const messages = new HandledMessages(file);
    let calls = 0;
    // 返回 false = 没处理成功：不能落盘，否则这条消息永远失去重试机会
    assert.equal(await messages.processOnce('om_retry', async () => {
      calls += 1;
      return false;
    }, 1000), false);
    assert.equal(await messages.processOnce('om_retry', async () => {
      calls += 1;
      return true;
    }, 1001), true);
    assert.equal(calls, 2);

    const restarted = new HandledMessages(file);
    assert.equal(restarted.has('om_retry', 1001), true);
  });
}

async function testTruthyResultIsNotSuccess() {
  await withStore(async (file) => {
    const messages = new HandledMessages(file);
    // 只有明确的 true 才算成功；"ok"、1 都不行，
    // 否则 runQuery 返回值一改形状就会悄悄把失败记成已回答。
    assert.equal(await messages.processOnce('om_truthy', async () => 'ok', 1000), false);
    assert.equal(await messages.processOnce('om_truthy', async () => 1, 1000), false);
    assert.equal(await messages.processOnce('om_truthy', async () => true, 1000), true);
  });
}

async function testHandlerErrorCanRetry() {
  await withStore(async (file) => {
    const messages = new HandledMessages(file);
    await assert.rejects(
      messages.processOnce('om_throw', async () => { throw new Error('boom'); }, 1000),
      /boom/
    );
    // 抛错后 in-flight 标记必须清掉，否则飞书重投永远被当成「正在处理」
    assert.equal(await messages.processOnce('om_throw', async () => true, 1000), true);
  });
}

async function testConcurrentDuplicateRunsOnce() {
  await withStore(async (file) => {
    const messages = new HandledMessages(file);
    let calls = 0;
    let release;
    const gate = new Promise((done) => { release = done; });
    const first = messages.processOnce('om_parallel', async () => {
      calls += 1;
      await gate;
      return true;
    }, 1000);
    const duplicate = await messages.processOnce('om_parallel', async () => {
      calls += 1;
      return true;
    }, 1000);
    release();
    assert.equal(await first, true);
    assert.equal(duplicate, false);
    assert.equal(calls, 1);
  });
}

function testExpiryAndCapacity() {
  return withStore(async (file) => {
    const messages = new HandledMessages(file, { ttlMs: 100, maxEntries: 2 });
    messages.mark('old', 1000);
    messages.mark('middle', 1050);
    messages.mark('new', 1100);

    // 条数上限优先：old 被挤掉，剩下 middle / new
    const restarted = new HandledMessages(file, { ttlMs: 100, maxEntries: 2 });
    assert.equal(restarted.has('old', 1100), false);
    assert.equal(restarted.has('middle', 1100), true);
    assert.equal(restarted.has('new', 1100), true);

    const expired = new HandledMessages(file, { ttlMs: 100, maxEntries: 2 });
    assert.equal(expired.has('middle', 1201), false);
    assert.equal(expired.has('new', 1201), false);
  });
}

function testCorruptStoreFailsClosed() {
  return withStore(async (file) => {
    fs.writeFileSync(file, '{invalid', 'utf8');
    assert.throws(() => new HandledMessages(file), /cannot parse handled message cache/);
  });
}

async function testStoreDirectoryLifecycle() {
  let rootSeen = '';
  await withStore(async (file, root) => {
    rootSeen = root;
    assert.equal(fs.existsSync(root), true, '用例内部临时目录应当存在');
    const store = new HandledMessages(file);
    store.mark('om_lifecycle', 1000);
    assert.equal(fs.existsSync(file), true, '标记后缓存文件应当已落盘');
  });
  assert.equal(fs.existsSync(rootSeen), false, '用例结束后临时目录必须删除');
}

(async () => {
  const cases = [
    testSuccessfulMessagePersistsAcrossRestarts,
    testFailedHandlerCanRetry,
    testTruthyResultIsNotSuccess,
    testHandlerErrorCanRetry,
    testConcurrentDuplicateRunsOnce,
    testExpiryAndCapacity,
    testCorruptStoreFailsClosed,
    testStoreDirectoryLifecycle,
  ];
  for (const run of cases) {
    await run();
  }
  console.log('handled message tests passed: ' + cases.length);
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
