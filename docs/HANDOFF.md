# 交接清单 · news-etl 内容生成服务

本服务为《每日英语听力》App 生产「多主题新闻 → AI 分级改写 → TTS 音频 + 时间轴 → 静态 JSON」内容。
本文档面向接手维护/二次开发的工程师，列清**每一个外部依赖部署在哪、凭证是什么、坑在哪**。

> 一句话架构：**没有服务器**。所有计算跑在 GitHub Actions 临时 runner 里，产物是静态文件，
> App 直接从 CDN 读，不经过任何后端。

---

## 0. 总体架构（无服务器，0 成本）

```
GitHub Actions（北京时间每天 05:30）
  抓 12 个国内中文 RSS（爱范儿/IT之家/极客公园/快科技×2/少数派/理想生活实验室/
  什么值得买/钛媒体/机核/游研社/触乐）
  + Google News 中文热度 → 红线过滤 + 去重/选题打分 → DeepSeek 改写 10 等级（含中译）
  → edge-tts 本地配音+时间轴 → Openverse 抓 CC0 配图
        ↓
  Neon Postgres（元数据 + 时间轴）
        ↓
  ┌─────────────┴──────────────┐
  ↓                            ↓
Cloudflare R2                GitHub 仓库 content/
（音频 mp3，~214MB）          （只有 JSON，16MB）
  ↓                            ↓
pub-*.r2.dev            jsDelivr CDN（@commit 不可变版本）
  └─────────────┬──────────────┘
                ↓
            App 直接读（无后端）
```

---

## 1. 调度 / 计算

| 项 | 值 |
|---|---|
| 平台 | **GitHub Actions** |
| 仓库 | `chjam12301-sys/news-etl`（**public**） |
| Workflow | `.github/workflows/daily.yml` |
| 触发 | cron `30 21 * * *` UTC = 北京时间 05:30；也可 `gh workflow run daily.yml` 手动 |
| 并发 | `concurrency: daily-news`（不叠加跑） |
| 超时 | `timeout-minutes: 30` |
| 权限 | `contents: write`（用于推送 `content/`） |
| 用量 | ~150 min/月（免费额度 2000） |

---

## 2. AI 改写（DeepSeek）

| 项 | 值 |
|---|---|
| 服务 | DeepSeek（`deepseek-chat`，OpenAI 兼容接口） |
| 环境变量 | `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL=https://api.deepseek.com/v1` / `DEEPSEEK_MODEL=deepseek-chat` |
| 兜底 | Gemini → OpenRouter 免费模型 → 离线降级（三级） |
| 产物 | 每篇 10 个等级（en_a1…en_c1 + ja_n5…ja_n1），各带中译、词汇、lead |

---

## 3. TTS 音频生成（⚠️ 没有独立音频服务）

**不存在单独的「音频生成服务」。** 音频在 Actions runner 内**本地调用 edge-tts** 实时合成——

| 项 | 值 |
|---|---|
| 引擎 | `edge-tts`（微软免费、无限额、免 key） |
| 音色 | `en-US-AriaNeural`（英）/ `ja-JP-NanamiNeural`（日） |
| 输出 | mp3 + **逐词时间轴**（word boundary，含 `cs/ce` 字符区间、`sm/em` 毫秒） |
| 对象 key | `audio/<article_id>/<level>.mp3`（例：`audio/35/en_a1.mp3`） |
| 对外 URL | `https://pub-aba43a6fb1db4dc08fede1dbc81f3241.r2.dev/audio/<article_id>/<level>.mp3` |
| 存哪 | Cloudflare R2（**不进 Git**） |

代码入口：`app/tts.py`（合成+对齐）、`app/pipeline.py::_process_article`（调用）、`app/audio_store.py`（存储抽象）。

---

## 4. 音频存储：Cloudflare R2

| 项 | 值 |
|---|---|
| 桶 | `news-etl-audio` |
| S3 端点 | `https://<account id>.r2.cloudflarestorage.com`（**不带桶名**） |
| 当前公开域名 | `pub-aba43a6fb1db4dc08fede1dbc81f3241.r2.dev` |
| 免费额度 | 10 GB/月 + 出网 0 费用；当前 ~214 MB，每天约 +7 MB |
| 护栏 | `STORAGE_LIMIT_GB=8`，超限跳过配音（绝不产生费用） |
| 凭证（Actions Secrets） | `R2_BUCKET` / `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` / `R2_ENDPOINT` / `R2_PUBLIC_BASE` |

⚠️ **已知 open item：R2 未挂自定义域名**，现在用的是 `r2.dev` 开发域名。Cloudflare 官方明确 `r2.dev` 不用于生产（限速、无 CDN 缓存）。上量前需给桶挂自定义域名并改 `R2_PUBLIC_BASE`。

---

## 5. JSON / 静态内容：GitHub + jsDelivr

| 项 | 值 |
|---|---|
| 内容存放 | 仓库 `content/` 目录（**只有 JSON**，16 MB，远低于 50 MB 硬上限；音频已搬走） |
| CDN | `https://cdn.jsdelivr.net/gh/chjam12301-sys/news-etl@<commit>/content/...` |
| 入口 | `content/latest.json`（`@main` 可变）→ 内部 `index_url` 钉在固定 commit |
| 保留 | `GITHUB_KEEP_DAYS=14`，自动 prune 更早的 JSON |

> 为什么音频和 JSON 分开：jsDelivr 对单仓库 **50 MB 硬上限**，超限后新路径一律 403。
> JSON 必须留 Git 才能用 `@<commit>` 不可变版本（App 版本发现 + `content_hash` 比对依赖它）。

---

## 6. 数据库：Neon Postgres

| 项 | 值 |
|---|---|
| 用途 | 元数据（文章 / 版本 / 音频资产 / 任务记录）。R2 只存音频字节，元数据在 Neon |
| 连接 | `DATABASE_URL`（**必须 pooler 端点 `-pooler`**，直连端点未开放） |
| 区域 | Singapore |
| 免费额度 | 0.5 GB（实际 ~10 MB/月） |

---

## 7. 配图：Openverse（CC0）

- 免注册、无 key。仅作为抓取源（只存 URL + 署名，不转存图）。
- 抓不到时回退渐变占位块。**绝不使用媒体原图**（版权风险 + 热链易失效）。
- 代码：`app/images.py`。

---

## 8. 后端 API（FastAPI）—— ⚠️ 未部署

- 代码 `app/api.py`：CDN Range 音频、`/api/v1/levels`、`/articles/today`、`/versions/{id}/timeline` 等。
- 仅本地 `python -m app.cli serve`（:8000）或 Dockerfile 蓝图使用。
- **App 不调用它**，直接从 CDN 读。
- ⚠️ README 里「Dockerfile + render.yaml 部署」是早期蓝图，**未落地**（仓库无 `render.yaml` 文件）。不要照那个描述去找服务器。

---

## 9. Actions Secrets 清单（交接必填）

| Secret | 用途 |
|---|---|
| `DATABASE_URL` | Neon pooler 连接串 |
| `DEEPSEEK_API_KEY` | DeepSeek |
| `R2_BUCKET` | `news-etl-audio` |
| `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` | R2 API Token |
| `R2_ENDPOINT` | `https://<account id>.r2.cloudflarestorage.com` |
| `R2_PUBLIC_BASE` | `https://pub-aba43a6fb1db4dc08fede1dbc81f3241.r2.dev` |

> 仓库是 **public**，任何凭据都**不得写进代码或提交进 git**。`scripts/republish.py` 曾硬编码过密码，已移除——若加运维脚本，务必从 env 读。

---

## 10. 发布四阶段（`app/publish.py` → `publish_all`）

顺序固定，**任何一步失败都不会更新 `latest.json`**，App 继续用上一份可用索引：

```
① 提交详情与音频   → 某 commit
② 用该 commit 生成索引（链接钉在 ①）
③ 全量校验：每份详情 + 每个音频都真实可访问、身份与 hash 一致
④ 才更新 latest.json 指向 ②
```

---

## 11. 其它 Workflow

| 文件 | 作用 |
|---|---|
| `republish.yml` | 修链接 / 换存储后完整重发 + 校验（`-f dry=true` 只校验不动） |
| `migrate-audio.yml` | 重传音频到 R2（`-f drop_git=true -f force=false`） |
| `diag-r2.yml` | R2 连通性分层排查（DNS/TLS/curl/boto3/aws-cli/公开域名） |
| `backfill-images.yml` | 补配图 |
| `build-dict.yml` | 构建 IPA 词典 |

---

## 12. 已知坑 / open items

1. **R2 无自定义域名**：`r2.dev` 非生产，上量前需挂自定义域名。
2. **R2 端点 account id 极易抄错**：抄错时 boto3 报 `SSL validation failed ... SSLV3_ALERT_HANDSHAKE_FAILURE`（像网络问题，实为证书不匹配）。
3. **Neon 必须用 pooler 端点**，直连未开放。
4. **本地连不上 Openverse / Google News**：配图与热度信号只能在 Actions 里验；国内 12 个 RSS 本地可直连（2026-10-27 实测全部 200；超能网 expreview 例外，https 握手直接失败，已从源表移除）。
5. **选题质量**：去重（`app/dedup.py`）+ 选题打分（`app/selector.py`）+ 字数硬上限（`app/levels.py`/`rewriter.py::_trim_to_limit`）是 2026-10-09 上线，A1 实际字数需实跑验证。
6. **红线过滤**（`app/editorial.py`，2026-10-27）：官方域名 / 时政与官方通稿词在抓取阶段直接丢，不参与打分、不走兜底。改词表前先看该文件顶部的取舍说明。
7. **仓库 public**：绝不提交任何 key/密码。
