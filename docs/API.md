# 每日英语听力 App · 内容后台 API 文档

> 版本 v1.0.0 · Base URL: `https://<你的域名>` · 交互式文档: `/docs`

## 0. 速览

| 项 | 说明 |
|---|---|
| 内容形态 | 每篇新闻 → **5 个英语等级 + 5 个日语等级** = 10 条改写版本 |
| 英语等级 | `en_a1` `en_a2` `en_b1` `en_b2` `en_c1`（CEFR） |
| 日语等级 | `ja_n5` `ja_n4` `ja_n3` `ja_n2` `ja_n1`（JLPT） |
| 音频 | `edge-tts`（微软引擎，免费无限制），`audio/mpeg` |
| 时间轴 | **逐词** `start/end`（秒 + 毫秒）+ `char_start/char_end`（字符偏移） |
| 鉴权 | **无**（当前为公开只读接口） |
| 更新频率 | 每天 05:00（北京时间）自动生成 |

### 数据模型关系

```
Article（源新闻）
  └─ 10 个 ArticleVersion（每语言 5 个等级）
       ├─ paragraphs[]  正文段落
       ├─ vocab[]       词汇表（含中文释义）
       └─ AudioAsset    音频文件 + 逐词时间轴 timeline[]
```

---

## 1. 元信息接口

### GET /health
服务健康检查。Render 免费版休眠后首次调用会先唤醒，约 30s。

**响应**
```json
{
  "status": "ok",
  "service": "每日英语听力 · 内容后台",
  "version": "1.0.0",
  "llm_provider": "gemini",
  "llm_enabled": true,
  "tts_enabled": true,
  "tts_engine": "edge-tts",
  "articles": 128,
  "versions": 1280,
  "audios": 1280,
  "server_time": "2026-10-06T04:00:00+00:00"
}
```

### GET /api/v1/levels
等级字典。**App 首次启动拉一次并缓存**，等级筛选器直接由它驱动。

**Query**

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `lang` | `en` \| `ja` | 否 | 只返回该语言 |

**响应**
```json
[
  { "code": "en_a1", "lang": "en", "label": "A1 入门",     "level": 1, "target_words": 110, "sentence_hint": "6-12 word sentences" },
  { "code": "en_a2", "lang": "en", "label": "A2 初级",     "level": 2, "target_words": 150, "sentence_hint": "up to 14 word sentences" },
  { "code": "en_b1", "lang": "en", "label": "B1 中级",     "level": 3, "target_words": 200, "sentence_hint": "two-clause sentences with connectors" },
  { "code": "en_b2", "lang": "en", "label": "B2 中高级",   "level": 4, "target_words": 250, "sentence_hint": "multi-clause sentences" },
  { "code": "en_c1", "lang": "en", "label": "C1 高级",     "level": 5, "target_words": 300, "sentence_hint": "dense sophisticated sentences" },
  { "code": "ja_n5", "lang": "ja", "label": "N5 初級",     "level": 1, "target_words": 120, "sentence_hint": "very short sentences" },
  { "code": "ja_n4", "lang": "ja", "label": "N4 初級",     "level": 2, "target_words": 160, "sentence_hint": "short everyday sentences" },
  { "code": "ja_n3", "lang": "ja", "label": "N3 中級",     "level": 3, "target_words": 220, "sentence_hint": "short coherent paragraphs" },
  { "code": "ja_n2", "lang": "ja", "label": "N2 中高級",   "level": 4, "target_words": 280, "sentence_hint": "formal written style" },
  { "code": "ja_n1", "lang": "ja", "label": "N1 上級",     "level": 5, "target_words": 340, "sentence_hint": "newspaper-grade prose" }
]
```

### GET /api/v1/topics
返回主题列表：`["business","culture","health","science","sports","tech","world"]`

---

## 2. 内容列表接口

### GET /api/v1/articles
通用列表，支持多维筛选。

**Query**

| 参数 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `date` | `YYYY-MM-DD` | 否 | — | 按发布日过滤 |
| `topic` | string | 否 | — | `tech` `business` `science` `health` `sports` `culture` `world` |
| `source` | string | 否 | — | 源标识，如 `ars_technica`、`bbc_world` |
| `lang` | `en` \| `ja` | 否 | — | 语言 |
| `level` | 1–5 | 否 | — | 等级数字 |
| `level_code` | string | 否 | — | 精确等级码，如 `b1` |
| `q` | string | 否 | — | 标题关键词（大小写不敏感） |
| `limit` | int | 否 | 20 | 1–100 |
| `offset` | int | 否 | 0 | 分页 |

> **注意**：`level` 与 `lang` 同时存在才返回 `body` 与 `vocab`；纯翻页场景建议只传 `lang`+`level`（见下条）。

**响应**
```json
{
  "total": 70,
  "count": 20,
  "limit": 20,
  "offset": 0,
  "items": [
    {
      "version_id": 1024,
      "article_id": 103,
      "level_code": "en_a1",
      "lang": "en",
      "level": 1,
      "level_label": "A1 入门",
      "title": "Scientists find a new frog",
      "topic": "tech",
      "source": "ars_technica",
      "source_url": "https://arstechnica.com/...",
      "image_url": "https://arstechnica.com/x.jpg",
      "published_date": "2026-10-06",
      "published_at": "2026-10-06T03:20:00+00:00",
      "word_count": 108,
      "reading_minutes": 0.5,
      "lead": "This is a short news story about a frog.",
      "preview": "Scientists found a new frog in the forest…",
      "body": null,
      "vocab": [],
      "audio": {
        "url": "/api/v1/audio/512.mp3",
        "duration": 41.2,
        "size_bytes": 318422,
        "engine": "edge-tts",
        "voice": "en-US-AriaNeural",
        "has_timeline": true,
        "word_count": 108
      }
    }
  ]
}
```

**字段说明**

| 字段 | 类型 | 说明 |
|---|---|---|
| `version_id` | int | **改写版本 id**，详情/音频/时间轴都用它 |
| `article_id` | int | 源文章 id，同一文章 10 个等级共享 |
| `body` | string \| null | 未指定 `lang`/`level` 时为 `null`（省流量） |
| `vocab` | array | 词汇表，仅在指定等级时返回 |
| `reading_minutes` | float | 按 en 200wpm / ja 350wpm 估算 |
| `audio.url` | string | 相对路径，App 端拼 Base URL；也支持直接换 CDN 域名 |

### GET /api/v1/articles/today
**App 首页专用**，一次请求拿全「今天 + 某语言某等级」列表，等价于上面带 `date=today&lang=&level=`。

**Query**

| 参数 | 类型 | 必填 | 默认 |
|---|---|---|---|
| `lang` | `en` \| `ja` | 否 | `en` |
| `level` | 1–5 | 否 | `1` |
| `limit` | int | 否 | 30 |

**响应**：同 `GET /api/v1/articles`（`items` 字段）。

---

## 3. 内容详情接口

### GET /api/v1/versions/{version_id}
按版本 id 取全文。**推荐用法**：列表拿到的 `version_id` 直接拼这里。

**响应**（`items` 的全部字段 + 以下）
```json
{
  "version_id": 1024,
  "article_id": 103,
  "level_code": "en_a1",
  "lang": "en",
  "level": 1,
  "level_label": "A1 入门",
  "title": "Scientists find a new frog",
  "paragraphs": [
    "Scientists found a new frog.",
    "The frog lives in the Amazon forest."
  ],
  "body": "Scientists found a new frog.\n\nThe frog lives in the Amazon forest.",
  "lead": "This is a short news story about a frog.",
  "vocab": [
    { "word": "species", "pos": "noun", "zh": "物种", "note": "生物分类单位" },
    { "word": "rainforest", "pos": "noun", "zh": "雨林", "note": "" }
  ],
  "word_count": 108,
  "reading_minutes": 0.5,
  "image_url": "https://.../x.jpg",
  "title_original": "New frog species discovered in Amazon",
  "summary_original": "原文摘要…",
  "body_original": "原文全文…",
  "fetched_at": "2026-10-06T03:21:00+00:00",
  "audio": { "url": "/api/v1/audio/512.mp3", "duration": 41.2, "has_timeline": true, "word_count": 108 }
}
```

### GET /api/v1/articles/{article_id}?lang=&level=
按「文章 id + 语言 + 等级」取详情。App 从列表跳详情、用户切等级时用这个（等级切换是高频操作）。

**Query**：`lang`（默认 `en`）、`level`（1–5，默认 `1`）
**响应**：同上。

### GET /api/v1/articles/{article_id}/levels
返回该文章已有的全部等级列表（10 条），用于「切换等级」选择器。**无需再请求**。

---

## 4. 音频与时间轴接口（核心）

### GET /api/v1/versions/{version_id}/audio
取音频元信息。响应：
```json
{
  "url": "/api/v1/audio/512.mp3",
  "duration": 41.2,
  "size_bytes": 318422,
  "engine": "edge-tts",
  "voice": "en-US-AriaNeural",
  "has_timeline": true,
  "word_count": 108
}
```

### GET /api/v1/audio/{audio_id}.mp3
音频文件本体。

- `Content-Type: audio/mpeg`（降级模式为 `audio/wav`）
- 支持 **HTTP Range**（`Accept-Ranges: bytes`）→ 播放器可拖动进度条
- `Cache-Control: public, max-age=604800`（7 天）
- 加 `?download=true` 触发下载

### GET /api/v1/versions/{version_id}/timeline
**逐词时间轴 —— App 跟读高亮的核心接口。**

**Query**

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `format` | `compact` \| `full` | `compact` | `full` 额外返回原始 boundary 与句子分段 |

**响应（compact）**
```json
{
  "version_id": 1024,
  "level_code": "en_a1",
  "lang": "en",
  "duration": 41.2,
  "text": "Scientists found a new frog.\n\nThe frog lives in the Amazon forest.",
  "voice": "en-US-AriaNeural",
  "engine": "edge-tts",
  "word_count": 13,
  "words": [
    { "i": 0, "w": "Scientists", "s": 0.1, "e": 0.787, "sm": 100,  "em": 787,  "cs": 0,  "ce": 10, "si": 0 },
    { "i": 1, "w": "found",     "s": 0.8, "e": 1.05, "sm": 800,  "em": 1050, "cs": 11, "ce": 16, "si": 0 },
    { "i": 2, "w": "a",         "s": 1.06,"e": 1.10, "sm": 1060, "em": 1100, "cs": 17, "ce": 18, "si": 0 }
  ]
}
```

**`words[]` 字段**

| 字段 | 类型 | 说明 |
|---|---|---|
| `i` | int | 词序号（从 0 起） |
| `w` | string | 词文本（含标点，如 `rainforest,`） |
| `s` / `e` | float | 起始 / 结束时间（秒，3 位小数） |
| `sm` / `em` | int | 起始 / 结束时间（毫秒，播放器直接用这两个） |
| `cs` / `ce` | int | 在 `text` 中的**字符偏移**区间 `[cs, ce)` |
| `si` | int | 所属句子序号 |

**`format=full` 额外字段**

| 字段 | 说明 |
|---|---|
| `sentences[]` | 按标点聚合的句子分段：`{i, start, end, start_ms, end_ms, text, char_start, char_end}` |
| `boundaries[]` | 引擎原始时间戳（100ns 制）：`{offset, duration, text}` |

**App 端用法要点**

1. **逐词高亮**：音频播放到 `t` 毫秒时，二分查找满足 `sm ≤ t < em` 的词 `i`，用 `cs/ce` 反查 `text` 高亮。
2. **点击词跳转**：点击 → 用 `sm` seek 音频。
3. **朗读速度 0.5–2.0x**：`em` 与 `sm` 同比例缩放即可，无需重新请求。
4. **字符覆盖保证**：`cs/ce` 单调递增、不重叠、100% 覆盖全文（不含空格），可直接 slice。
5. 标点单独成词（如 `,` `.`），便于实现「按词朗读」。

### GET /api/v1/versions/{version_id}/subtitle.srt
返回 SRT 字幕（`text/x-subrip`），每 6 词一条。用于「字幕模式」朗读。

---

## 5. 运维接口

### GET /api/v1/jobs?limit=20
查看任务执行历史。

```json
[
  {
    "id": 42,
    "job": "daily",
    "status": "success",
    "started_at": "2026-10-06T21:00:03+00:00",
    "finished_at": "2026-10-06T21:04:12+00:00",
    "stats": { "fetched": 7, "created": 7, "versions": 70, "audios": 70, "audio_errors": 0, "skipped": 0 },
    "error": null
  }
]
```

### POST /api/v1/jobs/daily
手动触发一次流水线（补数据用）。

**Query**：`topics`（逗号分隔）、`per_topic`（1–5）、`with_tts`（bool）、`force`（bool，重建已有内容）

**响应**：`{ "status": "success", "stats": {...}, "error": null, "finished_at": "..." }`

---

## 6. 错误码

| HTTP | `detail` | 处理建议 |
|---|---|---|
| 400 | `date 需为 YYYY-MM-DD` | 检查参数格式 |
| 404 | `version 不存在` / `该文章没有此语言等级的改写版本` | 检查 id；等级可能尚未生成 |
| 404 | `该版本尚未生成音频` | 该篇 TTS 失败，等下次补数 |
| 410 | `音频文件已丢失，请重新生成` | 触发 `POST /jobs/daily?force=true` |
| 500 | `internal error: ...` | 服务端异常，App 侧降级到缓存内容 |

---

## 7. App 端推荐调用顺序

```
启动
 ├─ GET /health                      探活（兼做「后端已唤醒」预热）
 ├─ GET /api/v1/levels               等级字典（缓存，等级筛选器）
 └─ GET /api/v1/topics               主题字典（缓存）

首页（list）
 └─ GET /api/v1/articles/today?lang=en&level=1
       └─ 点进详情
            ├─ GET /api/v1/articles/{article_id}?lang=en&level=1
            ├─ GET /api/v1/versions/{version_id}/timeline     加载时间轴
            └─ GET /api/v1/audio/{audio_id}.mp3                流式播放

练习/词汇
 ├─ GET /api/v1/versions/{version_id}          取 vocab[] 做题
 └─ GET /api/v1/articles/{article_id}/levels   切换等级（0 额外请求）
```

**缓存建议**
- `/levels`、`/topics`：版本号不变，永久缓存
- `today` 列表：按 `(date, lang, level)` 缓存当日结果
- 音频：交由 URL + `Cache-Control` 与 CDN 处理，App 侧落盘缓存
- 时间轴：与详情同生命周期，内存缓存即可

---

## 8. 数据规模估算

- 每天 7 主题 × 1 篇 = **7 篇源新闻 → 70 个改写版本 → 70 段音频**
- 每段音频约 0.3–0.5 MB（edge-tts 24kbps mp3，40 秒左右）
- 每天新增 ≈ 30 MB，**每月 ≈ 1 GB**
- 免费额度提醒：免费 Render 实例磁盘不持久，音频与 DB 需持久盘或外置对象存储，见部署说明