# 每日英语听力 · 内容 API 文档

---

## 📌 更新记录 / 变更说明

### v2.1 · 2026-10-07 —— 🔴 App 端必读

**问题**：线上内容缺少逐词时间轴。

**排查结论**：数据本身没问题（详情 JSON 里确有 189 词时间轴），根因是
**jsDelivr 对分支名 `@main` 缓存很久**，客户端拿到的是几小时前的旧版数据。

实测四个 CDN 节点：

| 节点 | `@main` 返回 |
|---|---|
| `cdn.jsdelivr.net` | timeline = 0（旧） |
| `fastly.jsdelivr.net` | timeline = 0（旧） |
| `gcore.jsdelivr.net` | timeline = 0（旧） |
| `@<commit>` | **timeline = 189（正确）** |

**修复**：所有内容链接固定到 commit，不再用分支名。

#### ✅ App 端改法：运行时查版本，不要写死

```javascript
// 🔴 运行时取最新 SHA（两种方式，任选其一，都无缓存问题）

// 方式 A：GitHub API（最简单，注意有 60 次/小时 限流，勿高频轮询）
const REPO = "chjam12301-sys/news-etl";
const { sha } = await fetch(`https://api.github.com/repos/${REPO}/commits/main`)
  .then(r => r.json());

// 方式 B：raw.githubusercontent（无限流，推荐 App 用这个）
// 它的 ETag 就是 commit SHA
const shaB = (await fetch(`https://raw.githubusercontent.com/${REPO}/main/content/data/latest.json`,
  { method: 'HEAD' })).headers.get('etag').replaceAll('"', '').slice(0, 7);

// 第 2 步：用 SHA 拼 CDN 地址 —— 所有内容固定到这个 commit，永不命中过期缓存
const BASE = `https://cdn.jsdelivr.net/gh/${REPO}@${sha}/content`;

// 第 3 步：正常拉取
const index = await fetch(`${BASE}/index.json`).then(r => r.json());
const detail = await fetch(index.latest.versions.en[0].detail_url).then(r => r.json());
// detail.timeline 有完整逐词时间轴
```

> ⚠️ **不要用 `ETag` 直接当 URL**：raw 的 ETag 是 **blob SHA**（文件级），
> jsDelivr 认commit SHA，用 blob SHA 会全部 404。必须取短 SHA（`.slice(0, 7)`）
> 或用 `git ls-remote` / GitHub API 拿完整 commit SHA。

**方式 B 的实测值**（供你核对格式）：

```
ETag: "5b6bd6d4248fc8638873244b012663e083fe0ffa5dab211dc3d44efd05799c1a"
→取前 7 位 = 5b6bd6d   ⚠️ 这是 blob SHA，拼 CDN 会 404，需换方式取 commit SHA
```

**为什么这样最好**：

| | 写死 SHA | 运行时查（本方案） |
|---|---|---|
| 内容更新后 | ❌ 要改代码再发版 | ✅ **无需任何改动** |
| 拿到旧缓存 | 静默出错 | `index.version.published_at` 一眼看出 |
| 文档是否会过期 | 文档里的 SHA 会变、旧文档反而误导 | **文档里没有 SHA，永不过期** |

#### ✅ 自检：怎么确认拿到的是新版

`index.json` 带 `version` 字段，**数据自己会告诉你版本**：

```json
{
  "version": {
    "published_at": "2026-10-06T16:11:37Z",
    "content_hash": "f39f4df26d73",
    "ref": "db6bbe0"
  }
}
```

- `published_at` 明显早于当前时间 → 命中了 CDN 旧缓存
- `content_hash` 每次内容更新都会变

#### ✅ 已实测通过

```
git ls-remote origin refs/heads/main          → sha        （无限流）
GET  {BASE}/index.json                        → 200, en 5 条 / ja 5 条
GET  index.latest.versions.en[0].detail_url   → 200, timeline 189 词，对齐 0 错位
HEAD index.latest.versions.en[0].audio.url    → 200, 434 KB
```

#### 本文档的改动位置

| 位置 | 改动 | 重要度 |
|---|---|---|
| **本页顶部** | 新增本更新说明 | — |
| 🔴 先看这两个坑 → 坑二 | 改写为「不要用 `@main`」 | 🔴 **必读** |
| 一、三步接入 | Base URL 改为**运行时取 SHA** | 🔴 **必改** |
| 二、首页索引 | 路径 `/data/index.json` → `/index.json` | 🟡 注意 |
| 九、完整流程 | 流程图同步为新路径 | 🟡 注意 |
| 十三、自检清单 | 新增「Base URL 用 SHA」检查项 | 🟡 建议 |

#### 以后怎么查版本号

内容更新后 SHA 会变，二选一：

```
① https://github.com/chjam12301-sys/news-etl/commits/main     看最新 commit
② GET {BASE}/data/latest.json    →  { ref, index_url }        推荐
```

方式②只需拉 276 字节的小文件，就知道该换哪个 SHA。

---
**Base URL 里不要写死 SHA**，用下面两步动态获取（否则本文档一更新就过期）：

```
① 拿 commit sha（任选）
   git ls-remote origin refs/heads/main
   或 GET api.github.com/repos/chjam12301-sys/news-etl/commits/main
② https://cdn.jsdelivr.net/gh/chjam12301-sys/news-etl@{sha}/content/index.json
```

索引里的 `detail_url` / `audio.url` **都已固定到 commit**，直接用即可、可永久缓存。
`index.json` 的 `version.published_at` 可用于判断是否命中旧缓存。
全部数据在 CDN 上，**无需服务器、无需鉴权**。
OpenAPI 规范见 `openapi-cdn.json`，可导入 Postman / Apifox 生成客户端。
---

## 🔴 先看这两个坑

>📌 **v2.1 更新**：坑二已改写，务必读完。

### 坑一：高亮必须用 `text`，不能用 `body`

```javascript
detail.text.slice(w.cs, w.ce) === w.w   // ✅ 始终成立
detail.body.slice(w.cs, w.ce)            // ❌ 多段正文一定跳字
```

`text` 是段落换行**压成空格**后的单行版本，时间轴的 `cs/ce` 就是按它算的。
`body` 保留了 `\n\n`，用它做偏移会错位。实测 189 词零错位，该契约有测试锁死。

---

## 一、三步接入

> 📌 **v2.1 改动**：Base URL 从写死 `@main` 改为**运行时从 GitHub API 取 SHA**。

```javascript
const BASE = "https://cdn.jsdelivr.net/gh/chjam12301-sys/news-etl@<commit>/content";  // ← commit 见文档开头

// ① 首页列表（必须加时间戳绕过 CDN 缓存）
const index = await fetch(`${BASE}/data/index.json?t=${Date.now()}`).then(r => r.json());
index.latest.versions.en   // 英语 5 条（level 1→5）
index.latest.versions.ja   // 日语 5 条
index.levels               // 等级字典，启动时缓存

// ② 详情（点进某篇）
const detail = await fetch(item.detail_url).then(r => r.json());
detail.paragraphs          // 段落数组，分段渲染
detail.vocab               // 词汇表，做练习题
detail.text                // 🔴 逐词高亮用这个
detail.timeline            // 逐词时间轴

// ③ 播放
player.src = item.audio.url;
```

---

## 二、首页索引

> 📌 **v2.1 改动**：入口路径改为 `/index.json`（短路径，少一层 `data/`）。

### `GET /index.json`（等价于 `/data/index.json`）

```json
{
  "service": "每日英语听力 · 内容后台",
  "generated_at": "2026-10-06T15:16:47+00:00",
  "dates": ["2026-10-06"],
  "base_url": "https://cdn.jsdelivr.net/gh/chjam12301-sys/news-etl@{sha}/content",
  "index_url": ".../data/index.json",
  "levels": [
    { "code": "en_a1", "label": "A1 入门", "lang": "en", "level": 1 },
    { "code": "ja_n1", "label": "N1 上級", "lang": "ja", "level": 5 }
  ],
  "latest": {
    "date": "2026-10-06",
    "versions": {
      "en": [],
      "ja": []
    }
  }
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `dates` | string[] | 有内容的日期（倒序），可做历史归档 |
| `levels` | object[] | **等级字典（10 个），App 启动时缓存**，不要硬编码 |
| `latest.date` | string | 最新一天，`YYYY-MM-DD` |
| `latest.versions.en` | object[] | 当天英语内容，按 level 升序 |
| `latest.versions.ja` | object[] | 当天日语内容，同上 |

> `latest` **只有一天**。查历史用下一节的按日索引。

---

## 三、按日索引

### `GET /data/{YYYY-MM-DD}/index.json`

例如 `GET /data/2026-10-06/index.json`

```json
{
  "date": "2026-10-06",
  "generated_at": "2026-10-06T15:16:47+00:00",
  "article_count": 1,
  "version_count": 10,
  "articles": [
    {
      "article_id": 1,
      "source": "arstechnica",
      "source_url": "https://arstechnica.com/...",
      "topic": "tech",
      "title_original": "OpenAI agents tried to hack Wikipedia tools",
      "published_date": "2026-10-06",
      "published_at": "..."
    }
  ],
  "versions": {
    "en": { "en_a1": [], "en_a2": [], "en_b1": [],
            "en_b2": [], "en_c1": [] },
    "ja": { "ja_n5": [], "ja_n4": [], "ja_n3": [], "ja_n2": [], "ja_n1": [] }
  }
}
```

**按语言 + 等级两级分组** —— 切换等级时直接取，**无需额外请求**：

```javascript
const day = await fetch(`${BASE}/data/${index.latest.date}/index.json`).then(r => r.json());
const item = day.versions.ja.ja_n3.find(x => x.article_id === currentId);
```

---

## 四、列表条目

```json
{
  "version_id": 1,
  "article_id": 1,
  "level_code": "en_a1",
  "lang": "en",
  "level": 1,
  "level_label": "A1 入门",

  "title": "OpenAI agents tried to hack Wikipedia tools",
  "lead": "Wikipedia says OpenAI computer programs did bad things...",
  "preview": "Wikipedia said this on Monday. OpenAI agents tried to hack...",

  "topic": "tech",
  "source": "arstechnica",
  "source_url": "https://arstechnica.com/security/...",

  "image": { "type": "photo", "url": "https://cdn.arstechnica.net/...", "credit": "" },

  "published_date": "2026-10-06",
  "word_count": 168,
  "reading_minutes": 0.8,

  "has_audio": true,
  "audio": {
    "id": 1,
    "url": "https://cdn.jsdelivr.net/.../content/audio/1/en_a1.mp3",
    "duration": 73.587,
    "size_bytes": 445248,
    "engine": "edge-tts",
    "voice": "en-US-AriaNeural",
    "word_count": 189,
    "has_timeline": true
  },

  "detail_url": "https://cdn.jsdelivr.net/.../content/data/versions/1.json"
}
```

| 字段 | 说明 |
|---|---|
| `version_id` | **这个等级的版本 id**，详情/时间轴都用它 |
| `article_id` | **文章 id**，同一篇有 10 个等级（5 英 + 5 日） |
| `level` / `level_code` | 等级数字（1–5）与等级码 |
| `image` | `type=photo` 有实图；`type=gradient` 用 `from/to/label` 画渐变块 |
| `audio.duration` | 秒 |
| `audio.word_count` | 时间轴词数（== `timeline` 长度） |
| `detail_url` | **完整可直接 fetch 的地址** |

---

## 五、版本详情 + 逐词时间轴

### `GET /data/versions/{version_id}.json`

**列表字段 + 以下**：

```json
{
  "paragraphs": [
    "Wikipedia said this on Monday.",
    "OpenAI agents tried to hack a note-taking tool."
  ],
  "body": "Wikipedia said this on Monday.\n\nOpenAI agents tried to hack...",

  "text": "Wikipedia said this on Monday. OpenAI agents tried to hack a note-taking tool. ...",
  "timeline": [
    { "i": 0, "w": "Wikipedia", "s": 0.1,"e": 0.3,  "sm": 100,  "em": 300,  "cs": 0,  "ce": 10, "si": 0 },
    { "i": 1, "w": "said",     "s": 0.31,"e": 0.52, "sm": 312, "em": 520,  "cs": 11, "ce": 15, "si": 0 },
    { "i": 2, "w": "this",     "s": 0.53,"e": 0.7,  "sm": 530, "em": 700,  "cs": 16, "ce": 20, "si": 0 }
  ],

  "vocab": [
    { "word": "agents", "pos": "noun", "zh": "智能体（AI 代理程序）", "note": "指能自主执行任务的 AI 程序" }
  ],

  "title_original": "OpenAI agents tried to hack Wikipedia tools...",
  "body_original": "原文英文"
}
```

### 三个文本字段的区别

| 字段 | 内容 | 用途 |
|---|---|---|
| **`text`** | 段落换行**已压成空格**的单行文本 | **逐词高亮必须用它** |
| `body` | 保留 `\n\n` 段落分隔 | 分段展示 |
| `paragraphs[]` | 段落数组 | 分段渲染（**推荐**） |

### `timeline[]` 字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `i` | int | 词序号（0 起） |
| `w` | string | 词文本（**含标点**，如 `rainforest,`） |
| `s` / `e` | float | 起止时间（秒，3 位小数） |
| `sm` / `em` | int | 起止时间（**毫秒，播放器直接用**） |
| `cs` / `ce` | int | 在 `text` 中的字符区间 `[cs, ce)` |
| `si` | int | 所属句子序号（按标点分段） |

**保证**：`sm` 严格单调递增、`cs` 严格递增不重叠、100% 覆盖全文（不含空格）。

---

## 六、高亮实现

```javascript
function activeIndex(tl, t) {          // t = 当前播放毫秒
  let lo = 0, hi = tl.length - 1, ans = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (tl[mid].sm <= t) { ans = mid; lo = mid + 1; }
    else hi = mid - 1;
  }
  return ans;
}

function render(detail, t) {
  const w = detail.timeline[activeIndex(detail.timeline, t)];
  if (!w) return;
  range.setStart(w.cs, w.ce);          // 在 detail.text 上标 range
}

// 倍速播放：sm/em 同比例缩放，无需重新请求
// 点词跳转：player.currentTime = w.sm / 1000
```

---

## 七、等级体系

| lang | level | code | label | 目标词数 |
|---|---|---|---|---|
| `en` | 1 | `en_a1` | A1 入门 | 110 |
| `en` | 2 | `en_a2` | A2 初级 | 150 |
| `en` | 3 | `en_b1` | B1 中级 | 200 |
| `en` | 4 | `en_b2` | B2 中高级 | 250 |
| `en` | 5 | `en_c1` | C1 高级 | 300 |
| `ja` | 1 | `ja_n5` | N5 初級 | 120 |
| `ja` | 2 | `ja_n4` | N4 初級 | 160 |
| `ja` | 3 | `ja_n3` | N3 中級 | 220 |
| `ja` | 4 | `ja_n2` | N2 中上級 | 280 |
| `ja` | 5 | `ja_n1` | N1 上級 | 340 |

用 `/data/index.json` 的 `levels` 驱动 UI。

---

## 八、音频

| 项 | 值 |
|---|---|
| 路径 | `/audio/{article_id}/{level_code}.mp3` |
| 格式 | MP3，edge-tts，24kbps mono |
| 音色（英） | `en-US-AriaNeural` |
| 音色（日） | `ja-JP-NanamiNeural` |
| 实测时长 | en_a1 74s / en_b2 128s / en_c1 165s / ja_n5 98s / ja_n1 355s |
| Range 请求 | ✅ 支持（可拖动进度条） |

内容不可变 → **URL 可永久缓存到本地**。

---

## 九、完整流程

> 📌 **v2.1 改动**：全部路径已同步为新Base URL。

```
App 启动
 ├─ GET /index.json             首页 + 等级字典（SHA 固定）
 │
 ├─ 首页（list）
 │    └─ 用 index.latest.versions.en / .ja 渲染
 │       每条自带 image / audio.url / detail_url
 │
 └─ 点进详情
      ├─ GET detail_url                 正文 + 词汇表 + 时间轴
      ├─ 播放 audio.url                 CDN 直出
      └─ 切等级 → GET /data/{date}/index.json，按 article_id 取另一等级
```

首次需拉 2 个文件（索引 + 首个详情），之后详情进内存缓存。

---

## 十、内容更新

| 项 | 值 |
|---|---|
| 更新频率 | 每天一次（北京时间 05:00） |
| 每篇产出 | 10 个版本（5 英 + 5 日），各带音频与时间轴 |
| 保留天数 | **14 天**（音频约 30MB/天） |
| 超出保留 | 旧 `version_id` 的详情与音频被删除，索引移除 |

> App 若需永久保存某篇，请在本地持久化 `paragraphs` + `timeline` + 音频文件。

---

## 十一、错误处理

| 情况 | 表现 | 处理 |
|---|---|---|
| 内容未生成 | 404 | 显示「今日内容准备中」，稍后重试 |
| CDN 缓存旧版 | 索引缺今日 | 加 `?t=${Date.now()}` 重试 |
| `version_id` 不存在 | 404 | 版本可能已被清理（保留 14 天） |
| 网络超时 | — | 读本地缓存，兜底上次内容 |

---

## 十二、一份真实响应

`GET /index.json`（实际数据，无省略）：

```json
{
  "service": "每日英语听力 · 内容后台",
  "generated_at": "2026-10-06T15:16:47+00:00",
  "dates": ["2026-10-06"],
  "base_url": "https://cdn.jsdelivr.net/gh/chjam12301-sys/news-etl@{sha}/content",
  "index_url": "https://cdn.jsdelivr.net/gh/chjam12301-sys/news-etl@{sha}/content/data/index.json",
  "levels": [
    { "code": "en_a1", "label": "A1 入门",   "lang": "en", "level": 1 },
    { "code": "en_a2", "label": "A2 初级",   "lang": "en", "level": 2 },
    { "code": "en_b1", "label": "B1 中级",   "lang": "en", "level": 3 },
    { "code": "en_b2", "label": "B2 中高级", "lang": "en", "level": 4 },
    { "code": "en_c1", "label": "C1 高级",   "lang": "en", "level": 5 },
    { "code": "ja_n5", "label": "N5 初級",   "lang": "ja", "level": 1 },
    { "code": "ja_n4", "label": "N4 初級",   "lang": "ja", "level": 2 },
    { "code": "ja_n3", "label": "N3 中級",   "lang": "ja", "level": 3 },
    { "code": "ja_n2", "label": "N2 中高級", "lang": "ja", "level": 4 },
    { "code": "ja_n1", "label": "N1 上級",   "lang": "ja", "level": 5 }
  ],
  "latest": {
    "date": "2026-10-06",
    "versions": {
      "en": [
        {
          "version_id": 1,
          "article_id": 1,
          "level_code": "en_a1",
          "lang": "en",
          "level": 1,
          "level_label": "A1 入门",
          "title": "OpenAI agents tried to hack Wikipedia tools",
          "topic": "tech",
          "source": "arstechnica",
          "source_url": "https://arstechnica.com/security/2026/10/openai-agents-tried-to-hack-wikipedia-tools-and-flooded-it-with-requests/",
          "image": {
            "type": "photo",
            "url": "https://cdn.arstechnica.net/wp-content/uploads/2026/10/ai-agentic-hacking.jpg",
            "credit": ""
          },
          "published_date": "2026-10-06",
          "word_count": 168,
          "reading_minutes": 0.8,
          "lead": "Wikipedia says OpenAI computer programs did bad things.",
          "preview": "Wikipedia said this on Monday. OpenAI agents tried to hack a note-taking tool.",
          "has_audio": true,
          "audio": {
            "id": 1,
            "url": "https://cdn.jsdelivr.net/gh/chjam12301-sys/news-etl@{sha}/content/audio/1/en_a1.mp3",
            "duration": 73.587,
            "size_bytes": 445248,
            "engine": "edge-tts",
            "voice": "en-US-AriaNeural",
            "word_count": 189,
            "has_timeline": true
          },
          "detail_url": "https://cdn.jsdelivr.net/gh/chjam12301-sys/news-etl@{sha}/content/data/versions/1.json"
        }
      ],
      "ja": []
    }
  }
}
```

---

## 十三、接入自检清单

> 📌 **v2.1 新增**：第 1 条检查 Base URL 是否用了 SHA。

- [ ] Base URL 的 SHA 来自 GitHub API（不是写死、也不是 `@main`）
- [ ] `index.version.published_at` 是最新发布时间（不是几天前）
- [ ] `index.latest.versions.en.length === 5`
- [ ] `audio.url` 能播放，且 `duration` 与实际时长一致
- [ ] `detail.timeline.length === item.audio.word_count`
- [ ] `detail.text.slice(w.cs, w.ce) === w.w`（至少验 20 个词，全部相等）
- [ ] 播放时高亮不跳字

第 4 条不过就是误用了 `body`。
