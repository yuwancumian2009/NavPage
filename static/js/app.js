/* NavPage 前端应用 —— Vue 3 + Element Plus (UMD, 无构建) */
(function () {
  'use strict';

  const { createApp, ref, reactive, computed, onMounted, watch, nextTick } = Vue;
  const { ElMessage, ElMessageBox } = ElementPlus;

  const TOKEN_KEY = 'navpage_token';
  const MODE_KEY = 'navpage_mode';
  const CARD_GAP = 12;    // 组内网格间隔（与 .np-grid 的 gap 保持一致）
                          // 2026-10-16：改成 12 是为了让「单位格 = 大卡高度 62」成立
                          // （大卡 3 格 + 2 间隔 = 3*62 + 2*12 = 210px，宽度与旧版完全一致），
                          // 顺带和组间间隔 GROUP_GAP 统一。
  const GROUP_GAP = 12;   // 组间横向间隔（与 .np-main 的 column-gap 保持一致）

  const app = createApp({
    setup() {
      /* ---------------- 基础状态 ---------------- */
      const s = ref({});
      const groups = ref([]);
      const network = ref({ mode: 'lan', auto_mode: 'lan', client_ip: '', source: '' });
      const locked = ref(false);
      const q = ref('');
      const pw = ref('');
      const busy = ref(false);
      const engineIdx = ref(0);
      const wallpapers = ref([]);
      const wallUrlInput = ref('');

      const token = ref(localStorage.getItem(TOKEN_KEY) || '');
      const adminOpen = ref(false);
      const tab = ref('items');
      const items = ref([]);
      const categories = ref([]);
      const docker = ref({ ok: false });
      const scanResult = ref(null);
      const scanning = ref(false);
      const autocatting = ref(false);
      const importing = ref(false);
      const selNew = ref([]);
      // 外网地址导入（粘贴 Lucky 规则 → 解析 → 匹配卡片 → 写 url_wan）
      const wanText = ref('');
      const wanRows = ref([]);
      const wanItems = ref([]);
      const wanParsing = ref(false);
      const wanApplying = ref(false);
      const wanOverwrite = ref(false);
      const wanSelectedCount = computed(function () {
        return wanRows.value.filter(function (r) { return !!r.item_id; }).length;
      });
      const newCat = ref('');
      const selCats = ref([]);   // 分类页勾选的分类 id
      const newPw = ref('');
      const itemDlg = ref(false);
      const editItem = ref({});
      const icons = ref([]);
      // 服务状态小组件：{ itemId: {ok, text, detail, error} }
      const wdg = ref({});
      const widgetProviders = ref([]);
      const wdgTesting = ref(false);
      const wdgResult = ref(null);
      // 打开编辑弹窗时没读到已保存的小组件配置（卡片数据里 widget 被服务端摘掉了）——
      // 这时保存要跳过 widget 字段，免得把用户配好的小组件清空
      const widgetUnknown = ref(false);
      let wdgTimer = null;

      const modeChoice = ref(localStorage.getItem(MODE_KEY) || 'auto');
      const isMobile = ref(window.matchMedia('(max-width: 768px)').matches);
      window.matchMedia('(max-width: 768px)').addEventListener('change', function (e) {
        isMobile.value = e.matches;
      });

      const modeOptions = [
        { v: 'auto', t: '自动' },
        { v: 'lan', t: '内网' },
        { v: 'wan', t: '外网' }
      ];
      const themeOptions = [
        { v: 'glass', t: '毛玻璃' },
        { v: 'solid', t: '纯白' },
        { v: 'dark', t: '深色' }
      ];
      // 卡片尺寸（2026-10-16 重构）：只剩两档。
      //   lg 大卡 = 原「大」尺寸；xs 小卡 = 纯图标方形。
      // 全局设置只是「默认档」，条目/分类可以各自覆盖（优先级：条目 > 分类 > 全局）。
      const sizeOptions = [
        { v: 'lg', t: '大' },
        { v: 'xs', t: '小（纯图标）' }
      ];
      // 分类级：跟随全局 / 大 / 小
      // 用 'inherit' 而不是 ''：Element Plus 把空串当「未选择」，会显示占位符而不是「跟随全局」
      const sizeOptionsCat = [
        { v: 'inherit', t: '跟随全局' },
        { v: 'lg', t: '大' },
        { v: 'xs', t: '小' }
      ];
      // 条目级：跟随分类 / 大 / 小
      const sizeOptionsItem = [
        { v: 'inherit', t: '跟随分类' },
        { v: 'lg', t: '大' },
        { v: 'xs', t: '小（纯图标）' }
      ];
      const schemeOptions = ['https', 'http'];

      /* ---------------- 小组件字段定义 ---------------- */
      const WDG_FIELD_META = {
        url: { t: '地址', ph: 'http://192.168.1.10:8080' },
        username: { t: '用户名' },
        password: { t: '密码', pw: true },
        apikey: { t: 'API Key / Token', pw: true },
        path: { t: 'JSON 路径', ph: '如 data.count' },
        label: { t: '显示名', ph: '如 在线设备' },
        unit: { t: '单位', ph: '如 台' }
      };
      function _curProvider() {
        const w = editItem.value.widget || {};
        return widgetProviders.value.filter(function (x) { return x.type === w.type; })[0];
      }
      const wdgFields = computed(function () {
        const p = _curProvider();
        if (!p) return [];
        // 两种字段写法都要支持：
        //   "url"                                  ← 旧式：只给键名，标签查 WDG_FIELD_META
        //   {key:"url", label:"面板地址", ph:"…"}   ← 新式：后端直接给全量描述
        // 【坑，2026-10-16 用户报「三行同时变化」】新式对象没被解包时 f.k 是整个对象，
        // v-model="editItem.widget[f.k]" 会把所有行都绑到同一个键 "[object Object]" 上
        // → 改任意一行，三行一起变。必须解包成真正的键名。
        return (p.fields || []).map(function (k) {
          if (k && typeof k === 'object') {
            const key = k.key || k.k || '';
            const meta = WDG_FIELD_META[key] || {};
            return {
              k: key,
              t: k.label || meta.t || key,
              ph: k.ph || meta.ph || '',
              hint: k.hint || '',
              pw: (k.pw === undefined) ? !!meta.pw : !!k.pw
            };
          }
          return Object.assign({ k: k, hint: '' }, WDG_FIELD_META[k] || { t: k });
        });
      });
      const wdgHint = computed(function () {
        const p = _curProvider();
        return p ? p.hint : '';
      });

      /* ---------------- 时钟 ---------------- */
      const clock = ref('');
      const dateStr = ref('');
      function tick() {
        const d = new Date();
        const p = function (n) { return n < 10 ? '0' + n : '' + n; };
        clock.value = p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
        const wd = ['日', '一', '二', '三', '四', '五', '六'][d.getDay()];
        dateStr.value = d.getFullYear() + '年' + (d.getMonth() + 1) + '月' + d.getDate() + '日 星期' + wd;
      }

      /* ---------------- axios ---------------- */
      const api = axios.create({ timeout: 20000 });
      api.interceptors.request.use(function (cfg) {
        if (token.value) cfg.headers.Authorization = 'Bearer ' + token.value;
        return cfg;
      });
      api.interceptors.response.use(
        function (r) { return r; },
        function (err) {
          if (err.response && err.response.status === 401) {
            token.value = '';
            localStorage.removeItem(TOKEN_KEY);
            if (!locked.value) loadBootstrap();
          }
          return Promise.reject(err);
        }
      );
      function apiErr(err) {
        ElMessage.error(apiMsg(err));
      }
      // 取后端错误文案（不弹提示，给内嵌消息区用）
      function apiMsg(err) {
        let msg = (err && err.message) || '请求失败';
        if (err && err.response && err.response.data) {
          const d = err.response.data;
          msg = d.detail || d.error || d.message || msg;
        }
        return String(msg);
      }

      /* ---------------- 视图计算 ---------------- */
      /* 横竖屏自适应壁纸（2026-10-18）：电脑/横屏取横图，手机竖屏取竖图。
         支持的三种写法（按顺序判断，命中即用）：
           1. `{orientation}` 占位符 —— 任意接口通用，如 https://pic.x.com/api/anime/{orientation}
           2. 查询参数 `?orientation=pc|mobile` 或 `?orient=`
           3. 自托管随机图片 API 的常用形态：`/api/random` 自动换成 `/api/pc|mobile`，
              末段已经是 pc/mobile 的自动互换
         屏幕方向变了（手机旋转、桌面窗口拉成竖的）就重新取一张，并等新图预载完成再替换，
         避免中间闪一下白底。 */
      const isPortrait = ref(false);
      if (window.matchMedia) {
        const mq = window.matchMedia('(orientation: portrait)');
        isPortrait.value = mq.matches;
        const onFlip = function (e) { isPortrait.value = e.matches; };
        if (mq.addEventListener) mq.addEventListener('change', onFlip);
        else if (mq.addListener) mq.addListener(onFlip);   // 老 Safari
      }

      function orientWallUrl(u, portrait) {
        const o = portrait ? 'mobile' : 'pc';
        if (!u || u.indexOf('http') !== 0) return u;
        if (u.indexOf('{orientation}') >= 0) return u.split('{orientation}').join(o);
        if (/[?&](orientation|orient)=(pc|mobile)/i.test(u)) {
          return u.replace(/([?&](?:orientation|orient)=)(pc|mobile)/i, '$1' + o);
        }
        if (/\/api\/random\/?(\?.*)?$/i.test(u)) {
          return u.replace(/\/random(\/?)(\?.*)?$/i, '/' + o + '$1$2');
        }
        return u.replace(/\/(pc|mobile)(\/?)(\?.*)?$/i, function (m, seg, slash, qs) {
          return seg.toLowerCase() === o ? m : '/' + o + (slash || '') + (qs || '');
        });
      }

      const wallpaperUrl = computed(function () {
        const w = s.value.wallpaper || '';
        if (!w) {
          if (s.value.wallpaper_random && wallpapers.value.length) {
            const pick = wallpapers.value[Math.floor(Math.random() * wallpapers.value.length)];
            return pick.url;
          }
          return '';
        }
        if (w.indexOf('http') === 0) return orientWallUrl(w, isPortrait.value);
        return '/api/wallpaper/' + encodeURIComponent(w);
      });

      // 实际渲染用的地址：先预载再换，避免方向切换时闪白
      const bgUrl = ref('');
      watch(wallpaperUrl, function (u) {
        if (!u) { bgUrl.value = ''; return; }
        if (!bgUrl.value) { bgUrl.value = u; return; }   // 首屏直接上，不等预载
        let done = false;
        const swap = function () { if (!done) { done = true; bgUrl.value = u; } };
        const im = new Image();
        im.onload = swap;
        setTimeout(swap, 4000);      // 远端慢或失败也别一直卡在旧图
        im.src = u;
      }, { immediate: true });

      const bgStyle = computed(function () {
        const st = {};
        if (bgUrl.value) st.backgroundImage = 'url("' + bgUrl.value + '")';
        const b = Number(s.value.wallpaper_blur || 0);
        if (b > 0) st.filter = 'blur(' + b + 'px)';
        return st;
      });

      // 卡片固定宽度（px）。**所有卡片宽度一律相同**（1 格），所以组内轨道必须是
      // **固定宽度**而不是 1fr —— 用 1fr 的话各分组的卡片宽度会随分组浮动，就对不齐了。
      // 带小组件的卡片只体现在**高度**上（跨 2 行），不占额外宽度。
      // 卡片尺寸 2026-10-16 重构：只剩两档 ——
      //   lg 大卡：沿用原「大」尺寸（桌面恒定 210px，不再随全局设置变）
      //   xs 小卡：纯图标方形，边长 = 单位格（桌面 62px、手机 51px），高度与大卡一致
      // 组内网格因此改成「细单位格」：大卡占 3 格、小卡占 1 格，
      // 4*单位 + 3*间隔 = 大卡宽度 → 大卡尺寸与改动前完全一致（210px）。
      const BIG_W = 210;         // 大卡宽度（桌面）
      // 2026-10-16 用户要求「小卡高度和大卡高度保持一致」：
      // 小卡改成**边长 = 大卡高度(62px) 的方形**，于是「单位格」直接取大卡高度：
      //   大卡 = 3 格 + 2 间隔 = 3*62 + 2*12 = 210px（与旧版逐像素相同）
      //   小卡 = 1 格 = 62×62（旧版 4 格/45px 单位下是 45×45，比大卡矮 17px，混排对不齐）
      const CARD_H = 62;         // 行高 = 大卡高度 = 小卡边长
      const LG_SPAN = 3;         // 大卡占几格
      const cardSizePx = computed(function () { return BIG_W; });
      const cardMin = computed(function () { return unitW.value + 'px'; });

      // .np-main 的实测宽度 → 一个分组最多能排几格（分组宽度按内容自适应，得有上限）
      const mainW = ref(0);
      // 轨道列数必须是 LG_SPAN 的整数倍（2026-10-16 用户反馈「分类整体不居中」）：
      // 大卡占 3 格，列数不是 3 的倍数时最后一两格永远空着，而轨道是整体居中的 →
      // 卡片块在轨道内左对齐 → 整页看起来偏左。实测偏移 = (cols%3)*74/2 px：
      // 1366/1536 偏 74px，1280/1680/1920/2560 偏 37px；1440/1600/2048 恰好整除才正常。
      const snapCols = function (n) {
        n = Math.max(1, Math.floor(n));
        return n < LG_SPAN ? n : Math.floor(n / LG_SPAN) * LG_SPAN;
      };
      const maxCols = computed(function () {
        const w = mainW.value || 1560;
        return snapCols(Math.max(1, Math.floor((w + CARD_GAP) / (unitW.value + CARD_GAP))));
      });
      function measureMain() {
        const el = document.querySelector('.np-main');
        if (el && el.clientWidth) mainW.value = el.clientWidth;
      }

      /* 手机端（≤768px）：分组独占一行 + 卡片横向排列（2026-10-15 用户要求）。
         手机屏窄，若沿用桌面的固定 210px，一行连两张都放不下 → 又退回「单列竖向堆叠」。
         所以按 .np-main 的实测宽度反算：能放几列放几列（至少 2 列），
         列宽向下取整后**刚好填满一行**，右侧不留白。
         桌面端不用这两个值（卡片宽度仍是恒定的 cardSizePx）。 */
      const MOBILE_UNIT = 170;   // 手机端单列的目标宽度（= 桌面 md 尺寸）
      const mobCols = computed(function () {
        const w = mainW.value || 351;
        return Math.max(2, Math.floor((w + CARD_GAP) / (MOBILE_UNIT + CARD_GAP)));
      });
      const mobCardW = computed(function () {
        const w = mainW.value || 351;
        return Math.max(120, Math.floor((w - (mobCols.value - 1) * CARD_GAP) / mobCols.value));
      });
      // 组内网格实际使用的「大卡宽度」：手机端按屏宽自适应，桌面端恒定 210px
      const gridW = computed(function () {
        return isMobile.value ? mobCardW.value : cardSizePx.value;
      });
      // 单位格宽：大卡固定占 3 格（3*单位 + 2*间隔 = 大卡宽度）
      const unitW = computed(function () {
        if (!isMobile.value) return CARD_H;   // 桌面：单位格 = 大卡高度 62
        // 手机：大卡宽度按屏宽反算（≈177），单位格 = (大卡宽 - 2 间隔) / 3 ≈ 51
        return Math.max(1, Math.floor((gridW.value - (LG_SPAN - 1) * CARD_GAP) / LG_SPAN));
      });
      // 小卡恒占 1 格（= 大卡高度 62px 的方形；手机端单位格 51px，高度仍与桌面一致）

      /* 卡片实际尺寸：条目 card_size > 所属分类 card_size > 全局 card_size。
         带小组件的卡片强制大卡（小组件在方形小卡里放不下）。 */
      function sizeOf(it, g) {
        if (it && it.has_widget) return 'lg';
        const own = it && it.card_size;
        if (own === 'lg' || own === 'xs') return own;
        const cs = g && g.card_size;
        if (cs === 'lg' || cs === 'xs') return cs;
        return s.value.card_size === 'xs' ? 'xs' : 'lg';
      }
      // 占几格：大卡 3 格、小卡 1 格。小组件卡**同宽**（3 格 = 210px），
      // 只靠 .np-card.is_widget 的 grid-row: span 2 变成 2 倍高（不要改成 span 2 列）
      function spanOf(it, g) {
        if (it && it.has_widget) return LG_SPAN;   // 小组件卡与大卡同宽（3 格 = 210px）
        return sizeOf(it, g) === 'xs' ? 1 : LG_SPAN;
      }
      // 卡片占的网格轨道：大卡 span 4、小卡 span 1~2；col1Id 的「另起一行」补丁要合并进来
      function cardStyle(it, g) {
        const sp = spanOf(it, g);
        if (g && g.col1Id === it.id) return { gridColumn: '1 / span ' + sp };
        return sp > 1 ? { gridColumn: 'span ' + sp } : null;
      }

      // 每行放几个「分组」。0 = 自适应（按内容宽度流式打包，一行能塞几个塞几个）。
      // n > 0 时每个分组占固定的 1/n 行宽（卡片仍按固定宽度左对齐，不拉伸），
      // 所以 **n=1 就是「每个分类独占一行」**。
      // 手机上不套这条：手机端分组**一律独占一行**（CSS 里 .np-group { flex: 0 0 100% }），
      // 组内卡片横向排列的列数 / 列宽由 mobCols / mobCardW 按屏宽反算。
      const groupStyle = computed(function () {
        const n = Number(s.value.columns || 0);
        if (n > 0 && !isMobile.value) {
          return { flex: '0 0 calc((100% - ' + ((n - 1) * GROUP_GAP) + 'px) / ' + n + ')', maxWidth: 'none' };
        }
        return {};
      });

      /* 分组形状推演：给定列数，按 CSS grid 的「行优先、游标不回退」规则摆一遍
         （小组件卡片跨 2 行），得出总行数和空洞格数。用于给含小组件的分组挑列数。 */
      function gridShape(items, cols) {
        const occ = {};
        let r = 0, c = 0, rows = 0, used = 0;
        for (let i = 0; i < items.length; i++) {
          const w = Math.min(items[i]._span || LG_SPAN, cols);   // 占几格（大卡 3 / 小卡 1）
          const h = items[i].has_widget ? 2 : 1;           // 小组件卡片跨 2 行
          for (;;) {
            if (c + w > cols) { c = 0; r++; }
            let ok = true;
            for (let d = 0; d < h && ok; d++) {
              for (let k = 0; k < w; k++) { if (occ[(r + d) + ':' + (c + k)]) { ok = false; break; } }
            }
            if (ok) break;
            c++;
          }
          for (let d = 0; d < h; d++) {
            for (let k = 0; k < w; k++) occ[(r + d) + ':' + (c + k)] = 1;
          }
          if (r + h > rows) rows = r + h;
          used += w * h;
          c += w;
        }
        return { rows: rows, holes: rows * cols - used };
      }

      /* 含小组件卡片的分组：挑一个「空洞最少 → 行数最少 → 列数最多」的列数，
         让它排成一个基本填满的矩形。不挑列数的话，跨 2 行的卡片旁边会空一大片
         （实测「媒体」组 6 张卡只占 12 格里的 7 格）。maxCols 用于「缩进当前行」时封顶。 */
      function bestShape(items, maxCols) {
        // 下界：不排成「一行一张卡」。按格算 —— 两张最窄的卡并排（大卡 3 格 → 6 格）；
        // 组里只有一张卡时下界就是它自己的格数（否则会把分组撑得比卡片还宽）。
        const narrow = Math.min.apply(null, items.map(function (x) { return x._span || LG_SPAN; }));
        const lo = items.length >= 2 ? Math.min(maxCols, Math.max(narrow, LG_SPAN * 2)) : Math.min(maxCols, narrow);
        let best = null;
        for (let c = lo; c <= maxCols; c++) {
          const sh = gridShape(items, c);
          if (!best ||
              sh.holes < best.holes ||
              (sh.holes === best.holes && sh.rows < best.rows) ||
              (sh.holes === best.holes && sh.rows === best.rows && c > best.cols)) {
            best = { cols: c, rows: sh.rows, holes: sh.holes };
          }
        }
        return best || { cols: 1, rows: items.length, holes: 0 };
      }

      const filteredGroups = computed(function () {
        const kw = (q.value || '').trim().toLowerCase();
        const out = [];
        const W = gridW.value;                    // 大卡宽度（手机端 = 按屏宽反算）
        const U = unitW.value;                    // 单位格宽（大卡 3 格 / 小卡 1 格）
        // 手机端 mobCols 是「几张大卡」→ 换算成单位格数
        const cap = isMobile.value ? mobCols.value * LG_SPAN : maxCols.value;
        const MAIN = mainW.value || 1560;
        // 「每行分组数」> 0：每个分组占固定的 1/n 行宽（见 groupStyle），组内列数要按
        // **这一格的宽度**算、不能按整行算；那条路不参与下面的流式排布（各占各的格）。
        const fixedRows = Number(s.value.columns || 0) > 0;
        // 手机端分组独占整行 → 组内列数就是「整行能放几列」，与桌面那套 1/n 无关
        const capOf = isMobile.value
          ? mobCols.value * LG_SPAN
          : (fixedRows
              ? snapCols(Math.max(1, Math.floor(((MAIN - (fixedRows - 1) * GROUP_GAP) / fixedRows + CARD_GAP) / (U + CARD_GAP))))
              : cap);
        let used = 0;   // 当前行已占用宽度（含组间间隔），与 .np-main 的 flex-wrap 同步
        for (let i = 0; i < groups.value.length; i++) {
          const g = groups.value[i];
          const arr = kw
            ? g.items.filter(function (it) {
                return ((it.name || '') + ' ' + (it.description || '') + ' ' + (it.url || ''))
                  .toLowerCase().indexOf(kw) >= 0;
              })
            : g.items;
          // 空分类也要渲染（2026-10-16 用户反馈「新建的没有卡片的分类在导航页看不见，
          // 没法拖卡片进去」）：原来这里直接跳过，分类整个消失、连拖拽落点都没有。
          // 搜索时仍然隐藏（没有内容可匹配）。
          if (!arr.length) {
            if (kw) continue;
            const ew = capOf * U + (capOf - 1) * CARD_GAP;
            if (!fixedRows && !isMobile.value) {
              used = (used === 0 || used + GROUP_GAP + ew > MAIN) ? ew : used + GROUP_GAP + ew;
            }
            // items 必须是**原数组引用**（g.items）：moveItem 会往 target.items 里 splice，
            // 若这里新建一个 []，拖进空分类的卡片会从源分组删除后凭空消失（实测踩到）。
            out.push({ key: g.id === null ? 'none' : 'c' + g.id, cid: g.id,
                       name: g.name, card_size: g.card_size || '',
                       items: g.items, empty: true, plain: false, cols: capOf,
                       gridStyle: { gridTemplateColumns: 'repeat(' + capOf + ', ' + U + 'px)' },
                       headStyle: { width: ew + 'px' }, col1Id: null });
            continue;
          }
          if (arr.length) {
            // 每张卡占几格（大卡 3 / 小卡 1）：排布推演全按格算
            for (let k = 0; k < arr.length; k++) arr[k]._span = spanOf(arr[k], g);
            const narrow = arr.reduce(function (a, it) { return Math.min(a, it._span); }, 99);
            // 单条目分组且分组名就是条目名时，标题纯属重复（自动扫描按 compose
            // 项目分组时最常见），标记出来让模板省略标题，页面更紧凑
            // 「分组名和唯一那张卡的名字一样就省略标题」只对**自动扫描**的条目生效。
            // 坑（2026-10-16 用户反馈「新建的分类在导航页不显示分类名」）：原来的 norm 把非
            // [a-z0-9] 的字符全删掉，中文名一律变成空串 → 空串 === 空串 → 任何「只有 1 个条目」
            // 的分组都被判成「标题重复」而隐藏标题。中文名回退原文比较，手动建的条目也不省略。
            const norm = function (x) {
              const a = (x || '').toLowerCase().replace(/[^a-z0-9]/g, '');
              return a || (x || '').trim().toLowerCase();
            };
            const plain = arr.length === 1 && arr[0].source !== 'manual' &&
                          norm(arr[0].name) === norm(g.name);
            // 组内列数：每张卡都占 1 格（宽度一致），普通分组就是「条目数」。
            // 但小组件卡片高度跨 2 行，分组形状不再是一行排开，得挑列数才不留空洞。
            const hasWidget = arr.some(function (it) { return it.has_widget; });
            // 列数（2026-10-15 用户第二次反馈定稿：「监控这种一行没满的，还是要跟上面的左对齐」）：
            // 「每行分组数」> 0（含手机端）时一律 = **整行满格** capOf，**不按实际卡片数收窄**。
            // 收窄的后果：这一组的轨道模板变窄，.np-grid 居中后整块偏右，与上下分组的左边缘错开
            // （监控 7 张 vs 满格 8 列 → 左边缘差 110px），末行也没法和上一行对齐。
            // 按满格算：所有分组共用同一条左边缘；行内从左往右排、没排满就左对齐（轨道固定）。
            // 流式排布（每行分组数 = 0）例外：那里靠「分组宽度」把多个分组塞进一行，宽度必须贴合卡片。
            let cols = (fixedRows || isMobile.value)
              ? capOf
              : (hasWidget ? bestShape(arr, capOf).cols
                           : Math.max(1, Math.min(arr.reduce(function (a, it) { return a + it._span; }, 0), capOf)));
            // 手机端每个分组都独占一行（CSS 定死，flex: 0 0 100%），
            // 不需要「缩列塞进当前行」那套推演，否则列数会被误缩、右侧留白。
            if (!fixedRows && !isMobile.value) {
              const w0 = cols * U + (cols - 1) * CARD_GAP;
              // 放不进当前行时先试着缩列塞进去 —— 直接甩到下一行会在行尾留一大块空白。
              // 但不缩到 1 列：单列就是竖向堆叠，正是要避免的观感。
              if (used > 0 && used + GROUP_GAP + w0 > MAIN) {
                // 剩余空间能放几列：可用宽度要扣掉组间间隔，而 cols 列的组宽是
                // cols*(W+CARD_GAP) - CARD_GAP，所以这里要把 CARD_GAP 加回去。
                const fit = Math.floor((MAIN - used - GROUP_GAP + CARD_GAP) / (U + CARD_GAP));
                // 不缩到「一行放不下两张最窄的卡」（大卡 3 格 → 6 格）
                if (fit >= 1 && fit < cols && (fit >= 2 * narrow || cols <= narrow)) {
                  cols = hasWidget ? bestShape(arr, fit).cols : fit;
                }
              }
              const w = cols * U + (cols - 1) * CARD_GAP;
              if (used > 0 && used + GROUP_GAP + w > MAIN) used = w;              // 换行
              else used = used === 0 ? w : used + GROUP_GAP + w;
            }
            // 末行左对齐补丁（2026-10-15 用户第三次反馈「一行没有满的还是要跟上面的左对齐」）：
            // 含小组件的分组里，跨 2 行的卡片会占住下一行的左侧格子，CSS 自动排布就把剩下的
            // 最后一张塞进右边的空位（实测 1440 下 监控 的 komga-cn 落在第 6 列、414 下落在第 2 列），
            // 视觉上那一行没跟上面的行左对齐。这里照 CSS 的 sparse 游标规则模拟一遍自动排布，
            // **只在「最后一张不在第 1 列」时**给它 grid-column-start:1（模板里绑 :style），
            // 让它另起一行、从左侧开始。不含小组件的分组不处理：普通卡片不留洞，强制反而凭空造洞。
            let col1Id = null;
            if (hasWidget) {
              const occ = [];
              const lineCnt = {};                    // 每个「起始网格行」上有几张卡 = 同一条视觉线
              let r0 = 0, c0 = 0, lastC = 0, lastR = 0;
              for (let i = 0; i < arr.length; i++) {
                const span = arr[i].has_widget ? 2 : 1;      // 行跨度
                const wspan = arr[i]._span;                  // 列跨度（格）
                let guard = 0;
                while (guard++ < 9999) {
                  if (c0 + wspan > cols) { r0++; c0 = 0; continue; }
                  let ok = true;
                  for (let k = 0; k < span && ok; k++) {
                    for (let j = 0; j < wspan; j++) {
                      if (occ[r0 + k] && occ[r0 + k][c0 + j]) { ok = false; break; }
                    }
                  }
                  if (ok) break;
                  c0++;
                }
                for (let k = 0; k < span; k++) {
                  occ[r0 + k] = occ[r0 + k] || [];
                  for (let j = 0; j < wspan; j++) occ[r0 + k][c0 + j] = true;
                }
                lineCnt[r0] = (lineCnt[r0] || 0) + 1;
                lastR = r0; lastC = c0; c0 += wspan;
              }
              // 收紧条件（实测 1920/1280 会误伤）：只有「最后一张**独占一条视觉线**且不在第 1 列」
              // 才是真的掉进小组件跨行占位留下的洞（1440 的第 6 列、414 的第 2 列）。
              // 如果它本来就与别的卡同行（1920 落在第 7 列、1280 落在 biliup 右边），纠正反而会多出一行。
              if (arr.length && lineCnt[lastR] === 1 && lastC !== 0) col1Id = arr[arr.length - 1].id;
            }
            // 一律固定轨道：卡片宽度不随分组宽度浮动。
            // （旧实现在「每行分组数」那条路用 auto-fit minmax(W,1fr)，分组被拉成整行后
            //  2 张卡会被撑到 700+px，与「卡片等宽」的既定标准冲突，已去掉。）
            // 小卡边长 = 大卡高度 → 行高恒为 62px，不需要再压缩（旧版全小卡时会压到 45px）
            const gridStyle = { gridTemplateColumns: 'repeat(' + cols + ', ' + U + 'px)' };
            // 标题栏跟卡片块同宽（2026-10-15 用户要求「窗口缩放时卡片要横向居中」）：
            // 组内轨道是固定宽度 + .np-grid 居中后，若标题仍是整行宽，标题就会顶在最左边、
            // 与居中的卡片错开。给出与轨道总宽一致的宽度，配合 CSS 的 margin:0 auto，
            // 标题左边缘与第一张卡对齐、右侧那条渐变线正好收在最后一张卡右边。
            const blockW = cols * U + (cols - 1) * CARD_GAP;
            const headStyle = { width: blockW + 'px' };
            out.push({ key: g.id === null ? 'none' : 'c' + g.id, cid: g.id,
                       name: g.name, card_size: g.card_size || '',
                       items: arr, plain: plain, cols: cols, gridStyle: gridStyle,
                       headStyle: headStyle, col1Id: col1Id });
          }
        }
        return out;
      });

      const drawerSize = computed(function () { return isMobile.value ? '100%' : '780px'; });

      /* ---------------- 小工具 ---------------- */
      function initial(name) {
        const t = (name || '').trim();
        return t ? t[0].toUpperCase() : '?';
      }
      function colorOf(name) {
        const t = name || '?';
        let h = 0;
        for (let i = 0; i < t.length; i++) h = (h * 31 + t.charCodeAt(i)) % 360;
        return 'linear-gradient(135deg, hsl(' + h + ',62%,54%), hsl(' + ((h + 42) % 360) + ',62%,44%))';
      }
      function hostOf(url) {
        if (!url) return '未设置地址';
        return String(url).replace(/^https?:\/\//, '').replace(/\/+$/, '');
      }
      function onIconError(it) { it.icon_url = ''; }
      function onCardClick(e, it) {
        if (layoutMode.value) { e.preventDefault(); return; }
        if (!it.url) {
          e.preventDefault();
          ElMessage.warning('「' + it.name + '」还没有设置地址');
        }
      }
      // 卡片悬停提示：把小组件明细拼进去（卡片上只放一行，细节靠这里看）
      function cardTitle(it) {
        const base = it.description || it.name || '';
        const w = wdg.value[it.id];
        if (!w) return base;
        if (!w.ok) return base + '\n状态获取失败：' + (w.error || '');
        const lines = (w.detail || []).map(function (d) { return d[0] + '：' + d[1]; });
        return base + (lines.length ? '\n' + lines.join('\n') : '');
      }

      function catName(cid) {
        const c = categories.value.filter(function (x) { return x.id === cid; })[0];
        return c ? c.name : '未分类';
      }
      function countInCat(cid) {
        return items.value.filter(function (i) { return i.category_id === cid; }).length;
      }

      /* ---------------- 载入 ---------------- */
      async function loadBootstrap() {
        try {
          // 「自动」模式先让浏览器探测内网是否可达（手动选了内网/外网则直接用）
          const eff = await autoModeNow();
          const r = await api.get('/api/bootstrap', { params: { mode: eff || 'auto' } });
          const d = r.data;
          if (d.app) appMeta.value = d.app;
          if (d.settings) s.value = Object.assign({}, s.value, d.settings);
          locked.value = !!d.locked;
          if (!d.locked) {
            groups.value = d.groups || [];
            network.value = d.network || network.value;
            // 记住内网地址：下次首屏（登录前 Bootstrap 不下发这个字段）也能立刻开始探测
            if (network.value.lan_base) {
              try { localStorage.setItem(LANBASE_KEY, network.value.lan_base); } catch (e) {}
            }
            engineIdx.value = Number(s.value.search_engine_default || 0);
            document.title = s.value.site_title || '导航';
            await loadWidgets();
            startWidgetTimer();
            await loadStatus();
            startStatusTimer();
          } else {
            // 未登录：一个条目都不留
            groups.value = [];
            wdg.value = {};
            stopWidgetTimer();
            st.value = {};
            stopStatusTimer();
          }
        } catch (e) { apiErr(e); }
      }

      /* ---------------- 服务状态小组件 ---------------- */
      async function loadWidgets() {
        if (locked.value || !token.value || !s.value.widgets_enabled) {
          wdg.value = {};
          return;
        }
        const ids = [];
        groups.value.forEach(function (g) {
          (g.items || []).forEach(function (i) { if (i.has_widget) ids.push(i.id); });
        });
        if (!ids.length) { wdg.value = {}; return; }
        try {
          const r = await api.get('/api/widgets', { params: { ids: ids.join(',') } });
          wdg.value = r.data.widgets || {};
        } catch (e) { /* 小组件挂了不影响导航本身 */ }
      }

      function startWidgetTimer() {
        stopWidgetTimer();
        const iv = Math.max(10, Number(s.value.widget_interval || 60)) * 1000;
        // 页面在后台时跳过这一轮：小组件每一轮都要面板去外网点 qb/emby/哪吒，白烧请求
        //（状态灯探测早就这么干了，小组件之前漏了）。回到前台由 visibilitychange 补一轮。
        wdgTimer = setInterval(function () { if (!document.hidden) loadWidgets(); }, iv);
      }
      function stopWidgetTimer() {
        if (wdgTimer) { clearInterval(wdgTimer); wdgTimer = null; }
      }

      /* 小组件悬停详情：把后端给的 detail 渲染成浮层（mihomo 的策略组线路/延迟就在这看）。
         位置用 fixed + 卡片矩形算，贴着卡片下方；下面放不开就翻到上方。
         面板自身可悬停（移鼠标进去不会立刻消失），滚页面自动收起。 */
      const wpop = ref(null);
      let wpopHideTimer = null;
      let wpopScrollBound = false;

      function wpopClearLater() {
        clearTimeout(wpopHideTimer);
        wpopHideTimer = setTimeout(function () { wpop.value = null; }, 160);
      }

      function wpopShow(it, ev) {
        if (isMobile.value) return;                       // 触屏没有 hover，别乱弹
        const w = wdg.value[it.id];
        if (!w || !w.ok || !w.detail || !w.detail.length) return;
        clearTimeout(wpopHideTimer);
        if (!wpopScrollBound) {
          wpopScrollBound = true;
          // 捕获阶段能收到面板自身的滚动，那种别关（否则滚列表浮层就没了）
          window.addEventListener('scroll', function (e) {
            const t = e.target;
            if (t && t.closest && t.closest('.np-wpop')) return;
            wpop.value = null;
          }, true);
        }
        const el = (ev && (ev.currentTarget || ev.target)) || null;
        if (!el) return;
        // 以整张卡片为锚点，浮层贴着卡片下沿，不会压住卡片自己的指标
        const anchor = (el.closest && el.closest('.np-card')) || el;
        const r = anchor.getBoundingClientRect();
        const W = Math.min(330, window.innerWidth - 24);
        const wantH = Math.min(460, 44 + w.detail.length * 22);
        const below = window.innerHeight - r.bottom - 12;
        const above = r.top - 12;
        const style = { width: W + 'px', maxHeight: Math.max(150, Math.min(wantH, 460)) + 'px' };
        if (below >= Math.min(180, wantH) || below >= above) {
          style.top = (r.bottom + 8) + 'px';
          style.maxHeight = Math.max(140, Math.min(wantH, below - 10)) + 'px';
        } else {
          style.bottom = (window.innerHeight - r.top + 8) + 'px';
          style.maxHeight = Math.max(140, Math.min(wantH, above - 10)) + 'px';
        }
        style.left = Math.max(12, Math.min(r.left, window.innerWidth - W - 12)) + 'px';
        wpop.value = { title: it.name, sub: w.text || '', rows: w.detail, style: style };
      }

      function wpopHide() { wpopClearLater(); }

      function wpopHold(on) {
        if (on) clearTimeout(wpopHideTimer);
        else wpopClearLater();
      }

      /* ---------------- 容器在线状态灯 ----------------
         只对「容器卡片」生效（container_name 非空）；网站收藏卡片没有 container_name，
         下面 containerIds() 自然就不会把它们的 id 发给后端，卡片上也不会有圆点。 */
      const st = ref({});
      let stTimer = null;

      function containerIds() {
        const ids = [];
        groups.value.forEach(function (g) {
          (g.items || []).forEach(function (i) { if (i.container_name) ids.push(i.id); });
        });
        return ids;
      }

      async function loadStatus() {
        if (locked.value || !token.value || !s.value.status_dot) { st.value = {}; return; }
        const ids = containerIds();
        if (!ids.length) { st.value = {}; return; }
        try {
          const r = await api.get('/api/status', { params: { ids: ids.join(',') } });
          st.value = r.data.status || {};
        } catch (e) { /* 探测失败不影响导航本身 */ }
      }

      function startStatusTimer() {
        stopStatusTimer();
        const iv = Math.max(10, Number(s.value.widget_interval || 60)) * 1000;
        stTimer = setInterval(function () { if (!document.hidden) loadStatus(); }, iv);
      }
      function stopStatusTimer() {
        if (stTimer) { clearInterval(stTimer); stTimer = null; }
      }

      // 圆点：网站收藏卡片 / 关掉开关 / 还没探到结果时都返回 null（不渲染 span）
      function dotOf(it) {
        if (!s.value.status_dot || !it || !it.container_name) return null;
        return st.value[it.id] || null;
      }
      function dotTip(it) {
        const d = dotOf(it); if (!d) return '';
        const ms = d.ms || 0;
        if (d.state === 'down') {
          return ms >= 1000 ? '访问不了：探测超时（' + ms + 'ms 无响应）'
                            : '访问不了：连接被拒绝（' + ms + 'ms）';
        }
        if (d.kind === 'tcp') return '端口可连通：' + ms + 'ms（该服务不返回 HTTP，按端口判定）';
        if (d.code >= 500) return '服务异常：HTTP ' + d.code + '（' + ms + 'ms）';
        if (d.state === 'slow') return '延迟偏高：' + d.ms + 'ms';
        return '正常：' + d.ms + 'ms';
      }

      async function loadWidgetProviders() {
        if (widgetProviders.value.length) return;
        try {
          const r = await api.get('/api/admin/widget/providers');
          widgetProviders.value = r.data.providers || [];
        } catch (e) { /* 忽略 */ }
      }

      function onWidgetTypeChange(v) {
        wdgResult.value = null;
        widgetUnknown.value = false;   // 用户明确选了类型（含「不显示」），保存时按所选提交
        const old = editItem.value.widget || {};
        if (!v) { editItem.value.widget = { type: '' }; return; }
        // 换类型时保留已填的字段（同一个服务地址/密码通常可复用），清掉测试结果
        const keep = { type: v };
        ['url', 'username', 'password', 'apikey', 'path', 'label', 'unit'].forEach(function (k) {
          if (old[k]) keep[k] = old[k];
        });
        editItem.value.widget = keep;
      }

      async function testWidget() {
        const w = editItem.value.widget;
        if (!w || !w.type) { ElMessage.warning('先选一个小组件类型'); return; }
        wdgTesting.value = true;
        wdgResult.value = null;
        try {
          const r = await api.post('/api/admin/widget/test', { widget: w });
          wdgResult.value = r.data.result || null;
        } catch (e) { apiErr(e); }
        wdgTesting.value = false;
      }

      async function loadWallpapers() {
        try {
          const r = await api.get('/api/wallpapers');
          wallpapers.value = r.data.files || [];
        } catch (e) { /* 忽略 */ }
      }

      async function reload() {
        try {
          const r = await api.get('/api/admin/state');
          const d = r.data;
          items.value = d.items || [];
          categories.value = d.categories || [];
          const alive = categories.value.map(function (c) { return c.id; });
          selCats.value = selCats.value.filter(function (id) { return alive.indexOf(id) >= 0; });
          docker.value = d.docker || { ok: false };
          icons.value = d.icons || [];
          s.value = Object.assign({}, s.value, d.settings || {});
          syncWallInput();
          await loadWidgetProviders();
        } catch (e) { apiErr(e); }
        await loadWallpapers();
      }

      async function boot() {
        tick();
        setInterval(tick, 1000);
        await loadBootstrap();
        recheckLan(false);      // 首屏（登录前）拿不到内网地址时，这次补一次探测
        // 常驻复检：WiFi→5G 既不触发 online 也不触发 visibilitychange（页面一直开着，
        // 网络也没断），只有定时主动探一次，判定才能自己跟上 —— 30 秒内自动切到外网链接。
        setInterval(function () { if (!document.hidden) recheckLan(true); }, PROBE_EVERY);
        await loadWallpapers();
        // 角标先发（只是个轻量 GET），再等 reload 把卡片拉回来 —— 否则卡片多的时候
        // 角标要等 reload 完才出现，顶栏会先空一下（2026-10-18 实测 1.2s 内还没渲染）
        if (token.value) { loadImgBadge(); await reload(); }
      }

      /* ---------------- 登录 ---------------- */
      async function doLogin() {
        if (busy.value) return;
        const p = (pw.value || '').trim();
        if (p.length < 4) { ElMessage.warning('密码至少 4 位'); return; }
        busy.value = true;
        try {
          const url = s.value.has_password ? '/api/login' : '/api/admin/password';
          const r = await api.post(url, { password: p });
          token.value = r.data.token;
          localStorage.setItem(TOKEN_KEY, token.value);
          pw.value = '';
          ElMessage.success('已登录');
          await loadBootstrap();
          loadImgBadge();       // 登录后才知道有没有可更新镜像（不等 reload，先把角标点亮）
          await reload();
          recheckLan(false);    // 登录后才知道内网地址，这次补上探测
        } catch (e) { apiErr(e); }
        busy.value = false;
      }

      function logout() {
        token.value = '';
        localStorage.removeItem(TOKEN_KEY);
        imgBadge.value = 0;
        adminOpen.value = false;
        groups.value = [];
        wdg.value = {};
        stopWidgetTimer();
        ElMessage.success('已退出');
        loadBootstrap();
      }

      async function changePw() {
        if ((newPw.value || '').length < 4) { ElMessage.warning('密码至少 4 位'); return; }
        try {
          await api.post('/api/admin/password', { password: newPw.value });
          newPw.value = '';
          ElMessage.success('密码已修改');
        } catch (e) { apiErr(e); }
      }

      /* ---------------- 模式 ---------------- */
      function setMode(v) {
        modeChoice.value = v;
        localStorage.setItem(MODE_KEY, v);
        loadBootstrap();
      }

      /* ------------- 内网可达性探测（frp 下区分「在家 / 在外」） -------------
         frp 走 TCP 直通时，服务端拿不到手机的真实网络：隧道里客户端 IP 恒为 frpc 的
         私网地址，所以「在家 WiFi」和「切 5G」在服务端一模一样，只能给一个默认判定。
         而**浏览器自己知道答案**：让它去够一下内网地址上的 /api/lanprobe ——
         够得到 = 同局域网（用内网链接）；够不到 / 超时 = 在外网（用外网链接）。

         只作用于「自动」模式：手动选了 内网 / 外网 一律以手动为准，
         探测结果**不写入** localStorage（免得偷偷改掉用户的手动选择）。 */
      const lanProbe = ref({ ok: null, ts: 0, base: '', ms: 0, detail: '未探测' });
      const PROBE_TIMEOUT = 1500;      // 单次探测超时（毫秒）：超时即判定「在外网」
      const PROBE_TTL_FAIL = 60000;    // 判定不可达后的缓存（1 分钟）
      const PROBE_TTL_OK = 20000;      // 判定可达后的缓存（20 秒）
      const PROBE_EVERY = 30000;       // 常驻复检间隔 —— 拿着手机走出家门不会触发
                                       // online / visibilitychange，只能靠定时器主动探
      const PROBE_TOKEN = 'navpage-lan-probe-v1';   // 必须与 app/main.py 的 LAN_PROBE_TOKEN 一致
      const PROBE_MIN_GAP = 3000;      // 防抖：同一瞬间的多个事件（focus+visibilitychange+pageshow）
                                       // 只探一次，但也不让它们吃到 20 秒缓存（要立刻反映当前网络）
      const LANBASE_KEY = 'navpage_lanbase';   // 记住内网地址，首屏就能开始探测

      function lanBaseGuess() {
        return (network.value.lan_base || localStorage.getItem(LANBASE_KEY) || '').trim();
      }

      function isLanHost() {
        const base = lanBaseGuess();
        return !!base && location.hostname === base;
      }

      function probeTarget() {
        const base = lanBaseGuess();
        if (!base) return '';                     // 没配「内网地址」：无从探测
        if (location.protocol !== 'http:') return '';   // https 页面里够 http 会被混合内容拦掉
        const port = location.port ? ':' + location.port : '';
        return 'http://' + base + port + '/api/lanprobe?t=' + Date.now();
      }

      function setProbe(ok, t0, detail) {
        lanProbe.value = { ok: ok, ts: Date.now(), base: lanBaseGuess(),
                           ms: Date.now() - t0, detail: detail };
      }

      // true=内网可达 / false=不可达 / null=判定不了（交给服务端的 auto 规则）
      //
      // 关键：CORS 取回并**校验标记**，而不是「能拉到就算通」。用 no-cors 时任何 HTTP
      // 应答（中间代理、运营商门户页、假 404）都会让 promise 成功 → 在外网被误判成内网。
      function probeLan(force) {
        const url = probeTarget();
        if (!url) return Promise.resolve(null);
        if (isLanHost()) {
          lanProbe.value = { ok: true, ts: Date.now(), base: lanBaseGuess(), ms: 0,
                             detail: '当前就在内网地址上访问，无需探测' };
          return Promise.resolve(true);
        }
        const c = lanProbe.value;
        // 防抖优先：几百毫秒内的重复触发直接复用，避免一次「回到前台」连打三个请求
        if (c.ts && (Date.now() - c.ts) < PROBE_MIN_GAP) return Promise.resolve(c.ok);
        const ttl = c.ok ? PROBE_TTL_OK : PROBE_TTL_FAIL;
        if (!force && c.ts && c.base === lanBaseGuess() && (Date.now() - c.ts) < ttl) {
          return Promise.resolve(c.ok);
        }
        const ctl = new AbortController();
        const timer = setTimeout(function () { ctl.abort(); }, PROBE_TIMEOUT);
        const t0 = Date.now();
        return fetch(url, { mode: 'cors', cache: 'no-store', signal: ctl.signal })
          .then(function (r) {
            return r.text().then(function (t) { return { status: r.status, body: t }; });
          })
          .then(
            function (res) {
              clearTimeout(timer);
              let ok = false;
              try { ok = (JSON.parse(res.body) || {}).probe === PROBE_TOKEN; } catch (e) { ok = false; }
              setProbe(ok, t0, ok
                ? '内网可达（应答标记正确）'
                : '连上了但不是本面板的应答（HTTP ' + res.status + '，疑似代理/门户页）');
              return ok;
            },
            function (e) {
              clearTimeout(timer);
              setProbe(false, t0, '内网不可达：' +
                ((e && e.name === 'AbortError') ? ('超时 ' + PROBE_TIMEOUT + ' ms')
                                                : ('连不上（' + ((e && e.name) || 'error') + '）')));
              return false;
            }
          );
      }

      // 自动模式实际用哪套链接：'lan' / 'wan' / ''（空 = 用服务端判定）
      function autoModeNow() {
        if (modeChoice.value !== 'auto') return Promise.resolve(modeChoice.value);
        if (isLanHost()) return Promise.resolve('lan');   // 本来就在用内网地址访问
        return probeLan(false).then(function (ok) {
          return ok === null ? '' : (ok ? 'lan' : 'wan');
        });
      }

      // 探测结果与服务端判定不一致就重新拉一次（补首屏、网络切换的场景）
      async function recheckLan(force) {
        if (modeChoice.value !== 'auto' || isLanHost()) return;
        const ok = await probeLan(force);
        if (ok === null) return;
        const want = ok ? 'lan' : 'wan';
        if (network.value.mode !== want) await loadBootstrap(true);
      }

      async function reprobeLan() {
        lanProbe.value = { ok: null, ts: 0, base: lanBaseGuess(), ms: 0 };
        await recheckLan(true);
        ElMessage.success(lanProbe.value.ok ? '探测完成：内网可达' : '探测完成：内网不可达，按外网显示');
      }

      // 给设置页「当前判定」用的一句话说明（用户在手表/手机上排障全靠它）
      const probeText = computed(function () {
        const c = lanProbe.value;
        if (!lanBaseGuess()) return '未启用：没配「内网地址」，只能按域名/IP 判';
        if (location.protocol !== 'http:') return '未启用：页面是 https，探测 http 会被浏览器拦截';
        if (modeChoice.value !== 'auto') {
          return '未启用：当前是手动模式（' + (modeChoice.value === 'lan' ? '内网' : '外网') + '），手动优先';
        }
        if (!c.ts) return '探测中…';
        return c.detail + '（' + c.ms + ' ms，' + Math.round((Date.now() - c.ts) / 1000) +
          ' 秒前）→ 用' + (c.ok ? '内网' : '外网') + '链接';
      });
      const probeUrlText = computed(function () { return probeTarget() || '（未启用）'; });

      // 从后台回到前台 / 网络恢复 / iOS 从 bfcache 恢复时**强制**复检（防抖 3 秒兜底）：
      // 这些场景下用户就是要马上看到当前网络的对错，不能吃 20 秒缓存。
      document.addEventListener('visibilitychange', function () {
        if (document.hidden) return;
        recheckLan(true);
        loadWidgets();          // 后台期间小组件跳过刷新，回前台补一轮
      });
      window.addEventListener('online', function () { recheckLan(true); });
      window.addEventListener('pageshow', function () { recheckLan(true); });
      window.addEventListener('focus', function () { recheckLan(true); });

      /* ---------------- 搜索 ---------------- */
      /* 搜索框防浏览器自动填充（2026-10-19 用户反馈：Chrome 把上次搜的词填进来了）。
         autocomplete=off 对 Chrome 的「表单历史」经常无效，真正管用的是初始 readonly：
         自动填充会跳过只读输入框。用户一碰（pointerdown/touchstart/focus）就解除。 */
      const searchLocked = ref(true);
      let searchTouched = false;
      function unlockSearch() {
        searchTouched = true;
        searchLocked.value = false;
        // 同时立刻摘掉 DOM 属性：iOS 上要赶在 focus 之前解除，否则不弹键盘
        const el = document.querySelector('.np-search input, .np-search textarea');
        if (el && el.hasAttribute('readonly')) el.removeAttribute('readonly');
      }

      function onEnter() {
        const kw = (q.value || '').trim();
        if (!kw) return;
        const flat = filteredGroups.value.reduce(function (a, g) { return a.concat(g.items); }, []);
        if (flat.length === 1 && flat[0].url) {
          window.open(flat[0].url, '_blank');
          return;
        }
        const eng = (s.value.search_engines || [])[engineIdx.value];
        if (eng) window.open(eng.url + encodeURIComponent(kw), '_blank');
      }

      /* ---------------- 条目管理 ---------------- */

      /* 地址拆成「地址 + 端口」两个输入框（2026-10-15 用户要求：换服务只改端口号）。
         存储仍是单字段 url_lan / url_wan（形如 {scheme}://{host}:8096），只在编辑时拆开展示，
         保存时再拼回去 —— 已有 42 条数据形状不变，后端和 {host} 展开逻辑都不用动。
         地址尾部若有 /路径?查询，原样存进 tail：编辑框里不显示、保存时接回，保证无损。 */
      const URL_PORT_RE = /^(.*?):(\d+)([/?#].*)?$/;
      function splitUrl(raw) {
        const u = (raw || '').trim();
        if (!u) return { addr: '', port: '', tail: '' };
        const m = URL_PORT_RE.exec(u);
        if (!m) return { addr: u, port: '', tail: '' };   // 惰性匹配：先试 http: 失败后回溯到真正的端口冒号
        return { addr: m[1], port: m[2], tail: m[3] || '' };
      }
      function joinUrl(addr, port, tail) {
        const a = (addr || '').trim().replace(/\/+$/, '');
        const p = (port || '').trim().replace(/^:+/, '');
        const t = tail || '';
        /* 地址框留空、只填端口时补上默认模板，而不是存成残缺的 ':9091'：
           残缺值会让管理台地址框显示为空（看着像「没填占位符」），
           批量刷新图标还会拿 ':9091' 去请求而失败。 */
        if (!a) return p ? '{scheme}://{host}:' + p + t : t;
        return p ? a + ':' + p + t : a + t;
      }
      function fillUrlParts(it) {
        const L = splitUrl(it.url_lan), W = splitUrl(it.url_wan);
        it.addr_lan = L.addr; it.port_lan = L.port; it.tail_lan = L.tail;
        it.addr_wan = W.addr; it.port_wan = W.port; it.tail_wan = W.tail;
        return it;
      }

      function findAdminItem(id) {
        return (items.value || []).filter(function (x) { return x.id === id; })[0];
      }

      async function openItem(it) {
        // 卡片数据来自 /api/bootstrap，服务端为安全把 widget 摘掉了（只留 has_widget / widget_type）：
        // 直接拿它编辑会让小组件显示成「不显示」，一保存还把配置清空（2026-10-15 用户反馈）。
        // 所以编辑已有条目时换成管理端完整数据——本地已加载的 items 优先，没有再拉一次 /api/admin/state。
        let base = it;
        if (it && it.id) {
          base = findAdminItem(it.id) || it;
          if (!("widget" in base)) {
            try {
              const r = await api.get('/api/admin/state');
              const full = (r.data.items || []).filter(function (x) { return x.id === it.id; })[0];
              if (full) base = full;
            } catch (e) { /* 取不到就按原对象编辑 */ }
          }
        }
        widgetUnknown.value = !!(base && base.id && !("widget" in base));
        if (base) {
          editItem.value = Object.assign({}, base);
          // 尺寸选择器用 'inherit' 表示「跟随上级」（空串会被 Element Plus 当未选择）
          if (editItem.value.card_size !== 'lg' && editItem.value.card_size !== 'xs') {
            editItem.value.card_size = 'inherit';
          }
        } else {
          editItem.value = {
            id: 0, name: '', url_lan: '', url_wan: '', icon: '',
            description: '', category_id: null, hidden: false, source: 'manual',
            url_visible: -1, card_size: 'inherit'
          };
        }
        fillUrlParts(editItem.value);   // 拆成「地址 / 端口」两个框再展示
        // widget 必须是完整对象，模板里的 v-model 才有地方写
        editItem.value.widget = Object.assign(
          { type: '', url: '', username: '', password: '', apikey: '', path: '', label: '', unit: '' },
          editItem.value.widget || {}
        );
        wdgResult.value = null;
        loadWidgetProviders();
        itemDlg.value = true;
      }

      async function saveItem() {
        const it = editItem.value;
        if (!(it.name || '').trim()) { ElMessage.warning('名称不能为空'); return; }
        // 端口框只收数字（留空 = 不加端口）；地址栏里自带的端口不受影响
        const badPort = [[it.port_lan, '内网端口'], [it.port_wan, '外网端口']]
          .filter(function (p) { return (p[0] || '').trim() !== '' && !/^\d{1,5}$/.test((p[0] || '').trim()); })[0];
        if (badPort) { ElMessage.warning(badPort[1] + '只能填数字'); return; }
        busy.value = true;
        // 小组件只提交该类型用得到的字段（且值非空才写）：默认模板里的空键
        // （apikey/path/label/unit）以前会被一起写进库，每次保存都无谓改一遍数据
        let widgetOut = null;
        if (it.widget && it.widget.type) {
          widgetOut = { type: it.widget.type };
          const keys = (wdgFields.value || []).map(function (f) { return f.k; });
          (keys.length ? keys : ['url', 'username', 'password', 'apikey', 'path', 'label', 'unit'])
            .forEach(function (k) {
              const v = it.widget[k];
              if (v !== undefined && v !== null && String(v) !== '') widgetOut[k] = v;
            });
        }
        const url_lan = joinUrl(it.addr_lan, it.port_lan, it.tail_lan);
        const url_wan = joinUrl(it.addr_wan, it.port_wan, it.tail_wan);
        const payload = {
          name: it.name, url_lan: url_lan, url_wan: url_wan,
          icon: it.icon || '', description: it.description || '',
          category_id: (it.category_id === '' || it.category_id === undefined) ? null : it.category_id,
          hidden: !!it.hidden,
          url_visible: (it.url_visible === 0 || it.url_visible === 1) ? it.url_visible : -1,
          card_size: (it.card_size === 'lg' || it.card_size === 'xs') ? it.card_size : '',
          widget: widgetOut
        };
        // 没读到已保存的配置就别提交这个键（后端按传入键更新），避免静默清空小组件
        if (widgetUnknown.value) delete payload.widget;
        try {
          if (it.id) await api.put('/api/admin/items/' + it.id, payload);
          else await api.post('/api/admin/items', payload);
          itemDlg.value = false;
          await reload();
          await loadBootstrap();
          ElMessage.success('已保存');
        } catch (e) { apiErr(e); }
        busy.value = false;
      }

      function delItem(it) {
        ElMessageBox.confirm('确定删除「' + it.name + '」？', '提示', { type: 'warning' })
          .then(async function () {
            try {
              await api.delete('/api/admin/items/' + it.id);
              await reload(); await loadBootstrap();
              ElMessage.success('已删除');
            } catch (e) { apiErr(e); }
          }).catch(function () {});
      }

      async function delItemById(id) {
        try {
          await api.delete('/api/admin/items/' + id);
          await reload(); await loadBootstrap();
        } catch (e) { apiErr(e); }
      }

      /* 排除 / 恢复：后端会同时写 hidden 和 scan_exclude，这里只要同步本地设置副本 */
      async function setExcluded(it, excluded) {
        busy.value = true;
        try {
          const r = await api.post('/api/admin/items/' + it.id + '/exclude', { excluded: !!excluded });
          if (r.data.scan_exclude !== undefined) s.value.scan_exclude = r.data.scan_exclude;
          await reload();
          await loadBootstrap();
          ElMessage.success(excluded ? '已排除「' + it.name + '」，卡片不再显示' : '已恢复显示');
        } catch (e) { apiErr(e); }
        busy.value = false;
      }

      /* ---------------- 分类 ---------------- */
      const catAllChecked = computed(function () {
        return categories.value.length > 0 && selCats.value.length === categories.value.length;
      });
      const catIndeterminate = computed(function () {
        return selCats.value.length > 0 && selCats.value.length < categories.value.length;
      });
      function onCatCheck(cid, on) {
        const arr = selCats.value.slice();
        const i = arr.indexOf(cid);
        if (on && i < 0) arr.push(cid);
        else if (!on && i >= 0) arr.splice(i, 1);
        selCats.value = arr;
      }
      function toggleAllCats(on) {
        selCats.value = on ? categories.value.map(function (c) { return c.id; }) : [];
      }
      async function addCat() {
        if (!(newCat.value || '').trim()) return;
        try {
          await api.post('/api/admin/categories', { name: newCat.value.trim() });
          newCat.value = '';
          await reload();
        } catch (e) { apiErr(e); }
      }
      /* 分类顺序：上移 / 下移。后端已有 POST /api/admin/categories/reorder，
         这里只把调换后的整体顺序（每档 10）下发，再刷新设置页和首页分组。 */
      function catIdx(cid) {
        return categories.value.findIndex(function (x) { return x.id === cid; });
      }
      async function moveCat(c, dir) {
        const arr = categories.value.slice();
        const i = catIdx(c.id);
        const j = i + dir;
        if (i < 0 || j < 0 || j >= arr.length) return;
        const t = arr[i]; arr[i] = arr[j]; arr[j] = t;
        try {
          await api.post('/api/admin/categories/reorder',
                         { order: arr.map(function (x, k) { return [x.id, (k + 1) * 10]; }) });
          await reload(); await loadBootstrap();
        } catch (e) { apiErr(e); }
      }
      function renameCat(c) {
        ElMessageBox.prompt('新的分类名称', '改名', { inputValue: c.name })
          .then(async function (r) {
            try {
              await api.put('/api/admin/categories/' + c.id, { name: r.value });
              await reload(); await loadBootstrap();
            } catch (e) { apiErr(e); }
          }).catch(function () {});
      }
      // 分类的卡片尺寸：'' = 跟随全局，lg = 大，xs = 小。改完刷新首页（分组尺寸立即跟着变）
      async function setCatSize(c, v) {
        const nv = (!v || v === 'inherit') ? '' : v;
        if ((c.card_size || '') === nv) return;
        try {
          await api.put('/api/admin/categories/' + c.id, { card_size: nv });
          await reload(); await loadBootstrap();
          ElMessage.success(nv === '' ? '「' + c.name + '」已跟随全局'
                                      : '「' + c.name + '」已设为' + (nv === 'xs' ? '小卡' : '大卡'));
        } catch (e) { apiErr(e); }
      }
      function delCat(c) {
        ElMessageBox.confirm('删除分类「' + c.name + '」？其下条目会变成未分类。', '提示', { type: 'warning' })
          .then(async function () {
            try {
              await api.delete('/api/admin/categories/' + c.id);
              selCats.value = selCats.value.filter(function (id) { return id !== c.id; });
              await reload(); await loadBootstrap();
            } catch (e) { apiErr(e); }
          }).catch(function () {});
      }
      /* 批量删除：一次请求删多个（后端一个事务），先把影响面说清楚再动手 */
      function bulkDelCats() {
        const ids = selCats.value.slice();
        if (!ids.length) return;
        const picked = categories.value.filter(function (c) { return ids.indexOf(c.id) >= 0; });
        const nItems = picked.reduce(function (a, c) { return a + countInCat(c.id); }, 0);
        const head = picked.slice(0, 3).map(function (c) { return '「' + c.name + '」'; }).join('、');
        const tail = picked.length > 3 ? ' 等 ' + picked.length + ' 个分类' : '';
        ElMessageBox.confirm(
          '删除 ' + head + tail + '？' + (nItems ? '其下 ' + nItems + ' 个条目会变成未分类。' : ''),
          '批量删除分类',
          { type: 'warning', confirmButtonText: '删除', confirmButtonClass: 'el-button--danger' }
        ).then(async function () {
          try {
            const r = await api.post('/api/admin/categories/bulk_delete', { ids: ids });
            const d = r.data || {};
            selCats.value = [];
            await reload(); await loadBootstrap();
            ElMessage.success('已删除 ' + (d.deleted || ids.length) + ' 个分类' +
              (d.unassigned ? '，' + d.unassigned + ' 个条目已变为未分类' : ''));
          } catch (e) { apiErr(e); }
        }).catch(function () {});
      }

      /* ---------------- 扫描 ---------------- */
      async function doScan() {
        scanning.value = true;
        try {
          const r = await api.get('/api/admin/scan');
          scanResult.value = r.data;
          if (!r.data.ok) ElMessage.error(r.data.error || '扫描失败');
          selNew.value = [];
        } catch (e) { apiErr(e); }
        scanning.value = false;
      }
      function selectAllNew() {
        selNew.value = (scanResult.value ? scanResult.value.new : []).map(function (c) { return c.container_name; });
      }
      async function importSelected() {
        if (!selNew.value.length) return;
        importing.value = true;
        try {
          const r = await api.post('/api/admin/scan/import', { names: selNew.value, category_from_project: true });
          ElMessage.success('导入完成：新增 ' + r.data.added + '，更新 ' + r.data.updated);
          await reload(); await loadBootstrap(); await doScan();
        } catch (e) { apiErr(e); }
        importing.value = false;
      }
      /* ---------------- 外网地址导入（粘贴 Lucky 规则） ---------------- */
      async function wanPreview() {
        if (!wanText.value.trim()) return;
        wanParsing.value = true;
        try {
          const r = await api.post('/api/admin/wan/preview', { text: wanText.value });
          wanRows.value = (r.data.rows || []).map(function (x) {
            return Object.assign({}, x, { scheme: x.scheme || 'https' });
          });
          wanItems.value = r.data.items || [];
          if (!wanRows.value.length) ElMessage.warning('没解析出域名，检查一下粘贴的内容');
        } catch (e) { apiErr(e); }
        wanParsing.value = false;
      }
      async function wanApply() {
        const rows = wanRows.value
          .filter(function (r) { return !!r.item_id; })
          .map(function (r) {
            return { item_id: r.item_id, url: (r.scheme || 'https') + '://' + r.domain };
          });
        if (!rows.length) return;
        wanApplying.value = true;
        try {
          const r = await api.post('/api/admin/wan/apply', { rows: rows, overwrite: wanOverwrite.value });
          const d = r.data;
          ElMessage.success('写入 ' + d.updated + ' 张卡片'
            + (d.skipped ? '，跳过 ' + d.skipped + ' 张（已有外网地址）' : ''));
          wanRows.value = [];
          wanText.value = '';
          await reload();
        } catch (e) { apiErr(e); }
        wanApplying.value = false;
      }
      async function syncAll() {
        scanning.value = true;
        try {
          const r = await api.post('/api/admin/scan/sync');
          ElMessage.success('同步完成：新增 ' + r.data.added + '，更新 ' + r.data.updated);
          await reload(); await loadBootstrap(); await doScan();
        } catch (e) { apiErr(e); }
        scanning.value = false;
      }
      async function autocatAll() {
        autocatting.value = true;
        try {
          const r = await api.post('/api/admin/scan/autocat');
          const d = r.data || {};
          const parts = Object.keys(d.by_category || {}).map(function (k) { return k + ' ' + d.by_category[k]; });
          ElMessage.success('已归类 ' + (d.done || 0) + ' 个' + (parts.length ? '：' + parts.join(' · ') : '')
            + (d.skipped ? '（' + d.skipped + ' 个判断不出类型，保持未分类）' : ''));
          await reload(); await doScan();
        } catch (e) { apiErr(e); }
        autocatting.value = false;
      }
      async function refreshIcons(ids) {
        busy.value = true;
        try {
          const r = await api.post('/api/admin/refresh-icons', { ids: ids || null });
          ElMessage.success('图标抓取完成：成功 ' + r.data.done + '，失败 ' + r.data.fail);
          await reload();
        } catch (e) { apiErr(e); }
        busy.value = false;
      }

      /* ---------------- 设置 / 壁纸 ---------------- */

      // 把「已保存」的壁纸值回填到输入框。
      // 修复的 bug：以前只写不读 —— 刷新后面板里看不到自己填过的 URL，
      // 既没法核对、也不敢再点「使用」（空值被 useWallUrl 静默拦住，看着就像坏了）。
      // 只回填 http(s) 开头的值；其它值是本地上传的文件名，由下方「当前壁纸」展示。
      function syncWallInput() {
        const w = String(s.value.wallpaper || '').trim();
        wallUrlInput.value = /^https?:\/\//i.test(w) ? w : '';
      }

      async function saveSettings(patch) {
        busy.value = true;
        try {
          const body = { patch: patch || s.value };
          await api.put('/api/admin/settings', body);
          syncWallInput();
          await loadBootstrap();
          ElMessage.success('设置已保存');
        } catch (e) { apiErr(e); }
        busy.value = false;
      }

      // ---- 设置项自动保存（2026-10-16，用户反馈「网络安全里填过的信息刷新就不显示」）----
      // 这些字段过去只活在本地表单状态里：没点下面的「保存」按钮就刷新 / 关页 → 白填。
      // 现在改成改动即存（防抖 1.2s），页面上显示「已自动保存 HH:MM:SS」，失败明确报错。
      const autoSaveAt = ref('');
      // 设置落盘成功后：状态灯开关可能刚由关变开，此刻服务端才认，补探一次
      function afterSettingsSaved() {
        if (s.value.status_dot) loadStatus();
        loadAuto();     // 定时检查的开关/cron 刚改过 → 「下次运行」要跟着变
      }

      // 失焦立刻落盘：用户填完直接点别处/关页面时不用等防抖（index.html 的 @focusout 调用）
      async function flushAutoSave() {
        if (!autoSaveTimer) return;
        clearTimeout(autoSaveTimer); autoSaveTimer = null;
        try {
          await api.put('/api/admin/settings', { patch: s.value });
          lastSavedJson = JSON.stringify(s.value);
          syncWallInput();
          autoSaveAt.value = new Date().toTimeString().slice(0, 8);
          afterSettingsSaved();
        } catch (e) { autoSaveAt.value = ''; apiErr(e); }
      }
      let autoSaveTimer = null;
      let lastSavedJson = '';
      // 状态灯开关：关掉立刻清空圆点；打开要等设置存到服务端之后再探，
      // 否则后端还当「开关=关」，直接回空对象 —— 表现就是「重新打开后圆点不回来」。
      watch(function () { return s.value.status_dot; }, function (v) {
        if (v) {
          startStatusTimer();
          setTimeout(function () { if (s.value.status_dot) loadStatus(); }, 1800);
        } else { st.value = {}; stopStatusTimer(); }
      });

      watch(s, function () {
        const now = JSON.stringify(s.value || {});
        if (!token.value) { lastSavedJson = now; return; }
        if (!lastSavedJson) { lastSavedJson = now; return; }
        if (now === lastSavedJson || busy.value) return;
        if (autoSaveTimer) clearTimeout(autoSaveTimer);
        autoSaveTimer = setTimeout(async function () {
          try {
            await api.put('/api/admin/settings', { patch: s.value });
            lastSavedJson = JSON.stringify(s.value || {});
            syncWallInput();
            autoSaveAt.value = new Date().toTimeString().slice(0, 8);
            afterSettingsSaved();
          } catch (e) { autoSaveAt.value = ''; apiErr(e); }
        }, 900);
      }, { deep: true });

      async function uploadWall(ev) {
        const f = ev.target.files && ev.target.files[0];
        if (!f) return;
        const fd = new FormData();
        fd.append('file', f);
        busy.value = true;
        try {
          const r = await api.post('/api/admin/wallpaper', fd);
          s.value.wallpaper = r.data.name;
          await loadWallpapers();
          await saveSettings({ wallpaper: r.data.name });
          ElMessage.success('壁纸已上传');
        } catch (e) { apiErr(e); }
        busy.value = false;
        ev.target.value = '';
      }

      /* ---------------- 左上角图标（2026-10-16 用户要求） ----------------
         上传接口只存文件、返回 URL，写设置交给前端（和壁纸一致），
         这样「上传了但没点保存」不会把线上图标换掉。带 ?v= 是为了绕开
         服务端 24h 的 Cache-Control：同名文件覆盖后不加参数浏览器不刷新。 */
      const logoUrlInput = ref('');
      const logoUrl = computed(function () { return String(s.value.site_logo || ''); });
      async function uploadLogo(ev) {
        const f = ev.target.files && ev.target.files[0];
        if (!f) return;
        const fd = new FormData();
        fd.append('file', f);
        busy.value = true;
        try {
          const r = await api.post('/api/admin/logo', fd);
          s.value.site_logo = r.data.url + '?v=' + Date.now();
          await saveSettings({ site_logo: s.value.site_logo });
          ElMessage.success('图标已上传');
        } catch (e) { apiErr(e); }
        busy.value = false;
        ev.target.value = '';
      }
      async function useLogoUrl() {
        const u = (logoUrlInput.value || '').trim();
        if (!u) { ElMessage.warning('请先填写图片 URL，或点上方「上传图片」'); return; }
        if (!/^https?:\/\//i.test(u) && u.indexOf('/api/logo/') !== 0) {
          ElMessage.warning('URL 需以 http:// 或 https:// 开头'); return;
        }
        s.value.site_logo = u;
        await saveSettings({ site_logo: u });
      }
      async function clearLogo() {
        s.value.site_logo = '';
        logoUrlInput.value = '';
        await saveSettings({ site_logo: '' });
      }

      async function pickWall(name) {
        s.value.wallpaper = name;
        await saveSettings({ wallpaper: name });
      }
      async function useWallUrl() {
        const u = (wallUrlInput.value || '').trim();
        if (!u) { ElMessage.warning('请先填写图片 URL，或点上方「上传图片」'); return; }
        if (!/^https?:\/\//i.test(u)) { ElMessage.warning('URL 需以 http:// 或 https:// 开头'); return; }
        s.value.wallpaper = u;
        await saveSettings({ wallpaper: u });
      }
      async function clearWall() {
        s.value.wallpaper = '';
        await saveSettings({ wallpaper: '' });
      }
      function delWall(w) {
        ElMessageBox.confirm('删除壁纸「' + w.name + '」？', '提示', { type: 'warning' })
          .then(async function () {
            try {
              await api.delete('/api/admin/wallpaper/' + encodeURIComponent(w.name));
              await loadWallpapers();
              ElMessage.success('已删除');
            } catch (e) { apiErr(e); }
          }).catch(function () {});
      }

      /* ---------------- 设置页签栏：横向滑动 + 选中页签自动居中 ----------------
         窄屏下 6 个页签放不下，EP 默认用箭头平移（会把最后一个直接切掉），
         改成原生横向滚动：触摸滑动 / 滚轮 / 鼠标拖拽；切换时把当前页签滑到中间。 */
      let tabsSc = null;

      function tabsFade() {
        if (!tabsSc) return;
        const head = tabsSc.closest('.el-tabs__header'); if (!head) return;
        const max = tabsSc.scrollWidth - tabsSc.clientWidth;
        head.classList.toggle('fade-l', tabsSc.scrollLeft > 4);
        head.classList.toggle('fade-r', max > 4 && tabsSc.scrollLeft < max - 4);
      }

      function initTabsScroll() {
        const sc = document.querySelector('.np-tabs .el-tabs__nav-scroll');
        if (!sc || sc.__npBound) return;
        sc.__npBound = true;
        tabsSc = sc;
        sc.addEventListener('scroll', tabsFade, { passive: true });
        window.addEventListener('resize', tabsFade);
        sc.addEventListener('wheel', function (e) {          // 竖向滚轮 → 横向滑动
          if (Math.abs(e.deltaY) <= Math.abs(e.deltaX)) return;
          e.preventDefault();
          sc.scrollLeft += e.deltaY;
        }, { passive: false });
        let down = false, x0 = 0, l0 = 0;                     // 鼠标按住拖拽（触摸走原生）
        sc.addEventListener('pointerdown', function (e) {
          if (e.pointerType === 'touch' || e.button !== 0) return;
          down = true; x0 = e.clientX; l0 = sc.scrollLeft; sc.classList.add('dragging');
        });
        sc.addEventListener('pointermove', function (e) {
          if (down) sc.scrollLeft = l0 - (e.clientX - x0);
        });
        ['pointerup', 'pointerleave', 'pointercancel'].forEach(function (t) {
          sc.addEventListener(t, function () { down = false; sc.classList.remove('dragging'); });
        });
        tabsFade();
      }

      function centerTab() {
        if (!tabsSc) initTabsScroll();
        const sc = tabsSc; if (!sc) return;
        nextTick(function () {
          const it = sc.querySelector('.el-tabs__item.is-active'); if (!it) return;
          sc.scrollTo({ left: Math.max(0, it.offsetLeft - (sc.clientWidth - it.offsetWidth) / 2), behavior: 'smooth' });
          tabsFade();
        });
      }

      /* 被截断的单元格 hover 时给出完整内容（原生 title，零依赖） */
      function bindCellTitle() {
        document.addEventListener('mouseover', function (e) {
          const td = e.target.closest && e.target.closest('.ep-table td');
          if (!td) return;
          if (td.scrollWidth > td.clientWidth + 1 || (td.firstElementChild && td.firstElementChild.scrollWidth > td.firstElementChild.clientWidth + 1)) {
            if (!td.title) td.title = td.innerText.replace(/\s+/g, ' ').trim();
          }
        });
      }

      function openAdmin(tabName) {
        adminOpen.value = true;
        // 支持直接跳到某个 tab（顶栏「可更新镜像」角标点进来就是 images）
        if (tabName) tab.value = tabName;
        nextTick(function () { initTabsScroll(); bindCellTitle(); });
        if (token.value) reload();
      }

      /* ---------------- 卡片地址显示开关 ---------------- */
      // url_visible: -1 跟随全局设置，0 强制隐藏，1 强制显示
      function showUrlFor(it) {
        if (it.url_visible === 0) return false;
        if (it.url_visible === 1) return true;
        return !!s.value.show_url;
      }
      function barW(v) {
        let n = Number(v);
        if (!isFinite(n)) n = 0;
        n = Math.max(0, Math.min(1, n));
        return Math.max(3, n * 100) + '%';
      }

      /* ---------------- 拖拽布局 ---------------- */
      const layoutMode = ref(false);
      const layoutDirty = ref(false);
      const savingLayout = ref(false);
      const dragId = ref(null);
      const dragOverKey = ref('');

      function startLayout() {
        if (q.value) { q.value = ''; }
        layoutMode.value = true;
        layoutDirty.value = false;
        ElMessage.info('拖动卡片调整位置或换分组，完成后点「保存布局」');
      }
      function cancelLayout() {
        layoutMode.value = false;
        dragId.value = null;
        dragOverKey.value = '';
        if (layoutDirty.value) loadBootstrap();
        layoutDirty.value = false;
      }
      function onDragStart(e, it, g) {
        if (!layoutMode.value) return;
        dragId.value = it.id;
        try {
          e.dataTransfer.effectAllowed = 'move';
          e.dataTransfer.setData('text/plain', String(it.id));
        } catch (_) {}
      }
      function onDragOverGroup(e, g) {
        if (!layoutMode.value || dragId.value === null) return;
        e.preventDefault();
        dragOverKey.value = g.key;
      }
      function onDragOverCard(e) {
        if (!layoutMode.value || dragId.value === null) return;
        e.preventDefault();
      }
      function onDragEnd() { dragId.value = null; dragOverKey.value = ''; }

      // 把 id 的卡片移动到分组 g 的 beforeItem 之前（beforeItem 为 null 表示放到末尾）
      function onDrop(e, g, beforeItem) {
        if (!layoutMode.value || dragId.value === null) return;
        e.preventDefault();
        e.stopPropagation();
        moveItem(dragId.value, g, beforeItem);
        dragId.value = null;
        dragOverKey.value = '';
      }
      function moveItem(id, g, beforeItem) {
        let moved = null;
        for (let i = 0; i < groups.value.length; i++) {
          const grp = groups.value[i];
          const arr = grp.items || [];
          const idx = arr.findIndex(function (x) { return x.id === id; });
          if (idx >= 0) { moved = arr.splice(idx, 1)[0]; break; }
        }
        if (!moved) return;
        const target = groups.value.filter(function (x) { return x.key === g.key; })[0] || g;
        const arr = target.items;
        let at = arr.length;
        if (beforeItem) {
          const j = arr.findIndex(function (x) { return x.id === beforeItem.id; });
          if (j >= 0) at = j;
        }
        arr.splice(at, 0, moved);
        moved.category_id = target.cid === undefined ? null : target.cid;
        // 空分组被清空后要留着，否则没地方再拖回去
        layoutDirty.value = true;
      }
      async function saveLayout() {
        savingLayout.value = true;
        try {
          const rows = [];
          let sort = 10;
          for (let i = 0; i < groups.value.length; i++) {
            const grp = groups.value[i];
            const cid = grp.cid === undefined ? grp.id : grp.cid;
            (grp.items || []).forEach(function (it) {
              rows.push({ id: it.id, category_id: cid === undefined ? null : cid, sort: sort });
              sort += 10;
            });
          }
          await api.post('/api/admin/layout', { rows: rows });
          layoutDirty.value = false;
          layoutMode.value = false;
          await loadBootstrap();
          if (token.value) await reload();
          ElMessage.success('布局已保存');
        } catch (e) { apiErr(e); }
        savingLayout.value = false;
      }
      async function quickAddGroup() {
        try {
          const r = await ElMessageBox.prompt('新分组名称', '新建分组', {
            inputPlaceholder: '如 常用 / 服务器 / 网站'
          });
          const name = (r.value || '').trim();
          if (!name) return;
          await api.post('/api/admin/categories', { name: name });
          await reload();
          ElMessage.success('已创建「' + name + '」，现在可以把卡片拖进去');
        } catch (e) { if (e !== 'cancel') apiErr(e); }
      }

      /* ---------------- Compose 管理 ---------------- */
      // 2026-10-19 用户要求：读飞牛上每个 compose 的 YAML，在面板里改完直接重构。
      const cmpRows = ref([]);
      const cmpLoading = ref(false);
      const cmpError = ref('');
      const cmpRoot = ref('');
      const cmpLast = ref(null);
      const cmpDlg = ref({ open: false, path: '', project: '', content: '', original: '',
                            busy: false, msg: '', ok: null, containers: [] });
      const cmpDirty = computed(function () {
        const d = cmpDlg.value;
        return !!(d.open && !d.busy && d.content !== d.original);
      });

      async function loadCompose() {
        cmpLoading.value = true;
        cmpError.value = '';
        try {
          const r = await api.get('/api/admin/compose', { timeout: 60000 });
          cmpRows.value = (r.data.rows || []).map(function (x) { return Object.assign({ _busy: false }, x); });
          cmpRoot.value = r.data.root || '';
        } catch (e) {
          cmpError.value = apiMsg(e);
        }
        cmpLoading.value = false;
      }

      async function openCompose(row) {
        cmpDlg.value = { open: true, path: row.path, project: row.project, content: '读取中…',
                         original: '', busy: true, msg: '', ok: null, containers: [] };
        try {
          const r = await api.get('/api/admin/compose/file', { params: { path: row.path } });
          const d = r.data || {};
          cmpDlg.value = { open: true, path: d.path || row.path, project: d.project || row.project,
                           content: d.content || '', original: d.content || '',
                           busy: false, msg: '', ok: null, containers: d.containers || [] };
        } catch (e) {
          cmpDlg.value.busy = false;
          cmpDlg.value.ok = false;
          cmpDlg.value.msg = apiMsg(e);
        }
      }

      async function saveCompose(restart) {
        const d = cmpDlg.value;
        if (!d.path || d.busy) return;
        d.busy = true;
        d.ok = null;
        d.msg = restart ? '正在保存并重构…' : '正在保存…';
        try {
          const r = await api.post('/api/admin/compose/save',
            { path: d.path, content: d.content, restart: !!restart, pull: false },
            { timeout: 600000 });
          const res = r.data || {};
          d.ok = !!res.ok;
          if (res.stage === 'validate') d.msg = '校验没通过，未写入：\n' + (res.message || '');
          else if (res.ok) d.msg = (restart ? '已重构' : '已保存') + (res.backup ? '（备份 ' + res.backup.split('/').pop() + '）' : '') + (res.message && restart ? '\n' + res.message : '');
          else d.msg = '失败：' + (res.message || '');
          if (res.ok) {
            d.original = d.content;
            if (res.detached) ElMessage.warning('面板容器正在重建，几秒后自动刷新');
            else ElMessage.success(restart ? '已重构' : '已保存');
            loadCompose();
          }
        } catch (e) {
          d.ok = false;
          d.msg = apiMsg(e);
        }
        d.busy = false;
      }

      /* ---------------- 新建 Compose 项目 ---------------- */
      function cmpTpl(name) {
        const n = name || 'app';
        return [
          'services:',
          '  ' + n + ':',
          '    image: nginx:alpine',
          '    container_name: ' + n,
          '    restart: unless-stopped',
          '    ports:',
          '      - "8080:80"',
          '    volumes:',
          '      - ./data:/data',
          ''
        ].join('\n');
      }
      const cmpNew = ref({ open: false, name: '', content: '', busy: false, msg: '', ok: null });

      function openComposeNew() {
        cmpNew.value = { open: true, name: '', content: '', busy: false, msg: '', ok: null };
      }

      function fillComposeTpl() {
        const d = cmpNew.value;
        if (!d.content.trim()) d.content = cmpTpl((d.name || '').trim().toLowerCase());
      }

      async function createCompose(andUp) {
        const d = cmpNew.value;
        if (d.busy) return;
        const name = (d.name || '').trim().toLowerCase();
        if (!/^[a-z0-9][a-z0-9_-]{0,62}$/.test(name)) {
          d.ok = false;
          d.msg = '项目名只能用小写字母、数字、- 和 _，以字母或数字开头';
          return;
        }
        if (!d.content.trim()) {
          d.ok = false;
          d.msg = 'YAML 内容不能为空';
          return;
        }
        d.busy = true;
        d.ok = null;
        d.msg = andUp ? '正在创建并构建…' : '正在创建…';
        try {
          const r = await api.post('/api/admin/compose/new',
            { name: name, content: d.content, up: !!andUp, pull: false },
            { timeout: 600000 });
          const res = r.data || {};
          d.ok = !!res.ok;
          if (res.stage === 'validate') d.msg = '校验没通过，未创建：\n' + (res.message || '');
          else if (res.ok) d.msg = (andUp ? '已创建并构建' : '已创建') + '：' + (res.path || '') +
                                   (andUp && res.message ? '\n' + res.message : '');
          else d.msg = '失败：' + (res.message || '');
          if (res.ok) {
            ElMessage.success(andUp ? '已创建并构建' : '已创建');
            d.open = false;
            loadCompose();
            if (andUp && res.message) cmpLast.value = { project: name, ok: true, message: res.message, ts: Date.now() };
          } else if (res.stage === 'up') {
            // 文件已经建好了，只是构建失败：关掉弹窗、刷新列表，把输出留在下方日志里
            ElMessage.error('已创建，但构建失败，看下方输出');
            d.open = false;
            loadCompose();
            cmpLast.value = { project: name, ok: false, message: res.message || '', ts: Date.now() };
          }
        } catch (e) {
          d.ok = false;
          d.msg = apiMsg(e);
        }
        d.busy = false;
      }

      async function upCompose(row, pull) {
        const name = row.project;
        try {
          await ElMessageBox.confirm(
            '将对「' + name + '」执行 docker compose up -d' + (pull ? ' --pull always' : '') +
            '。配置有变化的容器会被重建（服务短暂中断），继续？',
            '重构 ' + name,
            { type: 'warning', confirmButtonText: '重构', cancelButtonText: '取消' });
        } catch (e) { return; }
        row._busy = true;
        try {
          const r = await api.post('/api/admin/compose/up',
                                   { path: row.path, pull: !!pull }, { timeout: 600000 });
          const d = r.data || {};
          cmpLast.value = { project: name, ok: !!d.ok, message: d.message || '', ts: Date.now() };
          if (d.ok) ElMessage.success(d.detached ? '已提交，面板正在重建' : '重构完成');
          else ElMessage.error('重构失败，看下方输出');
        } catch (e) {
          cmpLast.value = { project: name, ok: false, message: apiMsg(e), ts: Date.now() };
          apiErr(e);
        }
        row._busy = false;
        loadCompose();
      }

      /* ---------------- 镜像更新 ---------------- */
      const imgRows = ref([]);
      const imgLoading = ref(false);
      // 顶栏角标上的「可更新镜像数量」。和 imgOutdated 分开存：imgOutdated 是
      // 「镜像」页里表格的统计（只在打开设置时才有值），角标要在首屏就显示。
      const imgBadge = ref(0);
      // 上次真正联网检查的时间（unix 秒，0 = 从没查过）。后端 /api/admin/images 下发。
      const imgCheckedAt = ref(0);
      const imgCheckedText = computed(function () {
        const t = imgCheckedAt.value;
        if (!t) return '还没检查过';
        const d = Math.max(0, Math.floor(Date.now() / 1000 - t));
        if (d < 60) return '刚刚';
        if (d < 3600) return Math.floor(d / 60) + ' 分钟前';
        if (d < 86400) return Math.floor(d / 3600) + ' 小时前';
        return Math.floor(d / 86400) + ' 天前';
      });
      // 角标用轻量接口，只读服务端落盘的上次结果，不联网、不触发检查
      async function loadImgBadge() {
        if (!token.value) { imgBadge.value = 0; return; }
        try {
          const r = await api.get('/api/admin/images/badge');
          imgBadge.value = r.data.outdated || 0;
          if (r.data.checked_at) imgCheckedAt.value = r.data.checked_at;
        } catch (e) { imgBadge.value = 0; }   // 未登录/无权限就当作没有
      }
      function openImagesPage() {
        loadImgBadge();          // 点角标时顺手刷一下计数
        openAdmin('images');
        // 角标点进来是想「看哪些镜像能更新」，而结果表排在更新设置/定时任务下面，
        // 所以进来把表滚进视野，别让人先翻两屏设置（2026-10-18）。
        nextTick(function () {
          setTimeout(function () {
            const t = document.querySelector('.np-img-table');   // 别用 .ep-table：项目页也有同名表格
            // block:'start'：54 行的表很高，用 center 会停在表格中间看不到表头
            if (t) t.scrollIntoView({ block: 'start', behavior: 'smooth' });
          }, 420);
        });
      }
      // 是否有任何一次镜像更新正在进行（单个或批量）。用 computed 返回纯布尔：
      // 直接把 ref 写进模板的或运算里，一旦取到的是 ref 对象（真值）就会把按钮全禁掉。
      const imgAnyBusy = computed(function () {
        return imgUpdating.value || !!imgUpdatingName.value;
      });
      const imgError = ref('');
      const imgUpdating = ref(false);
      const imgUpdatingName = ref('');
      const imgLog = ref([]);
      // null = 还没问到（**别当成没开**），false = 明确没开，true = 开了。
      // 2026-10-16 用户反馈 bug：右键卡片「启动/重启/停止容器」偶尔是灰的、还提示
      // 「开关没打开」，但设置里明明是勾上的，过一会儿又自己好了。
      // 根因：这个值以前**只在打开「镜像」页时**由 loadImages() 赋值，首屏永远是 false，
      // 于是右键菜单一律判定「没权限」；等设置页加载过一次才恢复。
      // 现在它是 DB 设置 allow_image_update，/api/admin/state 启动时就会带下来（见下面的 watch）。
      const imgAllow = ref(null);
      // 设置里勾了「允许从面板更新镜像」就同步过来。放 watch 而不是各加载函数里手动赋值，
      // 是因为加载入口有好几个（bootstrap / state / 设置保存），漏一个就会重现这个 bug。
      watch(function () { return s.value.allow_image_update; }, function (v) {
        if (typeof v !== 'undefined') imgAllow.value = !!v;
      }, { immediate: true });
      // 实时进度（2026-10-15 用户要求：更新时能看到日志、拉取进度和当前正在进行的操作）
      const imgStep = ref('');       // 当前操作（后端 Task.step，人话）
      const imgPct = ref(0);         // 拉取百分比
      const imgPhase = ref('');      // pull / recreate / prepare / rollback
      const imgElapsed = ref(0);     // 已用秒数
      const imgFollow = ref(true);   // 日志是否自动跟随到底部
      const imgLast = ref(null);     // 上一次更新的结果（落盘在后端，刷新也不丢）
      const imgOutdated = computed(function () {
        return imgRows.value.filter(function (r) { return r.status === 'outdated'; }).length;
      });

      /* ---- 镜像页卡片的收起状态（记在 localStorage，刷新后保持） ---- */
      const IMG_SEC_KEY = 'navpage_img_sec';
      function readImgSec() {
        const base = { upd: false, chk: false, prune: false };
        try {
          const raw = JSON.parse(localStorage.getItem(IMG_SEC_KEY) || '{}') || {};
          Object.keys(base).forEach(function (k) { if (raw[k] === true) base[k] = true; });
        } catch (e) { /* 忽略坏值 */ }
        return base;
      }
      const imgSec = ref(readImgSec());
      function toggleImgSec(k) {
        if (!(k in imgSec.value)) return;
        imgSec.value[k] = !imgSec.value[k];
        try { localStorage.setItem(IMG_SEC_KEY, JSON.stringify(imgSec.value)); } catch (e) { /* 隐私模式忽略 */ }
      }

      /* ---- 定时自动检查 / 自动更新（2026-10-16 用户要求）----
         cron 解析和「下次运行」都在服务端算（app/cron.py），前端只负责显示和改设置。 */
      const imgAuto = ref(null);
      const cronPresets = [['0 3 * * *', '每天 03:00'], ['0 */6 * * *', '每 6 小时'],
                           ['0 3 * * 0', '每周日 03:00'], ['0 3 1 * *', '每月 1 号 03:00']];
      // 选预设 → 写进设置；表达式是自定义时下拉显示「自定义」（不回写）
      const cronPreset = computed({
        get: function () {
          const v = String(s.value.image_auto_cron || '').trim();
          return cronPresets.some(function (p) { return p[0] === v; }) ? v : '';
        },
        set: function (v) { if (v) { s.value.image_auto_cron = v; saveSettings(); } }
      });
      const autoNames = computed(function () { return s.value.image_auto_names || []; });
      function isAutoName(name) { return autoNames.value.indexOf(name) >= 0; }
      async function toggleAutoName(name, on) {
        const arr = autoNames.value.slice();
        const i = arr.indexOf(name);
        if (on && i < 0) arr.push(name);
        if (!on && i >= 0) arr.splice(i, 1);
        s.value.image_auto_names = arr;
        // 勾选立刻落盘（不等 900ms 防抖）：用户勾完可能马上关页面
        try {
          await api.put('/api/admin/settings', { patch: { image_auto_names: arr } });
          lastSavedJson = JSON.stringify(s.value || {});
        } catch (e) { apiErr(e); }
        // 勾上就立刻检查这个镜像：有更新马上更，不等下一次定时检查
        //（2026-10-22 用户反馈：勾了自动更新，检查到更新却不动）
        if (on) await applyAutoNow([name]);
        loadAuto();
      }
      async function applyAutoNow(names) {
        const arr = names || [];
        if (!arr.length) return [];
        try {
          const r = await api.post('/api/admin/images/auto/apply', { names: arr }, { timeout: 180000 });
          const started = (r.data && r.data.targets) || [];
          if (started.length) {
            ElMessage.success('检测到更新，已开始自动更新：' + started.join('、'));
            await loadImages(false);      // 回显状态，进度由 restoreImgTask 接着跟
          } else if (r.data && r.data.reason) {
            ElMessage.info(r.data.reason);
          }
          return started;
        } catch (e) {
          // 开关没开这类业务提示用 warning，别用大红报错
          const msg = apiMsg(e);
          if (msg) ElMessage.warning(msg);
          return [];
        }
      }
      /* ---- 定时清理未使用镜像（2026-10-16 用户要求）----
         状态跟在 /api/admin/images/auto 的 prune 字段里（一次请求拿全，不额外开接口）。 */
      const pruneSt = computed(function () {
        return (imgAuto.value && imgAuto.value.prune) || null;
      });
      const pruneModes = [{ v: 'dangling', t: '悬空镜像' }, { v: 'unused', t: '未被使用' }];
      const prunePreset = computed({
        get: function () {
          const v = String(s.value.image_auto_prune_cron || '').trim();
          return cronPresets.some(function (p) { return p[0] === v; }) ? v : '';
        },
        set: function (v) { if (v) { s.value.image_auto_prune_cron = v; saveSettings(); } }
      });
      const keepInput = ref('');
      const keepList = computed(function () { return s.value.image_prune_keep || []; });
      function addKeep() {
        const v = (keepInput.value || '').trim();
        if (!v) return;
        const arr = keepList.value.slice();
        if (arr.indexOf(v) < 0) arr.push(v);
        s.value.image_prune_keep = arr;
        keepInput.value = '';
        saveSettings();
      }
      function delKeep(k) {
        s.value.image_prune_keep = keepList.value.filter(function (x) { return x !== k; });
        saveSettings();
      }
      const pruneBusy = ref(false);
      const prunePrev = ref(null);
      const pruneNames = computed(function () {
        const it = (prunePrev.value && prunePrev.value.items) || [];
        const names = it.slice(0, 6).map(function (x) {
          return (x.tags && x.tags[0]) || ('<' + String(x.id || '').replace('sha256:', '').slice(0, 12) + '>');
        });
        return names.join('、') + (it.length > 6 ? ' …（共 ' + it.length + ' 个）' : '');
      });
      const pruneLastText = computed(function () {
        const L = pruneSt.value && pruneSt.value.last;
        if (!L) return '';
        if (L.error) return '出错：' + L.error;
        const parts = [L.skipped || ('清理 ' + (L.removed || []).length + ' 个，释放 ' + (L.freed_text || '0B'))];
        if (L.failed && L.failed.length) parts.push('失败 ' + L.failed.length + ' 个：' + L.failed.join('；'));
        return parts.join('；');
      });
      async function previewPrune() {
        pruneBusy.value = true;
        try {
          const r = await api.get('/api/admin/images/unused',
                                  { params: { mode: s.value.image_prune_mode || 'dangling' } });
          prunePrev.value = r.data;
          if (!r.data.count) ElMessage.info('没有可清理的镜像');
          else ElMessage.success('可清理 ' + r.data.count + ' 个，共 ' + r.data.bytes_text);
        } catch (e) { apiErr(e); }
        pruneBusy.value = false;
      }
      async function runPrune() {
        pruneBusy.value = true;
        try {
          const r = await api.post('/api/admin/images/prune',
                                   { mode: s.value.image_prune_mode || 'dangling' });
          const d = r.data || {}, res = d.result || {};
          if (d.ok === false && d.error) ElMessage.error(d.error);
          else if (res.failed && res.failed.length) {
            ElMessage.warning('清理完成，但有 ' + res.failed.length + ' 个失败（详见日志）');
          } else {
            ElMessage.success(res.skipped || ('已清理 ' + (res.removed || []).length + ' 个，释放 ' + (res.freed_text || '0B')));
          }
          prunePrev.value = null;
          await loadAuto();
        } catch (e) { apiErr(e); }
        pruneBusy.value = false;
      }

      function fmtTs(ts) {
        if (!ts) return '';
        const d = new Date(ts * 1000);
        const p = function (n) { return (n < 10 ? '0' : '') + n; };
        return (d.getMonth() + 1) + '-' + p(d.getDate()) + ' 周'
               + '日一二三四五六'.charAt(d.getDay()) + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
      }
      const autoLastText = computed(function () {
        const L = imgAuto.value && imgAuto.value.last;
        if (!L) return '';
        const bits = [];
        if (L.checked) bits.push('检查 ' + L.checked + ' 个容器');
        bits.push((L.outdated || []).length + ' 个可更新');
        if ((L.updated || []).length) bits.push('已自动更新 ' + L.updated.join('、'));
        if ((L.failed || []).length) bits.push('失败：' + L.failed.join('；'));
        if (L.skipped) bits.push(L.skipped);
        if (L.error) bits.push(String(L.error).slice(0, 100));
        return bits.join(' · ');
      });
      async function loadAuto() {
        try {
          const r = await api.get('/api/admin/images/auto');
          imgAuto.value = r.data.auto || null;
        } catch (e) { /* 显示不出来不影响其它功能 */ }
      }

      async function loadImages(refresh, autoApply) {
        imgLoading.value = true;
        imgError.value = '';
        try {
          // 两种取数方式（2026-10-18 用户要求：打开页面先看上次结果，不自动检查）：
          //   refresh=1 → 真去 registry 逐个查最新（慢、耗配额，只在点「检查更新」时）
          //   cached=1  → 只回显服务端落盘的上一次结果，全程不联网（打开页面默认走这条）
          const params = refresh ? { refresh: 1, auto_apply: autoApply ? 1 : 0 } : { cached: 1 };
          const r = await api.get('/api/admin/images',
                                  { params: params, timeout: 180000 });
          imgRows.value = r.data.rows || [];
          imgError.value = r.data.error || '';
          imgAllow.value = !!r.data.allow_update;
          imgLast.value = r.data.last || null;
          imgCheckedAt.value = r.data.checked_at || 0;
          imgBadge.value = r.data.outdated || 0;
          if (r.data.auto) imgAuto.value = r.data.auto;
          // 检查到更新且开了自动更新：服务端已经动手了，这里明确告诉用户
          if ((r.data.auto_applied || []).length) {
            ElMessage.success('已自动更新：' + r.data.auto_applied.join('、'));
          }
        } catch (e) { apiErr(e); }
        imgLoading.value = false;
        restoreImgTask();   // 刷新页面后把还在跑的更新接回来
      }
      // 日志面板在表格上方，用户可能正滚在表格中间/底部，更新完把日志带进视野
      function showImgLog() {
        nextTick(function () {
          const el = document.querySelector('.np-log');
          if (el) el.scrollIntoView({ block: 'center', behavior: 'smooth' });
        });
      }

      // 日志自动滚到底：只在用户本来就贴着底部（或没手动滚上去）时跟随，
      // 否则用户往上翻历史会被不停拽回去。
      function imgScrollFollow() {
        nextTick(function () {
          const el = document.querySelector('.np-log');
          if (!el) return;
          const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
          if (imgFollow.value || nearBottom) el.scrollTop = el.scrollHeight;
        });
      }
      function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

      // 只轮询「已在跑」的任务，不发起新任务。刷新页面后接管任务时也用它。
      // 返回 {success:true/false/null, message}；success=null 表示结果不明。
      async function pollUpdate(since0) {
        const t0 = Date.now();
        let since = since0 || 0, miss = 0;
        for (;;) {
          await sleep(600);
          let d = null;
          try {
            const r = await api.get('/api/admin/images/update/status', { params: { since: since } });
            d = r.data;
            miss = 0;
          } catch (e) {
            miss++;
            // 端点连不上超过 ~4 秒：很可能正在更新面板自己的容器（进程被重建）。
            // 去更新历史里找这一轮的结果，别让用户只看到一个「取进度失败」。
            if (miss >= 6) {
              try {
                const h = await api.get('/api/admin/images/history');
                const it = (h.data.items || [])[0];
                if (it && it.ts >= Math.floor(t0 / 1000) - 2 && it.container === imgUpdatingName.value) {
                  return {
                    success: it.success === null ? null : !!it.success,
                    message: it.success === null
                      ? '面板正在更新自身，页面会短暂断开；刷新后刷新结果' : (it.message || ''),
                    viaHistory: true
                  };
                }
              } catch (e2) { /* 面板还没起来，等下一轮 */ }
            }
            if (miss > 12) return { success: null, message: '取进度失败：' + e.message };
            continue;
          }
          imgElapsed.value = Math.round(d.elapsed || ((Date.now() - t0) / 1000));
          if (d.idle) {                     // 面板重启过，任务记录没了
            miss++;
            if (miss > 4) return { success: null, message: '任务状态丢失（面板可能重启过），去容器列表确认一下结果' };
            continue;
          }
          imgStep.value = d.step || '';
          imgPct.value = d.pct || 0;
          imgPhase.value = d.phase || '';
          if (d.log && d.log.length) {
            imgLog.value = imgLog.value.concat(d.log);
            since = d.n;
            imgScrollFollow();
          }
          if (!d.running) return { success: !!d.success, message: d.message || '' };
          if (Date.now() - t0 > 25 * 60 * 1000) return { success: null, message: '超时（25 分钟）' };
        }
      }

      // 发起一次更新并轮询到结束。返回 {success:true/false/null, message, refused}
      // success=null 表示「结果不明」：面板重启导致任务没了，或一直取不到状态。
      async function runUpdate(container, pull) {
        let start;
        try {
          start = await api.post('/api/admin/images/update',
            { name: container, pull: pull === false ? false : true });
        } catch (e) { return { success: false, message: '发起失败：' + e.message, refused: true }; }
        if (!start.data.ok) {
          imgLog.value = imgLog.value.concat([start.data.message || '发起失败']);
          imgScrollFollow();
          return { success: false, message: start.data.message || '发起失败', refused: true };
        }
        return await pollUpdate(0);
      }

      // 更新结束后的收尾（单个 / 批量 / 接管 三处共用）
      async function finishImg(container, res) {
        imgStep.value = ''; imgPct.value = 0; imgPhase.value = '';
        imgUpdatingName.value = '';
        imgScrollFollow();
        if (res.success) {
          // 先把这一行就地改成「已最新」，别等整表重新比对（几十个容器要几十秒）
          const row = imgRows.value.find(function (r) { return r.container === container; });
          if (row && !row.local_only) { row.status = 'current'; row.remote_digest = row.local_digest; }
          ElMessage.success(container + ' ' + (res.message || '更新完成'));
          loadImages(true);
        } else if (res.success === null) {
          ElMessage.warning(container + '：' + res.message);
        } else {
          ElMessage.error(container + ' 更新失败：' + res.message);
          // 拉不到新镜像时给条退路：跳过拉取，用本地现有镜像重建（不升级，但服务能起来）
          if (res.message && res.message.indexOf('拉取') >= 0) {
            try {
              await ElMessageBox.confirm(
                '拉取不到新镜像：' + res.message
                + '\n\n要不要跳过拉取，直接用本地现有镜像重建容器？（不会升级到新版本）',
                '拉取失败', { type: 'warning', confirmButtonText: '仅重建', cancelButtonText: '知道了' });
            } catch (e) { return; }
            imgUpdatingName.value = container;
            imgElapsed.value = 0;
            imgLog.value = imgLog.value.concat(['', '=== 改用「仅重建」（跳过拉取） ===']);
            showImgLog();
            const res2 = await runUpdate(container, false);
            finishImg(container, res2);
          }
        }
      }

      function ago(ts) {
        const s = Math.max(0, Math.floor(Date.now() / 1000) - (ts || 0));
        if (s < 60) return s + ' 秒前';
        if (s < 3600) return Math.floor(s / 60) + ' 分钟前';
        if (s < 86400) return Math.floor(s / 3600) + ' 小时前';
        return Math.floor(s / 86400) + ' 天前';
      }

      // 刷新页面 / 重新进「镜像」页：任务本体在后端进程里还在跑，把界面接回来。
      // 用户 2026-10-15 反馈「刷新页面后进度就不显示了」——进度存在前端变量里，刷新即丢。
      const imgRestoring = ref(false);
      async function restoreImgTask() {
        if (imgAnyBusy.value || imgRestoring.value) return;
        imgRestoring.value = true;
        try {
          const r = await api.get('/api/admin/images/update/status', { params: { since: 0 } });
          const d = r.data || {};
          if (d.idle || !d.container) return;
          imgStep.value = d.step || '';
          imgPct.value = d.pct || 0;
          imgPhase.value = d.phase || '';
          if (!d.running) {
            // 只接管「进行中」的任务。已结束的旧任务不再往面板里灌日志 ——
            // 刷新后凭空冒出一段旧日志会让人以为更新还在跑，
            // 结果交给底部那行「上次：…」和日志区之外的信息交代即可。
            return;
          }
          imgLog.value = (d.log || []).slice();
          imgUpdatingName.value = d.container;
          imgElapsed.value = Math.round(d.elapsed || 0);
          imgLog.value = imgLog.value.concat(['', '=== 页面重新打开，已接上正在进行的更新 ===']);
          imgScrollFollow();
          const res = await pollUpdate(d.n || 0);
          await finishImg(d.container, res);
        } catch (e) {
          /* 接管失败就静默：不该因为一次取状态失败打扰用户 */
        } finally {
          imgRestoring.value = false;
        }
      }

      function imgFailureToast(name, res) {
        // 完整原因（可能很长）在日志面板里，toast 只给一句人话指路
        const m = res.message || '';
        ElMessage.error(m.indexOf('socket-proxy') >= 0
          ? name + ' 更新失败：被 socket-proxy 拦住了，怎么开权限见日志'
          : m.indexOf('正在更新中') >= 0
            ? name + '：' + m
            : name + ' 更新失败，原因见日志');
      }

      async function updateImage(row) {
        // 同一个容器 / 批量更新进行中都不再发第二次请求：后端虽然会去重，
        // 但这里先挡掉，用户看到的是「正在更新中」，而不是一次莫名其妙的失败。
        if (imgUpdating.value || imgUpdatingName.value) return;
        // 本地构建的镜像（registry 上没有）没有新版本可拉，点了只会是「用本地镜像重建容器」，
        // 先问一句，别让用户以为是升级。
        if (row.local_only) {
          try {
            await ElMessageBox.confirm(
              row.image + ' 是本地构建的镜像，registry 上没有这个仓库，没有新版本可拉。\n'
              + '继续＝直接用本地镜像重建容器（容器会短暂中断）。',
              '重建容器', { type: 'warning', confirmButtonText: '重建', cancelButtonText: '取消' });
          } catch (e) { return; }
        }
        imgUpdatingName.value = row.container;
        imgLog.value = [];
        imgStep.value = '发起更新…'; imgPct.value = 0; imgPhase.value = 'prepare'; imgElapsed.value = 0;
        showImgLog();
        const res = await runUpdate(row.container, !row.local_only);
        await finishImg(row.container, res);
      }
      async function updateAllImages() {
        if (imgUpdating.value || imgUpdatingName.value) return;
        const targets = imgRows.value.filter(function (r) { return r.status === 'outdated'; });
        if (!targets.length) return;
        try {
          await ElMessageBox.confirm(
            '将依次更新 ' + targets.length + ' 个容器（拉镜像 + 重建，每个会短暂中断）。继续？',
            '批量更新', { type: 'warning' });
        } catch (e) { return; }
        imgUpdating.value = true;
        imgLog.value = [];
        let okN = 0, failN = 0;
        for (let i = 0; i < targets.length; i++) {
          const row = targets[i];
          imgUpdatingName.value = row.container;
          imgLog.value = imgLog.value.concat(['', '=== [' + (i + 1) + '/' + targets.length + '] '
            + row.container + ' ===']);
          imgScrollFollow();
          const res = await runUpdate(row.container);
          if (res.success) okN++; else failN++;
        }
        imgUpdatingName.value = '';
        imgUpdating.value = false;
        imgStep.value = ''; imgPct.value = 0; imgPhase.value = '';
        await loadImages(true);
        ElMessage.success('批量更新结束：成功 ' + okN + ' 个'
          + (failN ? '，失败/待确认 ' + failN + ' 个' : ''));
        showImgLog();
      }

      /* ---------------- 图标选择器（本地库 + 在线图标库 + 上传） ---------------- */
      const iconDlg = ref(false);
      const iconTab = ref('local');
      const iconList = ref([]);
      const iconLoading = ref(false);
      const iconQ = ref('');
      const iconResults = ref([]);
      const iconSearching = ref(false);
      const iconLibErr = ref('');
      const iconLibAt = ref(0);
      const iconBusy = ref('');
      // 在线图标库可以配多个（GitHub 公开仓库）：iconLibs 是「配置 + 清单缓存状态」，
      // iconLibFilter 是当前筛选的库（'' = 全部），iconTotal/iconOffset 支撑「加载更多」翻页
      const iconLibs = ref([]);
      const iconLibFilter = ref('');
      const iconLibBusy = ref('');
      const iconTotal = ref(0);
      const iconOffset = ref(0);
      const ICON_PAGE = 60;
      const newLibUrl = ref('');
      const newLibLabel = ref('');

      // 时间戳 → 「3 分钟前 / 2 天前」（图标库清单新鲜度提示）
      function fmtAgo(ts) {
        const t = Number(ts || 0);
        if (!t) return '还没拉过清单';
        const d = Math.max(0, Math.floor(Date.now() / 1000) - t);
        if (d < 60) return '刚刚更新';
        if (d < 3600) return Math.floor(d / 60) + ' 分钟前更新';
        if (d < 86400) return Math.floor(d / 3600) + ' 小时前更新';
        return Math.floor(d / 86400) + ' 天前更新';
      }

      // 卡片的 icon 字段既可能是 http URL，也可能是本地文件名，统一算出预览地址
      function iconPreviewSrc(icon) {
        const v = (icon || '').trim();
        if (!v) return '';
        if (/^https?:\/\//i.test(v)) return '/api/icon?url=' + encodeURIComponent(v);
        return '/api/iconfile/' + encodeURIComponent(v.split('/').pop());
      }
      const iconPreview = computed(function () { return iconPreviewSrc(editItem.value.icon); });
      const iconPreviewName = computed(function () {
        const v = (editItem.value.icon || '').trim();
        return v ? v.split('/').pop() : '';
      });

      // 编辑弹窗里的「抓取图标」：拿当前填的地址去站点抓 favicon（服务端抓，落到本地图标库），
      // 只把文件名填进输入框 —— 点「保存」才真正生效，所以新增条目（还没 id）也能用。
      const iconFetching = ref(false);
      async function fetchItemIcon() {
        const it = editItem.value || {};
        const url_lan = joinUrl(it.addr_lan, it.port_lan, it.tail_lan);
        const url_wan = joinUrl(it.addr_wan, it.port_wan, it.tail_wan);
        if (!url_lan && !url_wan && !it.id) {
          ElMessage.warning('先在上面填一个网址，再抓图标');
          return;
        }
        iconFetching.value = true;
        try {
          const r = await api.post('/api/admin/item/icon',
                                   { url_lan: url_lan, url_wan: url_wan, item_id: it.id || 0 },
                                   { timeout: 60000 });
          if (r.data && r.data.ok) {
            it.icon = r.data.icon;
            ElMessage.success('已抓到图标（' + r.data.icon + '），记得保存');
          } else {
            ElMessage.warning((r.data && r.data.error) || '没抓到图标');
          }
        } catch (e) { apiErr(e); }
        iconFetching.value = false;
      }

      function openIconPicker() {
        iconDlg.value = true;
        iconTab.value = 'local';
        loadLocalIcons();
        loadIconLibs(false);
        if (!iconResults.value.length && !iconSearching.value) searchIconLib(false);
      }
      async function loadLocalIcons() {
        iconLoading.value = true;
        try {
          const r = await api.get('/api/admin/icons');
          iconList.value = r.data.files || [];
        } catch (e) { apiErr(e); }
        iconLoading.value = false;
      }
      // 配置的图标库 + 各自清单缓存状态（ensure=true 会顺手把缺清单/过期的拉一遍）
      async function loadIconLibs(ensure) {
        try {
          const r = await api.get('/api/admin/iconlibraries', { params: { ensure: ensure ? 1 : 0 } });
          iconLibs.value = r.data.items || [];
        } catch (e) { /* 非管理员/接口异常时不打扰用户 */ }
      }
      async function addIconLib() {
        const u = (newLibUrl.value || '').trim();
        if (!u) { ElMessage.warning('请填仓库地址，如 homarr-labs/dashboard-icons'); return; }
        iconLibBusy.value = 'add';
        try {
          const r = await api.post('/api/admin/iconlibraries',
                                   { url: u, label: (newLibLabel.value || '').trim() });
          iconLibs.value = r.data.items || [];
          newLibUrl.value = '';
          newLibLabel.value = '';
          if (r.data.warning) ElMessage.warning(r.data.warning);
          else ElMessage.success('已添加 ' + (r.data.added || u) + '，可以直接搜它的图标了');
          iconLibFilter.value = '';
          searchIconLib(false);
        } catch (e) { apiErr(e); }
        iconLibBusy.value = '';
      }
      function delIconLib(l) {
        ElMessageBox.confirm('移除图标库「' + l.label + '」（' + l.id + '）？已下载到 data/icons 的图标不受影响。',
                             '提示', { type: 'warning' })
          .then(async function () {
            iconLibBusy.value = l.id;
            try {
              const r = await api.delete('/api/admin/iconlibraries', { params: { id: l.id } });
              iconLibs.value = r.data.items || [];
              if (iconLibFilter.value === l.id) iconLibFilter.value = '';
              ElMessage.success('已移除');
              searchIconLib(false);
            } catch (e) { apiErr(e); }
            iconLibBusy.value = '';
          }).catch(function () {});
      }
      async function refreshIconLib(id) {
        iconLibBusy.value = id || '__all__';
        try {
          const r = await api.post('/api/admin/iconlibraries/refresh', { id: id || '' });
          iconLibs.value = r.data.items || [];
          ElMessage.success('清单已更新' + (r.data.warning ? '（部分失败：' + r.data.warning + '）' : ''));
          searchIconLib(false);
        } catch (e) { apiErr(e); }
        iconLibBusy.value = '';
      }
      async function searchIconLib(force, more) {
        iconSearching.value = true;
        iconLibErr.value = '';
        iconOffset.value = more ? iconOffset.value + ICON_PAGE : 0;
        try {
          const r = await api.get('/api/admin/iconlibrary', {
            params: {
              q: iconQ.value || '', limit: ICON_PAGE, offset: iconOffset.value,
              lib: iconLibFilter.value || '', refresh: force ? 1 : 0,
            }
          });
          const got = r.data.items || [];
          iconResults.value = more ? iconResults.value.concat(got) : got;
          iconTotal.value = r.data.total || iconResults.value.length;
          iconLibAt.value = r.data.cached_at || 0;
          if (r.data.errors && r.data.errors.length) iconLibErr.value = r.data.errors.join('；');
        } catch (e) {
          iconLibErr.value = (e.response && e.response.data && e.response.data.error) || '图标库加载失败';
          if (!more) iconResults.value = [];
        }
        iconSearching.value = false;
      }
      function useIcon(name, tip) {
        editItem.value.icon = name || '';
        iconDlg.value = false;
        if (tip !== false) ElMessage.success('已设为卡片图标');
      }
      async function pickLibIcon(it) {
        if (!it || !it.name) return;
        iconBusy.value = it.name;
        try {
          // 先落到本地再选用：其他卡片也能复用，之后也不依赖外网 / CDN
          const r = await api.post('/api/admin/iconlibrary/use',
                                   { name: it.name, variant: it.variant || 'svg', lib: it.lib || '' });
          await loadLocalIcons();
          useIcon(r.data.name);
        } catch (e) { apiErr(e); }
        iconBusy.value = '';
      }
      async function uploadIcon(ev) {
        const f = ev.target.files && ev.target.files[0];
        if (!f) return;
        const fd = new FormData();
        fd.append('file', f);
        iconBusy.value = '__upload__';
        try {
          const r = await api.post('/api/admin/icon', fd);
          await loadLocalIcons();
          iconTab.value = 'local';
          useIcon(r.data.name, false);
          ElMessage.success('图标已上传，保存在数据目录，其他卡片也能选它');
        } catch (e) { apiErr(e); }
        iconBusy.value = '';
        ev.target.value = '';
      }
      function delLocalIcon(f) {
        ElMessageBox.confirm('删除图标文件「' + f.name + '」？已用它的卡片会退回首字母。', '提示', { type: 'warning' })
          .then(async function () {
            try {
              await api.delete('/api/admin/icon/' + encodeURIComponent(f.name));
              await loadLocalIcons();
              ElMessage.success('已删除');
            } catch (e) { apiErr(e); }
          }).catch(function () {});
      }
      function libIconError(ev, it) {
        // 浏览器直连 CDN 失败（内网没有外网出口）→ 退回本机代理，服务端抓取并缓存
        const img = ev.target;
        if (it && it.proxy && img.getAttribute('src') !== it.proxy) img.setAttribute('src', it.proxy);
      }

      /* ---------------- 卡片右键菜单（编辑 / 删除） ---------------- */
      const ctxMenu = ref({ open: false, x: 0, y: 0, item: null });
      const ctxBusy = ref('');     // 正在执行容器操作的容器名（防重复点击）

      // 容器卡片才有 container_name；菜单里据此决定显不显示「启动/重启/停止」
      const ctxIsContainer = computed(function () {
        const it = ctxMenu.value.item;
        return !!(it && (it.container_name || '').trim());
      });
      const ctxContainerName = computed(function () {
        const it = ctxMenu.value.item;
        return it ? (it.container_name || '') : '';
      });
      // 容器操作和「更新镜像」共用一把开关（都要 socket-proxy 放开 POST 权限）
      // 只有「明确关掉」才置灰。还不知道时不禁用 —— 免得又把「没问到」显示成「没权限」。
      const ctxAllowOp = computed(function () { return imgAllow.value !== false; });

      async function onCardMenu(ev, it) {
        // 没登录（普通访客）就不接管右键，交给浏览器原生菜单
        if (!token.value || layoutMode.value) return;
        ev.preventDefault();
        // 先渲染到屏幕外量真实尺寸再定位。以前这里写死 216/104，加了三个容器操作项之后
        // 真实高度变 302px，贴屏幕底部右键会把最后一项露到屏幕外（实测溢出 81px）。
        // 菜单项以后还会增减，所以按实测高度算，别再写死。
        ctxMenu.value = { open: true, x: -9999, y: -9999, item: it };
        await nextTick();
        const el = document.querySelector('.np-ctx');
        const MW = (el && el.offsetWidth) || 190;
        const MH = (el && el.offsetHeight) || 220;
        ctxMenu.value = {
          open: true,
          x: Math.min(ev.clientX, Math.max(8, window.innerWidth - MW - 8)),
          y: Math.min(ev.clientY, Math.max(8, window.innerHeight - MH - 8)),
          item: it,
        };
      }
      function closeCtxMenu() {
        if (ctxMenu.value.open) ctxMenu.value = { open: false, x: 0, y: 0, item: null };
      }
      function onCtxKey(e) { if (e.key === 'Escape' || e.keyCode === 27) closeCtxMenu(); }
      function ctxEdit() { const it = ctxMenu.value.item; closeCtxMenu(); if (it) openItem(it); }
      function ctxDelete() { const it = ctxMenu.value.item; closeCtxMenu(); if (it) delItem(it); }

      // 右键「启动 / 重启 / 停止容器」。停止和重启会中断服务，先确认再发。
      async function ctxContainer(action) {
        const it = ctxMenu.value.item;
        const name = it ? (it.container_name || '').trim() : '';
        closeCtxMenu();
        if (!it || !name) return;
        if (imgAllow.value === false) {
          ElMessage.warning('容器操作开关没打开：去「镜像」页打开「允许从面板更新镜像」，'
                            + '并给 socket-proxy 放开 POST 权限');
          return;
        }
        if (ctxBusy.value) return;      // 上一个操作还没回来，别叠着发
        const word = { start: '启动', stop: '停止', restart: '重启' }[action] || action;
        if (action !== 'start') {
          try {
            await ElMessageBox.confirm(
              word + '容器「' + name + '」？' + (action === 'stop' ? '这个服务会立刻中断。' : '服务会中断几秒。'),
              '确认' + word, { type: 'warning', confirmButtonText: '确定' + word, cancelButtonText: '取消' });
          } catch (e) { return; }
        }
        ctxBusy.value = name;
        try {
          const r = await api.post('/api/admin/container/action',
                                   { name: name, action: action }, { timeout: 150000 });
          if (r.data && r.data.ok) ElMessage.success(r.data.message || (word + '成功'));
          else ElMessage.error((r.data && r.data.error) || (word + '失败'));
        } catch (e) { apiErr(e); }
        ctxBusy.value = '';
        // 状态灯立刻重探一次，别让圆点还停在旧状态
        if (s.value.status_dot) loadStatus();
      }

      /* ---------------- 顶栏汉堡菜单（编辑布局 / 设置） ----------------
         2026-10-15：设置齿轮从右下角悬浮按钮搬到顶栏最右侧，两个入口收进这个菜单。
         点菜单按钮/菜单项都不冒泡到 window（模板上 @click.stop），
         所以「点空白处关闭」沿用右键菜单那套全局监听即可。 */
      const menuOpen = ref(false);
      // 「关于」弹窗 + 版本/项目元信息（后端 /api/bootstrap 的 app 字段，未登录也会下发）
      const aboutOpen = ref(false);
      const appMeta = ref({});
      // 显示用：去掉 https:// 前缀，手机上才不会在数字中间折行（href 仍用完整地址）
      const appMetaShort = computed(() => (appMeta.value.project_url || "").replace(/^https?:\/\//, ""));

      function toggleMenu() { menuOpen.value = !menuOpen.value; }
      function closeMenu() { if (menuOpen.value) menuOpen.value = false; }
      function menuLayout() {
        closeMenu();
        if (token.value && !layoutMode.value) startLayout();
      }
      function menuAdmin() {
        closeMenu();
        openAdmin();
      }
      function menuAbout() {
        closeMenu();
        aboutOpen.value = true;
      }
      function onMenuKey(e) { if (e.key === 'Escape' || e.keyCode === 27) closeMenu(); }

      onMounted(boot);
      // 兜底：有的浏览器会在加载后 / 刷新还原时把值塞回来（甚至不走 input 事件，
      // 只改 DOM），用户没碰过就清掉，Vue 状态和 DOM 一起清。
      onMounted(function () {
        const clearIfUntouched = function () {
          if (searchTouched) return;
          q.value = '';
          const el = document.querySelector('.np-search input');
          if (el && el.value) el.value = '';
        };
        setTimeout(clearIfUntouched, 60);
        setTimeout(clearIfUntouched, 400);
        window.addEventListener('pageshow', clearIfUntouched);
      });

      // 量 .np-main 的宽度并持续跟踪（窗口缩放 / 抽屉开合都会变）。
      // 它自己宽度由父容器拉伸决定、与内容无关，所以观察它不会形成回路。
      // 主区宽度的重测（含拖窗口 / 转屏 / 内容壳重建）。
      // 2026-10-15 用户反馈：「窗口缩放时卡片要横向居中，右边距离随缩放变大变小」——
      // 查下来除了「组内靠左」，还有个真 bug：`.np-main` 会被 v-if（锁屏 / 内容壳）整体替换，
      // 旧 ResizeObserver 观测的是**已卸载**的元素、之后再也不会触发，于是 mainW 永远停在
      // 加载那一刻的宽度 → 列数 / 列宽不重算，缩到手机宽度还会横向溢出（实测 768px 下溢出到 1882）。
      // 所以：壳每次重建都重新 observe（先 disconnect 旧元素），另外再挂一层 window resize 兜底。
      let roMain = null;
      function watchMainSize() {
        nextTick(function () {
          measureMain();
          const el = document.querySelector('.np-main');
          if (!el || !window.ResizeObserver) return;
          if (!roMain) roMain = new ResizeObserver(function () { measureMain(); });
          roMain.disconnect();
          roMain.observe(el);
        });
      }

      onMounted(function () {
        watchMainSize();
        window.addEventListener('resize', measureMain);
        window.addEventListener('orientationchange', measureMain);
      });
      // 锁屏 → 内容壳（登录成功后）时补一次，否则新元素没人观测
      watch(locked, function (v) { if (!v) watchMainSize(); });

      // 右键菜单 / 顶栏汉堡菜单：点任意处 / 滚动 / 缩放 / Esc 都关掉，别让它残留在屏幕上
      onMounted(function () {
        window.addEventListener('click', closeCtxMenu);
        window.addEventListener('click', closeMenu);
        window.addEventListener('scroll', closeCtxMenu, true);
        window.addEventListener('scroll', closeMenu, true);
        window.addEventListener('resize', closeCtxMenu);
        window.addEventListener('resize', closeMenu);
        window.addEventListener('keydown', onCtxKey);
        window.addEventListener('keydown', onMenuKey);
      });

      // 切到「扫描导入」时自动扫一次，避免打开就是一片空白
      watch(tab, function (v) {
        if (v === 'scan' && token.value && !scanResult.value && !scanning.value) doScan();
        // Compose 页：每次进来都重扫（宿主机上可能刚加了项目），但不阻塞切换
        if (v === 'compose' && token.value && !cmpLoading.value) loadCompose();
        if (v === 'images' && token.value) {
          if (!imgRows.value.length && !imgLoading.value) loadImages(false);
          else restoreImgTask();   // 表已有数据：也把在跑的更新接回来
        }
      });

      return {
        s, groups, network, locked, q, pw, busy, engineIdx, wallpapers, wallUrlInput,
        token, adminOpen, tab, items, categories, docker, scanResult, scanning, autocatting,
        importing, selNew, newCat, newPw, itemDlg, editItem, icons,
        modeChoice, modeOptions, lanProbe, reprobeLan, probeText, probeUrlText, themeOptions, sizeOptions, schemeOptions, isMobile, centerTab,
        sizeOptionsCat, sizeOptionsItem, sizeOf, spanOf, cardStyle, setCatSize,
        clock, dateStr, wallpaperUrl, bgStyle, cardMin, groupStyle, filteredGroups, drawerSize,
        initial, colorOf, hostOf, onIconError, onCardClick, catName, countInCat, cardTitle,
        wdg, widgetProviders, wdgFields, wdgHint, wdgTesting, wdgResult,
        wpop, wpopShow, wpopHide, wpopHold,
        onWidgetTypeChange, testWidget, loadWidgets,
        st, dotOf, dotTip, loadStatus,
        showUrlFor, barW, layoutMode, layoutDirty, savingLayout, dragId, dragOverKey,
        startLayout, cancelLayout, onDragStart, onDragOverGroup, onDragOverCard, onDrop,
        onDragEnd, saveLayout, quickAddGroup,
        cmpRows, cmpLoading, cmpError, cmpRoot, cmpLast, cmpDlg, cmpDirty,
        cmpNew, openComposeNew, fillComposeTpl, createCompose,
        loadCompose, openCompose, saveCompose, upCompose,
        imgRows, imgLoading, imgError, imgUpdating, imgUpdatingName, imgLog, imgAllow,
        imgAnyBusy, imgStep, imgPct, imgPhase, imgElapsed, imgLast, ago,
        imgOutdated, loadImages, updateImage, updateAllImages,
        imgBadge, imgCheckedText, loadImgBadge, openImagesPage,
        imgAuto, cronPresets, cronPreset, autoNames, autoLastText, isAutoName, toggleAutoName, fmtTs, loadAuto,
        logoUrl, logoUrlInput, uploadLogo, useLogoUrl, clearLogo,
        pruneSt, pruneModes, prunePreset, keepInput, keepList, addKeep, delKeep, imgSec, toggleImgSec,
        pruneBusy, prunePrev, pruneNames, pruneLastText, previewPrune, runPrune,
        loadBootstrap, loadWallpapers, reload, boot, doLogin, logout, changePw,
        setMode, onEnter, openItem, saveItem, delItem, delItemById, setExcluded,
        addCat, renameCat, delCat, bulkDelCats, moveCat, catIdx,
        selCats, catAllChecked, catIndeterminate,
        onCatCheck, toggleAllCats, doScan, selectAllNew, importSelected, syncAll, autocatAll, autoSaveAt, flushAutoSave,
        wanText, wanRows, wanItems, wanParsing, wanApplying, wanOverwrite, wanSelectedCount, wanPreview, wanApply,
        refreshIcons, saveSettings, uploadWall, pickWall, useWallUrl, clearWall,
        delWall, openAdmin,
        iconDlg, iconTab, iconList, iconLoading, iconQ, iconResults, iconSearching,
        iconLibErr, iconLibAt, iconBusy, iconPreview, iconPreviewName, iconPreviewSrc,
        iconFetching, fetchItemIcon,
        openIconPicker, loadLocalIcons, searchIconLib, pickLibIcon, uploadIcon, unlockSearch, searchLocked,
        delLocalIcon, libIconError, useIcon,
        iconLibs, iconLibFilter, iconLibBusy, iconTotal, iconOffset,
        newLibUrl, newLibLabel, fmtAgo, loadIconLibs, addIconLib, delIconLib, refreshIconLib,
        ctxMenu, onCardMenu, closeCtxMenu, ctxEdit, ctxDelete,
        ctxIsContainer, ctxContainerName, ctxAllowOp, ctxBusy, ctxContainer,
        menuOpen, toggleMenu, closeMenu, menuLayout, menuAdmin, menuAbout, aboutOpen, appMeta, appMetaShort
      };
    }
  });

  /* 图标注册成 i-xxx，避免与原生 HTML 标签冲突（如 <menu> / <search>） */
  const Icons = window.ElementPlusIconsVue || {};
  Object.keys(Icons).forEach(function (name) {
    const kebab = 'i-' + name.replace(/([a-z0-9])([A-Z])/g, '$1-$2').toLowerCase();
    app.component(kebab, Icons[name]);
  });

  app.use(ElementPlus);
  app.mount('#app');
})();
