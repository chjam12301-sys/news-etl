# 每日发行与音色预热

日更定时保持北京时间 05:30（UTC 前一天 21:30），所有主题合计一篇。发行日期使用北京时间自然日，源新闻时间仍存 `published_at`。

同日重跑复用按 `fetched_at,id` 排序的第一篇；失败留下的文章继续补缺失等级和默认音频。数据库事务锁防止 API、CLI 与 Actions 并发创建第二篇。`--force` 只处理当日目标缺少译文的等级；daily 不支持清空历史。

可选音色（默认音色之外的 4 个 Kokoro 音色：`af_bella` / `am_michael` / `bf_emma` / `bm_george`）由**本流水线用 Kokoro 自托管生成**：为当日目标的五个英语等级各生成一份，共 20 份。音频落对象存储 `audio/<id>/<level>_<voice>.<ext>`，**词级时间轴单独存 `timeline/<id>/<level>_<voice>.json`**（与音频同桶、URL 永久稳定、**不内联进详情 JSON**——内联会让最大等级的详情膨胀 3~4 倍，而用户通常并不切音色）。元数据写入 `alt_voice_assets`，随详情 JSON 的 `audio.alternates` 一起发布。日语与默认音色不受影响。

生成**排在四阶段发布之后**：音色是增强项，绝不能拖累当天内容发布（20 份 × 3 次重试最坏会把 job 撑到超时被杀）。补完后再发一次——音频在对象存储不进 Git，只需重发 JSON；**没有新增时不重复发布**。因此新文章的首发详情里暂时没有 `alternates`，补发后才有。

单份**重试 3 次**（指数退避）、已存在的**跳过**（可续跑）、逐份提交；**上传前按字节嗅探**（`sniff_audio`），识别不出就判失败——绝不把 WAV 字节以 `.mp3`/`audio/mpeg` 上传。音色变了会**重算该版本的 `content_hash`**，App 才能感知到新增/下架。可选音色的可达性在发布校验里**只告警不阻断**（`summary` 会带 `⚠️ 可选音色告警`）。

音色清单由 env `ALT_VOICES` 覆盖（逗号分隔，读 `.env`）。**改动清单后重跑会下架多出来的音色**：删除对应 DB 行与 R2 对象，不会继续对外宣称。清单里的音色名会做格式校验，非法则整体跳过（非 kokoro 引擎、缺默认音频、`--no-tts` 也都会跳过）。

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
