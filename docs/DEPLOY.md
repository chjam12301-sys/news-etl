# 部署指南 · 全免费方案

> 一台机器都不要买。以下全部落在免费额度内。

---

## 一、选型与理由

| 组件 | 方案 | 免费额度 | 为什么选它 |
|---|---|---|---|
| 云服务器 | **Render Web Service** | 750 小时/月（≈ 全月） | 无需信用卡，Docker 直部署，有健康检查与自动休眠唤醒 |
| 定时任务 | **Render Cron Job** | 免费 | 独立于 Web 服务，跑完即退，不占常驻额度 |
| 数据库 | **SQLite**（本地开发）/ Postgres（Render） | — | 数据量小（每天 70 行版本）；免费 Render 不带持久盘，见下方「必须注意」 |
| AI 改写 | **Gemini 2.5 Flash** free tier | 1500 次/天 | 我们每天只用 70 次，余量极大；不够时自动切 OpenRouter 免费模型 |
| TTS | **edge-tts** | **无限制** | 微软 Edge 浏览器同款引擎，无需 key，**原生输出逐词时间戳** |
| 新闻源 | 21 个公开 RSS | 无限制 | BBC / Ars / Nature / WHO / ESPN 等，无需 API key |

**每天消耗**：70 次 Gemini 调用（额度 4.2%）+ 70 段 TTS（不限量）+ 约 30MB 数据。

---

## 二、需要你注册的（2 个，全程约 5 分钟，不绑卡）

### 1️⃣ Render —— https://render.com

1. 点 `Get Started` → 用 **GitHub 或邮箱**注册
2. **不要**选任何付费方案，免费版足够
3. 登录后进入 Dashboard

### 2️⃣ Google AI Studio（拿 Gemini key）—— https://aistudio.google.com/apikey

1. 用 Google 账号登录
2. 点 `Create API Key` → 复制保存（形如 `AIza...`）
3. 免费额度：Gemini 2.5 Flash 每天 1500 次，我们每天用 70 次

> ⚠️ **暂时不想注册 AI key 也能跑**：留空 `GEMINI_API_KEY` 时系统自动进入**离线降级模式**——
> 接口结构、正文、时间轴、音频全部正常产出，只是文本不做真正的分级改写（走分句裁剪）。
> 建议先这样验证链路，再补 key。

---

## 三、部署步骤

### 方式 A：一键 Blueprint（推荐）

1. 把本目录推到 GitHub 仓库
2. Render Dashboard → `New` → `Blueprint`
3. 选择你的仓库，Render 自动读 `render.yaml`
4. 在表单里填 `GEMINI_API_KEY`（sync:false 字段会要求你手填）
5. 点 `Apply` → 完成

得到两个服务：
- `news-etl-api`（Web）→ 提供 API
- `news-etl-daily`（Cron）→ 每天 UTC 21:00（北京时间次日 05:00）跑流水线

### 方式 B：手动 Web Service

1. `New` → `Web Service` → 连仓库
2. Environment：`Docker`
3. Region：`Singapore`
4. Instance Type：`Free`
5. Health Check Path：`/health`
6. 环境变量：

| Key | Value |
|---|---|
| `GEMINI_API_KEY` | 你的 key |
| `PYTHONPATH` | `/app` |
| `DATA_DIR` | `/var/data` |
| `TTS_ENABLED` | `true` |
| `TOPICS` | `tech,business,science,health,sports,culture,world` |
| `ARTICLES_PER_TOPIC` | `1` |

7. 再 `New` → `Cron Job`，Docker 命令填 `python -m app.cli daily --per-topic 1`，Schedule 填 `0 21 * * *`

---

## 四、⚠️ 免费版必须处理的两个坑

Render **免费实例的本地磁盘不持久**，重启/重新部署会丢数据。这会影响两个东西：数据库和音频文件。

### 方案 1：挂持久盘（需免费磁盘）

Render 免费 Web Service 不提供持久盘；若你的账号有，选一个挂到 `/var/data`，
并设 `DATA_DIR=/var/data`。数据库和音频都会持久化。

### 方案 2：全外置（100% 免费，推荐）

数据库换免费托管 Postgres，音频换对象存储：

```bash
# 数据库：Neon / Supabase / Aiven，任选，均有免费层
DATABASE_URL=postgresql+psycopg://user:pass@ep-xxx.region.aws.neon.tech/dbname

# 音频：Cloudflare R2（S3 兼容）—— 免费 10GB/月，足够放 1 年
```

代码侧把 `AudioAsset.file_path` 换成 R2 对象 key（当前已把路径存库，切换只需改读取处）。

### ⚠️ 还有一个坑：Cron Job 和 Web Service 不共享内存

免费版 Cron Job 每次都是**全新容器**，它写入的数据 Web Service 要能读到。
所以**两者必须共用同一个数据库**。只靠各自本地 SQLite 的话，Cron 跑出来的内容 API 读不到。

**最省事的做法**：把整条流水线挂到 Web 服务上，用一个内部定时器触发，不建独立 Cron Job。
在 `app/main_api.py` 加 APScheduler，或直接让 Render Cron 调 Web 服务的接口：

```
# Render Cron Job 命令（无需镜像和数据库）
curl -X POST https://<你的域名>/api/v1/jobs/daily
```

这样数据落在 Web 服务所在的同一个库里，且有持久盘时最稳。

> 推荐最终形态：**只建 Web Service** + Cron Job 调上面的 `curl`。代码已支持。

---

## 五、验证部署

```bash
BASE=https://你的域名

# 1. 探活（首次会唤醒免费实例，约 30s）
curl $BASE/health

# 2. 等级字典
curl $BASE/api/v1/levels

# 3. 手动触发一次流水线（补首日数据）
curl -X POST "$BASE/api/v1/jobs/daily?per_topic=1"

# 4. 看结果
curl "$BASE/api/v1/articles?lang=en&level=3&limit=5"
curl "$BASE/api/v1/versions/1/timeline"     # 逐词时间轴
curl -I "$BASE/api/v1/audio/1.mp3"           # 音频（应 200/206）
```

交互式文档：`<域名>/docs`

---

## 六、本地开发

```bash
cd news-etl
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

cp .env.example .env        # 按需填 key，留空也能跑

.venv/bin/python -m app.cli daily --topics tech --per-topic 1   # 跑一次
.venv/bin/python -m app.cli serve                              # 起服务 :8000
.venv/bin/python -m pytest tests/ -q                           # 跑测试
```

---

## 七、成本核算

| 项目 | 用量 | 免费额度 | 占比 |
|---|---|---|---|
| Render Web | ~12 h/月（每天 3 次请求，触发唤醒） | 750 h | 1.6% |
| Gemini | 70 次/天 = 2100 次/月 | 45000 次 | 4.7% |
| TTS | 70 段/天 | **无限** | — |
| RSS 抓取 | 70 次/天 | 无限 | — |
| 数据存储 | ~1 GB/月 | 10 GB（R2） | 10% |

**结论：0 元。** 触顶的唯一可能是 Web 被高频访问导致 Render 免费时长耗尽 —— 加一层 CDN 缓存即可化解。

---

## 八、想提高内容质量

1. **加 AI key**（第 2 步），这是从「裁剪」变成「真正分级改写」的分水岭
2. 提高频率：`ARTICLES_PER_TOPIC=2`，每天 14 篇 → 140 个版本（Gemini 仍只占 9%）
3. 换音色：`TTS_VOICE_EN` / `TTS_VOICE_JA`，可用 `edge-tts --list-voices` 查看全部
4. 加主题：编辑 `app/fetcher.py` 的 `FEEDS` 列表