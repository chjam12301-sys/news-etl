# 每日发行与音色预热

日更定时保持北京时间 05:30（UTC 前一天 21:30），所有主题合计一篇。发行日期使用北京时间自然日，源新闻时间仍存 `published_at`。

同日重跑复用按 `fetched_at,id` 排序的第一篇；失败留下的文章继续补缺失等级和默认音频。数据库事务锁防止 API、CLI 与 Actions 并发创建第二篇。`--force` 只处理当日目标缺少译文的等级；daily 不支持清空历史。

可选音色（默认音色之外的 4 个 Kokoro 音色：`af_bella` / `am_michael` / `bf_emma` / `bm_george`）由**本流水线用 Kokoro 自托管生成**：为当日目标的五个英语等级各生成一份，共 20 份，音频落 R2（`audio/<id>/<level>_<voice>.mp3`），元数据写入 `alt_voice_assets` 表，随详情 JSON 的 `audio.alternates` 一起发布。日语与默认音色不受影响。

生成**在 export 之前**执行，best-effort：每份独立**重试 3 次**（指数退避）、已存在的**跳过**（可续跑）、单份失败只记录，**绝不回滚已发布内容**。音色清单由 env `ALT_VOICES` 覆盖（逗号分隔）。

> 历史：该链路原走外部 Cloudflare Worker（`day-voice-api.innertalkdiary.workers.dev`，微软 edge-tts 音色 Ava/Emma/Sonia/Guy）。该 Worker 现返回 `503 generation_unavailable`（微软端点对数据中心 IP 封 403），已废弃，改为 Kokoro 自托管。

## 安全重跑

- 常规日更：`python -m app.cli daily`。
- 只为今日现有目标补音色：`python -m app.cli daily --warmup-only`，不会抓新闻、改写、生成默认音频或重新发布。它通过 Worker manifest 核验已发布版本身份。
- Actions `daily.yml` 的 `warmup_only=true` 对应仅预热；默认 false。
- 没有当天文章时仅预热返回失败，不退回昨天、不触发抓新稿。

## 2026-10-12 日期修复

先执行只读预览，再用完全相同的参数写入。按北京时间抓取日期匹配，ID 集合必须恰好是指定集合，才允许事务更新发行日期；不会更新文章版本或音频。

```sh
python scripts/republish.py --dry --repair-fetched-day 2026-10-12 --release-date 2026-10-12 --expected-ids 58,59,60,61,62,63,64
python scripts/republish.py --repair-fetched-day 2026-10-12 --release-date 2026-10-12 --expected-ids 58,59,60,61,62,63,64
```

对应 `republish.yml` inputs 为 `dry`、`repair_fetched_day`、`release_date`、`expected_ids`。dry 不初始化数据库、不写日期、不发布、不预热。执行版修复后走原四阶段发布；若发布失败，重跑相同参数仍安全。今天保留七篇历史产物，只为排序最早的一篇预热，其余由 App 按需生成。

## 发布指针的 CDN 可见性

指针推送成功后，自动对 jsDelivr 的 `content/latest.json` 与 `content/data/latest.json` 两个可变路径清缓存。每个请求最多等待 10 秒，不跟随重定向；只有响应 `finished`、未限流且各 provider 全部成功，才记录 purge 完成。失败会明确输出“指针已发布但 CDN 可见性未确认”，返回 `retry_needed`，不会撤销已发布内容。重新发布可安全重试这两个清缓存请求。仅清可变指针，不清固定 SHA 详情/索引/音频。

参考：[jsDelivr 官方清缓存说明](https://www.jsdelivr.com/tools/purge)。清缓存操作完成不等于所有网络节点立刻可见，发布联调应再读取 CDN 指针确认目标 ref。

## 按日目录与详情的固定版本

发布时先提交详情得到 `content_rev`，再只重写按日目录，使其中 `detail_url` 指向 `content_rev`，提交得到 `catalog_rev`。根索引的 `base_url` 指向 `catalog_rev`。这样 App 从根索引进入日期归档，再进入详情时，每一层引用的文件都已经存在，且发行日期一致；不依赖重复发布来碰巧赶上正确 commit。

发布校验沿根索引列出的每个日期读取真实固定版本日目录，再核对详情的版本、文章、等级、语言、发行日期及 hash。旧详情虽返回 200，但日期不一致时仍阻断 latest 更新。详情 JSON 内自身 `detail_url` 不做自引用相等要求。
