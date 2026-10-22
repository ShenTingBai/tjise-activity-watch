/**
 * deploy/atomic_json.js 的离线测试。
 *
 * 只验证原子写本身：新建、覆盖、序列化失败、rename 失败、临时文件清理、
 * 新建目录/文件的权限。不加载 server_refresh.js（它 require playwright，
 * 本机没有这个依赖），接线是否正确由 isolated-verification 里的静态核对保证。
 *
 * 用法：node deploy/test_atomic_json.js
 *
 * @author LiXinwen
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { atomicWriteJson } = require('./atomic_json');

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

const TEST_ROOT = testRoot();

function tmpDir(label) {
  return fs.mkdtempSync(path.join(TEST_ROOT, 'atomic-' + label + '-'));
}

/** 目录里是否还有本次没清掉的临时文件 */
function leftoverTemps(dir) {
  return fs.readdirSync(dir).filter((name) => name.endsWith('.tmp'));
}

function readJson(file) {
  return JSON.parse(fs.readFileSync(file, 'utf8'));
}

function removeDir(dir) {
  fs.rmSync(dir, { recursive: true, force: true });
}

const cases = [];
function test(name, fn) {
  cases.push({ name, fn });
}

test('写入新文件后内容可解析且不留临时文件', async () => {
  const dir = tmpDir('create');
  try {
    const file = path.join(dir, 'session.json');
    atomicWriteJson(file, { token: 'fixture', n: 1 });
    assert.deepEqual(readJson(file), { token: 'fixture', n: 1 });
    assert.deepEqual(leftoverTemps(dir), []);
    assert.ok(fs.readFileSync(file, 'utf8').endsWith('\n'), 'JSON 文件应以换行结尾');
  } finally {
    removeDir(dir);
  }
});

test('覆盖已有文件后旧内容完全被替换', async () => {
  const dir = tmpDir('overwrite');
  try {
    const file = path.join(dir, 'session.json');
    atomicWriteJson(file, { token: 'old', keep: [1, 2, 3] });
    atomicWriteJson(file, { token: 'new' });
    assert.deepEqual(readJson(file), { token: 'new' });
    assert.deepEqual(leftoverTemps(dir), []);
  } finally {
    removeDir(dir);
  }
});

test('rename 失败时旧文件保持完整且临时文件被清理', async () => {
  const dir = tmpDir('renamefail');
  try {
    const file = path.join(dir, 'session.json');
    atomicWriteJson(file, { token: 'old' });
    const before = fs.readFileSync(file, 'utf8');

    const realRename = fs.renameSync;
    fs.renameSync = () => {
      const error = new Error('EACCES: rename blocked for fixture');
      error.code = 'EACCES';
      throw error;
    };
    try {
      assert.throws(() => atomicWriteJson(file, { token: 'new' }), /rename blocked/);
    } finally {
      fs.renameSync = realRename;
    }

    assert.equal(fs.readFileSync(file, 'utf8'), before, '失败后旧文件必须一字不改');
    assert.deepEqual(leftoverTemps(dir), [], '失败路径不能留下临时文件');
  } finally {
    removeDir(dir);
  }
});

test('值无法序列化时不写文件也不留临时文件', async () => {
  const dir = tmpDir('circular');
  try {
    const file = path.join(dir, 'session.json');
    atomicWriteJson(file, { token: 'old' });
    const before = fs.readFileSync(file, 'utf8');
    const circular = {};
    circular.self = circular;
    assert.throws(() => atomicWriteJson(file, circular));
    assert.equal(fs.readFileSync(file, 'utf8'), before);
    assert.deepEqual(leftoverTemps(dir), []);
  } finally {
    removeDir(dir);
  }
});

test('父目录不存在时自动创建', async () => {
  const dir = tmpDir('nested');
  try {
    const file = path.join(dir, 'deep', 'nested', 'session.json');
    atomicWriteJson(file, { ok: true });
    assert.deepEqual(readJson(file), { ok: true });
  } finally {
    removeDir(dir);
  }
});

test('Linux 下新文件 0600、新目录 0700', async () => {
  if (process.platform === 'win32') {
    // Windows 不实现 POSIX 权限位，chmod 基本是空操作，这里只记录平台限制
    console.log('    （Windows 跳过权限位断言，POSIX 权限在服务器上由 Linux 保证）');
    return;
  }
  const dir = tmpDir('mode');
  try {
    const file = path.join(dir, 'data', 'session.json');
    atomicWriteJson(file, { ok: true });
    assert.equal(fs.statSync(file).mode & 0o777, 0o600);
    assert.equal(fs.statSync(path.dirname(file)).mode & 0o777, 0o700);
  } finally {
    removeDir(dir);
  }
});

(async () => {
  let failed = 0;
  for (const item of cases) {
    try {
      await item.fn();
      console.log('  ✓ ' + item.name);
    } catch (error) {
      failed += 1;
      console.log('  ✗ ' + item.name);
      console.log('    ' + (error && error.message ? error.message : error));
    }
  }
  console.log(`\n共 ${cases.length} 项，失败 ${failed} 项`);
  process.exitCode = failed === 0 ? 0 : 1;
})();
