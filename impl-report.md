# Implementation Report

## 已完成的修改清单项
- [x] 1. 跨主题候选全局排序、总量限制、正文过短继续下一个 — `app/fetcher.py`
- [x] 2. 北京时间发行日、当天目标幂等、缺失等级/音频恢复、发布后最终状态 — `app/pipeline.py`
- [x] 3. daily 固定一篇、拒绝 reset、force 限当天、仅预热入口 — `app/cli.py`, `.github/workflows/daily.yml`
- [x] 4. 五英语等级×四角色预热、manifest 身份核验、短退避、独立失败摘要 — `app/voice_warmup.py`
- [x] 5. 服务端 token 注入与部署说明 — `app/config.py`, `.github/workflows/daily.yml`, `docs/daily-publication.md`
- [x] 7. 精确 ID 日期修复与 dry-run — `scripts/republish.py`, `.github/workflows/republish.yml`
- [x] 8. ETL 聚焦非 UI 测试 — `tests/test_daily_publish.py`

## SPEC 对应关系
- Change ID: N/A
- 完成的 todo 编号: N/A
- 未完成/需回到文档层的 todo 编号: Worker 实现由另一 agent 负责；真实远端日期修复与预热由主会话执行。

## 文档未覆盖的实现差异
- 主会话明确追加：目标选定使用 PostgreSQL 事务级 advisory lock / SQLite BEGIN IMMEDIATE，并对当前目标处理使用行锁。建议回写计划关于 Actions concurrency 已足够的描述。
- 主会话明确追加：`daily --warmup-only` / Actions `warmup_only=true` 只补今天已有目标的 20 份可选音色，不重新生成或发布文章。建议回写 CLI 验收项。

## 改动文件清单
- `app/fetcher.py` — 全局总量模式。
- `app/pipeline.py` — 当天目标、恢复、最终发布状态、预热。
- `app/cli.py` — 移除 reset，加入仅预热。
- `app/config.py` — Worker URL 与服务端 token 设置。
- `app/voice_warmup.py` — 可信身份校验及 20 份音色投递。
- `.github/workflows/daily.yml` — 固定日更一篇与预热输入/secret。
- `.github/workflows/republish.yml` — 日期修复输入参数。
- `scripts/republish.py` — exact-match 日期修复与只读预览。
- `tests/test_daily_publish.py` — 14 个离线验证案例。
- `docs/daily-publication.md` — 配置、日更与日期修复操作说明。

## 我跑过的命令与结果
- `.venv/bin/python -m pytest tests/test_daily_publish.py tests/test_publish.py tests/test_topic_quality.py tests/test_content_hash.py -q` → 95 passed。
- `.venv/bin/python -m compileall -q app/fetcher.py app/pipeline.py app/cli.py app/config.py app/voice_warmup.py scripts/republish.py` → PASS。
- `git diff --check` → PASS。
- 未执行任何远端数据库写入、Actions dispatch、commit 或 push。

## 建议后续处理（不在本次 plan 范围）
- 无。
