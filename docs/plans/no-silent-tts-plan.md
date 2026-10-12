# Plan: TTS 失败时明确失败，禁止静音占位伪装成功

## 目标

Edge TTS 失败、关闭或返回空音频时，后台把它记录为音频失败，不再制造 `silent-fallback` 音频、估算时间轴或 `AudioAsset`。daily 沿用现有门槛停止发布并等待重跑；数据库中若已有旧静音占位，也不得继续从静态 JSON 或 API 暴露为可播放音频。

## 预期效果

### 用户或调用方体验

- TTS 正常时，文章音频、逐词时间轴和现有播放体验不变。
- TTS 失败时，本次 daily 不更新发布指针；已发布内容保持原样，用户不会收到一段“成功生成”的静音。
- 历史静音不删除、不补生成；API 或日后正常导出涉及该记录时，将其表现为无音频。

### 系统运行效果

- `synthesize()` 对空正文、TTS 关闭、上游异常、零字节音频均明确抛出失败。
- pipeline 只有在得到真实非空 TTS 结果后才上传对象、写 `AudioAsset` 和时间轴。
- 每个失败等级增加 `audio_errors`，文章 `tts_status` 为 `partial`；daily 沿用现有 `default audio incomplete` 门槛，不执行 export、publish 或 warmup。
- 所有对外音频消费者采用同一条规则：`engine == "silent-fallback"` 的旧记录等同无音频。
- 内容指纹按对外有效内容计算；旧静音记录不再参与导出的 `content_hash`。

### 必须保持不变

- 一天一篇、正文改写、配图、内容发布与可选音色 warmup 的既有流程不变。
- 真实音频的对象 key、URL、时间轴格式、hash 字段和发布校验不变。
- 不调用真实 TTS，不新增供应商，不迁移或删除数据，也不把历史静音记录加入批量补音队列。
- 不放宽 `run_daily()` 的音频完整性门槛；失败批次仍由后续重跑恢复缺失音频。

## 架构审视

### 当前架构判断

根因横跨生产端与消费端：`synthesize()` 把失败转成正常 `TTSResult`，pipeline 因而上传并写库；exporter、API 和 schema 又只按“有没有 `AudioAsset`”判断可播放。仅修生产端会让旧占位继续外露，因此需要“生产失败即失败”与“旧占位统一隔离”两层防线。

### 替代方向与取舍

- 只删除 `_synth_silent()`：能阻止新污染，不能保护已有记录。
- 立即清库和删除对象：能清理历史，但超出本任务且会引入不可逆线上操作。
- 采用共享的可播放判定，并在生产端抛错、对外端过滤：改动局部，可覆盖新旧数据；本次选择此方向。

### 复用已确认架构的证据 / 不适用理由

沿用现有 `AudioAsset | None` 契约表达“无音频”，无需新增状态字段或数据库迁移；现有 exporter、API response 与客户端均已支持 audio 为空。

## 技术模型

每日任务调用 `synthesize()` → 成功结果上传音频并落 `AudioAsset`；异常由单个等级捕获并累计 `audio_errors` → 正文、成功音频与 hash 先提交数据库 → `run_daily()` 发现错误后按现行规则失败，不进入 export、publish、warmup。直接 API、timeline、subtitle 和音频文件路由从同一数据库记录读取，因此旧静音记录必须在这些出口统一判为不可用。

历史补音脚本同样调用 `synthesize()`；改为抛错后它会沿用现有逐项捕获逻辑，不落库。其待处理集合仍只看 `audio is None`，所以旧静音记录不会因本修复自动进入批量重生成。

## 当前实现与预期效果的差距

- `app/tts.py` 在空文、TTS 关闭、Edge 异常时返回静音 WAV 和估算时间轴，调用方无法区分真正成功。
- `app/pipeline.py` 会上传该 WAV、写入 `AudioAsset` 并增加成功数；已有静音记录还会因 `audio is not None` 被当成完成项跳过，使 daily 错误地通过音频完整性检查。
- exporter、API、schema、timeline/SRT/文件路由均把任意 `AudioAsset` 当作可播放，旧 `silent-fallback` 会继续外露。
- `content_hash` 和 exporter 可能继续把旧静音记录算作内容，导致 `audio: null` 与 hash 语义不一致；仓内 JSON 未命中不代表生产数据库没有旧占位。

## 修改范围

1. **[修改]** `app/tts.py::synthesize` — 删除生产静音降级；空文、禁用、异常、空音频统一抛出可诊断异常，保留原始原因且不包含敏感配置。
2. **[修改]** `app/audio_store.py` — 增加共享的“可对外播放”判定，明确隔离 `silent-fallback`；不检查网络、不触发补生成。
3. **[修改]** `app/pipeline.py::_process_article` — 仅成功后上传落库；失败累计 `audio_errors`。遇到已有静音记录时不重生成、不删除，但必须计为错误并设 `tts_status=partial`，让现有 daily 门槛阻止发布。
4. **[修改]** `app/exporter.py::_audio_meta/_version_summary/_version_detail` — 旧静音资产输出 `has_audio=false`、`audio=null`、`timeline=[]`，并按无音频计算导出 hash。
5. **[修改]** `app/content_hash.py::from_version` — 共享规则下忽略旧静音资产，避免后续 rehash 再把它纳入内容身份。
6. **[修改]** `app/schemas.py::ArticleSummaryOut.from_row` 与 `app/api.py` 音频出口 — 摘要/详情返回 null；音频、timeline、subtitle 路由对旧静音记录返回未生成语义，不重定向或返回静音字节。
7. **[修改]** `tests/test_core.py`、`tests/test_daily_publish.py`、音频 API/exporter 相关测试 — 覆盖异常传播、无落库、daily 阻断与旧占位隔离。

## 明确不动

- `scripts/backfill_audio.py` — 现有逐项异常处理可承接新行为；不改变待处理集合，避免历史静音资产被自动批量补齐。
- `app/voice_warmup.py` 和 Cloudflare Worker — 多音色生成链路不是本次静音占位来源。
- `app/pipeline.py::run_daily` — 保留 `audio_errors` 后抛错以及不 export/publish/warmup 的现行策略。
- `app/db.py::AudioAsset` — 不加字段、不迁移、不删除旧记录。
- 线上数据库、R2/GitHub 内容与历史 JSON — 本次只改代码防线，不执行清理、补生成或重新发布。
- Day App 工作树 — 本任务仅处理 `news-etl` 后台。

## 验收

- [ ] `test_synthesize_*`：Edge 异常、空音频、空正文、TTS 禁用均抛错，且不再接受 `silent-fallback`。
- [ ] pipeline 聚焦测试：一部分合成失败时不上传、不写对应 `AudioAsset`，`audio_errors` 准确，真实成功项仍落库。
- [ ] daily 门槛测试：新失败或已有静音记录都会产生 `audio_errors`，任务失败且不 export、publish、warmup；数据库中已完成的正文和真实音频仍保留供重跑续接。
- [ ] exporter 测试：旧 `silent-fallback` 记录导出为 `audio=null`、`has_audio=false`、空 timeline，hash 等同无音频。
- [ ] API 测试：列表/详情不暴露旧静音；audio/timeline/subtitle 入口拒绝旧静音；真实音频响应保持不变。
- [ ] backfill 回归与普通导入检查：异常不落库、旧静音不进 todo；全程不发真实 TTS 请求、不操作线上数据。

## 风险与未确定项

- [NOTE] 已知旧静音对象即使被 API/导出隔离，若外部持有其原始公开 URL，仍可能直接访问；本任务不删除远程对象。
- [NOTE] 过滤历史记录后的新 JSON 只有在后续正常发布时才会覆盖 CDN 内容；本任务不主动重发历史文章。
