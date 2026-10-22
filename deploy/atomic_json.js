/**
 * JSON 文件的原子写。
 *
 * 为什么需要单独一个模块：server_refresh.js 会把续期后的 localStorage 和
 * session.json 写回磁盘，而 cron 每 5 分钟就会去读这两个文件。
 * 直接用 fs.writeFileSync 写，进程被杀或磁盘写满时就会留下半截 JSON：
 * 下一轮读到解析失败，token 续期链路直接断掉，而且文件已经被破坏、没法回滚。
 *
 * 做法：写同目录下的唯一临时文件（wx 独占、0600），write + fsync + close 之后再
 * rename 覆盖目标。rename 是文件系统层面的原子替换，读者要么看到旧文件，
 * 要么看到完整的新文件。
 *
 * 边界：
 * - 临时文件和目标文件必须同目录，跨设备 rename 会失败（EXDEV）；
 * - 失败时只清理「本次」的临时文件，不动目录里别的东西；
 * - 只给「新建的」目录 0700，已存在目录的权限保持原样，不擅自收紧。
 *
 * @author LiXinwen
 */
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

/** 建父目录。recursive mkdir 不会改已存在目录的权限，这里只影响新建的那几层。 */
function ensureDir(dir) {
  fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
}

/**
 * 原子写入一个 JSON 文件。
 *
 * @param {string} file 目标路径
 * @param {*} value 会被 JSON.stringify 的值
 */
function atomicWriteJson(file, value) {
  const target = path.resolve(file);
  const dir = path.dirname(target);
  ensureDir(dir);

  // 唯一临时文件名：同一目录里两个进程同时写也不能撞名
  const tmp = path.join(
    dir,
    '.' + path.basename(target) + '.' + process.pid + '.' +
    crypto.randomBytes(6).toString('hex') + '.tmp'
  );

  let fd = null;
  let committed = false;
  try {
    // 先序列化再开文件：值不可序列化时不会留下空临时文件
    const data = JSON.stringify(value, null, 2) + '\n';
    fd = fs.openSync(tmp, 'wx', 0o600);
    fs.writeFileSync(fd, data, { encoding: 'utf8' });
    // 先落盘再 rename：否则断电后可能 rename 成功、内容还在页缓存里
    fs.fsyncSync(fd);
    fs.closeSync(fd);
    fd = null;
    fs.renameSync(tmp, target);
    committed = true;
  } finally {
    if (fd !== null) {
      try { fs.closeSync(fd); } catch (e) { /* 已在异常路径上，不覆盖原始错误 */ }
    }
    if (!committed) {
      // 只删本次的临时文件：目录里可能还有别的进程正在写的临时文件
      try { fs.unlinkSync(tmp); } catch (e) { /* 没创建成功就没得删 */ }
    }
  }
}

module.exports = { atomicWriteJson };
