# 每日英语听力 · 内容 API 文档（v2 · CDN 版）

> **Base URL**
> ```
> https://cdn.jsdelivr.net/gh/chjam12301-sys/news-etl@main/content
> ```
>
> 全部数据与音频都在这个地址下，**无需服务器、无需鉴权**。

---

## 0. 三句话接入

```javascript
const BASE = "https://cdn.jsdelivr.net/gh/chjam12301-sys/news-etl@main/content";

// 1. 首页：拿今天的内容列表
const index = await fetch(`${BASE}/data/index.json`).then(r => r.json());
const todayEN = index.latest.versions.en;      // 英语各等级条目
const todayJA = index.latest.versions.ja;      // 日语各等级条目

// 2. 详情：点进某篇（自带逐词时间轴）
const detail = await fetch(item.detail_url).then(r => r.json());
detail.text;        // ← 高亮必须用这个字段
detail.timeline;    // 逐词时间轴

// 3. 播放
player.src = item.audio.url;
```

---

## ⚠️ 1. 必读：jsDelivr 缓存行为

**这是唯一会让 App 出bug 的地方。**

| 行为 | 说明 |
|---|---|
| `@main` 是**分支** | jsDelivr 会缓存，内容更新后**最长可能滞后 5–10 分钟** |
| `@{commit_sha}` 是**版本** | 立即拿到最新内容，但需要你每次记下新 SHA |

**建议做法**：

```javascript
// 首页可以用 @main（滞后几分钟无所谓，内容每天才更新一次）
const index = await fetch(`${BASE}/data/index.json?t=${Date.now()}`)
  .then(r => r.json());

// 但 index.json 里的 detail_url / audio.url 已经带了 @main，
// 内容是不可变的（同一篇不会变），所以长期缓存完全安全。
```

**内容不可变**：同一篇新闻的正文、音频、时间轴生成后就不再改变 →
`detail_url` 和 `audio.url` 可以**永久缓存**，不用管CDN 更新。

只有 `data/index.json`（首页列表）需要考虑刷新时机。

---

## 2. 首页索引

### `GET /data/index.json`

**响应**
```json
{
  "service": "每日英语听力 · 内容后台",
  "generated_at": "2026-10-06T15:16:47+00:00",
  "dates": ["2026-10-06"],
  "base_url": "https://cdn.jsdelivr.net/gh/chjam12301-sys/news-etl@main/content",
  "index_url": ".../data/index.json",
  "levels": [
    { "code": "en_a1", "label": "A1 入门", "lang": "en", "level": 1 },
    { "code": "ja_n1", "label": "N1 上級", "lang": "ja", "level": 5 }
  ],
  "latest": {
    "date": "2026-10-06",
    "versions": {
      "en": [ /* 5 条，level 1→5 */ ],
      "ja": [ /* 5 条，level 1→5 */ ]
    }
  }
}
```

**字段**

| 字段 | 类型 | 说明 |
|---|---|---|
| `dates` | string[] | 有内容的日期列表（倒序），可做历史归档 |
| `levels` | object[] | **等级字典，App 启动时缓存**（10 个） |
| `latest.date` | string | 最新一天，`YYYY-MM-DD` |
| `latest.versions.en` | object[] | 当天英语内容，**按 level 升序**，共 5 条 |
| `latest.versions.ja` | object[] | 当天日语内容，同上 |

>⚠️ `latest` **只有一天**。要查历史某天用下一节的按日索引。

---

## 3. 按日索引（历史/补拉）

### `GET /data/{YYYY-MM-DD}/index.json`

例如 `GET /data/2026-10-06/index.json`

**响应结构与 `index.json` 的 `latest` 相同**，但 `versions` 直接在顶层，且分组更细：

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
    "en": {
      "en_a1": [ /* 该等级所有条目 */ ],
      "en_a2": [ ... ],
      "en_b1": [ ... ],
      "en_b2": [ ... ],
      "en_c1": [ ... ]
    },
    "ja": { "ja_n5": [], "ja_n4": [], "ja_n3": [], "ja_n2": [], "ja_n1": [] }
  }
}
```

>按等级分组而非扁平数组 —— **切换等级时直接取 `versions[lang][level_code]`，无需再请求。**

---

## 4. 列表条目

`latest.versions.en[0]` 或 `versions.en.en_a1[0]` 的完整字段：

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

  "image": {
    "type": "photo",
    "url": "https://cdn.arstechnica.net/...",
    "credit": "..."
  },

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

**关键字段**

| 字段 | 说明 |
|---|---|
| `version_id` | **这个等级的版本 id**，详情/时间轴都用它 |
| `article_id` | **文章 id**，同一篇文章有 10 个等级（5英 + 5日） |
| `level` / `level_code` | 等级数字（1-5）与等级码 |
| `image` | `type=photo` 有实图；`type=gradient` 用 `from/to/label` 画渐变块 |
| `audio.duration` | 秒 |
| `audio.word_count` | 时间轴词数（== `timeline` 长度） |
| `detail_url` | **完整可直接 fetch 的地址** |

**等级切换**：同一 `article_id` 的 10 条即为全部等级。从按日索引里按 `article_id` 过滤即可。

---

## 5. 版本详情 + 逐词时间轴

### `GET /data/versions/{version_id}.json`

**响应**（列表字段 + 以下）
```json
{
  "paragraphs": [
    "Wikipedia said this on Monday.",
    "OpenAI agents tried to hack a note-taking tool.",
    "..."
  ],
  "body": "Wikipedia said this on Monday.\n\nOpenAI agents tried to hack...",

  "text": "Wikipedia said this on Monday. OpenAI agents tried to hack a note-taking tool. ...",
  "timeline": [
    { "i": 0, "w": "Wikipedia", "s": 0.1,"e": 0.3, "sm": 100,  "em": 300,  "cs": 0,  "ce": 10, "si": 0 },
    { "i": 1, "w": "said",     "s": 0.31,"e": 0.52,"sm": 312, "em": 520,  "cs": 11, "ce": 15, "si": 0 },
    { "i": 2, "w": "this",     "s": 0.53,"e": 0.7, "sm": 530, "em": 700,  "cs": 16, "ce": 20, "si": 0 }
  ],

  "vocab": [
    { "word": "agents", "pos": "noun", "zh": "智能体（AI 代理程序）", "note": "指能自主执行任务的 AI 程序" }
  ],

  "title_original": "OpenAI agents tried to hack Wikipedia tools...",
  "body_original": "原文英文（供对照）"
}
```

### 🔴 `text` vs `body` —— 最重要的一条

| 字段 | 内容 | 用途 |
|---|---|---|
| **`text`** | 段落换行**已压成空格**的单行文本 | **逐词高亮必须用它** |
| `body` | 保留 `\n\n` 段落分隔 | 分段显示用 |
| `paragraphs[]` | 段落数组 | 分段渲染用（推荐） |

**原因**：时间轴的 `cs/ce` 字符偏移是相对 **`text`** 计算的。
若用 `body` 去 slice，遇到多段正文（第二段起）**一定会跳字**。

```javascript
// ✅ 正确
const seg = detail.text.slice(w.cs, w.ce);   // === w.w

// ❌ 错误（多段正文会错位）
const seg = detail.body.slice(w.cs, w.ce);
```

>该契约有测试锁死：`tests/test_core.py::test_timeline_offsets_index_into_spoken_text_not_body`
> 实测 189 词**零错位**。

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

### 高亮实现

```javascript
// 播放到 t 毫秒时，二分找当前词
function activeIndex(timeline, t) {
  let lo = 0, hi = timeline.length - 1, ans = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (timeline[mid].sm <= t) { ans = mid; lo = mid + 1; }
    else hi = mid - 1;
  }
  return ans;
}

// 高亮
const w = timeline[activeIndex(timeline, player.currentTime * 1000)];
range.setStart(w.cs, w.ce);          // 用 text
```

**倍速播放**：`sm/em` 同比例缩放即可，无需重新请求。

**点词跳转**：`player.currentTime = w.sm / 1000`。

---

## 6. 等级体系

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
| `ja` | 4 | `ja_n2` | N2 中高級 | 280 |
| `ja` | 5 | `ja_n1` | N1 上級 | 340 |

用 `/data/index.json` 的 `levels` 字段驱动 UI，不要硬编码。

---

## 7. 音频特性

| 项 | 值 |
|---|---|
| 格式 | MP3（edge-tts，24kbps mono） |
| 音色（英） | `en-US-AriaNeural` |
| 音色（日） | `ja-JP-NanamiNeural` |
| 实测时长 | en_a1 74s / en_c1 165s / ja_n5 98s / ja_n1 355s |
| Range 请求 | ✅ 支持（`cdn.jsdelivr.net` 原生支持），可拖进度条 |

> 文件不可变 → **音频 URL 可永久缓存到本地**，不必重复下载。

---

## 8. 完整接入流程

```
App 启动
 ├─ GET  /data/index.json           首页 + 等级字典（缓存等级字典）
 │
 ├─ 首页（list）
 │    └─ 用 latest.versions.en / .ja 渲染
 │       每条自带 image / audio.url / detail_url
 │
 └─ 点进详情
      ├─ GET  detail_url            正文 + 词汇表 + 时间轴
      ├─ 播放 audio.url             CDN 直出，支持 Range
      └─ 切等级 → 从 /data/{date}/index.json 按 article_id 取另一等级
```

**首次加载可能需要拉 2 个文件**（索引 + 首个详情）。之后详情进内存缓存。

---

## 9. 错误处理

| 情况 | 表现 | 处理 |
|---|---|---|
| 内容还没生成 | 404 | App 显示「今日内容准备中」，可稍后重试 |
| jsDelivr 缓存旧版 | 索引缺今日 | 加 `?t=${Date.now()}` 重试 |
| `version_id` 不存在 | 404 | 检查版本是否已被清理（保留 14 天） |
| 网络超时 | — | 读本地缓存，兜底显示上次内容 |

---

## 10. 缓存策略建议

| 内容 | 策略 | 理由 |
|---|---|---|
| `levels` 字典 | 永久缓存 | 等级体系不变 |
| `detail_url` 拉到的详情 | 按 `version_id` 永久缓存 | 内容不可变 |
| `audio.url` 音频 | 落盘缓存 | 内容不可变，且带宽最贵 |
| `data/index.json` | 5–15 分钟 | 每天才更新一次 |

---

## 11. 内容更新与保留策略

- **更新频率**：每天一次（GitHub Actions，北京时间 05:00）
- **保留天数**：14 天（音频约 30MB/天，仓库稳定 400MB 内）
- **超出保留期**：旧 `version_id` 的详情和音频会被删除并从索引移除

> App 若需永久保存某篇内容，请在本地持久化 `paragraphs` + `timeline` + 音频文件。

---

## 12. 一份真实响应样本

`GET /data/index.json`（截取，实际字段无省略）：

```json
{
  "service": "每日英语听力 · 内容后台",
  "generated_at": "2026-10-06T15:16:47+00:00",
  "dates": ["2026-10-06"],
  "base_url": "https://cdn.jsdelivr.net/gh/chjam12301-sys/news-etl@main/content",
  "index_url": "https://cdn.jsdelivr.net/gh/chjam12301-sys/news-etl@main/content/data/index.json",
  "levels": [
    { "code": "en_a1", "label": "A1 入门", "lang": "en", "level": 1 },
    { "code": "en_a2", "label": "A2 初级", "lang": "en", "level": 2 },
    { "code": "en_b1", "label": "B1 中级", "lang": "en", "level": 3 },
    { "code": "en_b2", "label": "B2 中高级", "lang": "en", "level": 4 },
    { "code": "en_c1", "label": "C1 高级", "lang": "en", "level": 5 },
    { "code": "ja_n5", "label": "N5 初級", "lang": "ja", "level": 1 },
    { "code": "ja_n4", "label": "N4 初級", "lang": "ja", "level": 2 },
    { "code": "ja_n3", "label": "N3 中級", "lang": "ja", "level": 3 },
    { "code": "ja_n2", "label": "N2 中高級", "lang": "ja", "level": 4 },
    { "code": "ja_n1", "label": "N1 上級", "lang": "ja", "level": 5 }
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
            "url": "https://cdn.jsdelivr.net/gh/chjam12301-sys/news-etl@main/content/audio/1/en_a1.mp3",
            "duration": 73.587,
            "size_bytes": 445248,
            "engine": "edge-tts",
            "voice": "en-US-AriaNeural",
            "word_count": 189,
            "has_timeline": true
          },
          "detail_url": "https://cdn.jsdelivr.net/gh/chjam12301-sys/news-etl@main/content/data/versions/1.json"
        }
      ],
      "ja": [ /* 同结构，日语 */ ]
    }
  }
}
```

---

## 13. 自检清单

接入后请验证这 5 条：

- [ ] 打开 `BASE/data/index.json` 返回 `latest.versions.en` 长度为 5
- [ ] 每条的 `audio.url` 能播放，且 `duration` 与播放时长一致
- [ ] 拉 `detail_url`，确认 `timeline.length === audio.word_count`
- [ ] 用 `text.slice(w.cs, w.ce) === w.w` 校验至少 20 个词，全部相等
- [ ] 用 `text`（不是 `body`）渲染，播放时高亮不跳字

> 第 4 条是最容易出错的地方 —— 如果不等，说明误用了 `body`。