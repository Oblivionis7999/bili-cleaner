/* ============================================================================
 * BiliCleaner web UI — vanilla JS, no dependencies, no build step.
 *
 * Layout of this file:
 *   0. small helpers + runtime error log
 *   1. constants + global state
 *   2. HTTP layer (relative /api/*, 401 → login)
 *   3. demo data + demo API (used by ?demo=1 or when the backend is down)
 *   4. auth / QR login
 *   5. shared UI: toast / dialog / action bar / task dock
 *   6. router + views (dynamics, follows, favorites, toview, settings, logs)
 *   7. event handlers
 *   8. boot + demo screenshot hooks
 * ========================================================================== */
(() => {
  'use strict';

  /* ========================================================== 0. helpers */

  const errorLog = [];
  function recordError(message) {
    errorLog.push(String(message));
    const el = document.getElementById('js-error-log');
    if (el) el.textContent = errorLog.join('\n');
    document.documentElement.setAttribute('data-js-errors', String(errorLog.length));
    try {
      console.warn('[BiliCleaner]', message);
    } catch (_) {
      /* ignore */
    }
  }
  window.addEventListener('error', (event) => {
    recordError(`${event.message} @ ${event.filename || ''}:${event.lineno || 0}`);
  });
  window.addEventListener('unhandledrejection', (event) => {
    const reason = event.reason;
    recordError('unhandledrejection: ' + (reason && reason.message ? reason.message : String(reason)));
  });

  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  function esc(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, (ch) => ({
      '&': '&amp;',
      '<': '&lt;',
      '>': '&gt;',
      '"': '&quot;',
      "'": '&#39;',
    }[ch]));
  }
  const escAttr = esc;

  function escapeXml(value) {
    return String(value == null ? '' : value).replace(/[<>&"']/g, (ch) => ({
      '<': '&lt;',
      '>': '&gt;',
      '&': '&amp;',
      '"': '&quot;',
      "'": '&apos;',
    }[ch]));
  }

  function debounce(fn, ms) {
    let timer = 0;
    return function debounced(...args) {
      clearTimeout(timer);
      timer = setTimeout(() => fn.apply(this, args), ms);
    };
  }

  function clamp(value, min, max) {
    return Math.min(max, Math.max(min, value));
  }

  function fmtInt(value) {
    const n = Number(value);
    return Number.isFinite(n) ? n.toLocaleString('zh-CN') : '0';
  }

  function pad2(n) {
    return String(n).padStart(2, '0');
  }

  function nowSec() {
    return Math.floor(Date.now() / 1000);
  }

  function isoLocal(date) {
    const d = date || new Date();
    return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}T${pad2(d.getHours())}:${pad2(
      d.getMinutes()
    )}:${pad2(d.getSeconds())}`;
  }

  function isoLocalAfter(seconds) {
    return isoLocal(new Date(Date.now() + seconds * 1000));
  }

  function fmtDateTime(ts) {
    if (!ts) return '—';
    const d = new Date(Number(ts) * 1000);
    if (Number.isNaN(d.getTime())) return '—';
    return `${d.getMonth() + 1}月${d.getDate()}日 ${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
  }

  function fmtDate(ts) {
    if (!ts) return '—';
    const d = new Date(Number(ts) * 1000);
    if (Number.isNaN(d.getTime())) return '—';
    return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
  }

  function fmtDuration(seconds) {
    const total = Math.max(0, Math.round(Number(seconds) || 0));
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    if (h > 0) return `${h}:${pad2(m)}:${pad2(s)}`;
    return `${m}:${pad2(s)}`;
  }

  function fmtIsoDisplay(iso) {
    if (!iso) return '—';
    return String(iso).replace('T', ' ').slice(0, 19);
  }

  function truncate(text, max) {
    const str = String(text || '').replace(/\s+/g, ' ').trim();
    if (str.length <= max) return str;
    return str.slice(0, max) + '…';
  }

  function clone(value) {
    return JSON.parse(JSON.stringify(value));
  }

  function setText(sel, text) {
    const el = $(sel);
    if (el) el.textContent = text;
  }

  function setHTML(sel, html) {
    const el = $(sel);
    if (el) el.innerHTML = html;
  }

  function waitFor(predicate, timeout = 5000, interval = 100) {
    const started = Date.now();
    return new Promise((resolve) => {
      const check = () => {
        if (predicate()) {
          resolve(true);
          return;
        }
        if (Date.now() - started > timeout) {
          resolve(false);
          return;
        }
        setTimeout(check, interval);
      };
      check();
    });
  }

  /* ============================================= 1. constants + state */

  const QS = new URLSearchParams(location.search);
  const FORCE_DEMO = QS.get('demo') === '1';

  const CATEGORIES = [
    '游戏',
    '动画·二次元',
    '影视',
    '科技数码',
    '编程·AI',
    '科普·人文',
    '学习·教育',
    '音乐',
    '舞蹈',
    '美食',
    '生活·Vlog',
    '搞笑·娱乐',
    '体育·健身',
    '汽车·机械',
    '财经·职场',
    '时事·社会',
    '时尚·美妆',
    '宠物·动物',
    '手工·绘画·摄影',
    '虚拟UP主·VTuber',
    '其他·难判断',
  ];

  const DYNAMIC_LABELS = {
    DYNAMIC_TYPE_FORWARD: '转发',
    DYNAMIC_TYPE_DRAW: '图文',
    DYNAMIC_TYPE_WORD: '文字',
    DYNAMIC_TYPE_AV: '视频',
    DYNAMIC_TYPE_ARTICLE: '专栏',
    DYNAMIC_TYPE_LIVE_RCMD: '直播',
    DYNAMIC_TYPE_LIVE: '直播',
    DYNAMIC_TYPE_COMMON_SQUARE: '装扮',
    DYNAMIC_TYPE_COMMON_VERTICAL: '竖屏',
    DYNAMIC_TYPE_MUSIC: '音乐',
    DYNAMIC_TYPE_PGC: '番剧',
    DYNAMIC_TYPE_COURSES: '课程',
    DYNAMIC_TYPE_COURSES_SEASON: '课程',
  };

  const DYNAMIC_FILTER_TYPES = [
    ['DYNAMIC_TYPE_AV', '视频'],
    ['DYNAMIC_TYPE_DRAW', '图文'],
    ['DYNAMIC_TYPE_WORD', '文字'],
    ['DYNAMIC_TYPE_FORWARD', '转发'],
    ['DYNAMIC_TYPE_ARTICLE', '专栏'],
    ['DYNAMIC_TYPE_LIVE_RCMD', '直播'],
    ['DYNAMIC_TYPE_MUSIC', '音乐'],
    ['DYNAMIC_TYPE_PGC', '番剧'],
    ['DYNAMIC_TYPE_COURSES', '课程'],
    ['DYNAMIC_TYPE_COMMON_SQUARE', '装扮'],
    ['DYNAMIC_TYPE_COMMON_VERTICAL', '竖屏'],
  ];

  const TASK_TYPE_LABELS = {
    unfollow: '批量取关',
    dynamic_delete: '删除动态',
    favorite_clear: '清理收藏',
    favorite_clean_invalid: '清理失效收藏',
    toview_clear: '清空稍后再看',
    classify_llm: 'LLM 分类',
  };

  const TASK_STATUS_LABELS = {
    queued: '排队中',
    running: '进行中',
    paused: '已暂停',
    cooldown: '风控冷却',
    stopped: '已停止',
    done: '已完成',
    failed: '失败',
  };

  const DEFAULT_SETTINGS = {
    delay_seconds: 2.5,
    jitter_seconds: 0.8,
    batch_size: 20,
    batch_pause_seconds: 3.0,
    cooldown_seconds: 600,
    max_cooldowns: 2,
    protect_mutual: true,
    confirm_phrase: '清空',
    backup_keep: 0,
    llm: { enabled: false, base_url: '', api_key: '', model: '', batch_size: 20 },
  };

  const LIMITS = { follows: 24, dynamics: 20, favorites: 18, toview: 20 };

  const EMPTY_ICONS = {
    login:
      '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="4" y="10" width="16" height="10" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/></svg>',
    search:
      '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" aria-hidden="true"><circle cx="11" cy="11" r="6.5"/><path d="m16 16 4 4"/></svg>',
    inbox:
      '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 6h16v12H4z"/><path d="M4 13h4l2 3h4l2-3h4"/></svg>',
  };

  const PLAY_ICON =
    '<svg width="14" height="14" viewBox="0 0 14 14" fill="currentColor" aria-hidden="true"><path d="M4 2.5v9l8-4.5-8-4.5Z"/></svg>';

  const SEARCH_ICON =
    '<svg class="search__icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" aria-hidden="true"><circle cx="7" cy="7" r="4.5"/><path d="m10.5 10.5 3 3"/></svg>';

  const DEMO_USER = {
    mid: 1000001,
    name: '演示用户',
    face: '',
    vip: false,
  };

  const state = {
    route: 'dynamics',
    loggedIn: false,
    user: null,
    health: null,
    mock: false,
    settings: clone(DEFAULT_SETTINGS),
    settingsLoaded: false,
    groups: [],
    healthError: false,
    followStats: null,
    backups: [],
    backupsLoaded: false,
    logs: { items: [], loaded: false, level: '' },
    tasks: [],
    follows: {
      items: [],
      total: 0,
      loaded: false,
      loading: false,
      q: '',
      category: '',
      group: '',
      mutual: false,
      selected: new Set(),
    },
    dynamics: {
      items: [],
      cursor: '',
      hasMore: true,
      loaded: false,
      loading: false,
      q: '',
      type: '',
      from: '',
      to: '',
      selected: new Set(),
    },
    favorites: {
      folders: [],
      foldersLoaded: false,
      loadingFolders: false,
      folderId: null,
      items: [],
      total: 0,
      loaded: false,
      loading: false,
      q: '',
      selected: new Set(),
    },
    toview: { items: [], total: 0, loaded: false, loading: false, selected: new Set() },
  };

  let DEMO = FORCE_DEMO;

  /* ============================================================ 2. HTTP */

  class ApiError extends Error {
    constructor(status, code, message) {
      super(message || code || '请求失败');
      this.name = 'ApiError';
      this.status = status;
      this.code = code;
    }
  }

  async function request(method, path, body) {
    if (DEMO) return demoRequest(method, path, body);

    let res;
    try {
      const init = {
        method,
        credentials: 'same-origin',
        headers: { Accept: 'application/json' },
      };
      if (body !== undefined) {
        init.headers['Content-Type'] = 'application/json';
        init.body = JSON.stringify(body);
      }
      res = await fetch(path, init);
    } catch (err) {
      enableDemo('本地服务没有响应，已切换到演示数据。');
      return demoRequest(method, path, body);
    }

    if (res.status === 401) {
      handleAuthLost();
      throw new ApiError(401, 'NOT_LOGGED_IN', '登录状态已失效，请重新扫码登录');
    }

    if (!res.ok) {
      let payload = null;
      try {
        payload = await res.json();
      } catch (_) {
        payload = null;
      }
      const code = payload && payload.error ? payload.error.code : null;
      const message =
        (payload && payload.error && payload.error.message) || `请求失败（HTTP ${res.status}）`;
      throw new ApiError(res.status, code, message);
    }

    if (res.status === 204) return null;
    const text = await res.text();
    if (!text) return null;
    try {
      return JSON.parse(text);
    } catch (_) {
      throw new ApiError(res.status, 'BAD_JSON', '服务返回了无法解析的数据');
    }
  }

  const api = {
    get: (path) => request('GET', path),
    post: (path, body) => request('POST', path, body),
    put: (path, body) => request('PUT', path, body),
  };

  async function withBtn(btn, fn) {
    if (!btn || btn.disabled) return undefined;
    btn.disabled = true;
    btn.classList.add('is-loading');
    btn.setAttribute('aria-busy', 'true');
    try {
      return await fn();
    } catch (err) {
      handleError(err);
      return undefined;
    } finally {
      btn.classList.remove('is-loading');
      btn.removeAttribute('aria-busy');
      btn.disabled = false;
    }
  }

  function handleError(err) {
    if (!err) return;
    if (err.status === 401) {
      handleAuthLost();
      return;
    }
    toast(err.message || '操作失败，请稍后重试');
  }

  let authLostShown = false;
  function handleAuthLost() {
    if (authLostShown) return;
    authLostShown = true;
    state.loggedIn = false;
    state.user = null;
    demo.loggedIn = false;
    stopTaskPolling();
    closeDialog();
    updateNav();
    toast('登录状态已失效，请重新扫码登录');
    navigate('login');
    setTimeout(() => {
      authLostShown = false;
    }, 4000);
  }

  /* ================================================= 3. demo data / API */

  const demo = {
    ready: false,
    forced: FORCE_DEMO,
    loggedIn: true,
    qrConfirmed: false,
    seq: 1,
    follows: [],
    groups: [],
    dynamics: [],
    folders: [],
    favItems: {},
    toview: [],
    tasks: [],
    taskTimers: new Map(),
    logs: [],
    backups: [],
    settings: null,
  };

  const GROUP_DEFS = [
    { tagid: 74311568, name: '学习' },
    { tagid: 74311569, name: '游戏' },
    { tagid: 74311570, name: '科技' },
    { tagid: 74311571, name: '音乐' },
    { tagid: 74311572, name: '生活' },
  ];

  const NAME_STEMS = [
    '小满', '阿哲', '老猫', '林同学', '山雨', '青柠', '南枝', '一只鹿', '菜鸟小王', '深夜食堂',
    '北巷', '雾岛', '桃子汽水', '咸鱼君', '麦田', '远山', '白露', '阿茶', '木鱼', '大橘',
    '小鹿乱撞', '南风', '星野', '月半', '啾啾', '板栗', '柚子', '蓝莓', '山中', '海盐',
    '晚风', '铁柱', '二狗', '大脸猫', '土豆', '豆包', '阿九', '小柒', '南北', '朝雨',
    '故里', '长歌', '短笛', '清风', '明烛', '青衫', '白衣', '墨鱼', '阿柴', '汤圆',
  ];

  const NAME_SUFFIXES = [
    '研究所', '的日常', '频道', '手记', '工坊', '实验室', '笔记', '观察', '放映室', '厨房',
    '电台', '课堂', '测评', '碎碎念', 'Vlog', '杂货铺', '慢生活', '档案', '小队', '放映厅',
  ];

  const SIGNS = [
    '认真做内容，随缘更新。',
    '一个普通人的日常记录。',
    '视频更新看心情，感谢关注。',
    '做点有用的东西。',
    '喜欢拍点小片子。',
    '慢慢来，比较快。',
    '偶尔发疯，经常摸鱼。',
    '在读学生，记录学习过程。',
    '用爱发电，感谢每一个三连。',
    '每周五晚更新。',
    '做知识的搬运工。',
    '想看世界，也想被世界看见。',
    '这里是一只小透明的角落。',
    '不生产内容，只做内容的观众。',
    '主业生活，副业整活。',
    '写点代码，拍点照片。',
    '正在学习剪辑，欢迎交流。',
    '更新很慢，但不会断。',
    '谢谢你来过。',
    '一起变好。',
  ];

  const OFFICIAL_TITLES = [
    'bilibili 知名科技UP主',
    'bilibili 知名游戏UP主',
    'bilibili 知名美食UP主',
    'bilibili 知名知识UP主',
    'bilibili 知名音乐UP主',
  ];

  const DYNAMIC_TYPE_POOL = [
    'DYNAMIC_TYPE_AV',
    'DYNAMIC_TYPE_AV',
    'DYNAMIC_TYPE_AV',
    'DYNAMIC_TYPE_DRAW',
    'DYNAMIC_TYPE_DRAW',
    'DYNAMIC_TYPE_FORWARD',
    'DYNAMIC_TYPE_FORWARD',
    'DYNAMIC_TYPE_WORD',
    'DYNAMIC_TYPE_WORD',
    'DYNAMIC_TYPE_ARTICLE',
    'DYNAMIC_TYPE_MUSIC',
    'DYNAMIC_TYPE_LIVE_RCMD',
  ];

  const DYNAMIC_TEXTS = [
    '刚看完这期视频，讲得比我上过的课都清楚，推荐给同样在学的朋友。',
    '周末去了趟郊外，顺手拍了几张照片，下午四点的光真的好。',
    '转发一下，这个思路值得收藏，回头做项目的时候试试。',
    '今天的更新就到这，明天继续。最近在啃一本很厚的书。',
    '把之前的数据重新跑了一遍，结果比预期好一点，细节放到评论区了。',
    '新到的键盘手感不错，就是有点吵，室友已经开始抗议了。',
    '看了三遍还是觉得很震撼，这大概就是做内容的意义吧。',
    '最近失眠有点严重，有没有什么助眠的方法，在线等。',
    '整理了一下今年的计划，发现能完成的可能只有三分之一。',
    '给视频加了个新的片头，调了两天，最后发现还是原来的好。',
    '这期讲一下我平时用的工具链，不恰饭，纯分享。',
    '下雨天最适合在家剪片子，配一杯热咖啡。',
    '第一次尝试做菜，成品看起来还行，味道就不好说了。',
    '把收藏夹里的教程清了一波，终于敢点开看了。',
    '凌晨三点的安静是真的，效率也是真的。',
    '有人问我为什么还在更新，可能只是因为习惯了。',
    '记录一下今天解决的一个奇怪的 bug，改完之后一身轻松。',
    '拍了一个很短的小视频，剪完只有 40 秒，但很喜欢。',
    '感谢每一个还在等更新的朋友，这次不会鸽太久。',
    '换季了，终于可以穿外套了，虽然南方依旧很热。',
  ];

  const VIDEO_TITLES = [
    '从零开始做一个桌面小工具：完整开发记录',
    '十分钟讲清楚 Python 异步编程到底在异步什么',
    '我花了三个月，把老家的院子改成了工作室',
    '这可能是我今年看过最好的科幻短片',
    '深度测评：这台两千块的显示器值不值得买',
    '用一周时间，复刻一道失传的家常菜',
    '为什么你的代码总是越写越乱？',
    '在县城开一家书店，是种什么体验',
    '从入门到放弃：我的健身第三年',
    '一个人做一款独立游戏需要多久',
    '拆解一台二十年前的笔记本电脑',
    '这些冷门但好用的剪辑技巧，你可能真的不知道',
    '凌晨四点的菜市场，比想象中热闹',
    '我搬到了山脚下，开始一种很慢的生活',
    '重新学习一门语言，三十岁也不算晚',
  ];

  const FAV_TITLES = [
    '【4K】一个人旅行，从西北到西南',
    'Python 异步编程完全指南（含源码）',
    '从零开始做一个自己的博客',
    '这可能是今年最好的科幻短片',
    '十分钟看懂 TypeScript 类型体操',
    '老房子改造全记录：从毛坯到入住',
    '如何用三个月学会一门外语',
    '相机入门：光圈、快门和感光度',
    '我把一整年的 vlog 剪成了十分钟',
    '一锅到底的懒人晚餐做法',
    '备考经验分享：从 0 到 400+',
    '拆解一台十年前的手机',
    '在家就能做的健身计划',
    '理财入门：先搞懂这三个概念',
    '十分钟学会视频调色',
    '独立游戏开发日志 #12',
    '拍一组秋天的人像照片',
    '和爸妈一起去看海',
    '摄影构图：比器材更重要的东西',
    '一本让我改变想法的书',
    '音乐制作入门：从零编曲',
    '给老电脑装一个轻量系统',
    '凌晨的书店，和一个陌生人聊了很久',
    '为什么我们需要慢下来',
    '记录一次失败的露营',
  ];

  function makeRng(seed) {
    let a = seed >>> 0;
    return function rng() {
      a |= 0;
      a = (a + 0x6d2b79f5) | 0;
      let t = Math.imul(a ^ (a >>> 15), 1 | a);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  function svgAvatar(char, seed) {
    const tones = ['#e8e8ed', '#dcdce1', '#e2e0da', '#d9dde1', '#e4e1dc', '#d6d6db', '#ece8e2'];
    const bg = tones[Math.abs(seed) % tones.length];
    const ch = String(char || 'B').slice(0, 1);
    const svg =
      `<svg xmlns="http://www.w3.org/2000/svg" width="88" height="88" viewBox="0 0 88 88">` +
      `<rect width="88" height="88" fill="${bg}"/>` +
      `<text x="44" y="45" text-anchor="middle" dominant-baseline="middle" ` +
      `font-family="-apple-system, PingFang SC, Microsoft YaHei, sans-serif" font-size="36" ` +
      `font-weight="500" fill="#1d1d1f">${escapeXml(ch)}</text></svg>`;
    return 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svg);
  }

  function fallbackAvatar(char) {
    return svgAvatar(char || 'B', 0);
  }

  function svgCover(seed) {
    const tones = ['#ececef', '#e3e0da', '#dfe3e6', '#e8e5df', '#e1e1e6'];
    const bg = tones[Math.abs(seed) % tones.length];
    const svg =
      `<svg xmlns="http://www.w3.org/2000/svg" width="320" height="200" viewBox="0 0 320 200">` +
      `<rect width="320" height="200" fill="${bg}"/>` +
      `<circle cx="160" cy="100" r="30" fill="none" stroke="#7a7a7a" stroke-width="2"/>` +
      `<path d="M152 86v28l24-14-24-14Z" fill="#7a7a7a"/></svg>`;
    return 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svg);
  }

  function demoQrDataUrl() {
    const n = 29;
    const cell = 6;
    const quiet = 3;
    const size = (n + quiet * 2) * cell;
    const modules = Array.from({ length: n }, () => new Array(n).fill(false));
    const reserved = Array.from({ length: n }, () => new Array(n).fill(false));
    const rng = makeRng(97531);

    const set = (x, y, value) => {
      if (x < 0 || y < 0 || x >= n || y >= n) return;
      modules[y][x] = !!value;
      reserved[y][x] = true;
    };
    const finder = (ox, oy) => {
      for (let y = 0; y < 7; y += 1) {
        for (let x = 0; x < 7; x += 1) {
          const edge = x === 0 || x === 6 || y === 0 || y === 6;
          const core = x >= 2 && x <= 4 && y >= 2 && y <= 4;
          set(ox + x, oy + y, edge || core);
        }
      }
    };
    finder(0, 0);
    finder(n - 7, 0);
    finder(0, n - 7);
    for (let i = 8; i < n - 8; i += 1) {
      set(i, 6, i % 2 === 0);
      set(6, i, i % 2 === 0);
    }
    for (let y = 0; y < n; y += 1) {
      for (let x = 0; x < n; x += 1) {
        if (!reserved[y][x] && rng() < 0.47) modules[y][x] = true;
      }
    }

    let rects = '';
    for (let y = 0; y < n; y += 1) {
      for (let x = 0; x < n; x += 1) {
        if (modules[y][x]) {
          rects += `<rect x="${(x + quiet) * cell}" y="${(y + quiet) * cell}" width="${cell}" height="${cell}"/>`;
        }
      }
    }
    const svg =
      `<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}" ` +
      `viewBox="0 0 ${size} ${size}"><rect width="${size}" height="${size}" fill="#ffffff"/>` +
      `<g fill="#1d1d1f">${rects}</g></svg>`;
    return 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svg);
  }

  function initDemoData() {
    if (demo.ready) return;
    const rng = makeRng(20261102);
    const now = nowSec();

    /* ---- follows: 200 items ---- */
    const usedNames = new Set();
    for (let i = 0; i < 200; i += 1) {
      let name = '';
      let guard = 0;
      do {
        name =
          NAME_STEMS[Math.floor(rng() * NAME_STEMS.length)] +
          NAME_SUFFIXES[Math.floor(rng() * NAME_SUFFIXES.length)];
        guard += 1;
      } while (usedNames.has(name) && guard < 16);
      if (usedNames.has(name)) name = `${name}${i}`;
      usedNames.add(name);

      const categoryRoll = rng();
      const category = categoryRoll < 0.12 ? null : CATEGORIES[Math.floor(rng() * CATEGORIES.length)];
      const groups = [];
      const groupIds = [];
      if (rng() < 0.52) {
        const g1 = GROUP_DEFS[Math.floor(rng() * GROUP_DEFS.length)];
        groups.push(g1.name);
        groupIds.push(g1.tagid);
        if (rng() < 0.24) {
          const g2 = GROUP_DEFS[Math.floor(rng() * GROUP_DEFS.length)];
          if (g2.tagid !== g1.tagid) {
            groups.push(g2.name);
            groupIds.push(g2.tagid);
          }
        }
      }

      const mutual = rng() < 0.18;
      demo.follows.push({
        mid: 100000 + i * 7 + Math.floor(rng() * 5),
        name,
        sign: SIGNS[Math.floor(rng() * SIGNS.length)],
        face: svgAvatar(name.slice(0, 1), i),
        official: rng() < 0.09 ? OFFICIAL_TITLES[Math.floor(rng() * OFFICIAL_TITLES.length)] : '',
        attribute: mutual ? 6 : 2,
        mutual,
        groups,
        group_ids: groupIds,
        order: i + 1,
        mtime: now - Math.floor(rng() * 700) * 86400,
        category,
        category_source: category ? (rng() < 0.78 ? 'rule' : 'llm') : null,
      });
    }
    recomputeDemoGroups();

    /* ---- dynamics: 60 items ---- */
    for (let i = 0; i < 60; i += 1) {
      const type = DYNAMIC_TYPE_POOL[Math.floor(rng() * DYNAMIC_TYPE_POOL.length)];
      const isVideo = ['DYNAMIC_TYPE_AV', 'DYNAMIC_TYPE_ARTICLE', 'DYNAMIC_TYPE_PGC'].includes(type);
      const isForward = type === 'DYNAMIC_TYPE_FORWARD';
      demo.dynamics.push({
        id: '92' + String(100000000000 + i),
        type,
        type_label: DYNAMIC_LABELS[type] || '动态',
        pub_ts: now - Math.floor(2 + rng() * 88) * 86400 - Math.floor(rng() * 70000),
        text: DYNAMIC_TEXTS[Math.floor(rng() * DYNAMIC_TEXTS.length)],
        has_image: ['DYNAMIC_TYPE_DRAW', 'DYNAMIC_TYPE_FORWARD', 'DYNAMIC_TYPE_WORD'].includes(type)
          ? rng() < 0.72
          : rng() < 0.12,
        video: isVideo
          ? {
              bvid: 'BV1' + Math.floor(rng() * 1e9).toString(36).toUpperCase().slice(0, 8),
              title: VIDEO_TITLES[Math.floor(rng() * VIDEO_TITLES.length)],
            }
          : null,
        orig_author: isForward ? demo.follows[Math.floor(rng() * demo.follows.length)].name : null,
      });
    }
    demo.dynamics.sort((a, b) => b.pub_ts - a.pub_ts);

    /* ---- favorites: 3 folders ---- */
    const folderDefs = [
      { media_id: 916673043, title: '默认收藏夹', is_default: true, privacy: 0, count: 64 },
      { media_id: 916673044, title: '学习资料', is_default: false, privacy: 0, count: 36 },
      { media_id: 916673045, title: '稍后补看', is_default: false, privacy: 1, count: 20 },
    ];
    folderDefs.forEach((folder, folderIndex) => {
      const items = [];
      for (let i = 0; i < folder.count; i += 1) {
        const type = rng() < 0.16 ? 12 : 2;
        items.push({
          id: String(117528844000000 + folderIndex * 1000 + i),
          type,
          title:
            FAV_TITLES[(i + folderIndex * 5) % FAV_TITLES.length] +
            (rng() < 0.28 ? ` · 第 ${i + 1} 集` : ''),
          upper: demo.follows[Math.floor(rng() * demo.follows.length)].name,
          bvid: type === 2 ? 'BV1' + Math.floor(rng() * 1e9).toString(36).toUpperCase().slice(0, 8) : '',
          cover: svgCover(i + folderIndex * 11),
          fav_time: now - Math.floor(rng() * 420) * 86400,
          invalid: rng() < 0.07,
        });
      }
      items.sort((a, b) => b.fav_time - a.fav_time);
      demo.folders.push({ ...folder });
      demo.favItems[folder.media_id] = items;
    });

    /* ---- toview: 40 items ---- */
    for (let i = 0; i < 40; i += 1) {
      const duration = 120 + Math.floor(rng() * 3300);
      demo.toview.push({
        aid: 800000000 + i * 13,
        title: FAV_TITLES[Math.floor(rng() * FAV_TITLES.length)],
        upper: demo.follows[Math.floor(rng() * demo.follows.length)].name,
        duration,
        progress: rng() < 0.34 ? Math.floor(rng() * duration) : 0,
        add_ts: now - Math.floor(1 + rng() * 120) * 86400,
        bvid: 'BV1' + Math.floor(rng() * 1e9).toString(36).toUpperCase().slice(0, 8),
      });
    }

    /* ---- logs + backups + settings ---- */
    const logPool = [
      ['info', '已加载本地设置'],
      ['info', '检测到 B 站登录态（演示）'],
      ['warn', '请求较慢，已自动增加间隔'],
      ['info', '备份完成：CSV + JSONL 已写入本机目录'],
      ['info', '任务完成：批量取关（12 人）'],
      ['warn', '触发风控冷却，暂停 10 分钟'],
      ['error', '一条写操作失败，已跳过并记录'],
      ['info', '关注分类已按规则更新'],
      ['info', '清理失效收藏：跳过 3 条不可见内容'],
      ['info', '已清空稍后再看（演示）'],
    ];
    for (let i = 0; i < 28; i += 1) {
      const entry = logPool[Math.floor(rng() * logPool.length)];
      const created = new Date((now - (28 - i) * 1800) * 1000);
      demo.logs.push({
        ts: isoLocal(created),
        level: entry[0],
        msg: `${entry[1]}（演示数据 #${i + 1}）`,
        task_id: rng() < 0.4 ? `demo${String(1 + Math.floor(rng() * 3)).padStart(4, '0')}` : null,
      });
    }
    demo.backups = [
      {
        path: 'C:\\Users\\Demo\\AppData\\Roaming\\BiliCleaner\\backups\\1000001\\20261102-101530',
        task_id: 'demo0002',
        count: 12,
        created_at: isoLocal(new Date((now - 5 * 3600) * 1000)),
      },
      {
        path: 'C:\\Users\\Demo\\AppData\\Roaming\\BiliCleaner\\backups\\1000001\\20261101-214402',
        task_id: 'demo0001',
        count: 64,
        created_at: isoLocal(new Date((now - 24 * 3600) * 1000)),
      },
    ];
    demo.settings = clone(DEFAULT_SETTINGS);
    demo.ready = true;
  }

  function recomputeDemoGroups() {
    demo.groups = GROUP_DEFS.map((g) => ({
      tagid: g.tagid,
      name: g.name,
      count: demo.follows.filter((item) => item.group_ids.includes(g.tagid)).length,
    }));
  }

  function demoFollowStats() {
    const categories = {};
    let mutual = 0;
    let uncategorized = 0;
    CATEGORIES.forEach((name) => {
      categories[name] = 0;
    });
    demo.follows.forEach((item) => {
      if (item.mutual) mutual += 1;
      if (!item.category) uncategorized += 1;
      else if (Object.prototype.hasOwnProperty.call(categories, item.category)) {
        categories[item.category] += 1;
      }
    });
    return { total: demo.follows.length, mutual, uncategorized, categories };
  }

  function demoTaskTotal(type, params) {
    const p = params || {};
    switch (type) {
      case 'unfollow':
        return (p.mids || []).length;
      case 'dynamic_delete':
        return (p.ids || []).length;
      case 'favorite_clear': {
        if (Array.isArray(p.ids) && p.ids.length) return p.ids.length;
        const list = demo.favItems[p.folder_id] || [];
        return list.length;
      }
      case 'favorite_clean_invalid': {
        const list = demo.favItems[p.folder_id] || [];
        return list.filter((item) => item.invalid).length;
      }
      case 'toview_clear':
        return demo.toview.length;
      case 'classify_llm':
        return (p.mids || []).length;
      default:
        return 0;
    }
  }

  function demoTickTask(task) {
    const timer = setInterval(() => {
      if (task.status !== 'running') return;
      const step = task.total > 40 ? 2 : 1;
      task.done = Math.min(task.total, task.done + step);
      if (task.type === 'toview_clear' || task.type === 'favorite_clean_invalid') {
        if (task.done > 0 && task.failed < 3 && Math.random() < 0.12) task.failed += 1;
      }
      task.updated_at = isoLocal();
      if (task.done >= task.total) {
        task.status = 'done';
        task.message = '演示任务已完成';
        task.updated_at = isoLocal();
        clearInterval(timer);
        demo.taskTimers.delete(task.id);
        applyDemoTaskEffect(task);
      }
    }, 700);
    demo.taskTimers.set(task.id, timer);
  }

  function applyDemoTaskEffect(task) {
    const p = task.params || {};
    if (task.type === 'unfollow') {
      const mids = new Set(p.mids || []);
      demo.follows = demo.follows.filter((item) => !mids.has(item.mid));
      recomputeDemoGroups();
    } else if (task.type === 'dynamic_delete') {
      const ids = new Set(p.ids || []);
      demo.dynamics = demo.dynamics.filter((item) => !ids.has(item.id));
    } else if (task.type === 'favorite_clear') {
      const list = demo.favItems[p.folder_id] || [];
      if (Array.isArray(p.ids) && p.ids.length) {
        const keys = new Set(p.ids);
        demo.favItems[p.folder_id] = list.filter((item) => !keys.has(`${item.id}:${item.type}`));
      } else {
        demo.favItems[p.folder_id] = [];
      }
      const folder = demo.folders.find((f) => f.media_id === Number(p.folder_id));
      if (folder) folder.count = (demo.favItems[folder.media_id] || []).length;
    } else if (task.type === 'favorite_clean_invalid') {
      const list = demo.favItems[p.folder_id] || [];
      demo.favItems[p.folder_id] = list.filter((item) => !item.invalid);
      const folder = demo.folders.find((f) => f.media_id === Number(p.folder_id));
      if (folder) folder.count = (demo.favItems[folder.media_id] || []).length;
    } else if (task.type === 'toview_clear') {
      demo.toview = [];
    }
    demo.logs.push({
      ts: isoLocal(),
      level: 'info',
      msg: `任务完成：${TASK_TYPE_LABELS[task.type] || task.type}（成功 ${Math.max(
        0,
        task.done - task.failed
      )} · 失败 ${task.failed}）`,
      task_id: task.id,
    });
  }

  const DEMO_TASK_API = {
    create(body) {
      const type = body && body.type ? String(body.type) : 'unfollow';
      const params = (body && body.params) || {};
      const total = demoTaskTotal(type, params);
      const task = {
        id: 'demo' + String(demo.seq++).padStart(4, '0'),
        type,
        status: 'running',
        total,
        done: 0,
        failed: 0,
        created_at: isoLocal(),
        updated_at: isoLocal(),
        cooldown_until: null,
        message: '演示任务运行中',
        backup_path:
          type === 'classify_llm'
            ? null
            : `C:\\Users\\Demo\\AppData\\Roaming\\BiliCleaner\\backups\\1000001\\${isoLocal().replace(/[-:]/g, '')}`,
        params,
      };
      demo.tasks.unshift(task);
      demo.logs.push({
        ts: isoLocal(),
        level: 'info',
        msg: `任务开始：${TASK_TYPE_LABELS[type] || type}（${total} 项）`,
        task_id: task.id,
      });
      demoTickTask(task);
      return task;
    },
    find(id) {
      return demo.tasks.find((t) => t.id === id) || null;
    },
    action(id, action) {
      const task = this.find(id);
      if (!task) throw new ApiError(404, 'NOT_FOUND', '任务不存在');
      if (action === 'pause') {
        if (task.status === 'running' || task.status === 'cooldown') {
          task.status = 'paused';
          task.message = '已暂停（演示）';
        }
      } else if (action === 'resume') {
        if (task.status === 'paused') {
          task.status = 'running';
          task.message = '演示任务运行中';
        }
      } else if (action === 'stop') {
        if (!['done', 'failed', 'stopped'].includes(task.status)) {
          task.status = 'stopped';
          task.message = '已停止（演示）';
          const timer = demo.taskTimers.get(id);
          if (timer) {
            clearInterval(timer);
            demo.taskTimers.delete(id);
          }
        }
      }
      task.updated_at = isoLocal();
      return task;
    },
  };

  async function demoRequest(method, path, body) {
    if (!demo.ready) initDemoData();
    const url = new URL(path, 'http://demo.local');
    const p = url.pathname;
    const q = url.searchParams;
    const m = String(method || 'GET').toUpperCase();

    /* auth */
    if (p === '/api/health') return { ok: true, version: '0.1.0-demo', mock: true };
    if (p === '/api/me') return { logged_in: demo.loggedIn, user: demo.loggedIn ? DEMO_USER : null };
    if (p === '/api/auth/qrcode' && m === 'POST') {
      demo.qrConfirmed = false;
      return {
        url: 'https://passport.bilibili.com/h5-app/passport/login/scan',
        key: 'demo' + String(Date.now()),
        expires_in: 180,
        qr_png: demoQrDataUrl(),
      };
    }
    if (p === '/api/auth/qrcode/poll') {
      if (demo.qrConfirmed) return { status: 'confirmed', user: DEMO_USER };
      return { status: 'waiting', user: null };
    }
    if (p === '/api/auth/logout') {
      demo.loggedIn = false;
      return { ok: true };
    }
    if (p === '/api/shutdown' && m === 'POST') {
      return { ok: true };
    }

    /* follows */
    if (p === '/api/follows/stats') return demoFollowStats();
    if (p === '/api/follows/groups') return { items: demo.groups.map((g) => ({ ...g })) };
    if (p === '/api/follows/classify' && m === 'POST') {
      let updated = 0;
      demo.follows.forEach((item, index) => {
        if (!item.category) {
          const roll = makeRng(index + 7)();
          item.category = CATEGORIES[Math.floor(roll * CATEGORIES.length)];
          item.category_source = 'rule';
          updated += 1;
        }
      });
      demo.logs.push({
        ts: isoLocal(),
        level: 'info',
        msg: `规则分类完成：更新 ${updated} 人的分类`,
        task_id: null,
      });
      return { updated };
    }
    if (p === '/api/follows' && m === 'GET') {
      const offset = Number(q.get('offset') || 0);
      const limit = Number(q.get('limit') || 24);
      const keyword = (q.get('q') || '').trim().toLowerCase();
      const category = q.get('category') || '';
      const group = q.get('group') || '';
      const mutual = q.get('mutual');
      let items = demo.follows.slice();
      if (keyword) {
        items = items.filter(
          (item) =>
            String(item.name).toLowerCase().includes(keyword) ||
            String(item.sign).toLowerCase().includes(keyword)
        );
      }
      if (category === '__none__') items = items.filter((item) => !item.category);
      else if (category) items = items.filter((item) => item.category === category);
      if (group === '0') items = items.filter((item) => !item.group_ids.length);
      else if (group) items = items.filter((item) => item.group_ids.map(String).includes(group));
      if (mutual === '1') items = items.filter((item) => item.mutual);
      else if (mutual === '0') items = items.filter((item) => !item.mutual);
      return { total: items.length, offset, items: items.slice(offset, offset + limit) };
    }

    /* dynamics */
    if (p === '/api/dynamics' && m === 'GET') {
      const offset = Number(q.get('offset') || 0) || 0;
      const limit = Number(q.get('limit') || 20);
      const keyword = (q.get('q') || '').trim().toLowerCase();
      const type = q.get('type') || '';
      const dateFrom = q.get('date_from') || '';
      const dateTo = q.get('date_to') || '';
      let items = demo.dynamics.slice();
      if (keyword) {
        items = items.filter(
          (item) =>
            String(item.text).toLowerCase().includes(keyword) ||
            (item.video && String(item.video.title).toLowerCase().includes(keyword))
        );
      }
      if (type) items = items.filter((item) => item.type === type);
      if (dateFrom) {
        const from = new Date(dateFrom + 'T00:00:00').getTime() / 1000;
        items = items.filter((item) => item.pub_ts >= from);
      }
      if (dateTo) {
        const to = new Date(dateTo + 'T23:59:59').getTime() / 1000;
        items = items.filter((item) => item.pub_ts <= to);
      }
      const page = items.slice(offset, offset + limit);
      const next = offset + page.length;
      return {
        offset: next < items.length ? String(next) : '',
        has_more: next < items.length,
        items: page,
      };
    }

    /* favorites */
    if (p === '/api/favorites/folders' && m === 'GET') {
      return { items: demo.folders.map((f) => ({ ...f })) };
    }
    const favMatch = p.match(/^\/api\/favorites\/(\d+)\/items$/);
    if (favMatch && m === 'GET') {
      const mediaId = Number(favMatch[1]);
      const offset = Number(q.get('offset') || 0);
      const limit = Number(q.get('limit') || 18);
      const keyword = (q.get('q') || '').trim().toLowerCase();
      let items = (demo.favItems[mediaId] || []).slice();
      if (keyword) {
        items = items.filter(
          (item) =>
            String(item.title).toLowerCase().includes(keyword) ||
            String(item.upper).toLowerCase().includes(keyword)
        );
      }
      return { total: items.length, offset, items: items.slice(offset, offset + limit) };
    }

    /* toview */
    if (p === '/api/toview' && m === 'GET') {
      const offset = Number(q.get('offset') || 0);
      const limit = Number(q.get('limit') || 20);
      return { total: demo.toview.length, items: demo.toview.slice(offset, offset + limit) };
    }

    /* tasks */
    if (p === '/api/tasks' && m === 'POST') return DEMO_TASK_API.create(body);
    if (p === '/api/tasks' && m === 'GET') {
      return { items: demo.tasks.map((task) => ({ ...task })) };
    }
    const taskActionMatch = p.match(/^\/api\/tasks\/([^/]+)\/(pause|resume|stop)$/);
    if (taskActionMatch) return { ...DEMO_TASK_API.action(decodeURIComponent(taskActionMatch[1]), taskActionMatch[2]) };
    const taskMatch = p.match(/^\/api\/tasks\/([^/]+)$/);
    if (taskMatch) {
      const task = DEMO_TASK_API.find(decodeURIComponent(taskMatch[1]));
      if (!task) throw new ApiError(404, 'NOT_FOUND', '任务不存在');
      return { ...task };
    }

    /* misc */
    if (p === '/api/backups' && m === 'GET') {
      return { items: demo.backups.map((item) => ({ ...item })) };
    }
    if (p === '/api/logs' && m === 'GET') {
      const limit = Number(q.get('limit') || 200);
      const taskId = q.get('task_id') || '';
      let items = demo.logs.slice();
      if (taskId) items = items.filter((entry) => entry.task_id === taskId);
      return { items: items.slice(-limit) };
    }
    if (p === '/api/settings' && (m === 'GET' || m === 'PUT')) {
      if (m === 'PUT') {
        demo.settings = normalizeSettings({ ...demo.settings, ...(body || {}), llm: { ...demo.settings.llm, ...((body && body.llm) || {}) } });
      }
      return clone(demo.settings);
    }

    throw new ApiError(404, 'NOT_FOUND', `演示模式没有实现 ${m} ${p}`);
  }

  function enableDemo(reason) {
    const wasDemo = DEMO;
    if (!demo.ready) initDemoData();
    DEMO = true;
    if (!wasDemo && reason) toast(reason);
    updateNav();
  }

  function normalizeSettings(raw) {
    const merged = clone(DEFAULT_SETTINGS);
    const source = raw && typeof raw === 'object' ? raw : {};
    Object.keys(merged).forEach((key) => {
      if (key === 'llm') return;
      if (key === 'confirm_phrase') {
        merged.confirm_phrase = String(source.confirm_phrase || DEFAULT_SETTINGS.confirm_phrase);
        return;
      }
      if (key === 'protect_mutual') {
        merged.protect_mutual = source.protect_mutual !== undefined ? !!source.protect_mutual : true;
        return;
      }
      const n = Number(source[key]);
      merged[key] = Number.isFinite(n) ? n : DEFAULT_SETTINGS[key];
    });
    const llm = source.llm && typeof source.llm === 'object' ? source.llm : {};
    merged.llm = {
      enabled: !!llm.enabled,
      base_url: String(llm.base_url || ''),
      api_key: String(llm.api_key || ''),
      model: String(llm.model || ''),
      batch_size: Number.isFinite(Number(llm.batch_size)) ? Number(llm.batch_size) : 20,
    };
    return merged;
  }

  /* ======================================================= 4. auth / QR */

  const qrState = { key: null, timer: null, checking: false, expired: false, generation: 0 };

  const QR_STATUS_TEXT = {
    loading: '正在生成二维码…',
    waiting: '等待扫码',
    scanned: '已扫码，请在手机上确认登录',
    confirmed: '登录成功，正在进入…',
    expired: '二维码已过期',
    error: '二维码获取失败',
  };

  function setQrStatus(kind, text) {
    const wrap = $('#qr-status');
    const label = $('#qr-status-text');
    if (!wrap || !label) return;
    wrap.classList.toggle('is-active', kind === 'waiting' || kind === 'scanned' || kind === 'confirmed');
    wrap.classList.toggle('is-muted', kind === 'loading' || kind === 'expired' || kind === 'error');
    label.textContent = text || QR_STATUS_TEXT[kind] || '';
  }

  function showQrMask(title, desc) {
    const mask = $('#qr-mask');
    if (!mask) return;
    $('#qr-mask-title').textContent = title;
    $('#qr-mask-desc').textContent = desc;
    mask.hidden = false;
  }

  function hideQrMask() {
    const mask = $('#qr-mask');
    if (mask) mask.hidden = true;
  }

  function stopQrPolling() {
    if (qrState.timer) {
      clearInterval(qrState.timer);
      qrState.timer = null;
    }
  }

  async function startQrLogin() {
    stopQrPolling();
    qrState.key = null;
    qrState.expired = false;
    const generation = ++qrState.generation;
    hideQrMask();
    setQrStatus('loading');
    const refresh = $('#qr-refresh');
    if (refresh) refresh.disabled = true;
    try {
      const data = await api.post('/api/auth/qrcode', {});
      if (generation !== qrState.generation) return;
      qrState.key = data && data.key ? data.key : null;
      const img = $('#qr-image');
      if (img) {
        if (data && data.qr_png) {
          img.src = data.qr_png;
          img.hidden = false;
        } else {
          img.removeAttribute('src');
          img.hidden = true;
        }
      }
      setQrStatus('waiting');
      qrState.timer = setInterval(pollQr, 2000);
    } catch (err) {
      if (err && err.status === 401) return;
      setQrStatus('error');
      showQrMask('二维码获取失败', '请确认本地服务已启动，然后点击刷新');
    } finally {
      if (refresh) refresh.disabled = false;
    }
  }

  async function pollQr() {
    if (!qrState.key || qrState.checking) return;
    qrState.checking = true;
    try {
      const res = await api.get('/api/auth/qrcode/poll?key=' + encodeURIComponent(qrState.key));
      const status = res && res.status ? res.status : 'waiting';
      if (status === 'confirmed') {
        stopQrPolling();
        setQrStatus('confirmed', res.user && res.user.name ? `登录成功：${res.user.name}` : '登录成功');
        await onLoggedIn(res.user);
      } else if (status === 'scanned') {
        setQrStatus('scanned');
      } else if (status === 'expired') {
        stopQrPolling();
        qrState.expired = true;
        setQrStatus('expired');
        showQrMask('二维码已过期', '点击下方按钮重新获取');
      } else {
        setQrStatus('waiting');
      }
    } catch (err) {
      if (err && err.status === 401) return;
      setQrStatus('error', '轮询登录状态失败，稍后自动重试');
    } finally {
      qrState.checking = false;
    }
  }

  async function onLoggedIn(user) {
    state.loggedIn = true;
    state.user = user || (DEMO ? DEMO_USER : null);
    demo.loggedIn = true;
    updateNav();
    toast(state.user && state.user.name ? `已登录：${state.user.name}` : '登录成功');
    navigate('dynamics');
    startTaskPolling();
    try {
      const settings = await api.get('/api/settings');
      state.settings = normalizeSettings(settings);
      state.settingsLoaded = true;
    } catch (_) {
      /* settings are non-critical here */
    }
  }

  async function logout() {
    try {
      await api.post('/api/auth/logout', {});
    } catch (_) {
      /* even if the call fails, drop local state */
    }
    state.loggedIn = false;
    state.user = null;
    demo.loggedIn = false;
    stopTaskPolling();
    state.tasks = [];
    renderTaskDock();
    updateNav();
    toast('已退出登录');
    navigate('login');
  }

  function demoSimulateLogin() {
    demo.qrConfirmed = true;
    pollQr().catch(() => {
      onLoggedIn(DEMO_USER);
    });
  }

  /* ============================================= 5. shared chrome / UI */

  function toast(message, options) {
    const root = $('#toast-root');
    if (!root) return;
    const opts = options || {};
    const el = document.createElement('div');
    el.className = 'toast';
    el.textContent = String(message || '');
    root.appendChild(el);
    while (root.children.length > 3) root.removeChild(root.firstElementChild);
    requestAnimationFrame(() => el.classList.add('is-visible'));
    setTimeout(() => {
      el.classList.remove('is-visible');
      setTimeout(() => el.remove(), 260);
    }, opts.duration || 2600);
  }

  /* ---- dialog */

  let dialogState = null;

  function openDialog(opts) {
    const root = $('#modal-root');
    if (!root) return;
    dialogState = Object.assign({}, opts);
    const phrase = opts.phrase || '';
    root.innerHTML = `
      <div class="dialog" role="dialog" aria-modal="true" aria-labelledby="dialog-title">
        <h2 class="dialog__title" id="dialog-title">${esc(opts.title)}</h2>
        ${opts.body ? `<p class="dialog__body">${opts.body}</p>` : ''}
        ${opts.note ? `<p class="dialog__note">${opts.note}</p>` : ''}
        ${
          phrase
            ? `<div class="dialog__field">
                 <label class="dialog__label" for="dialog-phrase">输入“${esc(phrase)}”以确认</label>
                 <input class="dialog__input" id="dialog-phrase" type="text" autocomplete="off"
                        spellcheck="false" placeholder="${escAttr(phrase)}">
                 <span class="dialog__hint">这是防止误操作的最后一步。</span>
               </div>`
            : ''
        }
        ${
          opts.ack
            ? `<div class="dialog__check">
                 <label class="check"><input type="checkbox" id="dialog-ack"><span class="check__box"></span></label>
                 <label for="dialog-ack">${opts.ack}</label>
               </div>`
            : ''
        }
        <div class="dialog__actions">
          <button class="btn btn--pearl btn--small" type="button" data-dialog-action="cancel">取消</button>
          <button class="btn ${opts.danger ? 'btn--danger' : 'btn--primary'} btn--small" type="button"
                  data-dialog-action="confirm" disabled>${esc(opts.confirmLabel || '确认')}</button>
        </div>
      </div>`;
    root.classList.add('is-open');
    root.setAttribute('aria-hidden', 'false');
    document.body.classList.add('is-dialog-open');
    syncDialogConfirm();
    setTimeout(() => {
      const input = $('#dialog-phrase');
      if (input) input.focus();
      else {
        const first = $('[data-dialog-action="cancel"]');
        if (first) first.focus();
      }
    }, 60);
  }

  function syncDialogConfirm() {
    if (!dialogState) return;
    const root = $('#modal-root');
    if (!root) return;
    const confirmBtn = root.querySelector('[data-dialog-action="confirm"]');
    if (!confirmBtn) return;
    const input = root.querySelector('#dialog-phrase');
    const ack = root.querySelector('#dialog-ack');
    let ok = true;
    if (dialogState.phrase) ok = !!input && input.value.trim() === dialogState.phrase;
    if (dialogState.ack) ok = ok && !!ack && ack.checked;
    confirmBtn.disabled = !ok || !!dialogState.loading;
  }

  async function confirmDialog(btn) {
    if (!dialogState) return;
    const current = dialogState;
    const root = $('#modal-root');
    const input = root ? root.querySelector('#dialog-phrase') : null;
    const ack = root ? root.querySelector('#dialog-ack') : null;
    if (current.phrase && (!input || input.value.trim() !== current.phrase)) return;
    if (current.ack && (!ack || !ack.checked)) return;
    current.loading = true;
    syncDialogConfirm();
    btn.classList.add('is-loading');
    {
      const rootEl = $('#modal-root');
      if (rootEl) rootEl.setAttribute('aria-busy', 'true');
    }
    try {
      await current.onConfirm({ phrase: current.phrase ? input.value.trim() : undefined });
      closeDialog();
    } catch (err) {
      current.loading = false;
      syncDialogConfirm();
      btn.classList.remove('is-loading');
      const rootEl = $('#modal-root');
      if (rootEl) rootEl.removeAttribute('aria-busy');
      handleError(err);
    }
  }

  function closeDialog() {
    dialogState = null;
    const root = $('#modal-root');
    if (!root) return;
    root.classList.remove('is-open');
    root.setAttribute('aria-hidden', 'true');
    root.innerHTML = '';
    document.body.classList.remove('is-dialog-open');
  }

  /* ---- action bar */

  function renderActionBar() {
    const bar = $('#action-bar');
    if (!bar) return;
    let html = '';

    if (state.route === 'settings' && state.loggedIn) {
      html = `
        <div class="action-bar__inner">
          <p class="action-bar__count">设置只保存在本机<span class="action-bar__hint"> · 修改后点击保存</span></p>
          <div class="action-bar__actions">
            <button class="btn btn--ghost btn--small" type="button" data-action="reset-settings">恢复默认</button>
            <button class="btn btn--primary btn--small" type="button" data-action="save-settings">保存设置</button>
          </div>
        </div>`;
    } else if (state.loggedIn) {
      if (state.route === 'follows' && state.follows.selected.size > 0) {
        const protect = state.settings.protect_mutual;
        html = `
          <div class="action-bar__inner">
            <p class="action-bar__count">已选 <strong>${fmtInt(state.follows.selected.size)}</strong> 人
              <span class="action-bar__hint">· 互关保护${protect ? '已开启' : '已关闭'}</span></p>
            <div class="action-bar__actions">
              <button class="btn btn--ghost btn--small" type="button" data-action="clear-follows">清除选择</button>
              <button class="btn btn--secondary btn--small" type="button" data-action="classify-llm">LLM 分类</button>
              <button class="btn btn--danger btn--small" type="button" data-action="unfollow-selected">取关选中</button>
            </div>
          </div>`;
      } else if (state.route === 'dynamics' && state.dynamics.selected.size > 0) {
        html = `
          <div class="action-bar__inner">
            <p class="action-bar__count">已选 <strong>${fmtInt(state.dynamics.selected.size)}</strong> 条动态</p>
            <div class="action-bar__actions">
              <button class="btn btn--ghost btn--small" type="button" data-action="clear-dynamics">清除选择</button>
              <button class="btn btn--danger btn--small" type="button" data-action="delete-dynamics">删除选中</button>
            </div>
          </div>`;
      } else if (state.route === 'favorites' && state.favorites.selected.size > 0) {
        html = `
          <div class="action-bar__inner">
            <p class="action-bar__count">已选 <strong>${fmtInt(state.favorites.selected.size)}</strong> 个内容</p>
            <div class="action-bar__actions">
              <button class="btn btn--ghost btn--small" type="button" data-action="clear-favs">清除选择</button>
              <button class="btn btn--danger btn--small" type="button" data-action="fav-unfavorite-selected">取消收藏选中</button>
            </div>
          </div>`;
      } else if (state.route === 'toview' && state.toview.selected.size > 0) {
        html = `
          <div class="action-bar__inner">
            <p class="action-bar__count">已选 <strong>${fmtInt(state.toview.selected.size)}</strong> 个视频
              <span class="action-bar__hint">· 仅支持整体清空</span></p>
            <div class="action-bar__actions">
              <button class="btn btn--ghost btn--small" type="button" data-action="clear-toview-selection">清除选择</button>
            </div>
          </div>`;
      }
    }

    if (!html) {
      bar.classList.remove('is-visible');
      bar.setAttribute('aria-hidden', 'true');
      bar.innerHTML = '';
      document.body.classList.remove('has-action-bar');
      return;
    }
    bar.innerHTML = html;
    bar.classList.add('is-visible');
    bar.setAttribute('aria-hidden', 'false');
    document.body.classList.add('has-action-bar');
  }

  /* ---- task dock */

  let dockTicker = null;
  let taskSnapshotReady = false;
  const taskSnapshot = new Map();
  let taskPollTimer = null;
  let taskPollBusy = false;

  function startTaskPolling() {
    if (taskPollTimer || !state.loggedIn) return;
    pollTasks({ silent: true });
    taskPollTimer = setInterval(() => pollTasks({ silent: true }), 2200);
  }

  function stopTaskPolling() {
    if (taskPollTimer) {
      clearInterval(taskPollTimer);
      taskPollTimer = null;
    }
  }

  async function pollTasks(options) {
    const opts = options || {};
    if (!state.loggedIn || state.route === 'login' || taskPollBusy) return;
    taskPollBusy = true;
    try {
      const data = await api.get('/api/tasks');
      const items = data && Array.isArray(data.items) ? data.items : [];
      handleTaskTransitions(items);
      state.tasks = items;
      renderTaskDock();
    } catch (err) {
      if (!opts.silent) handleError(err);
    } finally {
      taskPollBusy = false;
    }
  }

  function handleTaskTransitions(items) {
    let shouldRefresh = false;
    if (!taskSnapshotReady) {
      items.forEach((task) => taskSnapshot.set(task.id, task.status));
      taskSnapshotReady = true;
      return;
    }
    items.forEach((task) => {
      const prev = taskSnapshot.get(task.id);
      if (prev && prev !== task.status && ['done', 'failed', 'stopped'].includes(task.status)) {
        const failed = Number(task.failed) || 0;
        const success = Math.max(0, (Number(task.done) || 0) - failed);
        const label = TASK_TYPE_LABELS[task.type] || '任务';
        if (task.status === 'done') {
          toast(`${label}已完成 · 成功 ${fmtInt(success)}${failed ? ` · 失败 ${fmtInt(failed)}` : ''}`);
        } else if (task.status === 'failed') {
          toast(`${label}失败${task.message ? '：' + task.message : ''}`);
        } else {
          toast(`${label}已停止`);
        }
        shouldRefresh = true;
      }
      taskSnapshot.set(task.id, task.status);
    });
    if (shouldRefresh) refreshCurrentView();
  }

  function renderTaskDock() {
    const dock = $('#task-dock');
    if (!dock) return;
    const tasks = state.tasks || [];
    const active = tasks.find((task) =>
      ['running', 'cooldown', 'paused', 'queued'].includes(task.status)
    );
    if (!active) {
      dock.classList.add('is-hidden');
      dock.innerHTML = '';
      stopDockTicker();
      return;
    }

    const total = Number(active.total) || 0;
    const done = Number(active.done) || 0;
    const failed = Number(active.failed) || 0;
    const pct = total > 0 ? clamp(Math.round((done / total) * 100), 0, 100) : 0;
    const statusLabel = TASK_STATUS_LABELS[active.status] || active.status;
    const isMuted = active.status === 'paused' || active.status === 'queued';
    const queued = tasks.filter((task) => task.status === 'queued' && task.id !== active.id).length;
    const canPause = active.status === 'running' || active.status === 'cooldown';
    const canResume = active.status === 'paused';

    dock.innerHTML = `
      <div class="task-dock__head">
        <span class="task-dock__type">${esc(TASK_TYPE_LABELS[active.type] || '任务')}</span>
        <span class="task-dock__status">
          <span class="task-dock__dot${isMuted ? ' task-dock__dot--muted' : ''}" aria-hidden="true"></span>
          ${esc(statusLabel)}
        </span>
      </div>
      <div class="task-dock__nums">
        <span>已完成 <strong>${fmtInt(done)}</strong> / ${fmtInt(total)}</span>
        <span>${failed ? `失败 <strong>${fmtInt(failed)}</strong>` : '失败 0'}</span>
      </div>
      <div class="progress" role="progressbar" aria-valuemin="0" aria-valuemax="100"
           aria-valuenow="${pct}" aria-label="任务进度">
        <div class="progress__fill" style="width:${pct}%"></div>
      </div>
      ${active.status === 'cooldown' ? `<div class="task-dock__cool" data-cool-until="${escAttr(active.cooldown_until || '')}">风控冷却中<span data-cool-text>—</span></div>` : ''}
      <div class="task-dock__actions">
        ${canPause ? `<button class="btn btn--secondary btn--small" type="button" data-action="task-pause" data-id="${escAttr(active.id)}">暂停</button>` : ''}
        ${canResume ? `<button class="btn btn--primary btn--small" type="button" data-action="task-resume" data-id="${escAttr(active.id)}">继续</button>` : ''}
        <button class="btn btn--ghost btn--small" type="button" data-action="task-stop" data-id="${escAttr(active.id)}">停止</button>
      </div>
      ${active.message ? `<p class="task-dock__more">${esc(active.message)}</p>` : ''}
      ${queued ? `<p class="task-dock__more">另有 ${queued} 个任务排队中</p>` : ''}
    `;
    dock.classList.remove('is-hidden');
    updateCooldownLabels();
    startDockTicker();
  }

  function startDockTicker() {
    if (dockTicker) return;
    dockTicker = setInterval(updateCooldownLabels, 1000);
  }

  function stopDockTicker() {
    if (dockTicker) {
      clearInterval(dockTicker);
      dockTicker = null;
    }
  }

  function updateCooldownLabels() {
    $$('[data-cool-until]').forEach((el) => {
      const iso = el.getAttribute('data-cool-until');
      const textEl = el.querySelector('[data-cool-text]');
      if (!iso || !textEl) return;
      const until = new Date(iso).getTime();
      if (Number.isNaN(until)) {
        textEl.textContent = '';
        return;
      }
      const seconds = Math.max(0, Math.round((until - Date.now()) / 1000));
      const m = Math.floor(seconds / 60);
      const s = seconds % 60;
      textEl.textContent = seconds > 0 ? ` · 剩余 ${pad2(m)}:${pad2(s)}` : ' · 即将结束';
    });
  }

  async function taskAction(btn, action) {
    const id = btn.getAttribute('data-id');
    if (!id) return;
    await withBtn(btn, async () => {
      await api.post(`/api/tasks/${encodeURIComponent(id)}/${action}`, {});
      toast(action === 'pause' ? '任务已暂停' : action === 'resume' ? '任务已继续' : '任务已停止');
      await pollTasks({ silent: true });
    });
  }

  function onTaskCreated(task) {
    const label = task && task.type ? TASK_TYPE_LABELS[task.type] || '任务' : '任务';
    toast(
      `${label}任务已创建${task && task.total ? ` · 共 ${fmtInt(task.total)} 项` : ''}`
    );
    if (task && task.id) taskSnapshot.set(task.id, task.status || 'queued');
    startTaskPolling();
    pollTasks({ silent: true });
  }

  function refreshCurrentView() {
    const route = state.route;
    if (route === 'follows') {
      fetchFollowStats();
      fetchFollows(true);
    } else if (route === 'dynamics') {
      fetchDynamics(true);
    } else if (route === 'favorites') {
      forceReloadFavorites();
    } else if (route === 'toview') {
      fetchToview(true);
    } else if (route === 'logs') {
      fetchLogs();
    }
  }

  /* ==================================================== 6. shared markup */

  function heroTpl(opts) {
    return `
      <section class="hero${opts.parchment ? ' hero--parchment' : ''}">
        <div class="page-inner">
          <div class="hero__inner">
            <div class="hero__copy">
              <p class="eyebrow">${esc(opts.eyebrow)}</p>
              <h1>${esc(opts.title)}</h1>
            </div>
            ${opts.stats ? `<div class="hero__stats">${opts.stats}</div>` : ''}
          </div>
          ${opts.extra || ''}
        </div>
      </section>`;
  }

  function statTpl(num, label, id) {
    return `<div class="stat"><div class="stat__num"${id ? ` id="${escAttr(id)}"` : ''}>${num}</div><div class="stat__label">${esc(label)}</div></div>`;
  }

  function pageFooterTpl() {
    return `<footer class="page-footer"><div class="page-footer__inner">
      <span>BiliCleaner · 本地运行</span>
      <span>所有操作都由这台电脑发起 · 不上传任何数据</span>
    </div></footer>`;
  }

  function emptyTpl(opts) {
    return `<div class="empty">
      <div class="empty__mark">${opts.icon || EMPTY_ICONS.inbox}</div>
      <p class="empty__title">${esc(opts.title)}</p>
      <p class="empty__desc">${esc(opts.desc)}</p>
      ${
        opts.action
          ? `<div class="empty__actions"><button class="btn btn--primary btn--small" type="button" data-action="${escAttr(
              opts.action
            )}">${esc(opts.actionLabel)}</button></div>`
          : ''
      }
    </div>`;
  }

  function setToolsVisible(id, visible) {
    const el = document.getElementById(id);
    if (el) el.hidden = !visible;
  }

  function skeletonRows(count) {
    let html = '';
    for (let i = 0; i < count; i += 1) {
      html += `<div class="skeleton" aria-hidden="true">
        <div class="skeleton__block" style="width:18px;height:18px;border-radius:5px"></div>
        <div class="skeleton__block skeleton__block--avatar"></div>
        <div>
          <div class="skeleton__block skeleton__block--mid" style="margin-bottom:8px"></div>
          <div class="skeleton__block skeleton__block--short"></div>
        </div>
      </div>`;
    }
    return html;
  }

  /* ============================================================ 7. router */

  const ROUTES = ['login', 'dynamics', 'follows', 'favorites', 'toview', 'settings', 'logs'];
  const TAB_ROUTES = ['dynamics', 'follows', 'favorites', 'toview'];

  function currentRoute() {
    const raw = String(location.hash || '')
      .replace(/^#\/?/, '')
      .split('?')[0]
      .trim();
    return ROUTES.includes(raw) ? raw : 'dynamics';
  }

  function navigate(route) {
    const target = ROUTES.includes(route) ? route : 'dynamics';
    const hash = '#/' + target;
    if (location.hash === hash) {
      renderRoute();
      return;
    }
    location.hash = hash;
  }

  function updateSubNav() {
    $$('.tab').forEach((tab) => {
      tab.setAttribute('aria-selected', tab.dataset.route === state.route ? 'true' : 'false');
    });
    $$('.sub-nav__link').forEach((link) => {
      link.setAttribute('aria-current', link.dataset.route === state.route ? 'page' : 'false');
    });
  }

  function updateNav() {
    const mode = $('#nav-mode');
    if (mode) {
      if (DEMO) {
        mode.hidden = false;
        mode.textContent = demo.forced ? '演示模式' : '演示数据';
      } else if (state.mock) {
        mode.hidden = false;
        mode.textContent = 'Mock 服务';
      } else {
        mode.hidden = false;
        mode.textContent = '本地服务';
      }
    }
    const user = state.loggedIn ? state.user : null;
    const userWrap = $('#nav-user');
    if (userWrap) userWrap.hidden = !user;
    const logoutBtn = $('#nav-logout');
    if (logoutBtn) logoutBtn.hidden = !state.loggedIn;
    if (user) {
      const avatar = $('#nav-avatar');
      if (avatar) {
        // B站 CDN uses a RefererWhite anti-hotlink check: the <img> must be
        // no-referrer, and the fallback marker has to be cleared on every
        // account/route refresh or the initial "B" placeholder sticks forever.
        delete avatar.dataset.fallbackApplied;
        avatar.dataset.name = user.name || 'B';
        avatar.alt = user.name || 'B 站用户';
        avatar.src = user.face || fallbackAvatar((user.name || 'B').slice(0, 1));
      }
      setText('#nav-username', user.name || 'B 站用户');
    }
  }

  function renderRoute() {
    const route = currentRoute();
    const previous = state.route;
    state.route = route;
    const isLogin = route === 'login';

    const loginView = $('#view-login');
    const app = $('#app');
    if (loginView) loginView.hidden = !isLogin;
    if (app) app.hidden = isLogin;

    updateNav();

    if (isLogin) {
      stopTaskPolling();
      renderLoginPage();
    } else {
      stopQrPolling();
      updateSubNav();
      if (!state.loggedIn) {
        renderLoggedOut(route);
      } else {
        renderLoggedIn(route);
        startTaskPolling();
      }
    }

    if (previous !== route) {
      closeDialog();
      window.scrollTo(0, 0);
    }
    renderActionBar();
  }

  function renderLoginPage() {
    const demoBtn = $('#qr-demo');
    if (demoBtn) demoBtn.hidden = !DEMO;
    updateNav();
    startQrLogin();
  }

  function renderLoggedIn(route) {
    if (route === 'dynamics') renderDynamicsView();
    else if (route === 'follows') renderFollowsView();
    else if (route === 'favorites') renderFavoritesView();
    else if (route === 'toview') renderToviewView();
    else if (route === 'settings') renderSettingsView();
    else if (route === 'logs') renderLogsView();
  }

  const LOGGED_OUT_META = {
    dynamics: {
      eyebrow: '清理 · 动态',
      title: '动态',
      emptyTitle: '登录后才能清理动态',
      emptyDesc: '扫码登录后，这里会显示你的动态列表。登录信息只保存在本机。',
    },
    follows: {
      eyebrow: '管理 · 关注',
      title: '关注',
      emptyTitle: '登录后才能查看关注',
      emptyDesc: '扫码登录后，这里会显示你关注的 UP 主，并按分类统计。',
    },
    favorites: {
      eyebrow: '整理 · 收藏夹',
      title: '收藏夹',
      emptyTitle: '登录后才能整理收藏夹',
      emptyDesc: '扫码登录后，这里会列出你的收藏夹和其中的内容。',
    },
    toview: {
      eyebrow: '清理 · 稍后再看',
      title: '稍后再看',
      emptyTitle: '登录后才能清理稍后再看',
      emptyDesc: '扫码登录后，这里会显示你加入“稍后再看”的视频。',
    },
    settings: {
      eyebrow: '本地配置',
      title: '设置',
      emptyTitle: '登录后才能访问设置',
      emptyDesc: '设置保存在本机，登录后可以调整请求节奏与分类选项。',
    },
    logs: {
      eyebrow: '运行记录',
      title: '日志',
      emptyTitle: '登录后才能查看日志',
      emptyDesc: '登录后这里会显示本机的操作日志，方便排查问题。',
    },
  };

  function renderLoggedOut(route) {
    const meta = LOGGED_OUT_META[route] || LOGGED_OUT_META.dynamics;
    const root = $('#view-root');
    if (!root) return;
    root.innerHTML =
      heroTpl({
        eyebrow: meta.eyebrow,
        title: meta.title,
        parchment: route === 'follows' || route === 'favorites' || route === 'settings',
      }) +
      `<section class="section"><div class="page-inner">${emptyTpl({
        icon: EMPTY_ICONS.login,
        title: meta.emptyTitle,
        desc: meta.emptyDesc,
        action: 'go-login',
        actionLabel: '去扫码登录',
      })}</div></section>`;
    renderActionBar();
  }

  /* ========================================================== 8. follows */

  let followsSearchTimer = 0;

  function followPassesFilters(item) {
    const s = state.follows;
    if (s.category === '__none__') {
      if (item.category) return false;
    } else if (s.category && item.category !== s.category) {
      return false;
    }
    if (s.group === '0') {
      if ((item.group_ids || []).length) return false;
    } else if (s.group) {
      if (!(item.group_ids || []).map(String).includes(s.group)) return false;
    }
    if (s.mutual && !item.mutual) return false;
    return true;
  }

  function renderFollowsView() {
    const s = state.follows;
    const root = $('#view-root');
    if (!root) return;
    const stats = state.followStats;
    root.innerHTML =
      heroTpl({
        eyebrow: '管理 · 关注',
        title: '关注',
        parchment: true,
        stats:
          statTpl(stats ? fmtInt(stats.total) : '—', '关注总数', 'follows-stat-total') +
          statTpl(stats ? fmtInt(stats.mutual) : '—', '互关', 'follows-stat-mutual'),
      }) +
      `<div class="toolbar" role="search">
        <div class="page-inner toolbar__inner">
          <div class="search">
            ${SEARCH_ICON}
            <input class="search__input" id="follows-q" type="search" placeholder="搜索昵称或签名"
                   value="${escAttr(s.q)}" autocomplete="off">
          </div>
          <div class="select-wrap">
            <select class="select" id="follows-category" aria-label="按分类筛选">
              ${categoryOptionsTpl()}
            </select>
          </div>
          <div class="select-wrap">
            <select class="select" id="follows-group" aria-label="按分组筛选">
              <option value="">全部分组</option>
              ${state.groups
                .map(
                  (group) =>
                    `<option value="${group.tagid}"${s.group === String(group.tagid) ? ' selected' : ''}>${esc(
                      group.name
                    )}（${fmtInt(group.count)}）</option>`
                )
                .join('')}
              <option value="0"${s.group === '0' ? ' selected' : ''}>未分组</option>
            </select>
          </div>
          <label class="switch"><input type="checkbox" id="follows-mutual"${
            s.mutual ? ' checked' : ''
          }><span class="switch__track"></span>只看互关</label>
          <div class="toolbar__actions">
            <button class="btn btn--secondary btn--small" type="button" data-action="classify-rule">规则分类</button>
          </div>
        </div>
      </div>` +
      `<section class="section">
        <div class="page-inner">
          <div class="list-head">
            <p class="list-meta" id="follows-meta">加载中…</p>
            <div class="list-tools" id="follows-tools" hidden>
              <button class="btn btn--ghost btn--small" type="button" data-action="select-follows">全选本页</button>
              <button class="btn btn--ghost btn--small" type="button" data-action="invert-follows">反选</button>
              <button class="btn btn--ghost btn--small" type="button" data-action="clear-follows">清除选择</button>
            </div>
          </div>
          <div class="item-list" id="follows-list">${skeletonRows(6)}</div>
          <div class="list-footer" id="follows-footer"></div>
        </div>
      </section>`;

    if (!s.loaded) {
      fetchFollowStats();
      if (!state.groups.length) fetchGroups();
      fetchFollows(true);
    } else {
      renderFollowList();
      if (!state.groups.length) fetchGroups();
      if (!state.followStats) fetchFollowStats();
    }
    renderActionBar();
  }

  /* category dropdown carries the counts; no chip strip / stats line on the page */
  function categoryOptionsTpl() {
    const stats = state.followStats;
    const s = state.follows;
    const totalLabel = stats ? `全部分类 (${fmtInt(stats.total)})` : '全部分类';
    const options = [`<option value="">${esc(totalLabel)}</option>`];
    CATEGORIES.forEach((name) => {
      const count = stats && stats.categories ? Number(stats.categories[name] || 0) : null;
      if (stats && !count) return;
      const label = stats ? `${name} (${fmtInt(count)})` : name;
      options.push(
        `<option value="${escAttr(name)}"${s.category === name ? ' selected' : ''}>${esc(label)}</option>`
      );
    });
    const uncategorized = stats ? Number(stats.uncategorized || 0) : 0;
    if (!stats || uncategorized > 0 || s.category === '__none__') {
      options.push(
        `<option value="__none__"${s.category === '__none__' ? ' selected' : ''}>未分类${
          stats ? ` (${fmtInt(uncategorized)})` : ''
        }</option>`
      );
    }
    return options.join('');
  }

  function updateCategorySelect() {
    const select = $('#follows-category');
    if (!select) return;
    select.innerHTML = categoryOptionsTpl();
    if (state.follows.category) select.value = state.follows.category;
  }

  async function fetchFollowStats() {
    try {
      const data = await api.get('/api/follows/stats');
      state.followStats = {
        total: Number(data.total) || 0,
        mutual: Number(data.mutual) || 0,
        uncategorized: Number(data.uncategorized) || 0,
        categories: data.categories && typeof data.categories === 'object' ? data.categories : {},
      };
      if (state.route === 'follows') {
        updateCategorySelect();
        setText('#follows-stat-total', fmtInt(state.followStats.total));
        setText('#follows-stat-mutual', fmtInt(state.followStats.mutual));
      }
    } catch (err) {
      if (err && err.status === 401) return;
      /* stats are decorative — keep going */
    }
  }

  async function fetchGroups() {
    try {
      const data = await api.get('/api/follows/groups');
      state.groups = data && Array.isArray(data.items) ? data.items : [];
      if (state.route === 'follows') updateGroupSelect();
    } catch (err) {
      if (err && err.status === 401) return;
    }
  }

  function updateGroupSelect() {
    const select = $('#follows-group');
    if (!select) return;
    const current = state.follows.group;
    const options = ['<option value="">全部分组</option>']
      .concat(
        state.groups.map(
          (group) =>
            `<option value="${group.tagid}"${current === String(group.tagid) ? ' selected' : ''}>${esc(
              group.name
            )}（${fmtInt(group.count)}）</option>`
        )
      )
      .concat([`<option value="0"${current === '0' ? ' selected' : ''}>未分组</option>`]);
    select.innerHTML = options.join('');
  }

  async function fetchFollows(reset) {
    const s = state.follows;
    if (s.loading) return;
    s.loading = true;
    if (reset) {
      s.items = [];
      s.loaded = false;
    }
    if (state.route === 'follows') renderFollowList();
    try {
      const params = new URLSearchParams();
      params.set('offset', String(reset ? 0 : s.items.length));
      params.set('limit', String(LIMITS.follows));
      if (s.q.trim()) params.set('q', s.q.trim());
      if (s.category) params.set('category', s.category);
      if (s.group) params.set('group', s.group);
      if (s.mutual) params.set('mutual', '1');
      const data = await api.get('/api/follows?' + params.toString());
      const items = data && Array.isArray(data.items) ? data.items : [];
      s.items = reset ? items : s.items.concat(items);
      s.total = Number(data && data.total) || s.items.length;
      s.loaded = true;
    } catch (err) {
      if (reset) s.loaded = true;
      handleError(err);
    } finally {
      s.loading = false;
    }
    if (state.route === 'follows') {
      renderFollowList();
      if (!state.followStats) fetchFollowStats();
    }
  }

  function followRowTpl(item) {
    const selected = state.follows.selected.has(item.mid);
    const mutual = item.mutual ? '<span class="chip chip--mutual">互关</span>' : '';
    const official = item.official
      ? `<span class="chip chip--source">${esc(truncate(item.official, 16))}</span>`
      : '';
    const category = item.category
      ? `<span class="chip">${esc(item.category)}</span>`
      : '<span class="chip chip--source">未分类</span>';
    const groups = (item.groups || [])
      .slice(0, 2)
      .map((group) => `<span class="chip chip--source">${esc(group)}</span>`)
      .join('');
    const face = item.face || fallbackAvatar((item.name || 'B').slice(0, 1));
    return `<div class="item${selected ? ' is-selected' : ''}" data-mid="${item.mid}">
      <label class="check"><input type="checkbox" data-check-follow data-id="${item.mid}"${
      selected ? ' checked' : ''
    } aria-label="选择 ${escAttr(item.name)}"><span class="check__box"></span></label>
      <img class="avatar" src="${escAttr(face)}" data-name="${escAttr(item.name)}" alt=""
           loading="lazy" referrerpolicy="no-referrer">
      <div class="item__main">
        <div class="item__title-row">
          <p class="item__title">${esc(item.name)}</p>
          ${mutual}${official}
        </div>
        <p class="item__sign">${esc(item.sign || '这个人很懒，什么都没写。')}</p>
        <div class="item__meta">${category}${groups}</div>
      </div>
      <div class="item__aside">
        <span>关注顺序 <strong>#${item.order == null ? '—' : item.order}</strong></span>
        <span>${esc(fmtDate(item.mtime))}</span>
      </div>
    </div>`;
  }

  function renderFollowList() {
    const s = state.follows;
    const list = $('#follows-list');
    if (!list) return;
    if (!s.loaded) {
      list.innerHTML = skeletonRows(6);
    } else if (!s.items.length) {
      list.innerHTML = emptyTpl({
        icon: EMPTY_ICONS.search,
        title: '没有符合条件的关注',
        desc: '换个关键词、分类或分组试试，或者清空筛选条件再看。',
      });
    } else {
      list.innerHTML = s.items.map(followRowTpl).join('');
    }

    const meta = $('#follows-meta');
    if (meta) {
      meta.innerHTML = `已加载 <strong>${fmtInt(s.items.length)}</strong> / 共 <strong>${fmtInt(
        s.total
      )}</strong> 人${s.selected.size ? ` · 已选 <strong>${fmtInt(s.selected.size)}</strong> 人` : ''}`;
    }

    const footer = $('#follows-footer');
    if (footer) {
      if (s.loading && s.items.length) footer.innerHTML = '<span class="list-footer__done">正在加载…</span>';
      else if (s.items.length < s.total)
        footer.innerHTML =
          '<button class="btn btn--secondary btn--small" type="button" data-action="load-more-follows">加载更多</button>';
      else footer.innerHTML = s.items.length ? '<span class="list-footer__done">已经到底了</span>' : '';
    }
    setToolsVisible('follows-tools', !!s.loaded && s.items.length > 0);
    renderActionBar();
  }

  function toggleFollowSelection(input) {
    const mid = Number(input.getAttribute('data-id'));
    if (input.checked) state.follows.selected.add(mid);
    else state.follows.selected.delete(mid);
    updateFollowsMeta();
    renderActionBar();
  }

  function updateFollowsMeta() {
    const s = state.follows;
    const meta = $('#follows-meta');
    if (meta) {
      meta.innerHTML = `已加载 <strong>${fmtInt(s.items.length)}</strong> / 共 <strong>${fmtInt(
        s.total
      )}</strong> 人${s.selected.size ? ` · 已选 <strong>${fmtInt(s.selected.size)}</strong> 人` : ''}`;
    }
  }

  function selectAllFollows() {
    const s = state.follows;
    s.items.forEach((item) => {
      if (followPassesFilters(item)) s.selected.add(item.mid);
    });
    renderFollowList();
  }

  function invertFollows() {
    const s = state.follows;
    s.items.forEach((item) => {
      if (!followPassesFilters(item)) return;
      if (s.selected.has(item.mid)) s.selected.delete(item.mid);
      else s.selected.add(item.mid);
    });
    renderFollowList();
  }

  function clearFollowSelection() {
    state.follows.selected.clear();
    renderFollowList();
  }

  async function classifyByRule(btn) {
    await withBtn(btn, async () => {
      const data = await api.post('/api/follows/classify', {});
      toast(`规则分类完成 · 更新 ${fmtInt(data && data.updated)} 人`);
      await fetchFollowStats();
      await fetchFollows(true);
    });
  }

  function openUnfollowDialog() {
    const n = state.follows.selected.size;
    if (!n) return;
    const protect = !!state.settings.protect_mutual;
    const mids = Array.from(state.follows.selected);
    openDialog({
      title: `取关选中的 ${fmtInt(n)} 人？`,
      body: '取关后，这些人在你关注列表中的顺序会发生变化。B 站没有恢复原关注顺序的接口，这一步无法撤销。',
      note: protect
        ? '互关保护已开启：互关用户会被自动跳过，不会被取关。任务开始前会先在本地导出 CSV 与 JSONL 备份。'
        : '互关保护已关闭：互关用户也会被取关。任务开始前会先在本地导出 CSV 与 JSONL 备份。',
      ack: '我已了解取关后关注顺序不可恢复，并确认继续。',
      confirmLabel: '确认取关',
      danger: true,
      onConfirm: async () => {
        const task = await api.post('/api/tasks', {
          type: 'unfollow',
          params: { mids, reason: 'manual', ack_risk: true },
        });
        state.follows.selected.clear();
        renderFollowList();
        onTaskCreated(task);
      },
    });
  }

  function openLlmClassifyDialog() {
    const n = state.follows.selected.size;
    if (!n) return;
    const mids = Array.from(state.follows.selected);
    const llm = state.settings.llm || {};
    openDialog({
      title: `用 LLM 分类选中的 ${fmtInt(n)} 人？`,
      body: '会把选中的关注交给你在设置里配置的 LLM 接口判断，可能产生接口费用。没有配置时任务会失败。',
      note: llm.enabled
        ? `当前模型：${esc(llm.model || '未填写')} · 接口：${esc(truncate(llm.base_url || '未填写', 40))}`
        : 'LLM 分类尚未在设置中启用，请先到「设置」里填写接口信息并打开开关。',
      ack: '我确认使用自己的 LLM 接口额度执行这次分类。',
      confirmLabel: '开始分类',
      onConfirm: async () => {
        const task = await api.post('/api/tasks', {
          type: 'classify_llm',
          params: { mids },
        });
        state.follows.selected.clear();
        renderFollowList();
        onTaskCreated(task);
      },
    });
  }

  /* ========================================================== 9. dynamics */

  let dynamicsSearchTimer = 0;

  function renderDynamicsView() {
    const s = state.dynamics;
    const root = $('#view-root');
    if (!root) return;
    root.innerHTML =
      heroTpl({
        eyebrow: '清理 · 动态',
        title: '动态',
        parchment: true,
        stats:
          statTpl(fmtInt(s.items.length), '已加载', 'dynamics-stat-loaded') +
          statTpl(fmtInt(s.selected.size), '已选择', 'dynamics-stat-selected'),
      }) +
      `<div class="toolbar" role="search">
        <div class="page-inner toolbar__inner">
          <div class="search">
            ${SEARCH_ICON}
            <input class="search__input" id="dynamics-q" type="search" placeholder="搜索动态正文或视频标题"
                   value="${escAttr(s.q)}" autocomplete="off">
          </div>
          <div class="select-wrap">
            <select class="select" id="dynamics-type" aria-label="按类型筛选">
              <option value="">全部类型</option>
              ${DYNAMIC_FILTER_TYPES.map(
                ([value, label]) =>
                  `<option value="${value}"${s.type === value ? ' selected' : ''}>${esc(label)}</option>`
              ).join('')}
            </select>
          </div>
          <div class="date-wrap">
            <label for="dynamics-from">从</label>
            <input class="date-input" id="dynamics-from" type="date" value="${escAttr(s.from)}" aria-label="开始日期">
            <label for="dynamics-to">至</label>
            <input class="date-input" id="dynamics-to" type="date" value="${escAttr(s.to)}" aria-label="结束日期">
          </div>
          <div class="toolbar__actions">
            <button class="btn btn--ghost btn--small" type="button" data-action="clear-dynamics-filters">清除筛选</button>
          </div>
        </div>
      </div>` +
      `<section class="section">
        <div class="page-inner">
          <div class="list-head">
            <p class="list-meta" id="dynamics-meta">加载中…</p>
            <div class="list-tools" id="dynamics-tools" hidden>
              <button class="btn btn--ghost btn--small" type="button" data-action="select-dynamics">全选本页</button>
              <button class="btn btn--ghost btn--small" type="button" data-action="invert-dynamics">反选</button>
              <button class="btn btn--ghost btn--small" type="button" data-action="clear-dynamics">清除选择</button>
            </div>
          </div>
          <div class="item-list" id="dynamics-list">${skeletonRows(5)}</div>
          <div class="list-footer" id="dynamics-footer"></div>
        </div>
      </section>`;

    if (!s.loaded) fetchDynamics(true);
    else renderDynamicList();
  }

  function dynamicRowTpl(item) {
    const selected = state.dynamics.selected.has(item.id);
    const typeLabel = item.type_label || DYNAMIC_LABELS[item.type] || '动态';
    const videoTitle = item.video && item.video.title ? item.video.title : '';
    const text = item.text || '';
    const title = videoTitle || truncate(text, 60) || '（无正文）';
    const summary = text && videoTitle ? truncate(text, 140) : '';
    return `<div class="item${selected ? ' is-selected' : ''}" data-id="${escAttr(item.id)}">
      <label class="check"><input type="checkbox" data-check-dynamic data-id="${escAttr(
        item.id
      )}"${selected ? ' checked' : ''} aria-label="选择这条动态"><span class="check__box"></span></label>
      <div class="type-tile">${esc(typeLabel.slice(0, 2))}</div>
      <div class="item__main">
        <div class="item__title-row">
          <span class="chip chip--type">${esc(typeLabel)}</span>
          <p class="item__title">${esc(title)}</p>
        </div>
        ${summary ? `<p class="item__sign">${esc(summary)}</p>` : ''}
        <div class="item__meta">
          <span>${esc(fmtDateTime(item.pub_ts))}</span>
          ${item.orig_author ? `<span>转发自 ${esc(item.orig_author)}</span>` : ''}
          ${item.has_image ? '<span>含图片</span>' : ''}
          ${item.video && item.video.bvid ? `<span>${esc(item.video.bvid)}</span>` : ''}
        </div>
      </div>
      <div class="item__aside"><span>${esc(fmtDate(item.pub_ts))}</span></div>
    </div>`;
  }

  async function fetchDynamics(reset) {
    const s = state.dynamics;
    if (s.loading) return;
    s.loading = true;
    if (reset) {
      s.items = [];
      s.cursor = '';
      s.hasMore = true;
      s.loaded = false;
    }
    if (state.route === 'dynamics') renderDynamicList();
    try {
      const params = new URLSearchParams();
      params.set('limit', String(LIMITS.dynamics));
      if (!reset && s.cursor) params.set('offset', s.cursor);
      if (s.q.trim()) params.set('q', s.q.trim());
      if (s.type) params.set('type', s.type);
      if (s.from) params.set('date_from', s.from);
      if (s.to) params.set('date_to', s.to);
      const data = await api.get('/api/dynamics?' + params.toString());
      const items = data && Array.isArray(data.items) ? data.items : [];
      s.items = reset ? items : s.items.concat(items);
      s.cursor = data && data.offset ? String(data.offset) : '';
      s.hasMore = !!(data && data.has_more);
      s.loaded = true;
    } catch (err) {
      if (reset) s.loaded = true;
      handleError(err);
    } finally {
      s.loading = false;
    }
    if (state.route === 'dynamics') renderDynamicList();
  }

  function renderDynamicList() {
    const s = state.dynamics;
    const list = $('#dynamics-list');
    if (!list) return;
    if (!s.loaded) list.innerHTML = skeletonRows(5);
    else if (!s.items.length) {
      list.innerHTML = emptyTpl({
        icon: EMPTY_ICONS.search,
        title: '没有符合条件的动态',
        desc: '换个关键词、类型或日期范围试试。',
      });
    } else {
      list.innerHTML = s.items.map(dynamicRowTpl).join('');
    }
    setText('#dynamics-stat-loaded', fmtInt(s.items.length));
    setText('#dynamics-stat-selected', fmtInt(s.selected.size));
    const meta = $('#dynamics-meta');
    if (meta) {
      meta.innerHTML = `已加载 <strong>${fmtInt(s.items.length)}</strong> 条${
        s.selected.size ? ` · 已选 <strong>${fmtInt(s.selected.size)}</strong> 条` : ''
      }${s.hasMore ? '' : ' · 没有更多了'}`;
    }
    const footer = $('#dynamics-footer');
    if (footer) {
      if (s.loading && s.items.length) footer.innerHTML = '<span class="list-footer__done">正在加载…</span>';
      else if (s.hasMore)
        footer.innerHTML =
          '<button class="btn btn--secondary btn--small" type="button" data-action="load-more-dynamics">加载更多</button>';
      else footer.innerHTML = s.items.length ? '<span class="list-footer__done">已经到底了</span>' : '';
    }
    setToolsVisible('dynamics-tools', !!s.loaded && s.items.length > 0);
    renderActionBar();
  }

  function updateDynamicsMeta() {
    const s = state.dynamics;
    setText('#dynamics-stat-loaded', fmtInt(s.items.length));
    setText('#dynamics-stat-selected', fmtInt(s.selected.size));
    const meta = $('#dynamics-meta');
    if (meta) {
      meta.innerHTML = `已加载 <strong>${fmtInt(s.items.length)}</strong> 条${
        s.selected.size ? ` · 已选 <strong>${fmtInt(s.selected.size)}</strong> 条` : ''
      }${s.hasMore ? '' : ' · 没有更多了'}`;
    }
  }

  function toggleDynamicSelection(input) {
    const id = input.getAttribute('data-id');
    if (input.checked) state.dynamics.selected.add(id);
    else state.dynamics.selected.delete(id);
    updateDynamicsMeta();
    renderActionBar();
  }

  function selectAllDynamics() {
    state.dynamics.items.forEach((item) => state.dynamics.selected.add(item.id));
    renderDynamicList();
  }

  function invertDynamics() {
    const selected = state.dynamics.selected;
    state.dynamics.items.forEach((item) => {
      if (selected.has(item.id)) selected.delete(item.id);
      else selected.add(item.id);
    });
    renderDynamicList();
  }

  function clearDynamicSelection() {
    state.dynamics.selected.clear();
    renderDynamicList();
  }

  function openDeleteDynamicsDialog() {
    const n = state.dynamics.selected.size;
    if (!n) return;
    const ids = Array.from(state.dynamics.selected);
    openDialog({
      title: `删除选中的 ${fmtInt(n)} 条动态？`,
      body: '删除会从 B 站动态中永久移除这些内容，无法恢复。任务开始前会先在本地导出备份。',
      note: '如果动态里有你不想删的转发或图片，建议先取消勾选再确认。',
      confirmLabel: `删除 ${fmtInt(n)} 条`,
      danger: true,
      onConfirm: async () => {
        const task = await api.post('/api/tasks', {
          type: 'dynamic_delete',
          params: { ids },
        });
        state.dynamics.selected.clear();
        renderDynamicList();
        onTaskCreated(task);
      },
    });
  }

  /* ========================================================= 10. favorites */

  let favSearchTimer = 0;

  function activeFolder() {
    const s = state.favorites;
    return s.folders.find((folder) => folder.media_id === s.folderId) || null;
  }

  function renderFavoritesView() {
    const s = state.favorites;
    const root = $('#view-root');
    if (!root) return;
    const folder = activeFolder();
    root.innerHTML =
      heroTpl({
        eyebrow: '整理 · 收藏夹',
        title: '收藏夹',
        parchment: true,
        stats:
          statTpl(fmtInt(s.folders.length), '收藏夹', 'fav-stat-count') +
          statTpl(folder ? fmtInt(folder.count) : '—', '当前夹内容', 'fav-stat-total'),
      }) +
      `<aside class="fav-sidebar" id="fav-sidebar" aria-label="我的收藏夹">
        <p class="fav-sidebar__title">我的收藏夹</p>
        <div class="folder-list" id="folder-list">${folderButtonsTpl()}</div>
      </aside>` +
      `<div class="toolbar" role="search">
        <div class="page-inner toolbar__inner">
          <div class="search">
            ${SEARCH_ICON}
            <input class="search__input" id="fav-q" type="search" placeholder="搜索标题或 UP 主"
                   value="${escAttr(s.q)}" autocomplete="off">
          </div>
          <div class="select-wrap fav-folder-select">
            <select class="select" id="fav-folder" aria-label="全部收藏夹" title="全部收藏夹">
              ${folderOptionsTpl()}
            </select>
          </div>
          <div class="toolbar__actions">
            <button class="btn btn--danger-outline btn--small" type="button" data-action="fav-clear-folder"${
              folder ? '' : ' disabled'
            }>清空此收藏夹</button>
            <button class="btn btn--danger-text btn--small" type="button" data-action="fav-clean-invalid"${
              folder ? '' : ' disabled'
            }>清理失效内容</button>
          </div>
        </div>
      </div>` +
      `<section class="section">
        <div class="page-inner">
          <div class="list-head">
            <p class="list-meta" id="fav-meta">加载中…</p>
            <div class="list-tools" id="fav-tools" hidden>
              <button class="btn btn--ghost btn--small" type="button" data-action="select-favs">全选本页</button>
              <button class="btn btn--ghost btn--small" type="button" data-action="invert-favs">反选</button>
              <button class="btn btn--ghost btn--small" type="button" data-action="clear-favs">清除选择</button>
            </div>
          </div>
          <div class="item-list" id="fav-list">${skeletonRows(5)}</div>
          <div class="list-footer" id="fav-footer"></div>
        </div>
      </section>`;

    if (!s.foldersLoaded) {
      fetchFavoriteFolders();
    } else if (s.folderId && !s.loaded) {
      fetchFavItems(true);
    } else {
      renderFavList();
    }
    renderActionBar();
  }

  /* same folder list is rendered twice: fixed rail (>=1600px) and select (<1600px) */
  function folderButtonsTpl() {
    const s = state.favorites;
    if (!s.foldersLoaded) return '<p class="folder-empty">加载中…</p>';
    if (!s.folders.length) return '<p class="folder-empty">没有收藏夹</p>';
    return s.folders
      .map(
        (folder) => `<button class="folder" type="button" data-action="select-folder" data-id="${folder.media_id}"
          aria-current="${s.folderId === folder.media_id ? 'true' : 'false'}">
          <span class="folder__name">
            <span>${esc(folder.title)}</span>
            ${folder.is_default ? '<span class="chip">默认</span>' : ''}
            ${folder.privacy ? '<span class="chip chip--source">私密</span>' : ''}
          </span>
          <span class="folder__count">${fmtInt(folder.count)} 个内容</span>
        </button>`
      )
      .join('');
  }

  function folderOptionsTpl() {
    const s = state.favorites;
    if (!s.foldersLoaded) return '<option value="">加载中…</option>';
    if (!s.folders.length) return '<option value="">没有收藏夹</option>';
    return s.folders
      .map(
        (folder) =>
          `<option value="${folder.media_id}"${s.folderId === folder.media_id ? ' selected' : ''}>${esc(
            folder.title
          )}（${fmtInt(folder.count)}）</option>`
      )
      .join('');
  }

  async function fetchFavoriteFolders() {
    const s = state.favorites;
    if (s.loadingFolders) return;
    s.loadingFolders = true;
    try {
      const data = await api.get('/api/favorites/folders');
      s.folders = data && Array.isArray(data.items) ? data.items : [];
      s.foldersLoaded = true;
      if (!s.folders.length) {
        s.folderId = null;
      } else if (!s.folders.some((folder) => folder.media_id === s.folderId)) {
        const def = s.folders.find((folder) => folder.is_default) || s.folders[0];
        s.folderId = def.media_id;
        s.items = [];
        s.total = 0;
        s.loaded = false;
        s.selected.clear();
      }
    } catch (err) {
      s.foldersLoaded = true;
      handleError(err);
    } finally {
      s.loadingFolders = false;
    }
    if (state.route === 'favorites') renderFavoritesView();
  }

  async function fetchFavItems(reset) {
    const s = state.favorites;
    if (!s.folderId || s.loading) return;
    s.loading = true;
    if (reset) {
      s.items = [];
      s.loaded = false;
    }
    if (state.route === 'favorites') renderFavList();
    try {
      const params = new URLSearchParams();
      params.set('offset', String(reset ? 0 : s.items.length));
      params.set('limit', String(LIMITS.favorites));
      if (s.q.trim()) params.set('q', s.q.trim());
      const data = await api.get(`/api/favorites/${s.folderId}/items?` + params.toString());
      const items = data && Array.isArray(data.items) ? data.items : [];
      s.items = reset ? items : s.items.concat(items);
      s.total = Number(data && data.total) || s.items.length;
      s.loaded = true;
    } catch (err) {
      if (reset) s.loaded = true;
      handleError(err);
    } finally {
      s.loading = false;
    }
    if (state.route === 'favorites') renderFavList();
  }

  function forceReloadFavorites() {
    const s = state.favorites;
    s.foldersLoaded = false;
    s.loaded = false;
    fetchFavoriteFolders();
  }

  function favRowTpl(item) {
    const key = `${item.id}:${item.type}`;
    const selected = state.favorites.selected.has(key);
    const typeLabel = Number(item.type) === 12 ? '专栏' : '视频';
    return `<div class="item item--fav${selected ? ' is-selected' : ''}" data-key="${escAttr(key)}">
      <label class="check"><input type="checkbox" data-check-fav data-key="${escAttr(
        key
      )}"${selected ? ' checked' : ''} aria-label="选择 ${escAttr(item.title)}"><span class="check__box"></span></label>
      <div class="cover">${
        item.cover
          ? `<img src="${escAttr(item.cover)}" alt="" data-cover="1" data-seed="${Number(
              String(item.id).slice(-3)
            ) || 0}" loading="lazy" referrerpolicy="no-referrer">`
          : ''
      }</div>
      <div class="item__main">
        <div class="item__title-row">
          <p class="item__title">${esc(item.title || '（无标题）')}</p>
          ${item.invalid ? '<span class="chip chip--invalid">已失效</span>' : ''}
        </div>
        <div class="item__meta">
          <span>UP主：${esc(item.upper || '未知')}</span>
          <span>收藏于 ${esc(fmtDate(item.fav_time))}</span>
        </div>
      </div>
      <div class="item__aside">
        <span>${typeLabel}</span>
        ${item.bvid ? `<span>${esc(item.bvid)}</span>` : ''}
      </div>
    </div>`;
  }

  function renderFavList() {
    const s = state.favorites;
    const list = $('#fav-list');
    if (!list) return;
    if (s.foldersLoaded && !s.folderId) {
      list.innerHTML = emptyTpl({
        icon: EMPTY_ICONS.inbox,
        title: '没有可用的收藏夹',
        desc: '登录的账号下还没有收藏夹，或者收藏夹列表加载失败。',
      });
    } else if (!s.loaded) list.innerHTML = skeletonRows(5);
    else if (!s.items.length)
      list.innerHTML = emptyTpl({
        icon: EMPTY_ICONS.inbox,
        title: '这个收藏夹是空的',
        desc: '换一个收藏夹，或者清空搜索词再看。',
      });
    else list.innerHTML = s.items.map(favRowTpl).join('');

    const folder = activeFolder();
    setText('#fav-stat-count', fmtInt(s.folders.length));
    setText('#fav-stat-total', folder ? fmtInt(folder.count) : '—');
    const meta = $('#fav-meta');
    if (meta) {
      meta.innerHTML = `已加载 <strong>${fmtInt(s.items.length)}</strong> / 共 <strong>${fmtInt(
        s.total
      )}</strong> 个内容${s.selected.size ? ` · 已选 <strong>${fmtInt(s.selected.size)}</strong> 个` : ''}`;
    }
    const footer = $('#fav-footer');
    if (footer) {
      if (s.loading && s.items.length) footer.innerHTML = '<span class="list-footer__done">正在加载…</span>';
      else if (s.items.length < s.total)
        footer.innerHTML =
          '<button class="btn btn--secondary btn--small" type="button" data-action="load-more-favs">加载更多</button>';
      else footer.innerHTML = s.items.length ? '<span class="list-footer__done">已经到底了</span>' : '';
    }
    setToolsVisible('fav-tools', !!s.loaded && s.items.length > 0);
    renderActionBar();
  }

  function updateFavMeta() {
    const s = state.favorites;
    const meta = $('#fav-meta');
    if (meta) {
      meta.innerHTML = `已加载 <strong>${fmtInt(s.items.length)}</strong> / 共 <strong>${fmtInt(
        s.total
      )}</strong> 个内容${s.selected.size ? ` · 已选 <strong>${fmtInt(s.selected.size)}</strong> 个` : ''}`;
    }
  }

  function toggleFavSelection(input) {
    const key = input.getAttribute('data-key');
    if (input.checked) state.favorites.selected.add(key);
    else state.favorites.selected.delete(key);
    updateFavMeta();
    renderActionBar();
  }

  function selectAllFavs() {
    state.favorites.items.forEach((item) => state.favorites.selected.add(`${item.id}:${item.type}`));
    renderFavList();
  }

  function invertFavs() {
    const selected = state.favorites.selected;
    state.favorites.items.forEach((item) => {
      const key = `${item.id}:${item.type}`;
      if (selected.has(key)) selected.delete(key);
      else selected.add(key);
    });
    renderFavList();
  }

  function clearFavSelection() {
    state.favorites.selected.clear();
    renderFavList();
  }

  function selectFolder(mediaId) {
    const s = state.favorites;
    if (s.folderId === mediaId) return;
    s.folderId = mediaId;
    s.items = [];
    s.total = 0;
    s.loaded = false;
    s.selected.clear();
    renderFavoritesView();
  }

  function openFavClearDialog() {
    const folder = activeFolder();
    if (!folder) return;
    openDialog({
      title: `清空「${folder.title}」？`,
      body: `将取消收藏这个收藏夹里的全部 ${fmtInt(folder.count)} 个内容。收藏夹本身会保留。`,
      note: '清空整个收藏夹需要手打确认词。任务开始前会先在本机导出 CSV 与 JSONL 备份。',
      phrase: state.settings.confirm_phrase || '清空',
      confirmLabel: '清空收藏夹',
      danger: true,
      onConfirm: async ({ phrase }) => {
        const task = await api.post('/api/tasks', {
          type: 'favorite_clear',
          params: { folder_id: folder.media_id, ids: [] },
          confirm_phrase: phrase,
        });
        state.favorites.selected.clear();
        state.favorites.loaded = false;
        renderFavList();
        onTaskCreated(task);
      },
    });
  }

  function openFavCleanInvalidDialog() {
    const folder = activeFolder();
    if (!folder) return;
    openDialog({
      title: `清理「${folder.title}」的失效内容？`,
      body: '只处理当前收藏夹里已经失效、点开也看不到的内容，其他内容不受影响。',
      note: '这个操作需要手打确认词。任务开始前会先在本机导出 CSV 与 JSONL 备份。',
      phrase: state.settings.confirm_phrase || '清空',
      confirmLabel: '清理失效内容',
      danger: true,
      onConfirm: async ({ phrase }) => {
        const task = await api.post('/api/tasks', {
          type: 'favorite_clean_invalid',
          params: { folder_id: folder.media_id },
          confirm_phrase: phrase,
        });
        state.favorites.loaded = false;
        renderFavList();
        onTaskCreated(task);
      },
    });
  }

  function openFavUnfavoriteDialog() {
    const s = state.favorites;
    const folder = activeFolder();
    const keys = Array.from(s.selected);
    if (!folder || !keys.length) return;
    openDialog({
      title: `取消收藏选中的 ${fmtInt(keys.length)} 个内容？`,
      body: '这些内容会从当前收藏夹移除，但不会删除 B 站上的视频本身。',
      note: '这批条目会先备份到本机，再取消收藏。',
      ack: '我确认取消收藏这些内容。',
      confirmLabel: '取消收藏选中',
      danger: true,
      onConfirm: async () => {
        const task = await api.post('/api/tasks', {
          type: 'favorite_clear',
          params: { folder_id: folder.media_id, ids: keys, ack_risk: true },
        });
        s.selected.clear();
        s.loaded = false;
        renderFavList();
        onTaskCreated(task);
      },
    });
  }

  /* =========================================================== 11. toview */

  function renderToviewView() {
    const s = state.toview;
    const root = $('#view-root');
    if (!root) return;
    root.innerHTML =
      heroTpl({
        eyebrow: '清理 · 稍后再看',
        title: '稍后再看',
        parchment: true,
        stats:
          statTpl(fmtInt(s.total || s.items.length), '视频数', 'toview-stat-total') +
          statTpl(fmtInt(s.items.length), '已加载', 'toview-stat-loaded'),
      }) +
      `<div class="toolbar">
        <div class="page-inner toolbar__inner">
          <span class="toolbar__spacer"></span>
          <div class="toolbar__actions">
            <button class="btn btn--danger-outline btn--small" type="button" data-action="toview-clear">清空稍后再看</button>
          </div>
        </div>
      </div>` +
      `<section class="section">
        <div class="page-inner">
          <div class="list-head">
            <p class="list-meta" id="toview-meta">加载中…</p>
            <div class="list-tools" id="toview-tools" hidden>
              <button class="btn btn--ghost btn--small" type="button" data-action="select-toview">全选本页</button>
              <button class="btn btn--ghost btn--small" type="button" data-action="invert-toview">反选</button>
              <button class="btn btn--ghost btn--small" type="button" data-action="clear-toview-selection">清除选择</button>
            </div>
          </div>
          <div class="item-list" id="toview-list">${skeletonRows(5)}</div>
          <div class="list-footer" id="toview-footer"></div>
        </div>
      </section>`;

    if (!s.loaded) fetchToview(true);
    else renderToviewList();
  }

  function toviewRowTpl(item) {
    const duration = Number(item.duration) || 0;
    const progress = Number(item.progress) || 0;
    const watched = progress > 0 && duration > 0 ? clamp(Math.round((progress / duration) * 100), 1, 99) : 0;
    const selected = state.toview.selected.has(Number(item.aid));
    return `<div class="item item--no-check${selected ? ' is-selected' : ''}" data-aid="${item.aid}">
      <div class="type-tile" aria-hidden="true">${PLAY_ICON}</div>
      <div class="item__main">
        <p class="item__title">${esc(item.title || '（无标题）')}</p>
        <div class="item__meta">
          <span>UP主：${esc(item.upper || '未知')}</span>
          <span>时长 ${esc(fmtDuration(duration))}</span>
          <span>添加于 ${esc(fmtDate(item.add_ts))}</span>
        </div>
      </div>
      <div class="item__aside">
        <span>${watched ? `已看 ${watched}%` : '未观看'}</span>
        ${item.bvid ? `<span>${esc(item.bvid)}</span>` : ''}
      </div>
    </div>`;
  }

  async function fetchToview(reset) {
    const s = state.toview;
    if (s.loading) return;
    s.loading = true;
    if (reset) {
      s.items = [];
      s.loaded = false;
      s.selected.clear();
    }
    if (state.route === 'toview') renderToviewList();
    try {
      const params = new URLSearchParams();
      params.set('offset', String(reset ? 0 : s.items.length));
      params.set('limit', String(LIMITS.toview));
      const data = await api.get('/api/toview?' + params.toString());
      const items = data && Array.isArray(data.items) ? data.items : [];
      s.items = reset ? items : s.items.concat(items);
      s.total = Number(data && data.total) || s.items.length;
      s.loaded = true;
    } catch (err) {
      if (reset) s.loaded = true;
      handleError(err);
    } finally {
      s.loading = false;
    }
    if (state.route === 'toview') renderToviewList();
  }

  function renderToviewList() {
    const s = state.toview;
    const list = $('#toview-list');
    if (!list) return;
    if (!s.loaded) list.innerHTML = skeletonRows(5);
    else if (!s.items.length)
      list.innerHTML = emptyTpl({
        icon: EMPTY_ICONS.inbox,
        title: '稍后再看是空的',
        desc: '没有待清理的视频。',
      });
    else list.innerHTML = s.items.map(toviewRowTpl).join('');
    updateToviewMeta();
    const footer = $('#toview-footer');
    if (footer) {
      if (s.loading && s.items.length) footer.innerHTML = '<span class="list-footer__done">正在加载…</span>';
      else if (s.items.length < s.total)
        footer.innerHTML =
          '<button class="btn btn--secondary btn--small" type="button" data-action="load-more-toview">加载更多</button>';
      else footer.innerHTML = s.items.length ? '<span class="list-footer__done">已经到底了</span>' : '';
    }
    setToolsVisible('toview-tools', !!s.loaded && s.items.length > 0);
    renderActionBar();
  }

  function selectAllToview() {
    state.toview.items.forEach((item) => state.toview.selected.add(Number(item.aid)));
    renderToviewList();
  }

  function invertToview() {
    const selected = state.toview.selected;
    state.toview.items.forEach((item) => {
      const aid = Number(item.aid);
      if (selected.has(aid)) selected.delete(aid);
      else selected.add(aid);
    });
    renderToviewList();
  }

  function updateToviewMeta() {
    const s = state.toview;
    setText('#toview-stat-total', fmtInt(s.total || s.items.length));
    setText('#toview-stat-loaded', fmtInt(s.items.length));
    const meta = $('#toview-meta');
    if (meta) {
      meta.innerHTML = `共 <strong>${fmtInt(s.total)}</strong> 个视频 · 已加载 <strong>${fmtInt(
        s.items.length
      )}</strong> 个${s.selected.size ? ` · 已选 <strong>${fmtInt(s.selected.size)}</strong> 个` : ''}`;
    }
  }

  function openToviewClearDialog() {
    openDialog({
      title: '清空稍后再看？',
      body: `将移除全部 ${fmtInt(state.toview.total || state.toview.items.length)} 个视频。清空后列表会变成空的。`,
      note: '这个操作需要手打确认词。任务开始前会先在本机导出 CSV 与 JSONL 备份。',
      phrase: state.settings.confirm_phrase || '清空',
      confirmLabel: '清空稍后再看',
      danger: true,
      onConfirm: async ({ phrase }) => {
        const task = await api.post('/api/tasks', {
          type: 'toview_clear',
          params: {},
          confirm_phrase: phrase,
        });
        state.toview.loaded = false;
        renderToviewList();
        onTaskCreated(task);
      },
    });
  }

  /* =========================================================== 12. settings */

  async function loadSettings(force) {
    if (state.settingsLoaded && !force) return;
    try {
      const data = await api.get('/api/settings');
      state.settings = normalizeSettings(data);
      state.settingsLoaded = true;
    } catch (err) {
      if (err && err.status === 401) return;
      state.settings = normalizeSettings(state.settings);
    }
    if (state.route === 'settings') renderSettingsView();
  }

  let backupsLoading = false;
  async function fetchBackups() {
    if (backupsLoading) return;
    backupsLoading = true;
    try {
      const data = await api.get('/api/backups');
      state.backups = data && Array.isArray(data.items) ? data.items : [];
      state.backupsLoaded = true;
    } catch (err) {
      if (err && err.status === 401) return;
      state.backupsLoaded = true;
    } finally {
      backupsLoading = false;
    }
    if (state.route === 'settings') renderSettingsView();
  }

  function renderSettingsView() {
    const root = $('#view-root');
    if (!root) return;
    const settings = state.settings;
    const llm = settings.llm || DEFAULT_SETTINGS.llm;
    root.innerHTML =
      heroTpl({
        eyebrow: '本地配置',
        title: '设置',
        parchment: true,
      }) +
      `<section class="section"><div class="settings__inner">
        <div class="form-section">
          <h2 class="form-section__title">请求节奏</h2>
          <p class="form-section__desc">慢一点更安全。B 站对高频操作敏感，默认值已经比较保守。</p>
          <div class="form-row">
            <div><p class="form-row__label">基础间隔（秒）</p><p class="form-row__hint">每次请求前等待的基础时间，建议 2–5 秒。</p></div>
            <div class="form-row__control"><input class="input input--number" id="set-delay" type="number" min="0.5" step="0.1" value="${settings.delay_seconds}"></div>
          </div>
          <div class="form-row">
            <div><p class="form-row__label">随机抖动（秒）</p><p class="form-row__hint">在基础间隔上叠加的随机时间，让请求看起来不那么规律。</p></div>
            <div class="form-row__control"><input class="input input--number" id="set-jitter" type="number" min="0" step="0.1" value="${settings.jitter_seconds}"></div>
          </div>
          <div class="form-row">
            <div><p class="form-row__label">每批请求数</p><p class="form-row__hint">连续请求多少个后进入批次休息。</p></div>
            <div class="form-row__control"><input class="input input--number" id="set-batch" type="number" min="1" step="1" value="${settings.batch_size}"></div>
          </div>
          <div class="form-row">
            <div><p class="form-row__label">批次休息（秒）</p><p class="form-row__hint">每批之间额外休息的时间。</p></div>
            <div class="form-row__control"><input class="input input--number" id="set-batch-pause" type="number" min="0" step="0.5" value="${settings.batch_pause_seconds}"></div>
          </div>
          <div class="form-row">
            <div><p class="form-row__label">风控冷却（秒）</p><p class="form-row__hint">触发风控后暂停任务的时间，默认 10 分钟。</p></div>
            <div class="form-row__control"><input class="input input--number" id="set-cooldown" type="number" min="30" step="10" value="${settings.cooldown_seconds}"></div>
          </div>
          <div class="form-row">
            <div><p class="form-row__label">最多冷却次数</p><p class="form-row__hint">连续触发多少次风控后停止任务。</p></div>
            <div class="form-row__control"><input class="input input--number" id="set-max-cooldowns" type="number" min="0" step="1" value="${settings.max_cooldowns}"></div>
          </div>
        </div>

        <div class="form-section">
          <h2 class="form-section__title">安全保护</h2>
          <p class="form-section__desc">这些选项决定批量操作时哪些人不会被碰。</p>
          <div class="form-row">
            <div><p class="form-row__label">互关保护</p><p class="form-row__hint">开启后，批量取关会跳过与你互相关注的人。</p></div>
            <div class="form-row__control"><label class="switch"><input type="checkbox" id="set-protect-mutual"${settings.protect_mutual ? ' checked' : ''}><span class="switch__track"></span>${settings.protect_mutual ? '已开启' : '已关闭'}</label></div>
          </div>
          <div class="form-row">
            <div><p class="form-row__label">确认词</p><p class="form-row__hint">清空类操作需要手打这个词才能执行。</p></div>
            <div class="form-row__control"><input class="input" id="set-confirm-phrase" type="text" value="${escAttr(settings.confirm_phrase)}" maxlength="12"></div>
          </div>
          <div class="form-row">
            <div><p class="form-row__label">保留备份数量</p><p class="form-row__hint">0 表示全部保留。删除类任务会自动生成 CSV + JSONL 备份。</p></div>
            <div class="form-row__control"><input class="input input--number" id="set-backup-keep" type="number" min="0" step="1" value="${settings.backup_keep}"></div>
          </div>
        </div>

        <div class="form-section">
          <h2 class="form-section__title">LLM 分类</h2>
          <p class="form-section__desc">可选功能：用 OpenAI 兼容接口给关注列表分类。没有配置时，规则分类依然可用。</p>
          <div class="form-row">
            <div><p class="form-row__label">启用 LLM 分类</p><p class="form-row__hint">关闭时，下面的配置会保留但不会被调用。</p></div>
            <div class="form-row__control"><label class="switch"><input type="checkbox" id="set-llm-enabled"${llm.enabled ? ' checked' : ''}><span class="switch__track"></span>${llm.enabled ? '已启用' : '未启用'}</label></div>
          </div>
          <div class="form-row">
            <div><p class="form-row__label">接口地址</p><p class="form-row__hint">例如 https://api.openai.com/v1（OpenAI 兼容）。</p></div>
            <div class="form-row__control"><input class="input input--wide" id="set-llm-base-url" type="url" placeholder="https://api.openai.com/v1" value="${escAttr(llm.base_url)}"${llm.enabled ? '' : ' disabled'} data-llm-input></div>
          </div>
          <div class="form-row">
            <div><p class="form-row__label">API Key</p><p class="form-row__hint">只保存在本机。页面不会把它回显到任何地方。</p></div>
            <div class="form-row__control"><input class="input input--wide" id="set-llm-api-key" type="password" autocomplete="off" placeholder="sk-..." value="${escAttr(llm.api_key)}"${llm.enabled ? '' : ' disabled'} data-llm-input></div>
          </div>
          <div class="form-row">
            <div><p class="form-row__label">模型</p><p class="form-row__hint">例如 gpt-4o-mini，按你的接口支持填写。</p></div>
            <div class="form-row__control"><input class="input" id="set-llm-model" type="text" placeholder="gpt-4o-mini" value="${escAttr(llm.model)}"${llm.enabled ? '' : ' disabled'} data-llm-input></div>
          </div>
          <div class="form-row">
            <div><p class="form-row__label">每批分类数量</p><p class="form-row__hint">一次发给 LLM 的条目数，太大容易超时。</p></div>
            <div class="form-row__control"><input class="input input--number" id="set-llm-batch" type="number" min="1" step="1" value="${llm.batch_size}"${llm.enabled ? '' : ' disabled'} data-llm-input></div>
          </div>
        </div>

        <div class="form-section">
          <h2 class="form-section__title">备份</h2>
          <p class="form-section__desc">删除类任务开始前会自动导出 CSV + JSONL 到本机数据目录。这里只是展示，不支持改路径。</p>
          <div class="form-row">
            <div><p class="form-row__label">备份记录</p><p class="form-row__hint">最新在前。路径可以直接在文件管理器里打开。</p></div>
            <div class="form-row__control"><button class="btn btn--pearl btn--small" type="button" data-action="refresh-backups">刷新</button></div>
          </div>
          <div class="backup-list" id="backup-list">${backupListTpl()}</div>
        </div>

        <div class="form-section">
          <h2 class="form-section__title">账户</h2>
          <p class="form-section__desc">登录状态只保存在这台电脑上。</p>
          <div class="form-row">
            <div><p class="form-row__label">当前登录</p><p class="form-row__hint">${esc(
              state.user && state.user.name ? state.user.name : '未登录'
            )}${state.user && state.user.mid ? ` · mid ${state.user.mid}` : ''}</p></div>
            <div class="form-row__control"><button class="btn btn--pearl btn--small" type="button" data-action="logout">退出登录</button></div>
          </div>
        </div>
      </div></section>` +
      pageFooterTpl();

    if (!state.settingsLoaded) loadSettings(true);
    else if (!state.backupsLoaded) fetchBackups();
    renderActionBar();
  }

  function backupListTpl() {
    if (!state.backupsLoaded) return '<p class="backup-empty">加载中…</p>';
    if (!state.backups.length) {
      return '<p class="backup-empty">还没有备份记录。执行删除类任务时会自动创建。</p>';
    }
    return state.backups
      .slice(0, 8)
      .map(
        (item) => `<div class="backup-row">
          <span class="backup-row__path">${esc(item.path || '—')}</span>
          <span class="backup-row__meta">${fmtInt(item.count)} 条 · ${esc(fmtIsoDisplay(item.created_at))}</span>
        </div>`
      )
      .join('');
  }

  function readSettingsForm() {
    const num = (id, fallback) => {
      const el = document.getElementById(id);
      const value = el ? Number(el.value) : NaN;
      return Number.isFinite(value) ? value : fallback;
    };
    const str = (id) => {
      const el = document.getElementById(id);
      return el ? String(el.value).trim() : '';
    };
    const checked = (id) => {
      const el = document.getElementById(id);
      return !!(el && el.checked);
    };
    const payload = {
      delay_seconds: num('set-delay', DEFAULT_SETTINGS.delay_seconds),
      jitter_seconds: num('set-jitter', DEFAULT_SETTINGS.jitter_seconds),
      batch_size: Math.round(num('set-batch', DEFAULT_SETTINGS.batch_size)),
      batch_pause_seconds: num('set-batch-pause', DEFAULT_SETTINGS.batch_pause_seconds),
      cooldown_seconds: Math.round(num('set-cooldown', DEFAULT_SETTINGS.cooldown_seconds)),
      max_cooldowns: Math.round(num('set-max-cooldowns', DEFAULT_SETTINGS.max_cooldowns)),
      protect_mutual: checked('set-protect-mutual'),
      confirm_phrase: str('set-confirm-phrase') || DEFAULT_SETTINGS.confirm_phrase,
      backup_keep: Math.round(num('set-backup-keep', DEFAULT_SETTINGS.backup_keep)),
      llm: {
        enabled: checked('set-llm-enabled'),
        base_url: str('set-llm-base-url'),
        api_key: str('set-llm-api-key'),
        model: str('set-llm-model'),
        batch_size: Math.round(num('set-llm-batch', DEFAULT_SETTINGS.llm.batch_size)),
      },
    };
    return normalizeSettings(payload);
  }

  async function saveSettings(btn) {
    const payload = readSettingsForm();
    if (payload.delay_seconds < 0.5) {
      toast('基础间隔不能小于 0.5 秒');
      return;
    }
    if (!payload.confirm_phrase) {
      toast('确认词不能为空');
      return;
    }
    await withBtn(btn, async () => {
      const saved = await api.put('/api/settings', payload);
      state.settings = normalizeSettings(saved);
      state.settingsLoaded = true;
      toast('设置已保存');
    });
  }

  function resetSettingsForm() {
    state.settings = clone(DEFAULT_SETTINGS);
    renderSettingsView();
    toast('已恢复默认值，点击保存后生效');
  }

  function toggleLlmFields(enabled) {
    $$('[data-llm-input]').forEach((input) => {
      input.disabled = !enabled;
    });
  }

  /* =============================================================== 13. logs */

  async function fetchLogs() {
    const logs = state.logs;
    try {
      const data = await api.get('/api/logs?limit=200');
      logs.items = data && Array.isArray(data.items) ? data.items : [];
      logs.loaded = true;
    } catch (err) {
      if (err && err.status === 401) return;
      logs.loaded = true;
      handleError(err);
    }
    if (state.route === 'logs') renderLogList();
  }

  function renderLogsView() {
    const root = $('#view-root');
    if (!root) return;
    root.innerHTML =
      heroTpl({
        eyebrow: '运行记录',
        title: '日志',
        stats: statTpl(fmtInt(state.logs.items.length), '条记录', 'logs-stat-count'),
      }) +
      `<div class="toolbar">
        <div class="page-inner toolbar__inner">
          <div class="select-wrap">
            <select class="select" id="logs-level" aria-label="按级别筛选">
              <option value="">全部级别</option>
              <option value="info"${state.logs.level === 'info' ? ' selected' : ''}>info</option>
              <option value="warn"${state.logs.level === 'warn' ? ' selected' : ''}>warn</option>
              <option value="error"${state.logs.level === 'error' ? ' selected' : ''}>error</option>
            </select>
          </div>
          <div class="toolbar__actions">
            <button class="btn btn--secondary btn--small" type="button" data-action="refresh-logs">刷新</button>
          </div>
        </div>
      </div>` +
      `<section class="section">
        <div class="page-inner">
          <div class="list-head"><p class="list-meta" id="logs-meta">加载中…</p></div>
          <div class="log-list" id="logs-list">${skeletonRows(4)}</div>
        </div>
      </section>` +
      pageFooterTpl();
    fetchLogs();
  }

  function renderLogList() {
    const list = $('#logs-list');
    if (!list) return;
    const logs = state.logs;
    if (!logs.loaded) {
      list.innerHTML = skeletonRows(4);
      return;
    }
    const items = logs.level ? logs.items.filter((entry) => entry.level === logs.level) : logs.items;
    if (!items.length) {
      list.innerHTML = emptyTpl({
        icon: EMPTY_ICONS.inbox,
        title: '暂无日志记录',
        desc: '执行操作或任务后，这里会出现对应的记录。',
      });
    } else {
      list.innerHTML = items
        .slice()
        .reverse()
        .map((entry) => {
          const level = entry.level === 'error' ? 'error' : entry.level === 'warn' ? 'warn' : 'info';
          return `<div class="log-row">
            <span class="log-row__ts">${esc(fmtIsoDisplay(entry.ts))}</span>
            <span class="chip chip--level-${level}">${esc(level)}</span>
            <span class="log-row__msg">${esc(entry.msg || '')}</span>
            <span class="log-row__task">${entry.task_id ? esc(entry.task_id) : ''}</span>
          </div>`;
        })
        .join('');
    }
    setText('#logs-stat-count', fmtInt(logs.items.length));
    const meta = $('#logs-meta');
    if (meta) meta.innerHTML = `共 <strong>${fmtInt(items.length)}</strong> 条${logs.level ? ` · 仅看 ${esc(logs.level)}` : ''}`;
  }

  /* ================================================ 13b. selection gestures */
  /* Desktop-only drag-select + Shift range select. Touch/mobile keeps plain
     checkbox interaction (gestures are disabled below 641px). */

  const rowAnchors = { follows: null, dynamics: null, favorites: null, toview: null };
  let dragSession = null;

  function isDesktopPointer() {
    return window.matchMedia('(min-width: 641px)').matches;
  }

  function selectionContextForRoute() {
    const route = state.route;
    if (route === 'follows') {
      const list = $('#follows-list');
      if (!list) return null;
      return {
        route,
        list,
        selected: state.follows.selected,
        keyOf: (row) => Number(row.getAttribute('data-mid')),
        after: () => {
          updateFollowsMeta();
          renderActionBar();
        },
        rerender: renderFollowList,
      };
    }
    if (route === 'dynamics') {
      const list = $('#dynamics-list');
      if (!list) return null;
      return {
        route,
        list,
        selected: state.dynamics.selected,
        keyOf: (row) => row.getAttribute('data-id'),
        after: () => {
          updateDynamicsMeta();
          renderActionBar();
        },
        rerender: renderDynamicList,
      };
    }
    if (route === 'favorites') {
      const list = $('#fav-list');
      if (!list) return null;
      return {
        route,
        list,
        selected: state.favorites.selected,
        keyOf: (row) => row.getAttribute('data-key'),
        after: () => {
          updateFavMeta();
          renderActionBar();
        },
        rerender: renderFavList,
      };
    }
    if (route === 'toview') {
      const list = $('#toview-list');
      if (!list) return null;
      return {
        route,
        list,
        selected: state.toview.selected,
        keyOf: (row) => Number(row.getAttribute('data-aid')),
        after: () => {
          updateToviewMeta();
          renderActionBar();
        },
        rerender: renderToviewList,
      };
    }
    return null;
  }

  function rowsForContext(ctx) {
    return Array.from(ctx.list.querySelectorAll('.item'));
  }

  function rowSelected(row, ctx) {
    const cb = row.querySelector('input[type="checkbox"]');
    if (cb) return cb.checked;
    const key = ctx.keyOf(row);
    return row.classList.contains('is-selected') || (key != null && ctx.selected.has(key));
  }

  function setRowSelected(row, selected) {
    row.classList.toggle('is-selected', !!selected);
    const cb = row.querySelector('input[type="checkbox"]');
    if (cb) cb.checked = !!selected;
  }

  function applyDragBand(currentIndex) {
    const s = dragSession;
    if (!s) return;
    const lo = Math.min(s.anchorIndex, currentIndex);
    const hi = Math.max(s.anchorIndex, currentIndex);
    for (let i = 0; i < s.rows.length; i += 1) {
      const target = i >= lo && i <= hi ? s.mode === 'add' : s.originals[i];
      if (s.applied.get(i) === target) continue;
      s.applied.set(i, target);
      setRowSelected(s.rows[i], target);
    }
    s.ctx.after();
  }

  function onDragMouseMove(event) {
    if (!dragSession) return;
    event.preventDefault();
    const s = dragSession;
    if (Math.abs(event.clientY - s.startY) > 3 || Math.abs(event.clientX - s.startX) > 3) {
      s.moved = true;
    }
    const el = document.elementFromPoint(event.clientX, event.clientY);
    if (!el) return;
    const row = el.closest('.item');
    if (!row || !s.ctx.list.contains(row)) return;
    const index = s.rows.indexOf(row);
    if (index < 0) return;
    applyDragBand(index);
  }

  function onDragMouseUp() {
    window.removeEventListener('mousemove', onDragMouseMove, true);
    window.removeEventListener('mouseup', onDragMouseUp, true);
    window.removeEventListener('blur', onDragMouseUp, true);
    document.body.classList.remove('is-drag-selecting');
    const s = dragSession;
    dragSession = null;
    if (!s || !s.moved) return;
    const rows = s.rows.filter((row) => row.isConnected);
    rows.forEach((row) => {
      const key = s.ctx.keyOf(row);
      if (key == null || Number.isNaN(key)) return;
      if (rowSelected(row, s.ctx)) s.ctx.selected.add(key);
      else s.ctx.selected.delete(key);
    });
    s.ctx.rerender();
  }

  function startRowDrag(ctx, rows, anchorIndex, event) {
    const originals = rows.map((row) => rowSelected(row, ctx));
    dragSession = {
      ctx,
      rows,
      anchorIndex,
      mode: originals[anchorIndex] ? 'remove' : 'add',
      originals,
      applied: new Map(),
      startX: event.clientX,
      startY: event.clientY,
      moved: false,
    };
    document.body.classList.add('is-drag-selecting');
    window.addEventListener('mousemove', onDragMouseMove, true);
    window.addEventListener('mouseup', onDragMouseUp, true);
    window.addEventListener('blur', onDragMouseUp, true);
  }

  function onDocumentMouseDown(event) {
    if (!isDesktopPointer() || event.button !== 0) return;
    if (document.body.classList.contains('is-shut-down')) return;
    if (event.target.closest('#modal-root')) return;
    const row = event.target.closest('.item');
    if (!row) return;
    const ctx = selectionContextForRoute();
    if (!ctx || !ctx.list.contains(row)) return;
    const rows = rowsForContext(ctx);
    const index = rows.indexOf(row);
    if (index < 0) return;
    rowAnchors[ctx.route] = index;
    // never hijack the checkbox / buttons / links inside a row
    if (event.target.closest('input, label, button, a, select, textarea')) return;
    event.preventDefault();
    startRowDrag(ctx, rows, index, event);
  }

  function applyShiftRange(ctx, row) {
    const rows = rowsForContext(ctx);
    const targetIndex = rows.indexOf(row);
    if (targetIndex < 0) return;
    let anchor = rowAnchors[ctx.route];
    if (anchor == null || anchor < 0 || anchor >= rows.length) anchor = targetIndex;
    const lo = Math.min(anchor, targetIndex);
    const hi = Math.max(anchor, targetIndex);
    const deselect = rowSelected(row, ctx);
    for (let i = lo; i <= hi; i += 1) {
      setRowSelected(rows[i], !deselect);
      const key = ctx.keyOf(rows[i]);
      if (key == null || Number.isNaN(key)) continue;
      if (deselect) ctx.selected.delete(key);
      else ctx.selected.add(key);
    }
    rowAnchors[ctx.route] = targetIndex;
    ctx.after();
  }

  /* ---------------------------------------------------------- shutdown */

  function openShutdownDialog() {
    if (document.body.classList.contains('is-shut-down')) return;
    openDialog({
      title: '关闭 BiliCleaner？',
      body: '关闭后正在跑的任务会暂停，下次启动会从断点继续。',
      confirmLabel: '关闭程序',
      danger: true,
      onConfirm: async () => {
        try {
          await api.post('/api/shutdown', {});
        } catch (_) {
          toast('后端未就绪，无法关闭程序');
          return;
        }
        showShutdownScreen();
      },
    });
  }

  function showShutdownScreen() {
    stopTaskPolling();
    stopQrPolling();
    stopDockTicker();
    closeDialog();
    document.body.classList.add('is-shut-down');
    document.body.style.overflow = 'hidden';
    document.documentElement.style.overflow = 'hidden';
    let screen = $('#shutdown-screen');
    if (!screen) {
      screen = document.createElement('div');
      screen.id = 'shutdown-screen';
      screen.className = 'shutdown-screen';
      screen.setAttribute('role', 'status');
      screen.innerHTML = `
        <div class="shutdown-screen__card">
          <div class="shutdown-screen__mark" aria-hidden="true">
            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                 stroke-width="1.6" stroke-linecap="round">
              <path d="M12 3v9"></path>
              <path d="M6.34 6.34a8 8 0 1 0 11.32 0"></path>
            </svg>
          </div>
          <p class="shutdown-screen__title">BiliCleaner 已关闭</p>
          <p class="shutdown-screen__desc">可以关掉这个页面了。</p>
        </div>`;
      document.body.appendChild(screen);
    }
    screen.style.display = 'grid';
  }

  /* ============================================================ 14. handlers */

  function handleAction(action, el) {
    switch (action) {
      case 'go-login':
        navigate('login');
        return;
      case 'logout':
        logout();
        return;
      case 'select-follows':
        selectAllFollows();
        return;
      case 'invert-follows':
        invertFollows();
        return;
      case 'clear-follows':
        clearFollowSelection();
        return;
      case 'load-more-follows':
        fetchFollows(false);
        return;
      case 'classify-rule':
        classifyByRule(el);
        return;
      case 'unfollow-selected':
        openUnfollowDialog();
        return;
      case 'classify-llm':
        openLlmClassifyDialog();
        return;
      case 'select-dynamics':
        selectAllDynamics();
        return;
      case 'invert-dynamics':
        invertDynamics();
        return;
      case 'clear-dynamics':
        clearDynamicSelection();
        return;
      case 'load-more-dynamics':
        fetchDynamics(false);
        return;
      case 'delete-dynamics':
        openDeleteDynamicsDialog();
        return;
      case 'clear-dynamics-filters': {
        state.dynamics.q = '';
        state.dynamics.type = '';
        state.dynamics.from = '';
        state.dynamics.to = '';
        renderDynamicsView();
        fetchDynamics(true);
        return;
      }
      case 'select-favs':
        selectAllFavs();
        return;
      case 'invert-favs':
        invertFavs();
        return;
      case 'clear-favs':
        clearFavSelection();
        return;
      case 'select-folder':
        selectFolder(Number(el.getAttribute('data-id')));
        return;
      case 'load-more-favs':
        fetchFavItems(false);
        return;
      case 'fav-clear-folder':
        openFavClearDialog();
        return;
      case 'fav-clean-invalid':
        openFavCleanInvalidDialog();
        return;
      case 'fav-unfavorite-selected':
        openFavUnfavoriteDialog();
        return;
      case 'refresh-toview':
        fetchToview(true);
        return;
      case 'load-more-toview':
        fetchToview(false);
        return;
      case 'toview-clear':
        openToviewClearDialog();
        return;
      case 'clear-toview-selection':
        state.toview.selected.clear();
        renderToviewList();
        return;
      case 'select-toview':
        selectAllToview();
        return;
      case 'invert-toview':
        invertToview();
        return;
      case 'save-settings':
        saveSettings(el);
        return;
      case 'reset-settings':
        resetSettingsForm();
        return;
      case 'refresh-backups':
        withBtn(el, async () => {
          state.backupsLoaded = false;
          await fetchBackups();
          toast('备份记录已刷新');
        });
        return;
      case 'refresh-logs':
        withBtn(el, async () => {
          await fetchLogs();
          toast('日志已刷新');
        });
        return;
      case 'task-pause':
        taskAction(el, 'pause');
        return;
      case 'task-resume':
        taskAction(el, 'resume');
        return;
      case 'task-stop':
        taskAction(el, 'stop');
        return;
      default:
        return;
    }
  }

  function onDocumentClick(event) {
    if (event.shiftKey && !document.body.classList.contains('is-shut-down')) {
      const shiftRow = event.target.closest('.item');
      if (
        shiftRow &&
        !event.target.closest('input, label, button, a, select, textarea')
      ) {
        const shiftCtx = selectionContextForRoute();
        if (shiftCtx && shiftCtx.list.contains(shiftRow)) {
          event.preventDefault();
          applyShiftRange(shiftCtx, shiftRow);
          return;
        }
      }
    }
    const dialogAction = event.target.closest('[data-dialog-action]');
    if (dialogAction) {
      if (dialogAction.getAttribute('data-dialog-action') === 'cancel') closeDialog();
      else confirmDialog(dialogAction);
      return;
    }
    if (event.target === $('#modal-root')) {
      closeDialog();
      return;
    }
    const routeEl = event.target.closest('[data-route]');
    if (routeEl) {
      event.preventDefault();
      navigate(routeEl.getAttribute('data-route'));
      return;
    }
    const actionEl = event.target.closest('[data-action]');
    if (!actionEl) return;
    handleAction(actionEl.getAttribute('data-action'), actionEl);
  }

  function onDocumentInput(event) {
    const target = event.target;
    if (target.id === 'follows-q') {
      state.follows.q = target.value;
      clearTimeout(followsSearchTimer);
      followsSearchTimer = setTimeout(() => {
        if (state.route === 'follows') fetchFollows(true);
      }, 320);
    } else if (target.id === 'dynamics-q') {
      state.dynamics.q = target.value;
      clearTimeout(dynamicsSearchTimer);
      dynamicsSearchTimer = setTimeout(() => {
        if (state.route === 'dynamics') fetchDynamics(true);
      }, 320);
    } else if (target.id === 'fav-q') {
      state.favorites.q = target.value;
      clearTimeout(favSearchTimer);
      favSearchTimer = setTimeout(() => {
        if (state.route === 'favorites') fetchFavItems(true);
      }, 320);
    } else if (target.id === 'dialog-phrase') {
      syncDialogConfirm();
    }
  }

  function onDocumentChange(event) {
    const target = event.target;
    const id = target.id;
    if (id === 'follows-category') {
      state.follows.category = target.value;
      fetchFollows(true);
    } else if (id === 'follows-group') {
      state.follows.group = target.value;
      fetchFollows(true);
    } else if (id === 'follows-mutual') {
      state.follows.mutual = target.checked;
      fetchFollows(true);
    } else if (id === 'dynamics-type') {
      state.dynamics.type = target.value;
      fetchDynamics(true);
    } else if (id === 'dynamics-from' || id === 'dynamics-to') {
      const from = $('#dynamics-from');
      const to = $('#dynamics-to');
      const fromValue = from ? from.value : '';
      const toValue = to ? to.value : '';
      if (fromValue && toValue && fromValue > toValue) {
        toast('开始日期不能晚于结束日期');
        return;
      }
      state.dynamics.from = fromValue;
      state.dynamics.to = toValue;
      fetchDynamics(true);
    } else if (id === 'logs-level') {
      state.logs.level = target.value;
      renderLogList();
    } else if (id === 'fav-folder') {
      selectFolder(Number(target.value));
    } else if (id === 'set-llm-enabled') {
      toggleLlmFields(target.checked);
      const label = target.closest('.switch');
      if (label) {
        const textNode = Array.from(label.childNodes).find(
          (node) => node.nodeType === Node.TEXT_NODE && node.textContent.trim()
        );
        if (textNode) textNode.textContent = target.checked ? '已启用' : '未启用';
      }
    } else if (id === 'set-protect-mutual') {
      const label = target.closest('.switch');
      if (label) {
        const textNode = Array.from(label.childNodes).find(
          (node) => node.nodeType === Node.TEXT_NODE && node.textContent.trim()
        );
        if (textNode) textNode.textContent = target.checked ? '已开启' : '已关闭';
      }
    } else if (id === 'dialog-ack') {
      syncDialogConfirm();
    } else if (target.matches('[data-check-follow]')) {
      toggleFollowSelection(target);
    } else if (target.matches('[data-check-dynamic]')) {
      toggleDynamicSelection(target);
    } else if (target.matches('[data-check-fav]')) {
      toggleFavSelection(target);
    }
  }

  function onDocumentKeydown(event) {
    if (event.key === 'Escape' && dialogState) {
      closeDialog();
      return;
    }
    if (
      event.key === 'Enter' &&
      dialogState &&
      event.target &&
      event.target.id === 'dialog-phrase'
    ) {
      const confirmBtn = $('#modal-root [data-dialog-action="confirm"]');
      if (confirmBtn && !confirmBtn.disabled) confirmBtn.click();
    }
  }

  /* ================================================== 15. boot + hooks */

  function installImageFallback() {
    document.addEventListener(
      'error',
      (event) => {
        const img = event.target;
        if (!img || img.tagName !== 'IMG') return;
        if (img.id === 'qr-image') return;
        if (img.dataset.fallbackApplied === '1') return;
        img.dataset.fallbackApplied = '1';
        if (img.dataset.cover === '1') {
          img.src = svgCover(Number(img.dataset.seed) || 0);
          return;
        }
        const name = img.dataset.name || img.getAttribute('alt') || 'B';
        img.src = fallbackAvatar(String(name).slice(0, 1) || 'B');
      },
      true
    );
  }

  async function probeBackend() {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 2500);
    try {
      const res = await fetch('/api/health', {
        signal: controller.signal,
        headers: { Accept: 'application/json' },
        credentials: 'same-origin',
      });
      if (!res.ok) throw new Error('bad status');
      const data = await res.json();
      state.health = data;
      state.mock = !!(data && data.mock);
    } catch (_) {
      enableDemo('未检测到本地服务，已切换到演示数据。');
    } finally {
      clearTimeout(timer);
    }
  }

  async function loadMe() {
    try {
      const me = await api.get('/api/me');
      state.loggedIn = !!(me && me.logged_in);
      state.user = (me && me.user) || null;
    } catch (err) {
      state.loggedIn = false;
      state.user = null;
      if (err && err.status !== 401) recordError('loadMe: ' + err.message);
    }
  }

  function bindGlobalEvents() {
    document.addEventListener('click', onDocumentClick, false);
    document.addEventListener('mousedown', onDocumentMouseDown, false);
    document.addEventListener('input', onDocumentInput, false);
    document.addEventListener('change', onDocumentChange, false);
    document.addEventListener('keydown', onDocumentKeydown, false);
    window.addEventListener('hashchange', renderRoute);

    const logoutBtn = $('#nav-logout');
    if (logoutBtn) logoutBtn.addEventListener('click', logout);
    const refreshBtn = $('#qr-refresh');
    if (refreshBtn) refreshBtn.addEventListener('click', () => startQrLogin());
    const demoBtn = $('#qr-demo');
    if (demoBtn) demoBtn.addEventListener('click', demoSimulateLogin);
    const shutdownBtn = $('#shutdown-btn');
    if (shutdownBtn) shutdownBtn.addEventListener('click', openShutdownDialog);
  }

  function defaultRoute() {
    if (location.hash) return;
    const route = state.loggedIn ? 'dynamics' : 'login';
    history.replaceState(null, '', '#/' + route);
  }

  async function boot() {
    bindGlobalEvents();
    installImageFallback();

    if (QS.get('debug') === '1') {
      setTimeout(() => {
        const inner = window.innerWidth;
        const scrollW = document.documentElement.scrollWidth;
        const offenders = [];
        $$('body *').forEach((el) => {
          const r = el.getBoundingClientRect();
          if (r.right > inner + 1 || r.left < -1) {
            if (offenders.length < 25) {
              offenders.push(
                `${el.tagName.toLowerCase()}.${String(el.className || '').replace(/\s+/g, '.').slice(0, 60)} L=${Math.round(r.left)} R=${Math.round(r.right)} W=${Math.round(r.width)}`
              );
            }
          }
        });
        const pre = document.createElement('pre');
        pre.id = 'debug-info';
        pre.hidden = true;
        pre.textContent = JSON.stringify({ inner, scrollW, offenders }, null, 1);
        document.body.appendChild(pre);
        document.title = `DEBUG ${inner}/${scrollW}`;
      }, 1200);
    }

    if (FORCE_DEMO) {
      enableDemo(null);
      state.loggedIn = QS.get('guest') !== '1';
      state.user = state.loggedIn ? DEMO_USER : null;
    } else {
      await probeBackend();
      if (DEMO) {
        state.loggedIn = QS.get('guest') !== '1';
        state.user = state.loggedIn ? DEMO_USER : null;
      } else {
        await loadMe();
        if (state.loggedIn) await loadSettings(false);
      }
    }

    updateNav();
    defaultRoute();
    renderRoute();

    if (state.loggedIn) startTaskPolling();

    if (DEMO) setupDemoScreenshotState();
  }

  /* waits for the view's own initial fetches instead of racing them */
  async function ensureRouteData(route) {
    if (!state.loggedIn) return;
    if (route === 'follows') {
      await waitFor(() => state.follows.loaded || !state.loggedIn, 6000);
      if (!state.followStats) await fetchFollowStats();
    } else if (route === 'favorites') {
      await waitFor(
        () =>
          (state.favorites.foldersLoaded &&
            (state.favorites.loaded || !state.favorites.folderId)) ||
          !state.loggedIn,
        6000
      );
    } else if (route === 'toview') {
      await waitFor(() => state.toview.loaded || !state.loggedIn, 6000);
    } else if (route === 'dynamics') {
      await waitFor(() => state.dynamics.loaded || !state.loggedIn, 6000);
    }
  }

  /* demo/screenshot hooks: ?demo=1&open=favorite-clear&task=1&scroll=bottom
     These only run in demo mode and exist so headless screenshots can show
     dialogs / task overlay without synthetic clicks. */
  async function setupDemoScreenshotState() {
    if (!DEMO) return;
    const openHook = QS.get('open');
    const taskHook = QS.get('task');
    const scrollHook = QS.get('scroll');

    if (taskHook === '1' || taskHook === 'cooldown') {
      await waitFor(() => state.loggedIn, 3000);
      try {
        const mids = demo.follows.slice(0, 12).map((item) => item.mid);
        const task = await api.post('/api/tasks', {
          type: 'unfollow',
          params: { mids, reason: 'manual', ack_risk: true },
        });
        if (taskHook === 'cooldown' && task) {
          task.status = 'cooldown';
          task.cooldown_until = isoLocalAfter(9 * 60 + 41);
          task.message = '触发风控，冷却结束后继续';
          const timer = demo.taskTimers.get(task.id);
          if (timer) {
            clearInterval(timer);
            demo.taskTimers.delete(task.id);
          }
        }
        await pollTasks({ silent: true });
      } catch (_) {
        /* ignore demo hook failures */
      }
    }

    if (openHook) {
      await waitFor(() => state.loggedIn, 3000);
      try {
        if (openHook === 'unfollow') {
          navigate('follows');
          await ensureRouteData('follows');
          state.follows.items.slice(0, 3).forEach((item) => state.follows.selected.add(item.mid));
          renderFollowList();
          openUnfollowDialog();
        } else if (openHook === 'favorite-clear') {
          navigate('favorites');
          await waitFor(() => state.favorites.foldersLoaded && state.loggedIn, 4000);
          openFavClearDialog();
        } else if (openHook === 'fav-clean-invalid') {
          navigate('favorites');
          await waitFor(() => state.favorites.foldersLoaded && state.loggedIn, 4000);
          openFavCleanInvalidDialog();
        } else if (openHook === 'toview-clear') {
          navigate('toview');
          await ensureRouteData('toview');
          openToviewClearDialog();
        } else if (openHook === 'delete-dynamics') {
          navigate('dynamics');
          await ensureRouteData('dynamics');
          state.dynamics.items.slice(0, 3).forEach((item) => state.dynamics.selected.add(item.id));
          renderDynamicList();
          openDeleteDynamicsDialog();
        } else if (openHook === 'classify-llm') {
          navigate('follows');
          await ensureRouteData('follows');
          state.follows.items.slice(0, 5).forEach((item) => state.follows.selected.add(item.mid));
          renderFollowList();
          openLlmClassifyDialog();
        }
      } catch (_) {
        /* ignore demo hook failures */
      }
    }

    if (scrollHook === 'bottom') {
      await waitFor(() => state.loggedIn, 3000);
      setTimeout(() => {
        window.scrollTo(0, document.body.scrollHeight);
      }, 600);
    }
  }

  boot();
})();
