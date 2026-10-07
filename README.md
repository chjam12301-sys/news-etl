# 每日英语听力 · 内容后台

每天自动抓取多主题新闻 → AI 改写成 **5 个英语等级（CEFR A1–C1）+ 5 个日语等级（JLPT N5–N1）**
→ 生成带 **逐词时间轴** 的免费 TTS 音频 → 导出 JSON 与音频，App 从 CDN 直读。

**全免费、无服务器**（无需信用卡 / 无需付费模型 / 没有冷启动）。

## 架构

```
GitHub Actions（每天 05:00 北京时间）
   抓 RSS → Gemini 分级改写 → edge-tts 配音+时间轴
                ↓                    ↓
         Neon Postgres          Cloudflare R2（10GB 免费）
         元数据 + 时间轴          音频 + JSON
                                        ↓
                            App 直接读 CDN（无服务器）
```

| 组件 | 服务 | 免费额度 | 我们的用量 |
|---|---|---|---|
| 定时计算 | GitHub Actions | 2000 min/月 | ~150 min |
| 对象存储 | Cloudflare R2 | 10 GB + 出网免费 | ~1 GB/月 |
| 数据库 | Neon Postgres | 0.5 GB | ~1 MB/月 |
| AI 改写 | Gemini 2.5 Flash | 1500次/天 | 70 次/天 |
| TTS | edge-tts | **无限制** | 70 段/天 |

> 原方案是 Render 免费版，但实测发现三个硬伤：空闲 15 分钟休眠（App 首请求等 30–60 秒）、
> 无持久盘（数据必丢）、自带 Postgres 30 天过期、且注册需绑卡。故改为上述无服务器方案。
> 详见 [`docs/DEPLOY.md`](docs/DEPLOY.md)。

---

## 特性

| 能力 | 实现 |
|---|---|
| 新闻抓取 | 21 个公开 RSS（BBC / Ars / Nature / WHO / ESPN / Guardian…），7 大主题，正文抽取 |
| AI 改写 | Gemini 2.5 Flash（free 1500次/天）→ OpenRouter 免费模型 → 离线降级，三级兜底 |
| 存储 | 本地磁盘 / Cloudflare R2 双实现，配环境变量即切换 |
| 静态分发 | 每天自动导出 JSON（index / 按日/ 版本详情），CDN 直读 |
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
  storage.py     存储抽象：本地磁盘 / R2
  exporter.py    静态 JSON 导出（CDN 直读）
  pipeline.py    每日流水线编排
  api.py         FastAPI 路由
  schemas.py     出参模型
  db.py          SQLAlchemy 模型 + 建表/迁移
docs/
  API.md             接口文档（给 App 端）
  DEPLOY.md          部署运维（架构 / 配额 / 排查）
  防扣费清单.md两道护栏
tests/           34 个回归测试
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

> ⚠️ 注意 1：`edge-tts >= 6.1` 把 `boundary` 参数移到了 `Communicate.__init__`，
> 且**默认只返回句边界**。必须显式传 `boundary="WordBoundary"` 才有逐词时间戳。
> 详见 `app/tts.py::_synth_edge`。
>
> ⚠️ 注意 2：**字符偏移 `cs/ce` 的坐标系是「压平为单行后的 `text` 字段」，
> 不是带换行的 `body`。** 多段正文若用 `body` 去 slice 必然错位。
> 已在 `tests/test_core.py::test_timeline_offsets_index_into_spoken_text_not_body` 锁死。

---

## 测试

```bash
.venv/bin/python -m pytest tests/ -q     # 34 passed
```

覆盖：等级体系、时间轴对齐（单调/缺口/标点/坐标系契约）、LLM JSON 解析、离线降级、
TTS 端到端、存储层（本地 + R2，含路径穿越防护）。


---

## 文档

| 文档 | 用途 |
|---|---|
| **[API.md](docs/API.md)** | **接口文档** —— 给 App 端对接用 |
| [DEPLOY.md](docs/DEPLOY.md) | 部署运维 —— 架构、配额、故障排查 |
| [防扣费清单.md](docs/防扣费清单.md) | 两道护栏，防止意外产生费用 |


---

## 常用命令

```bash
# 补数据 / 重建
python -m app.cli daily --per-topic 2
python -m app.cli daily --force                    # 忽略已有结果
python -m app.cli daily --topics tech --no-tts     # 只改写不配音

# App 启动配置（拿到 CDN 地址模板）
GET  /api/v1/config

# 静态 JSON（CDN 直读，无需服务器）
GET  /data/index.json# 全量索引
GET  /data/{date}/index.json           # 某天完整列表
GET  /data/versions/{id}.json          # 详情 + 时间轴

# REST 接口
GET  /health
GET  /api/v1/levels                                # 等级字典
GET  /api/v1/articles/today?lang=en&level=1        # 首页列表
GET  /api/v1/articles/{id}?lang=en&level=2         # 详情（切等级）
GET  /api/v1/versions/{id}/timeline                # 逐词时间轴
GET  /api/v1/audio/{audio_id}.mp3                  # 音频（支持 Range）
POST /api/v1/jobs/daily                            # 手动触发流水线
```

完整说明见 [`docs/API.md`](docs/API.md)，部署与运维见 [`docs/DEPLOY.md`](docs/DEPLOY.md)。