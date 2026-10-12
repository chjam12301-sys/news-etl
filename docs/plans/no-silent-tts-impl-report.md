# Implementation Report

## 已完成的修改清单项
- [x] synthesize 明确拒绝空正文、关闭、上游错误、空音频，删除静音制造路径。
- [x] audio_store 共享判定；pipeline 已有静音计错且不重生成。
- [x] exporter/content_hash/schema/API 隔离旧静音音频与时间轴。
- [x] 合成、daily 阻断、静态导出、API 出口、backfill 聚焦离线检查。

## SPEC 对应关系
- Change ID: N/A
- 完成的 todo 编号: N/A
- 未完成/需回到文档层的 todo 编号: N/A

## 文档未覆盖的实现差异
- 无。主会话明确补充：/files 已知静音 file_path 同样过滤；非音频文件维持原行为。

## 改动文件清单
- app/tts.py — 删除静音占位生成，失败抛错。
- app/audio_store.py — 共用可播放判定。
- app/pipeline.py — 旧静音增加 audio_errors。
- app/exporter.py — 静音导出空音频和时间轴，重新计算无音频 hash。
- app/content_hash.py — 排除静音资产。
- app/schemas.py — 静音输出空音频。
- app/api.py — 音频、时间轴、字幕和已知文件入口拒绝静音。
- tests/test_core.py — 确定性合成流和失败场景，取消真实 TTS 请求。
- tests/test_daily_publish.py — 新失败和旧静音阻断发布，保留成功音频和正文。
- tests/test_silent_audio.py — 输出隔离与 backfill 离线回归。

## 我跑过的命令与结果
- `.venv/bin/python -m pytest tests/test_core.py tests/test_daily_publish.py tests/test_silent_audio.py tests/test_content_hash.py -q` → 77 passed，2 条既有 FastAPI deprecation warning。
- `.venv/bin/python -m pytest tests/test_audio_store.py -q` → 16 passed。
- `.venv/bin/python -m compileall -q app tests/test_silent_audio.py` → PASS。
- `git diff --check` → PASS。

## 建议后续处理（不在本次 plan 范围）
- 既有静音远程对象/已发布 JSON 未删除或重发。
- 禁用 TTS 时保留 pipeline skipped 策略。

## 独立复验返修
- `app/api.py`：本地文件过滤与读取共用 `LocalStorage._p`；请求 key 和旧数据库 key 均按实际存储映射比对。
- `tests/test_silent_audio.py`：补充数据库 key 别名、正常音频和 JSON 路径兼容检查。
- `.venv/bin/python -m pytest tests/test_silent_audio.py tests/verify/test_silent_boundary.py tests/test_audio_store.py -q` → 28 passed；`git diff --check` → PASS。
