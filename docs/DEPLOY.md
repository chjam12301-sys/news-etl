# 部署指南 · 无服务器全免费方案

> **架构已改**：不再用 Render。原因是 Render 免费版空闲 15 分钟即休眠（App 首请求要等 30–60 秒）、
> 无持久盘（数据必丢）、自带 Postgres 30 天过期、且注册需绑卡。
>
> 新方案：GitHub Actions 负责「生成」，Neon + R2 负责「存储」，CDN 负责「读取」。
> **没有服务器**，因此没有冷启动、没有数据库连接失败、没有月费。

---

## 一、架构与配额

```
       每天 05:00（北京时间）
GitHub Actions ──► 抓 RSS ──► Gemini 分级改写 ──► edge-tts 配音+时间轴
   2000 min/月          │
   （我们约 150）        ├──► 音频 ──► Cloudflare R2（10GB 免费 + 出网免费）
                        └──► 数据 ──► Neon Postgres（0.5GB 免费）
                                └──► JSON ──► R2（同一bucket）

App 端：直接读 R2 / CDN 的 JSON 与音频，完全不经过服务器
```

| 组件 | 服务 | 免费额度 | 我们的用量 | 占比 |
|---|---|---|---|---|
| 定时计算 | GitHub Actions | 2000 min/月 | ~150 min | **7.5%** |
| 对象存储 | Cloudflare R2 | 10 GB + 出网免费 | ~1 GB/月 | 10% |
| 数据库 | Neon Postgres | 0.5 GB，100 项目 | ~1 MB/月 | 0.2% |
| AI 改写 | Gemini 2.5 Flash | 1500 次/天 | 70 次/天 | **4.7%** |
| TTS | edge-tts | **无限制** | 70 段/天 | — |
| 新闻源 | 21 个公开 RSS | 无限制 | 70 次/天 | — |

**总成本 0 元，全部不绑卡。**

注册步骤见 → **[`注册指南.md`](注册指南.md)**（3 个账号，约 15 分钟）

---

## 二、为什么这样更好

| 问题 | 旧方案（Render 免费） | 新方案 |
|---|---|---|
| App 首请求延迟 | 30–60 秒（休眠唤醒） | **CDN 边缘，几十毫秒** |
| 数据持久性 | 本地盘，部署即丢 | R2 / Neon 持久 |
| 数据库过期 | 自带 Postgres 30 天 | Neon 免费永久 |
| 定时任务 | 独立容器，不共享内存 | GitHub Actions，无需共享 |
| 需否绑卡 | 需要 | **不需要** |
| 服务器成本 | 免费但有休眠 | 无服务器 |

---

## 三、三种消费模式

### 1. CDN 直读（推荐，App 端首选）

配好 R2 后，内容自动导出为 JSON：

```
https://<bucket>.r2.dev/data/index.json首页列表（en/ja 分组）
https://<bucket>.r2.dev/data/<date>/index.json   某天完整列表
https://<bucket>.r2.dev/data/versions/<id>.json  详情 + 逐词时间轴
https://<bucket>.r2.dev/audio/<article_id>/<level>.mp3
```

App 启动调一次 `/api/v1/config` 拿到这些模板，之后全部走 CDN。

### 2. REST 接口（本地开发 / 需要动态筛选）

14 个接口保持不变，见 [`API.md`](API.md)。本地 `python -m app.cli serve` 即可。

### 3. 兜底：手动跑

```bash
python -m app.cli daily --per-topic 2 --force
```

---

## 四、必须注意的三件事

### 1. 时间轴的坐标系

详情 JSON 里的 `cs/ce` 字符偏移是相对 **`text`**（段落换行已压成空格）计算的。

```json
{
  "text": "第一段内容 第二段内容",   // ← 高亮必须用它
  "body": "第一段内容\n\n第二段内容", // ← 不能用它做 slice
  "timeline": [{ "w": "第一段内容", "cs": 0, "ce": 5, "sm": 100, "em": 500 }]
}
```

直接用 `body` 会导致多段正文的高亮跳字。该契约已有测试锁死。

### 2. R2 的 10GB 会满

按当前用量（1GB/月）约 10 个月。超出后需要清理或升级。简单做法是加个保留策略：

```bash
# 只保留最近 30 天的音频
aws s3 rm s3://news-etl-audio/audio/ --recursive --exclude "*"  # 按需自行实现
```

或在流水线里加一步 `_cleanup_old_audio(days=30)`。

### 3. GitHub 的定时任务会延迟

GitHub 不保证准时，繁忙时段延迟 15–20 分钟很常见。已做两件事缓解：
- cron 用 `30 21 * * *` 而非整点，避开调度高峰
- 逻辑不依赖精确时间（每天只要跑成一次即可）

---

## 五、国内访问速度

三个服务商在国内都不是最优解：

| 服务 | 国内体验 |
|---|---|
| Cloudflare | 一般，部分时段不稳定 |
| Neon | 一般，无大陆节点 |
| GitHub Actions | 仅影响生成速度，不影响 App 读取 |

**影响面分析**：App 读的是 Cloudflare CDN 的静态 JSON 和音频，走的是 Cloudflare 全球节点。
如果你的用户主要在国内且对速度敏感，后续可考虑把 R2 的公开域名换成国内 CDN
（又拍云 / 七牛等），**代码无需改动** —— 因为 App 只认 `/api/v1/config` 返回的 `base_url`。

如果确定要国内极速访问，另一条路是付费国内 VPS（约 ¥10–24/月），
数据库和音频已在外部存储，迁移只需改 API 的读取源，不需要搬数据。

---

## 六、本地开发

```bash
cd news-etl
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env              # 留空即可运行（走离线降级 + 本地存储）

.venv/bin/python -m app.cli daily --per-topic 1
.venv/bin/python -m app.cli serve          # http://127.0.0.1:8000/docs
.venv/bin/python -m pytest tests/ -q
```

想模拟 R2，在 `.env` 里填上 R2 四项配置即可，代码零改动自动切换。

---

## 七、故障排查

| 现象 | 排查 |
|---|---|
| Actions 没跑 | 仓库 60 天无活动会被禁用；workflow 末尾有自动 commit 保持活跃 |
| `ModuleNotFoundError: botocore` | 忘了装 boto3（本地）或 requirements 未更新（Actions） |
| 音频 404 | R2 的 Access Key 没有写权限，或 Secret 复制时带空格 |
| `no such table` | Neon 上跑一次任意请求即可自动建表（`init_db()`） |
| JSON 导出为空 | 检查 `EXPORT_JSON` 是否为 false，或查看 Actions 日志中 `export` 字段 |
| 音频返回 302 | 正常 ——说明已走 CDN 跳转 |
| 高亮跳字 | App 端改用 `text` 字段而非 `body`（见第四节第1 条） |