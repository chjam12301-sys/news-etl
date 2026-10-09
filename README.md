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
| 新闻抓取 | 30 个公开 RSS（BBC 全主题 / Guardian / Ars / CNBC / ESPN / Nature…），7 大主题，正文抽取 |
| 选题去重 | 按标题实词判重，跨源 + 跨天（同一事件被多家改写也拦得住） |
| 选题打分 | 源权重 + 标题规则 + Google News 热度，挡掉「科研流程」类无事件标题 |
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
  levels.py      10 个等级定义与改写指令（含段落区间 + 字数硬上限）
  dedup.py       选题去重：标题实词集合，跨源 + 跨天
  selector.py    选题打分：源权重 + 标题规则 + 外媒热度
  fetcher.py     RSS 抓取 + 正文抽取 + 去重指纹 + Google News 热度信号
  rewriter.py    LLM 改写（三级 provider + 离线降级 + 超长裁剪）
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
tests/           138 个回归测试
```

---

## 内容质量：三道闸

抓来的稿子要过三关才会进入改写。这三层都是被真实问题逼出来的：
北极冻土写了 4 篇、Nvidia 3 篇、石油公司 3 篇，A1 产出 116~230 词（目标 110）。

### ① 去重（`app/dedup.py`）

源指纹只按 URL + 标题算，挡不住这些：

| 情况 | 例子 |
|---|---|
| 源自己改了标题 | `Oil companies ask top court…` → `Oil Companies Ask Supreme Court…` |
| 同一事件两家报 | `Former German spy chief arrested…` / `Germany arrests former spy chief…` |
| 同一件事连报 4 天 | 北极冻土 ×4 |

按**标题实词集合**判重（去停用词、去发布方后缀）：

- Jaccard ≥ 0.45，或
- 共享 ≥ 3 个实词 且 占较短标题 ≥ 40%

回看窗口 60 天（`DEDUP_LOOKBACK_DAYS`）。判重发生在**抓全文之前** ——
全文抓取是最贵的一步，先判重能省掉重复稿的网络开销。

### ② 选题打分（`app/selector.py`）

| 维度 | 规则 |
|---|---|
| 源权重 | BBC / Guardian / CNBC / ESPN 等大众编辑部 1.0；Nature / Phys.org / NIH / WHO 等学术源 0.35–0.45 |
| 出局 | `How…` / `Why…` 解释型、勘误、评论、科研计量（`Nature Index tables`、`peer review`） |
| 扣分 | `study` / `paper` / `researchers` −0.20；`needs` / `urges` 倡议腔 −0.15；`small` / `mistake` −0.25 |
| 加分 | 强动词（arrests / dies / bans / launches）+0.15；人名地名 +0.12；数字金额 +0.10 |

兜底分三级：合格 → 未出局但低分 → 只剩出局稿。空一天 App 就没内容，
所以宁可出一条平庸的；但**已判出局的稿子不能被捞回来**（它分数恒为 0，
按分数排会排到低分但可用的稿子前面）。

### ③ Google News 只做热度信号

`news.google.com` 的 RSS 链接是 `CBMi…` 跳转壳 —— 跟随重定向拿到的是
Google 自己的 JS 页面（实测 593 KB，无正文），解 base64 也只得到不透明 token。
**取不到正文，所以不能当正文源。** 只取标题，统计「有多少家外媒在报同一件事」，
每家 +0.05（上限 +0.3）。

### ④ 字数硬上限（`app/levels.py` + `rewriter.py`）

只写「about N words」没用，模型稳定超出。现在每级给**段落区间 + 全局硬上限**，
并由 `_trim_to_limit()` 逐句裁到上限内（中译同步裁，保证段落一一对应）：

| 等级 | 目标 | 上限 | 段落 |
|---|---|---|---|
| A1 | 90 | 100 | 3–4 段 × 18–28 词 |
| A2 | 150 | 170 | 3–4 段 × 30–50 词 |
| B1 | 200 | 230 | 3–5 段 × 40–60 词 |
| B2 | 250 | 290 | 3–5 段 × 50–70 词 |
| C1 | 300 | 345 | 3–6 段 × 60–85 词 |

标题另有一套新闻学规则（`HEADLINE_RULES`）：具体的事 + 谁做的 + 强动词，
禁止 `A look at…` / `Researchers explore` / `study` 这类空泛名词。

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
.venv/bin/python -m pytest tests/ -q     # 138 passed
```

覆盖：等级体系与字数硬上限、时间轴对齐（单调/缺口/标点/坐标系契约）、LLM JSON 解析、
离线降级、选题去重（真实历史重复稿）、选题打分、TTS 端到端、
存储层（本地 + R2，含路径穿越防护）、发布校验。


---

## 文档

| 文档 | 用途 |
|---|---|
| **[API.md](docs/API.md)** | **接口文档** —— 给 App 端对接用 |
| **[APP-封面图需求.md](docs/APP-封面图需求.md)** | 封面图与署名 —— App 端必做项与验收清单 |
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