/**
 * 飞书机器人：一个进程只扮演一种角色，角色由 BOT_ROLE 显式声明。
 *
 *   BOT_ROLE=group_query   —— 群里被 @ 时只读查询公开清单，任何个人动作一律拒绝
 *   BOT_ROLE=private_owner —— 只服务 PRIVATE_OWNER_OPEN_ID 本人的私聊，才允许预报名/报名动作
 *
 * 为什么必须显式声明：以前一个进程同时拥有「群查询」和「owner 私聊」两条权限，
 * 靠一条 chat_type 判断分流。任何一次判断写漏（字段改名、事件结构变化、新入口）
 * 都会把个人权限漏给公共群。现在缺 BOT_ROLE 直接拒绝启动，
 * 「少一个环境变量就少一个权限」，而不是「少一个环境变量就多一份权限」。
 *
 * 走「长连接」模式，不需要公网域名和证书。
 * 依赖：Node >= 16，@larksuiteoapi/node-sdk
 */
const { execFile } = require('child_process');
const fs = require('fs');
const path = require('path');
const lark = require('@larksuiteoapi/node-sdk');
const { HandledMessages } = require('./handled_messages');

const APP_ID = process.env.FEISHU_APP_ID;
const APP_SECRET = process.env.FEISHU_APP_SECRET;
const APP_DIR = process.env.APP_DIR || '/root/tjise-activity-watch';
const PY = process.env.PY_BIN || '/usr/bin/python3';
const USERS_FILE = path.join(__dirname, 'data', 'users.json');

// 两个入口角色。名字会写进文档与 systemd 单元，改动要同步 docs/FEISHU_BOT.md。
const ROLE_GROUP = 'group_query';
const ROLE_OWNER = 'private_owner';
const ROLES = [ROLE_GROUP, ROLE_OWNER];
const BOT_ROLE = String(process.env.BOT_ROLE || '').trim();
const ROLE_OK = ROLES.includes(BOT_ROLE);

if (!APP_ID || !APP_SECRET) {
  console.error('缺少 FEISHU_APP_ID / FEISHU_APP_SECRET');
  process.exit(1);
}

if (!ROLE_OK) {
  // fail-closed：不猜、不默认、不「先跑起来再说」。
  // 一个拥有报名权限的机器人因为少写一行环境变量而静默上线，是最坏的一种事故。
  console.error('BOT_ROLE 缺失或非法：当前 ' + JSON.stringify(BOT_ROLE)
    + '，必须是 ' + ROLES.join(' | ') + '；拒绝启动');
  process.exit(1);
}

const client = new lark.Client({
  appId: APP_ID,
  appSecret: APP_SECRET,
  appType: lark.AppType.SelfBuild,
  domain: lark.Domain.Feishu,
});

// 自己的 open_id，用来判断群里那条消息是不是 @ 了我
let BOT_OPEN_ID = process.env.BOT_OPEN_ID || '';

async function loadBotOpenId() {
  if (BOT_OPEN_ID) return true;
  try {
    const res = await client.request({ url: '/open-apis/bot/v3/info', method: 'GET' });
    BOT_OPEN_ID = (res && res.bot && res.bot.open_id) || '';
  } catch (e) {
    log('获取 bot open_id 失败:', e && e.message ? e.message : e);
    BOT_OPEN_ID = '';
  }
  if (!BOT_OPEN_ID) {
    // 拿不到身份就不要起来。起来之后群里 @ 它永远没反应，
    // 而进程看起来是健康的 —— 这种"假在线"比直接挂掉难发现得多。
    log('[bot] bot open_id 获取失败，拒绝启动');
    return false;
  }
  log('bot open_id:', BOT_OPEN_ID);
  return true;
}

// 已处理消息 ID 落在 bot/data 下：进程重启后飞书重投的旧消息不会再答一遍。
// 只存 message_id 和时间戳，不存正文和用户 ID。
const HANDLED_FILE = path.join(__dirname, 'data', 'handled_messages.json');
let handledStore = null;

/** 惰性加载去重表。文件解析失败时抛错，交给调用方记日志；
 *  下一次事件会重新尝试，人工修好文件后自动恢复。
 *  不静默重建：那等于把 48 小时的去重记录整片丢掉，
 *  重启后群里会突然重复回复，而且日志里看不出是缓存坏了。 */
function handledMessages() {
  if (!handledStore) handledStore = new HandledMessages(HANDLED_FILE);
  return handledStore;
}

function log(...args) {
  console.log(new Date().toISOString(), ...args);
}

/** 记住给机器人发过消息的人，之后可以主动私信他。
 *  只记私聊：群消息会把同一个人的记录覆盖成 group，
 *  notify._p2p_chat_id 只认 p2p 记录，覆盖掉就再也找不到私人提醒的收件人。 */
function rememberUser(openId, chatType, chatId) {
  if (!openId) return;
  if (chatType !== 'p2p') return;
  try {
    fs.mkdirSync(path.dirname(USERS_FILE), { recursive: true });
    let db = {};
    try {
      db = JSON.parse(fs.readFileSync(USERS_FILE, 'utf8'));
    } catch (e) {
      db = {};
    }
    db[openId] = {
      open_id: openId,
      chat_type: chatType,
      chat_id: chatId,
      seen_at: new Date().toISOString(),
    };
    fs.writeFileSync(USERS_FILE, JSON.stringify(db, null, 2));
  } catch (e) {
    log('remember user failed:', e.message);
  }
}

/** 跑一次子进程查询。返回 {ok, stdout, diagnostic}。
 *
 *  失败必须显式带出来：以前把 stderr 拼进 stdout，调用方分不清
 *  「查询失败」和「查到了内容」，还会拿一段错误文本去 JSON.parse 当卡片。
 *  stderr 只留在 diagnostic 里做日志诊断，绝不拼进卡片 JSON。
 */
function runQuery(view, type, format) {
  return new Promise((resolve) => {
    const args = ['scripts/query.py', view];
    if (type) args.push('--type', type);
    if (format) args.push('--format', format);

    let settled = false;
    const fail = (reason, extra) => {
      if (settled) return;
      settled = true;
      resolve({
        ok: false,
        stdout: '',
        diagnostic: [reason, String(extra || '').slice(0, 300)].filter(Boolean).join(' | '),
      });
    };

    try {
      execFile(
        PY,
        args,
        { cwd: APP_DIR, timeout: 120000, maxBuffer: 8 * 1024 * 1024 },
        (err, stdout, stderr) => {
          const out = String(stdout || '').trim();
          const warn = String(stderr || '').trim();
          if (err) {
            // 超时被杀和普通非零退出要分开：排查时一个看超时，一个看脚本报错
            const timedOut = !!(err.killed && err.signal);
            fail(timedOut ? '查询超时（120s 未返回）'
              : '查询子进程失败：' + (err.message || String(err)), warn);
            return;
          }
          // 退出码 0 但什么都没输出，同样不能用：卡片会是空 JSON
          if (!out) {
            fail('查询没有返回内容', warn);
            return;
          }
          if (settled) return;
          settled = true;
          resolve({
            ok: true,
            stdout: out.slice(0, 200000),
            diagnostic: warn ? 'stderr: ' + warn.slice(0, 200) : '',
          });
        }
      );
    } catch (e) {
      // execFile 同步抛错（比如 PY 路径不存在）不会走回调
      fail('查询启动失败：' + (e && e.message ? e.message : e));
    }
  });
}

/** 跑一次 watchlist.py（预报名清单的增删查）。 */
function runWatchlist(args) {
  return new Promise((resolve) => {
    execFile(
      PY,
      ['scripts/watchlist.py'].concat(args),
      { cwd: APP_DIR, timeout: 30000, maxBuffer: 1024 * 1024 },
      (err, stdout, stderr) => {
        resolve({
          ok: !err,
          out: String(stdout || '').trim(),
          err: String(stderr || '').trim() || (err ? String(err.message || err) : ''),
        });
      }
    );
  });
}

/** 按当前清单状态重建预报名卡片。
 *  点击按钮后必须把新卡片放进回调响应里：飞书客户端当场替换。
 *  只靠 PATCH 再改一次的话，两次更新会抢同一张卡片，表现就是
 *  「刚变成已预报名，一眨眼又变回预报名」。 */
function runGrabCard() {
  return new Promise((resolve) => {
    execFile(
      PY,
      ['scripts/grab_card.py', 'card'],
      { cwd: APP_DIR, timeout: 30000, maxBuffer: 1024 * 1024 },
      (err, stdout) => {
        if (err) return resolve(null);
        try {
          resolve(JSON.parse(String(stdout || '').trim()));
        } catch (e) {
          resolve(null);
        }
      }
    );
  });
}

/** owner 的私聊 chat_id，只从 users.json 里 owner 那条 p2p 记录取。
 *  取不到就返回空：预报名按钮宁可点不动，也不能把操作权给到别人。 */
function ownerP2pChatId() {
  const owner = String(process.env.PRIVATE_OWNER_OPEN_ID || '').trim();
  if (!owner) return '';
  try {
    const db = JSON.parse(fs.readFileSync(USERS_FILE, 'utf8'));
    const rec = db[owner];
    if (rec && rec.chat_type === 'p2p' && rec.chat_id) return String(rec.chat_id);
  } catch (e) {
    log('read users.json failed:', e.message);
  }
  return '';
}

/** 解出 card.py 塞进按钮的 base64url JSON */
function decodeBtnValue(value) {
  const b64 = value.replace(/-/g, '+').replace(/_/g, '/');
  const pad = b64 + '='.repeat((4 - (b64.length % 4)) % 4);
  return JSON.parse(Buffer.from(pad, 'base64').toString('utf8'));
}

const GRAB_DENIED = '这个按钮只对私聊里的本人有效';
const GRAB_GROUP_DENIED = '群里只提供查询；报名动作只对本人私聊开放';

/** 「预报名 / 取消预报名」按钮的落点。
 *
 *  三重校验缺一不可：点击人就是 PRIVATE_OWNER_OPEN_ID、会话是 owner 的私聊、
 *  卡片确实是在那个会话里点的。少任何一条，别人拿到卡片转发一下就能替你报名。
 */
async function handleGrabAction(data, text) {
  const ctx = (data && data.context) || {};
  const chatId = String(ctx.open_chat_id || ctx.chat_id || data.open_chat_id || '');
  const operator = String((data && data.operator && data.operator.open_id) || '');
  const owner = String(process.env.PRIVATE_OWNER_OPEN_ID || '').trim();
  const p2p = ownerP2pChatId();

  if (!owner || !p2p || operator !== owner || chatId !== p2p) {
    log('grab action denied:', JSON.stringify({
      ownerSet: !!owner, p2pKnown: !!p2p,
      operatorMatch: operator === owner, chatMatch: chatId === p2p,
    }));
    return { toast: { type: 'error', content: GRAB_DENIED } };
  }

  if (text.startsWith('u:')) {
    const id = text.slice(2).trim();
    const res = await runWatchlist(['remove', '--id', id]);
    if (!res.ok) log('watchlist remove failed:', res.err.slice(0, 200));
    await reply(chatId, res.ok ? '已取消预报名。' : '取消失败，稍后再试。',
      { title: '预报名', template: res.ok ? 'green' : 'red' });
    if (!res.ok) return { toast: { type: 'error', content: '取消失败' } };
    // 新卡片随回调返回，客户端当场替换，不依赖再 PATCH 一次
    const rmCard = await runGrabCard();
    return rmCard
      ? { toast: { type: 'success', content: '已取消' }, card: { type: 'raw', data: rmCard } }
      : { toast: { type: 'success', content: '已取消' } };
  }

  let payload;
  try {
    payload = decodeBtnValue(text.slice(2));
  } catch (e) {
    log('grab value decode failed:', e.message);
    return { toast: { type: 'error', content: '按钮数据坏了，等下一次推送再点' } };
  }
  if (!payload || !payload.i || !payload.t) {
    return { toast: { type: 'error', content: '按钮数据不完整' } };
  }

  const res = await runWatchlist([
    'add', '--id', String(payload.i), '--name', String(payload.n || '活动'),
    '--at', String(payload.t), '--lead', String(payload.l || 100),
  ]);
  if (!res.ok) {
    log('watchlist add failed:', res.err.slice(0, 200));
    // 「窗口已经结束」这类拒绝原因由 watchlist.py 打在 stdout 的 [x] 行里，
    // 直接回给用户，比统一的「失败，已记录日志」有用。
    const why = (res.out.match(/^\[x\]\s*(.+)$/m) || [])[1]
      || '预报名失败，已记录日志';
    return { toast: { type: 'error', content: why.slice(0, 60) } };
  }
  log('watchlist added:', String(payload.i).slice(0, 8));
  await reply(chatId,
    '已预报名：' + (payload.n || '活动') + '\n'
    + '开放时间 ' + payload.t + '\n'
    + '到点自动报名，结果会私聊告诉你。反悔就点卡片上的「取消预报名」。',
    { title: '预报名成功', template: 'green' });
  const addedCard = await runGrabCard();
  return addedCard
    ? { toast: { type: 'success', content: '已预报名' }, card: { type: 'raw', data: addedCard } }
    : { toast: { type: 'success', content: '已预报名' } };
}

const HELP_TEXT = [
  '我能查这些，@我 直接说关键词就行：',
  '',
  '· 待报名 —— 还没开放报名的活动（默认）',
  '· 报名中 —— 报名已开放的活动（能否报名以详情页为准）',
  '· 全部 —— 前两种都列',
  '· 类型 —— 有哪些活动类型',
  '',
  '按类型查：「实践学习」「宿舍文化」「就业创业」',
  '例：@我 说「宿舍」，或「就业 报名中」',
].join('\n');

const PRIVATE_NOTICE = [
  '报名记录属于个人数据，不在群里展示。',
].join('\n');

// 私聊但不是 owner：固定拒绝，不透露任何个人数据，也不引导去别的通道
const PRIVATE_DENIED = [
  '个人报名记录只对账号主人提供，这里不提供查询。',
].join('\n');

// 查询失败时只说这一句：不把子进程报错、traceback 或接口原文发到群里
const QUERY_FAILED = '查询失败，请稍后重新发送';

const TYPE_WORDS = [
  ['实践', '实践学习'],
  ['宿舍', '宿舍文化'],
  ['就业', '就业创业'],
];

/** 把群里的自然语言解析成查询条件 */
function parseIntent(raw) {
  const text = String(raw || '')
    .replace(/@_user_\d+/g, '')
    .replace(/\s+/g, ' ')
    .trim();

  let type = '';
  for (const [kw, name] of TYPE_WORDS) {
    if (text.includes(kw)) {
      type = name;
      break;
    }
  }

  let view = 'pending';
  if (/帮助|help|怎么用|命令/i.test(text)) view = 'help';
  else if (/菜单|按钮|功能/.test(text)) view = 'menu';
  else if (/类型|类别|分类|都有啥/.test(text)) view = 'types';
  else if (/我的|报名了|我报了|已报名|参加了/.test(text)) view = 'private';
  else if (/报名中|可报名|在招|已开放|碰运气|还能报/.test(text)) view = 'open';
  else if (/全部|所有|都列|都要/.test(text)) view = 'all';

  return { view, type };
}

/** 文字回复统一套成飞书卡片：标题上色、正文走 markdown。
 *  纯文本和推送卡片在客户端里长相差别太大，一眼就能看出是两种东西。 */
function textCard(text, opts = {}) {
  return {
    config: { wide_screen_mode: true },
    header: {
      template: opts.template || 'turquoise',
      title: { tag: 'plain_text', content: opts.title || '校园活动助手' },
    },
    elements: [{ tag: 'markdown', content: text }],
  };
}

/** 发回复。返回是否真的发出去了：
 *  去重表只记「确实回成功」的消息，发失败的要留给飞书重投再试一次。
 *  飞书接口 HTTP 200 时也可能带业务码，`code !== 0` 一律当没发出去。
 *  默认走卡片，卡片接口失败才退回纯文本——样式可以降级，消息不能丢。 */
async function reply(chatId, text, opts = {}) {
  if (await replyCard(chatId, textCard(text, opts))) return true;
  try {
    const res = await client.im.message.create({
      params: { receive_id_type: 'chat_id' },
      data: {
        receive_id: chatId,
        msg_type: 'text',
        content: JSON.stringify({ text }),
      },
    });
    if (!res || res.code !== 0) {
      log('reply rejected: code=' + (res && res.code) + ' msg=' + (res && res.msg));
      return false;
    }
    log('replied to', chatId);
    return true;
  } catch (e) {
    log('reply failed:', e && e.message ? e.message : e);
    return false;
  }
}

async function replyCard(chatId, card) {
  try {
    const res = await client.im.message.create({
      params: { receive_id_type: 'chat_id' },
      data: {
        receive_id: chatId,
        msg_type: 'interactive',
        content: JSON.stringify(card),
      },
    });
    if (!res || res.code !== 0) {
      log('card rejected: code=' + (res && res.code) + ' msg=' + (res && res.msg));
      return false;
    }
    log('card sent to', chatId);
    return true;
  } catch (e) {
    log('card failed:', e && e.message ? e.message : e);
    return false;
  }
}

/** 只处理真人发的消息。机器人/应用发的消息也会推过来，必须挡掉，
 *  否则群里的 webhook 推送会触发机器人自己回复，形成「推送 → 回复」的假象。 */
function isUserMessage(data) {
  const sender = (data && data.sender) || {};
  // 字段缺失时不能默认当成真人：事件结构变化、模拟事件、
  // 或 SDK 换版本时，这道门会悄悄失效，机器人开始在群里插话。
  return sender.sender_type === 'user';
}

/** 私人查询的唯一判据：私聊 + 发送者就是 PRIVATE_OWNER_OPEN_ID 配置的本人。
 *  只判断「是不是私聊」是不够的：应用可见范围内任何人都能私聊机器人，
 *  那等于把绑定账号的报名记录给了第一个来问的人。
 *  owner 或发送者缺失一律拒绝，绝不从聊天、users.json 或 BOT_OPEN_ID 自动认领。 */
function isPrivateOwner(chatType, senderOpenId, ownerId) {
  return chatType === 'p2p'
    && ownerId !== ''
    && senderOpenId !== ''
    && senderOpenId === ownerId;
}

/** 群里必须 @ 它才响应；私聊不需要 @（私聊本身就是对它说的）。
 *  拿不到自己的 open_id 时**不响应**——宁可少说话，也不能变成"群里说啥都插嘴"。 */
function isAddressedToBot(msg, chatType, botOpenId) {
  if (chatType !== 'group') return true;      // 私聊一律响应
  if (!botOpenId) return false;               // 身份未知，拒绝响应（fail-closed）
  const mentions = msg.mentions || [];
  if (!mentions.length) return false;
  return mentions.some((m) => {
    const id = m.id || {};
    return id.open_id === botOpenId;
  });
}

/** 角色能接的会话类型：群入口只接群，owner 入口只接私聊。
 *  这条判断放在最外层，任何一类会话进错进程都在读消息之前就被挡住，
 *  不需要依赖后面的 view 分支去兜底。 */
function roleAcceptsChat(chatType) {
  return BOT_ROLE === ROLE_GROUP ? chatType === 'group' : chatType === 'p2p';
}

/** owner 私聊会话是否已记录、是否与当前会话一致。
 *
 *  owner 还没私聊过机器人时 users.json 里没有记录，此时 known=false：
 *  - 消息入口允许通过并当场记录，否则第一条消息永远进不来，
 *    机器人再也学不到 owner 的 chat_id（自锁）；
 *  - 卡片动作入口必须 known && match：卡片只可能发到已知的私聊会话，
 *    拿不到记录就说明这张卡不属于当前会话，一律拒绝。
 */
function ownerChatState(chatId) {
  const p2p = ownerP2pChatId();
  if (!p2p) return { known: false, match: false };
  return { known: true, match: String(chatId || '') === p2p };
}

/** 卡片动作的 owner 校验：点击人就是 owner、会话是已记录的 owner 私聊。 */
function ownerCardActionAllowed(data) {
  const ctx = (data && data.context) || {};
  const chatId = String(ctx.open_chat_id || ctx.chat_id || data.open_chat_id || '');
  const operator = String((data && data.operator && data.operator.open_id) || '');
  const owner = String(process.env.PRIVATE_OWNER_OPEN_ID || '').trim();
  const chat = ownerChatState(chatId);
  return !!owner && operator === owner && chat.known && chat.match;
}

/** 卡片动作允许的视图：群入口没有 mine，个人视图只存在于 owner 私聊。 */
function allowedViews() {
  return BOT_ROLE === ROLE_GROUP
    ? ['pending', 'open', 'all', 'menu']
    : ['pending', 'open', 'all', 'menu', 'mine'];
}

/** 卡片按钮点击：按 value 重新查一次，就地更新卡片。
 *
 *  两种角色共用这一个回调，但门禁完全不同：
 *  - group_query：v:* 可以，g:/u: 一律拒绝（群里没有个人动作）；
 *  - private_owner：先过三重一致（点击人=owner、会话=已记录的 owner 私聊），
 *    通过后才区分 v:* 与 g:/u:*。
 */
async function handleCardAction(data) {
  try {
    const action = (data && data.action) || {};
    let raw = action.value;
    if (raw && typeof raw === 'object') raw = raw.value || '';
    // 飞书回调里的值可能带一层引号（"v:open"），先剥干净再解析
    let text = String(raw == null ? '' : raw).trim();
    if (text.length > 1 && text[0] === '"' && text[text.length - 1] === '"') {
      text = text.slice(1, -1).trim();
    }
    const isGrab = text.startsWith('g:') || text.startsWith('u:');

    if (BOT_ROLE === ROLE_GROUP && isGrab) {
      // 群入口永远不进 handleGrabAction：不是「校验后拒绝」，是压根不进去
      log('grab action denied: group_query role');
      return { toast: { type: 'error', content: GRAB_GROUP_DENIED } };
    }

    if (BOT_ROLE === ROLE_OWNER && !ownerCardActionAllowed(data)) {
      // 卡片可以被转发，别人点到「报名中」也不该拿到 owner 的会话上下文
      log('card action denied: operator or chat is not the configured owner');
      return { toast: { type: 'error', content: GRAB_DENIED } };
    }

    const m = text.match(/v:([A-Za-z]+)/);
    const view = m ? m[1].toLowerCase() : 'pending';
    log('card action:', JSON.stringify(raw), '-> text:', text, '-> view:', view,
      '-> role:', BOT_ROLE);

    // 预报名 / 取消预报名：value 是 g:<base64> 或 u:<活动id>，
    // 只有 private_owner 角色能走到这里，handleGrabAction 里还会再校验一次。
    if (isGrab) {
      return await handleGrabAction(data, text);
    }

    if (!allowedViews().includes(view)) {
      return { toast: { type: 'info', content: '这个按钮暂时没做' } };
    }

    const cardRaw = await runQuery(view, '', 'card');
    if (!cardRaw.ok) {
      log('card action query failed:', cardRaw.diagnostic);
      return { toast: { type: 'error', content: '刷新失败，稍后再试' } };
    }
    if (cardRaw.diagnostic) log('card action stderr:', cardRaw.diagnostic);
    const card = JSON.parse(cardRaw.stdout);
    return {
      toast: { type: 'info', content: '已更新' },
      card: { type: 'raw', data: card },
    };
  } catch (e) {
    log('card action failed:', e && e.message ? e.message : e);
    return { toast: { type: 'error', content: '刷新失败，稍后再试' } };
  }
}

/** 处理一条已通过前置过滤的消息。
 *  返回 true 表示「已经处理成功、可以记为处理过」；
 *  返回 false 表示这次没成功（比如回复没发出去），让飞书重投时再试一次。
 *  返回 false 的消息不进去重表，否则一次失败的查询会被永久当成「答过了」。 */
async function handleMessage(data) {
  const msg = (data && data.message) || {};
  const chatId = msg.chat_id;
  if (!chatId) return false;

  let text = '';
  try {
    text = (JSON.parse(msg.content || '{}').text || '').trim();
  } catch (e) {
    text = '';
  }

  const chatType = msg.chat_type || 'group';   // p2p | group
  const senderOpenId = (data && data.sender && data.sender.sender_id)
    ? data.sender.sender_id.open_id
    : '';

  // 角色门禁：群查询进程只处理群消息；owner 进程只处理 owner 本人的私聊。
  // 放在最前面，后面所有分支都可以假定「会话类型已经被角色确认过」。
  if (!roleAcceptsChat(chatType)) {
    log('skip message: chat type not allowed for role', BOT_ROLE, chatType);
    // 不回复，但记为「处理过」：飞书重投同一条不属于本角色的消息没有意义，
    // 让它反复重投只会在日志里刷屏。
    return true;
  }

  if (msg.message_type && msg.message_type !== 'text') {
    return await reply(chatId, '我只认文字消息，@我 说「待报名」或「帮助」就行。',
      { title: '提示', template: 'grey' });
  }

  const { view, type } = parseIntent(text);

  if (BOT_ROLE === ROLE_GROUP) {
    // 群入口：不写 users.json（那是私聊收件人表），不查任何个人视图。
    log('query:', view, type || '-', '| group |', senderOpenId || 'no-openid',
      '| raw:', text.slice(0, 30));
    if (view === 'private') {
      // 群里的「我的报名」：固定说明，不透露数据，也不引导去私聊
      return await reply(chatId, PRIVATE_NOTICE, { title: '提示', template: 'grey' });
    }
    if (view === 'help') {
      return await reply(chatId, HELP_TEXT, { title: '帮助', template: 'blue' });
    }
    return await answer(chatId, view, type);
  }

  // owner 私聊入口
  const ownerId = String(process.env.PRIVATE_OWNER_OPEN_ID || '').trim();
  if (!isPrivateOwner(chatType, senderOpenId, ownerId)) {
    log('private message denied: sender is not the configured owner');
    return await reply(chatId, PRIVATE_DENIED, { title: '不能查', template: 'orange' });
  }
  // 记录 owner 的私聊会话（只记 p2p）。群消息进不到这里，所以不会被覆盖成 group。
  // 换设备/重装应用会带来新的 chat_id：这里自动跟随并在日志留痕，
  // 否则一次重装就把机器人永久锁死；卡片动作仍然按记录严格校验，不会因此放宽。
  const before = ownerChatState(chatId);
  rememberUser(senderOpenId, chatType, chatId);
  if (before.known && !before.match) {
    log('owner private chat_id changed, following new chat');
  }
  log('query:', view, type || '-', '| p2p owner |', senderOpenId.slice(0, 8),
    '| raw:', text.slice(0, 30));

  if (view === 'help') {
    return await reply(chatId, HELP_TEXT, { title: '帮助', template: 'blue' });
  }

  // 个人数据（我的报名等）只有 owner 私聊才有；群入口在上面的分支已经拦掉了
  const effectiveView = view === 'private' ? 'mine' : view;
  if (effectiveView === 'mine') log('private query allowed for owner');

  return await answer(chatId, effectiveView, type);
}

/** 查询并回复（卡片优先、纯文本兜底）。两个角色共用，保证同一批数据两类会话
 *  看到的版式一致；角色差异只体现在「能查哪些视图」，不体现在渲染上。 */
async function answer(chatId, view, type) {
  // 先用卡片，失败再退回纯文本
  if (view !== 'types') {
    const raw = await runQuery(view, type, 'card');
    if (!raw.ok) {
      // 查不到就明说，但**不能记成「已回答」**：否则一次临时失败会让这条消息
      // 永久失去重试机会，用户只会看到一句失败提示。
      log('query failed (card):', raw.diagnostic);
      await reply(chatId, QUERY_FAILED, { title: '查询失败', template: 'red' });
      return false;
    }
    if (raw.diagnostic) log('query stderr (card):', raw.diagnostic);
    let card = null;
    try {
      card = JSON.parse(raw.stdout);
    } catch (e) {
      card = null;
      log('card json invalid, fall back to text');
    }
    if (card && (await replyCard(chatId, card))) return true;
    log('card reply failed, fall back to text');
  }

  const result = await runQuery(view, type);
  if (!result.ok) {
    log('query failed (text):', result.diagnostic);
    await reply(chatId, QUERY_FAILED, { title: '查询失败', template: 'red' });
    return false;
  }
  if (result.diagnostic) log('query stderr (text):', result.diagnostic);
  return await reply(chatId, result.stdout, { title: '查询结果', template: 'turquoise' });
}

const dispatcher = new lark.EventDispatcher({}).register({
  'im.message.receive_v1': async (data) => {
    const msg = (data && data.message) || {};
    const chatId = msg.chat_id;
    const msgId = msg.message_id;
    if (!chatId) return;

    if (!isUserMessage(data)) {
      log('skip non-user message:', (data.sender || {}).sender_type,
          (msg.message_type || '?'));
      return;
    }

    // 会话类型门禁在「读内容」之前：进错进程的消息连日志正文都不会被解析
    const chatType = msg.chat_type || 'group';
    if (!roleAcceptsChat(chatType)) {
      log('skip message: chat type not allowed for role', BOT_ROLE, chatType);
      return;
    }

    if (!isAddressedToBot(msg, chatType, BOT_OPEN_ID)) {
      log('skip 未 @ 我的群消息');
      return;
    }

    // 没有 message_id 就没法去重：宁可回一次，也不要因为取不到 ID 就把消息吞掉
    if (!msgId) {
      await handleMessage(data);
      return;
    }

    try {
      // 只有处理成功才落盘；失败的留给飞书重投再试一次
      const ran = await handledMessages().processOnce(msgId, () => handleMessage(data));
      if (!ran) log('skip duplicate or in-flight message:', msgId);
    } catch (e) {
      log('message handling failed, retry on redelivery:', msgId,
          e && e.message ? e.message : e);
    }
  },

  // 卡片按钮点击回调（需要在后台「事件与回调 → 回调配置」里订阅 card.action.trigger）
  'card.action.trigger': handleCardAction,
});

// 自检模式：只验证解析和卡片构造，不建立长连接
const ws = new lark.WSClient({
  appId: APP_ID,
  appSecret: APP_SECRET,
  loggerLevel: lark.LoggerLevel.info,
});

if (!ROLE_OK) {
  // 已经 exit(1)。这里不再建立长连接：沙箱测试与异常路径下，
  // process.exit 之后的代码仍可能被执行，权限门禁不能靠「进程已经死了」兜底。
} else if (process.argv.includes('--selftest')) {
  (async () => {
    let failed = 0;
    console.log('== 角色门禁（BOT_ROLE=' + BOT_ROLE + '）==');
    console.log('  允许的会话：' + (BOT_ROLE === ROLE_GROUP ? 'group（必须 @ 我）' : 'p2p（只认 owner）'));
    console.log('  允许的视图：' + allowedViews().join(', '));

    const roleCases = [
      ['group', ROLE_GROUP, true],
      ['p2p', ROLE_GROUP, false],
      ['group', ROLE_OWNER, false],
      ['p2p', ROLE_OWNER, true],
    ];
    for (const [chatType, role, want] of roleCases) {
      // 用真实判断函数 + 当前进程角色比对，角色不符的用例只报告不判失败
      const got = roleAcceptsChat(chatType);
      const expect = role === BOT_ROLE ? want : null;
      console.log('  ' + chatType + ' 在 ' + role + ' 下 -> ' + got
        + (expect === null ? '（非当前角色，仅展示）' : '（期望 ' + expect + '）'));
      if (expect !== null && got !== expect) failed++;
    }

    console.log('== parseIntent ==');
    const samples = ['', '我的', '报名中', '帮助', '菜单', '实践', '全部', '宿舍 报名中'];
    for (const t of samples) {
      const r = parseIntent(t);
      console.log('  ' + JSON.stringify(t) + ' -> view=' + r.view + ' type=' + (r.type || '-'));
    }

    console.log('== 卡片构造 ==');
    for (const v of ['pending', 'open', 'all', 'menu', 'mine']) {
      const raw = await runQuery(v, '', 'card');
      if (!raw.ok) {
        failed++;
        console.log('  ' + v + ' -> 查询失败: ' + raw.diagnostic);
        continue;
      }
      try {
        const c = JSON.parse(raw.stdout);
        const btns = (c.elements || []).filter((e) => e.tag === 'action').length;
        console.log('  ' + v + ' -> ' + (c.header && c.header.title ? c.header.title.content : '?')
          + ' elements=' + (c.elements || []).length + ' action组=' + btns);
      } catch (e) {
        failed++;
        console.log('  ' + v + ' -> 解析失败: ' + raw.stdout.slice(0, 100));
      }
    }

    console.log('== 发送者过滤 ==');
    const senderCases = [
      [{ sender: { sender_type: 'user' } }, true],
      [{ sender: { sender_type: 'app' } }, false],
      [{ sender: { sender_type: 'bot' } }, false],
      [{ sender: {} }, false],        // 字段缺失一律拒绝（fail-closed）
      [{}, false],
    ];
    for (const [payload, want] of senderCases) {
      const got = isUserMessage(payload);
      console.log('  ' + JSON.stringify(payload) + ' -> ' + got + '（期望 ' + want + '）');
      if (got !== want) failed++;
    }

    console.log('== 卡片按钮回调（模拟）==');
    // owner 角色要先有「已记录的 owner 私聊会话」才可能通过门禁；
    // 群查询角色不需要任何个人数据，所以两边共用一份模拟数据即可。
    const canDriveCards = BOT_ROLE === ROLE_GROUP || !!ownerP2pChatId();
    if (!canDriveCards) {
      console.log('  （没有 owner 私聊记录，跳过 v:* 实测；这是首次启动的正常状态）');
    } else {
      const btnSamples = ['v:pending', 'v:open', 'v:all', 'v:menu', '"v:open"', ' v:all ',
        'v:mine', 'v:unknown', ''];
      for (const v of btnSamples) {
        const r = await handleCardAction({
          action: { value: v },
          context: { open_chat_id: BOT_ROLE === ROLE_GROUP ? 'oc_group_selftest' : ownerP2pChatId() },
          operator: { open_id: String(process.env.PRIVATE_OWNER_OPEN_ID || 'ou_selftest') },
        });
        const hasCard = !!(r && r.card && r.card.data);
        const title = hasCard ? r.card.data.header.title.content : '-';
        console.log('  ' + JSON.stringify(v) + ' -> 卡片=' + hasCard
          + ' 标题=' + title + ' toast=' + (r.toast ? r.toast.content : '-'));
        // 带引号/空格的值也必须解析出正确视图
        const wantOpen = /open/.test(v);
        const wantAll = /all/.test(v);
        if (wantOpen && !/报名已开放/.test(title)) failed++;
        if (wantAll && !/报名已开放/.test(title)) failed++;
        // 群查询角色没有 mine；owner 角色才有
        if (v === 'v:mine' && BOT_ROLE === ROLE_GROUP && hasCard) failed++;
        if (v === 'v:mine' && BOT_ROLE === ROLE_OWNER && !hasCard) failed++;
      }
    }

    console.log('== 个人动作门禁 ==');
    const grabValue = 'g:' + Buffer.from(JSON.stringify({ i: 'evt_selftest', n: 'x', t: '2026-10-30 12:00:00' }))
      .toString('base64').replace(/=+$/, '');
    const grabRes = await handleCardAction({
      action: { value: grabValue },
      context: { open_chat_id: 'oc_selftest' },
      operator: { open_id: 'ou_not_owner' },
    });
    const grabDenied = !!(grabRes && grabRes.toast && grabRes.toast.type === 'error');
    console.log('  非本人点预报名按钮 -> 拒绝=' + grabDenied
      + ' toast=' + (grabRes && grabRes.toast ? grabRes.toast.content : '-'));
    if (!grabDenied) failed++;

    console.log('== @ 判定 ==');
    const atBot = { id: { open_id: 'ou_bot' } };
    const atOther = { id: { open_id: 'ou_other' } };
    const atCases = [
      ['group', { mentions: [atBot] }, 'ou_bot', true],
      ['group', { mentions: [atOther] }, 'ou_bot', false],
      ['group', { mentions: [] }, 'ou_bot', false],
      ['group', {}, 'ou_bot', false],
      ['group', { mentions: [atBot] }, '', false],   // 身份未知时 fail-closed
      ['p2p', {}, '', true],
    ];
    for (const [chatType, msg, botId, want] of atCases) {
      const got = isAddressedToBot(msg, chatType, botId);
      console.log('  ' + chatType + ' ' + JSON.stringify(msg.mentions || [])
        + ' botId=' + (botId || '(空)') + ' -> ' + got + '（期望 ' + want + '）');
      if (got !== want) failed++;
    }

    console.log(failed === 0 ? '== 自检通过 ==' : '== 自检失败 ' + failed + ' 项 ==');
    process.exit(failed === 0 ? 0 : 1);
  })();
} else {
  (async () => {
    // 启动时先把去重表读一遍：文件损坏就让进程立刻失败，由 systemd / cron 报错。
    // 以前是懒加载，坏缓存要等到第一条消息才暴露，表现为「机器人不回复」，
    // 而且日志里看不出是缓存坏了。这里也不静默重建——那等于把 48 小时的
    // 去重记录整片丢掉，重启后群里会突然重复回复。
    try {
      handledMessages();
    } catch (e) {
      log('[bot] 去重缓存无法加载：', e && e.message ? e.message : e);
      process.exit(1);
      return;
    }
    if (!(await loadBotOpenId())) {
      process.exit(1);
      return;
    }
    // owner 入口没有 owner 身份就等于「谁都能用的报名机器人」，直接不起来。
    if (BOT_ROLE === ROLE_OWNER
      && !String(process.env.PRIVATE_OWNER_OPEN_ID || '').trim()) {
      log('[bot] private_owner 角色缺少 PRIVATE_OWNER_OPEN_ID，拒绝启动');
      process.exit(1);
      return;
    }
    try {
      await ws.start({ eventDispatcher: dispatcher });
      log('[bot] 长连接已启动', APP_ID, '｜角色', BOT_ROLE,
        BOT_ROLE === ROLE_GROUP ? '｜群里只在被 @ 时回答查询' : '｜只服务 owner 私聊');
    } catch (e) {
      log('[bot] 启动失败：', e && e.message ? e.message : e);
      process.exit(1);
    }
  })();
}
