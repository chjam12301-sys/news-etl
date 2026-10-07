# 部署与运维 · 全免费方案

📄 **在线查看**：[github.com/chjam12301-sys/news-etl/blob/main/docs/DEPLOY.md](https://github.com/chjam12301-sys/news-etl/blob/main/docs/DEPLOY.md)

---

## 一、当前架构

```
        GitHub Actions（每天北京时间 05:30 自动跑）
  抓 21 个 RSS → DeepSeek 改写 10 等级（含中译）→ edge-tts 配音+时间轴
                → Openverse 抓 CC0 配图
                        ↓
              Neon Postgres（元数据 + 时间轴）
              GitHub 仓库 content/（音频 + JSON）
                        ↓
              jsDelivr CDN → App 直接读
```

**没有服务器**，因此没有冷启动、没有数据库连接失败、没有月费。

---

## 二、配额核算

| 组件 | 服务 | 免费额度 | 实际用量 | 占比 |
|---|---|---|---|---|
| 定时计算 | GitHub Actions | 2000 min/月 | ~150 min | **7.5%** |
| 对象存储/CDN | GitHub 仓库 + jsDelivr | 公开仓库无限 + 10GB 流量 | ~1 GB/月 | ~10% |
| 数据库 | Neon Postgres | 0.5 GB | ~10 MB/月 | **0.2%** |
| AI 改写 | DeepSeek | 有免费额度 | 70 次/天 | 低 |
| TTS | edge-tts | **无限制** | 70 段/天 | — |
| 配图 | Openverse | 无限制 | 7 次/天 | — |

**总成本 0 元，全程不绑卡**（GitHub 绑卡仅为解除账单锁定，Actions 额度为 0/2000 分钟）。

---

## 三、需要的账号

| 服务 | 地址 | 用途 |
|---|---|---|
| **GitHub** | 已登录 | 代码仓库 + Actions 定时任务 + CDN 源|
| **Neon** | console.neon.tech | Postgres（GitHub 一键登录，区域选 **Singapore**） |
| **DeepSeek** | platform.deepseek.com | AI 分级改写 |
| Openverse | 免注册 | CC0 配图源 |
| edge-tts | 免注册 | TTS + 逐词时间轴 |

⚠️ **不需要**：Cloudflare R2、Supabase、Backblaze、Render —— 这几个都试过，走不通或有限制。

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
| **保留天数** | **14 天**（`GITHUB_KEEP_DAYS`），自动清理更早的音频与 JSON |
| 仓库体积 | 稳定在 400 MB 内，远低于 GitHub 1 GB 软警告线 |

调大内容量会同时增加仓库体积与 CDN 流量，注意 14 天保留策略是否够用。

---

## 六、CDN 缓存机制（重要）

jsDelivr 对**分支名** `@main` 缓存很久，会返回几小时前的旧数据（表现为「详情没有时间轴」）。

**因此 App 端不要写死 SHA**，运行时查：

```javascript
const REPO = "chjam12301-sys/news-etl";
const { sha } = await fetch(`https://api.github.com/repos/${REPO}/commits/main`)
  .then(r => r.json());
const BASE = `https://cdn.jsdelivr.net/gh/${REPO}@${sha}/content`;
```

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
| `created: 0` | RSS 去重，用 `reset=true` |
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
.venv/bin/python -m pytest tests/ -q             # 34 个测试
```

⚠️ 本机网络连不上 Openverse（Cloudflare 相关域名被阻断），**配图只能在 Actions 里跑**。

---

## 十、关键文件

| 文件 | 作用 |
|---|---|
| `app/pipeline.py` | 流水线编排：抓取 → 改写 → 配音 → 导出 → 提交 |
| `app/tts.py` | TTS + 逐词时间轴对齐（核心） |
| `app/rewriter.py` | DeepSeek 改写（10 等级 + 中译） |
| `app/images.py` | Openverse CC0 配图 |
| `app/storage_github.py` | GitHub 仓库作为存储 |
| `app/exporter.py` | 静态 JSON 导出（含 CDN 链接生成） |
| `.github/workflows/daily.yml` | 定时任务定义 |
| `docs/API.md` | **接口文档**（给 App 端） |