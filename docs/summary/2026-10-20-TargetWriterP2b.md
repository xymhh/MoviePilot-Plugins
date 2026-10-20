# P2b：TargetWriter 统一目标写入

> 状态：已发布 1.7.20（2026-10-20，Release `CloudSubscribeX_v1.7.20`，CI 三绿）
> 分支：`feat/p2b-target-writer`（基线 `16de7ad`）
> 提交：`e79c83a` → `f12280b` → `1090711` → `3c0b50c` → `e544500`；合并 `e285537`；发版 `a5815b7`
> 测试：定向 51 passed；全量 677 passed / 2 例既有 p115client 环境失败；lint 639 文件 0 error

## 做了什么

1. 新增 `core/target_writer.py`：
   - `TargetPlan` / `TargetResult` / `TargetConflictError` / `TargetUnknownError` / `TargetWriter`。
   - 进程级目标锁（`provider|path|name`）；`commit` 内完成 `probe → write → verify`。
   - 目标已存在且匹配 → `reused`，不调用写入；同名但内容不匹配 → `conflict`，不覆盖、不自动加 `(1)`。
   - 写后精确名查不到时，允许在目标目录按大小 + 非空哈希找真实名（覆盖 115 自动改名）。
2. 中继路径 `_relay_transfer` 接入：
   - 删除 `_RELAY_TARGET_DONE` 的正确性依赖（缓存/辅助函数已移除）。
   - 目标已存在时直接复用，不提交中继；冲突/未知直接失败，不再回落本地重复上传。
3. 本地上传路径接入：
   - `_progressive_transfer` 整体包在 `TargetWriter.commit` 内，目标已存在时跳过下载/上传。
   - `CrossDriveTransfer().transfer` 包在 `TargetWriter.commit` 内，复用真实结果。
   - Range 直传 `upload_from_link` 前增加 `probe`，目标已存在则跳过直传。
4. 结果回填：
   - `result_file_id/name/size/sha1/md5` 使用 `TargetWriter` 返回的真实文件，不再假设目标名等于请求名。
   - 上传成功但精确名查不到时，`CrossDriveTransfer` / `_progressive_transfer` 返回“已上传但文件待核验”的结果，由 `TargetWriter` 找真实名，避免误判失败后重复上传。
5. `_target_plan` 在没有显式算法/校验和时回退使用源文件 `sha1`/`md5`，降低“仅大小相同”误复用概率。

## 关键行为变化

- **同一目标、同内容**：第二次任务复用已有文件，不重复中继/上传。
- **同一目标、不同内容**：返回冲突并失败，不再生成 `name (1).ext` 副本；需要人工处理同名冲突。
- **进程重启**：不再依赖 30 分钟内存缓存，仍以目标实际查询为准。
- **115 自动改名**：上传成功后可在目标目录找到 `name (2).ext` 并回填真实文件名。
- **中继失败**：仍保留原有本地回退；只有目标冲突/无法确认时才直接失败。

## 主要文件

- `plugins.v2/cloudsubscribex/core/target_writer.py`（新增）
- `plugins.v2/cloudsubscribex/core/transfer.py`（relay / progressive / local / Range 接线）
- `tests/test_target_writer.py`（新增）
- `tests/test_cross_transfer_relay.py`、`tests/test_same_source_relay_guard.py`、`tests/test_rapid_miss_abort.py`（适配与补充）
- `docs/plan/2026-10-20-P2b-TargetWriter任务书.md`

## 未做 / 已知边界

- **未做真实 115 e2e**：本机只有 fake provider 测试，未在线上目录验证真实删除/改名/并发。
- **Range 直传只做前置 probe**：直传成功后的落盘仍沿用原逻辑，未整体包进 `commit`。
- **账本 claim 仍在 `service._transfer_file`**：TargetWriter 只负责目标存在性/冲突/串行/写后核验，未接管 `sync_jobs` 状态机。
- **无哈希时仍可能仅按大小匹配**：已通过源文件 `sha1`/`md5` 回退降低概率；源文件也没有哈希时，同名同大小会被视为匹配。
- **`list_files_recursive` 是目录递归**：理论上可能匹配到子目录中同大小/同哈希文件；有哈希时风险很低。
- **1.7.20 已发布**；真实 115 e2e 仍未做。

## 下一步

1. 真实 115 小目录 e2e：
   - 目标已存在同内容 → 复用、无重复上传；
   - 同名不同内容 → 失败/待处理，不产生 `(1)`；
   - 删除目标文件后重跑 → 重新上传并回填；
   - 上传后自动改名 → `result_file_name` 为真实名。
2. P2c 可评估：把 Range 直传写后核验也纳入 `TargetWriter`，以及把账本 claim 与目标写入合并为单一 commit。
