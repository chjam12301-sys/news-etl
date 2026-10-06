# 每日英语听力 · 内容后台

每天自动抓取多主题新闻 → AI 改写成 **5 个英语等级（CEFR A1–C1）+ 5 个日语等级（JLPT N5–N1）**
→ 生成带 **逐词时间轴** 的免费 TTS 音频 → 通过 REST API 供 App 调用。

**全免费**（无需信用卡 / 无需付费模型）。

---

## 特性

| 能力 | 实现 |
|---|---|
| 新闻抓取 | 21 个公开 RSS（BBC / Ars / Nature / WHO / ESPN / Guardian…），7 大主题，正文抽取 |
| AI 改写 | Gemini 2.5 Flash（free 1500次/天）→ OpenRouter 免费模型 → 离线降级，三级兜底 |
| 等级体系 | `en_a1`…`en_c1`（CEFR）+ `ja_n5`…`ja_n1`（JLPT），每级独立提示词与目标词数 |
| TTS | `edge-tts`（免费无限），英/日各一个音色，**逐词时间戳 + 字符偏移** |
| 时间轴 | `sm/em` 毫秒 + `cs/ce` 字符区间，保证单调递增、不重叠、全覆盖 |
| API | FastAPI + OpenAPI，含音频 Range 请求、SRT 导出、句子分段 |
| 部署 | Dockerfile + `render.yaml`（Web Service + Cron Job 蓝图） |

---

## 快速开始

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # GEMINI_API_KEY 可留空（走离线降级）

.venv/bin/python -m app.cli daily --topics tech,science --per-topic 1
.venv/bin/python -m app.cli serve
```

打开 http://127.0.0.1:8000/docs 查看交互式接口文档。

---

## 目录结构

```
app/
  config.py      配置（全部走环境变量）
  levels.py      10 个等级定义与改写指令
  fetcher.py     RSS 抓取 + 正文抽取 + 去重指纹
  rewriter.py    LLM 改写（三级 provider + 离线降级）
  tts.py         TTS 合成 + 逐词时间轴对齐   ← 核心
  pipeline.py    每日流水线编排
  api.py         FastAPI 路由
  schemas.py     出参模型
  db.py          SQLAlchemy 模型 + 建表/迁移
docs/
  API.md         接口文档（给 App 端）
  DEPLOY.md      部署指南（全免费方案）
tests/           16 个回归测试
```

---

## 核心：时间轴是怎么对齐的

`edge-tts` 返回的 boundary 文本**不保证**等于送进去的原文（日文按词组切、数字会被改写）。
本项目的对齐策略：

1. **精确子串定位** —— 每个 boundary 在原文中 `find`，定位成功即完全可信
2. **定位失败就跳过** —— 不猜测、不错位
3. **缺口插值** —— 剩余字符（标点、被跳过的词）按字符长度在相邻时间点之间等比插值

由此保证 `text[cs:ce] == word` 恒成立、字符区间单调递增、100% 覆盖 ——
这三点是 App 端逐词高亮不跳字的前提。

> ⚠️ 注意：`edge-tts >= 6.1` 把 `boundary` 参数移到了 `Communicate.__init__`，
> 且**默认只返回句边界**。必须显式传 `boundary="WordBoundary"` 才有逐词时间戳。
> 详见 `app/tts.py::_synth_edge`。

---

## 测试

```bash
.venv/bin/python -m pytest tests/ -q     # 16 passed
```

覆盖：等级体系、文本规整、时间轴对齐（单调/缺口/标点）、LLM JSON 解析、离线降级、TTS 端到端。

---

## 常用命令

```bash
# 补数据 / 重建
python -m app.cli daily --per-topic 2
python -m app.cli daily --force                    # 忽略已有结果
python -m app.cli daily --topics tech --no-tts     # 只改写不配音

# 接口
GET  /health
GET  /api/v1/levels                                # 等级字典
GET  /api/v1/articles/today?lang=en&level=1        # 首页列表
GET  /api/v1/articles/{id}?lang=en&level=2         # 详情（切等级）
GET  /api/v1/versions/{id}/timeline                # 逐词时间轴
GET  /api/v1/audio/{audio_id}.mp3                  # 音频（支持 Range）
POST /api/v1/jobs/daily                            # 手动触发流水线
```

完整说明见 [`docs/API.md`](docs/API.md)。