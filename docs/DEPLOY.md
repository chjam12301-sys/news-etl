# 部署与运维 · 全免费方案

📄 **在线查看**：[github.com/chjam12301-sys/news-etl/blob/main/docs/DEPLOY.md](https://github.com/chjam12301-sys/news-etl/blob/main/docs/DEPLOY.md)

---

## 一、当前架构

```
        GitHub Actions（每天北京时间 05:30 自动跑）
  抓 30 个 RSS + Google News 热度 → 去重/选题打分 → DeepSeek 改写 10 等级（含中译） → edge-tts 配音+时间轴
                → Openverse 抓 CC0 配图
                        ↓
              Neon Postgres（元数据 + 时间轴）
                        ↓
        ┌───────────────┴────────────────┐
        ↓                                ↓
  Cloudflare R2                      GitHub 仓库 content/
  （音频 mp3，214MB）                 （**只有 JSON**，16MB）
        ↓                                ↓
  pub-aba43a6fb1db4dc08fede1dbc81f3241.r2.dev                     jsDelivr CDN（@commit 不可变版本）
        └───────────────┬────────────────┘
                        ↓
                    App 直接读
```

**没有服务器**，因此没有冷启动、没有数据库连接失败、没有月费。

### 为什么音频与 JSON 要分开

jsDelivr 对单个 GitHub 仓库有 **50MB 硬上限**。超限后，**此前已缓存的旧路径
还能取，但任何新路径都返回 403**（`Package size exceeded the configured limit
of 50 MB`）—— 表现是「老文章能看，新文章点进去 404」。

实测音频 214MB / JSON 16MB，音频是唯一超标项。所以：

| | 存放位置 | 理由 |
|---|---|---|
| JSON（索引、详情） | GitHub + jsDelivr | 需要 `@<commit>` 不可变版本，App 的版本发现与 `content_hash` 比对都依赖它 |
| 音频 mp3 | Cloudflare R2 | 体积大；R2 免费额度 10GB/月，**出网流量 0 费用** |

---

## 二、配额核算

| 组件 | 服务 | 免费额度 | 实际用量 | 占比 |
|---|---|---|---|---|
| 定时计算 | GitHub Actions | 2000 min/月 | ~150 min | **7.5%** |
| JSON 分发 | GitHub 仓库 + jsDelivr | 公开仓库无限 | ~16 MB | <1% |
| 音频存储 | Cloudflare R2 | 10 GB/月 + 出网免费 | ~214 MB（增长中） | ~2% |
| 数据库 | Neon Postgres | 0.5 GB | ~10 MB/月 | **0.2%** |
| AI 改写 | DeepSeek | 有免费额度 | 70 次/天 | 低 |
| TTS | edge-tts | **无限制** | 70 段/天 | — |
| 配图 | Openverse | 无限制 | 7 次/天 | — |

**总成本 0 元，全程不绑卡**（GitHub 绑卡仅为解除账单锁定，Actions 额度为 0/2000 分钟）。

> ⚠️ 音频按每天 ~7MB 增长，10GB 免费额度约可用 4~5 年；`STORAGE_LIMIT_GB=8`
> 留了余量，超限时 `storage_guard` 会跳过配音而非产生费用。

---

## 三、需要的账号

| 服务 | 地址 | 用途 |
|---|---|---|
| **GitHub** | 已登录 | 代码仓库 + Actions 定时任务 + JSON 分发 |
| **Cloudflare** | dash.cloudflare.com → R2 | 音频存储（**已开通**） |
| **Neon** | console.neon.tech | Postgres（GitHub 一键登录，区域选 **Singapore**） |
| **DeepSeek** | platform.deepseek.com | AI 分级改写 |
| Openverse | 免注册 | CC0 配图源 |
| edge-tts | 免注册 | TTS + 逐词时间轴 |

⚠️ **不需要**：Supabase、Backblaze、Render —— 试过，走不通或有限制。

### Cloudflare R2 配置要点（踩过的坑）

1. **S3 API 端点的 account ID 极易抄错**。抄错时 boto3 报的错是
   `SSL validation failed ... SSLV3_ALERT_HANDSHAKE_FAILURE` ——
   看起来像网络问题，实际是服务端证书不匹配直接拒绝握手。
   正确格式：`https://<account id>.r2.cloudflarestorage.com`（**不带桶名**）。
2. `r2.dev` 公开域名**官方明确不用于生产**（有速率限制、无CDN 缓存）。
   上线前应给桶挂自定义域名；在此之前，`r2.dev` 对当前量级完全够用。
3. 桶需要开启「Public Development URL」才能匿名读。

### GitHub Actions Secrets

| 名称 | 说明 |
|---|---|
| `DATABASE_URL` | Neon 连接串（pooler 端点） |
| `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` | DeepSeek |
| `R2_BUCKET` | 桶名，如 `news-etl-audio` |
| `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` | R2 API Token |
| `R2_ENDPOINT` | `https://<account id>.r2.cloudflarestorage.com` |
| `R2_PUBLIC_BASE` | `https://pub-aba43a6fb1db4dc08fede1dbc81f3241.r2.dev` |

⚠️ 仓库是 **public**，任何凭据都不得写进代码或提交进 git。
`scripts/republish.py` 曾硬编码过数据库密码，已移除。

---

## 四、日常操作

### 手动触发一次生成

```bash
gh workflow run daily.yml -f topics=tech,science,world -f per_topic=1
```

可用参数：

| 参数 | 说明 |
|---|---|
| `topics` | 逗号分隔，留空用全部 7 个主题 |
| `per_topic` | 每主题几篇（默认 1） |
| `force` | 强制重建（默认 true） |
| `reset` | **清空数据库后重跑** —— RSS 去重导致抓不到新文章时用 |

### 重新发布（修链接 / 换存储后）

```bash
gh workflow run republish.yml -f dry=false     # 完整跑四阶段并校验
gh workflow run republish.yml -f dry=true      # 只校验现状，不改动
```

发布顺序固定为四步，**任何一步失败都不会更新 `latest.json`**，
App 继续用上一份可用索引：

```
① 提交详情与音频   → bcf1e0b
② 用该 commit 生成索引 → 51bbe21（链接钉在 bcf1e0b）
③ 全量校验：每份详情 + 每个音频都真实可访问、身份与 hash 一致
④ 才更新 latest.json 指向 ②
```

### 音频重新上传到 R2

```bash
gh workflow run migrate-audio.yml -f drop_git=true -f force=false
```

只有换桶、换域名或音频丢失时才需要。`force=true` 会全部重传（214MB）。

### 重新发布（修链接 / 换存储后）

```bash
gh workflow run republish.yml -f dry=false     # 完整跑四阶段并校验
gh workflow run republish.yml -f dry=true      # 只校验现状，不改动
```

发布顺序固定为四步，**任何一步失败都不会更新 `latest.json`**，
App 继续用上一份可用索引：

```
① 提交详情与音频   → bcf1e0b
② 用该 commit 生成索引 → 51bbe21（链接钉在 bcf1e0b）
③ 全量校验：每份详情 + 每个音频都真实可访问、身份与 hash 一致
④ 才更新 latest.json 指向 ②
```

### 音频重新上传到 R2

```bash
gh workflow run migrate-audio.yml -f drop_git=true -f force=false
```

只有换桶、换域名或音频丢失时才需要。`force=true` 会全部重传（214MB）。

### 查看运行日志

```bash
gh run list -R chjam12301-sys/news-etl --limit 3
gh run view <run-id> -R chjam12301-sys/news-etl --log | grep -E "\[pipe\]|\[img\]"
```

日志里几个关键标记：

| 标记 | 含义 |
|---|---|
| `[img] Openverse 'x' → N 条` | 配图搜索命中数，0 表示该词没结果 |
| `[img] article_id=N → CC0 <url>` | 配图成功（**应看到这条**） |
| `[pipe] 改写完成 10 个等级` | DeepSeek 正常 |
| `[pipe] git_pushed: true` | 内容已提交到仓库 |

---

## 五、内容更新策略

| 项 | 值 |
|---|---|
| 频率 | 每天北京时间 05:30 |
| 每篇产出 | 10 个版本（en_a1…en_c1 + ja_n5…ja_n1），各带中译、音频、时间轴、配图 |
| **保留天数** | **14 天**（`GITHUB_KEEP_DAYS`），自动清理更早的 JSON |
| 仓库体积（JSON） | **16 MB** —— 远低于 jsDelivr 的 50 MB 硬上限 |
| 音频体积（R2） | 214 MB，每天约 +7 MB，R2 免费额度 10 GB |

> ⚠️ 仓库体积有硬上限保护：`publish_all` 在超过 40 MB 时会自动清理，
> 仍超过 50 MB 则**拒绝发布**（保留旧索引），不会让线上处于半坏状态。

---

## 六、CDN 缓存机制（重要）

**首选：读 `latest.json`（推荐，v2.1 起）**

`https://cdn.jsdelivr.net/gh/chjam12301-sys/news-etl@main/content/latest.json`
本身就是为「自刷新」设计的 —— 它不能钉在固定 ref 上，否则 jsDelivr 缓存会让它
永远停在旧值。里面的 `index_url` 指向本次发布的固定 ref。

```javascript
const { index_url: INDEX } = await fetch(
  "https://cdn.jsdelivr.net/gh/chjam12301-sys/news-etl@main/content/latest.json"
).then(r => r.json());
const index = await fetch(INDEX).then(r => r.json());
```

**兜底：GitHub API 查 SHA**（注意匿名请求会被限流，务必带认证或不要依赖）

```javascript
const REPO = "chjam12301-sys/news-etl";
const { sha } = await fetch(`https://api.github.com/repos/${REPO}/commits/main`)
  .then(r => r.json());
const BASE = `https://cdn.jsdelivr.net/gh/${REPO}@${sha}/content`;
```

**音频不走 jsDelivr**，地址在 JSON 的 `audio.url` 里直接给出，
对象存储不做缓存，所以读到什么就是什么。

自证是否拿到新版：看 `index.json` 的 `version.published_at`，明显早于当前时间就是命中旧缓存了。

> `raw.githubusercontent.com` 的 `ETag` 是 **blob SHA**（文件级），
> 不是 commit SHA，拼 CDN 会全部 404 —— **不要用它当版本号**。

---

## 七、防扣费

| 护栏 | 位置 | 行为 |
|---|---|---|
| Actions 预算 | GitHub Settings → Billing → Budgets | 建议设10 美元 + 超支停止 |
| 仓库体积 | `GITHUB_KEEP_DAYS=14` 自动 prune | 超期文件自动删除 |
| 时间上限 | workflow `timeout-minutes: 30` | 超时被杀 |
| 并发限制 | `concurrency: daily-news` | 不叠加跑 |

**若真产生费用**，立刻：
1. 仓库 → Settings → Actions → **Disable**（停止定时任务）
2. 删除仓库或降级 GitHub 计划

数据在 Neon 与 content/ 里，跟计费无关，不会丢。

---

## 八、故障排查

| 现象 | 排查 |
|---|---|
| Actions 没跑 | 仓库 60 天无活动会被禁用；或看 Actions 页是否有黄色横幅 |
| `created: 0` | RSS 去重或选题去重拦下了；日志搜 `[dedup]`。确要重来用 `reset=true` |
| 某主题 0 篇 | 日志 `[fetch] 主题 X 无可用候选` —— 该主题源全挂或全被判重 |
| 配图全是 gradient | 看日志有没有 `[img] Openverse 'x' → N 条`；N=0 说明查询词太具体 |
| 中译为空 | 日志里 `llm=` 是否为 deepseek；若是 `offline` 说明 DeepSeek 没生效 |
| 客户端说没时间轴 | CDN 命中旧缓存，让 App 运行时查 SHA |
| `ModuleNotFoundError: socksio` | 代理为 SOCKS，需 `httpx[socks]`（requirements 已含） |
| Neon 连接失败 | 必须用 `-pooler` 端点，直连端点未开放 |

---

## 九、本地开发

```bash
cd news-etl
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env# 留空也能跑（走离线降级 + 本地存储）

.venv/bin/python scripts/run_full.py --force     # 完整重跑
.venv/bin/python -m app.cli serve                # 起服务 :8000
.venv/bin/python -m pytest tests/ -q             # 138 个测试
```

⚠️ 本机网络连不上 Openverse（Cloudflare 相关域名被阻断），**配图只能在 Actions 里跑**。

---

## 十、关键文件

| 文件 | 作用 |
|---|---|
| `app/pipeline.py` | 流水线编排：抓取 → 改写 → 配音 → 导出 → 提交 |
| `app/dedup.py` | 选题去重（标题实词，跨源跨天） |
| `app/selector.py` | 选题打分（源权重 + 标题规则 + 外媒热度） |
| `app/tts.py` | TTS + 逐词时间轴对齐（核心） |
| `app/rewriter.py` | DeepSeek 改写（10 等级 + 中译） |
| `app/images.py` | Openverse CC0 配图 |
| `app/storage_github.py` | GitHub 仓库作为存储 |
| `app/exporter.py` | 静态 JSON 导出（含 CDN 链接生成） |
| `.github/workflows/daily.yml` | 定时任务定义 |
| `docs/API.md` | **接口文档**（给 App 端） |