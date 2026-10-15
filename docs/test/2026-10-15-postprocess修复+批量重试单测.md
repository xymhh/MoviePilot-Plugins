# 单测与验收证据：postprocess 循环修复 + 历史批量重试（1.6.46）

日期：2026-10-15
任务书：[../plan/2026-10-15-postprocess循环修复+历史批量重试.md](../plan/2026-10-15-postprocess循环修复+历史批量重试.md)
复盘：[../summary/2026-10-15-postprocess循环与批量重试.md](../summary/2026-10-15-postprocess循环与批量重试.md)

## 1. 基线与总量

| 项目 | 结果 |
| --- | --- |
| 基线（1.6.45 提交后全量） | 370 passed, 2 failed（2 例为 `test_checkin_days.py` 既有 p115client 环境失败，允许保持） |
| 本次全量 | **382 passed, 2 failed**（+12：A 部分 7 例 + B 部分 5 例） |
| lint | `python scripts/lint.py --all` → **601 file(s), 0 error(s)**, 307 warning（新 warning 均为续行缩进风格） |
| 编译 | `python -m compileall -q` 通过 |

## 2. A 部分：postprocess 死循环（tests/test_sync_postprocess.py，新增 7 例，共 21 例全绿）

| 用例 | 锁定行为 |
| --- | --- |
| `TestOfflineHistoryFallbackMatching::test_no_finalize_key_matches_by_file_name_and_cloud_dir` | 历史记录无 `finalize_key` 但 `(file_name, cloud_dir)` 全等 → `_mark_offline_history_status_batch` 命中并写终态 |
| `…::test_same_file_name_different_cloud_dir_not_matched` | 仅文件名相同、`cloud_dir` 不同 → 禁止模糊匹配，不写终态 |
| `…::test_finalize_key_match_path_unchanged` | 正常路径（key 匹配）行为不变（含 failure_reason 写入） |
| `…::test_success_overwrite_failure_protection_unchanged` | 兜底命中也走「成功不被失败覆盖」保护 |
| `TestHistoryResyncTermination::test_resync_count_persists_between_calls` | `history_resync_count` 写回 `_OFFLINE_PENDING_KEY` 数据 |
| `…::test_resync_at_limit_settles_and_removes_pending` | 达上限 → 返回放行 + pending 项从持久化移除；保留一条 WARNING、无 ERROR |
| `…::test_grace_and_resync_warnings_fire_once` | 宽限放行 WARNING 与尚未落盘 WARNING 各只 1 次，后续降 debug |

## 3. B 部分：批量重试（新增 tests/test_history_retry_batch.py，5 例全绿）

| 用例 | 锁定行为 |
| --- | --- |
| `test_mixed_batch_counts` | 可重试 / 不可重试（成功且非修复）/ 不存在 / 已在飞 → submitted=2、skipped=3，原因逐条正确 |
| `test_more_than_limit_raises` | 21 条 → `ValueError`（含「20」），不静默截断 |
| `test_rate_limit_keeps_inflight_at_most_two` | 4 条串行提交，观测在飞峰值 ≤ 2；条间 sleep ≥ 3 次 |
| `test_single_retry_behavior_unchanged` | 单条：入队置「处理中」、后台执行一次、在飞集合 finally 清理、处理中禁止重复提交 |
| `test_single_retry_validation_unchanged` | 单条校验不变：记录不存在 / 不可重试 / 仍在飞均报错 |

## 4. 变异反证（主 agent 亲手执行后恢复，恢复后 26/26 全绿）

| 变异 | 对应用例 | 结果 |
| --- | --- | --- |
| 兜底匹配条件改 `if False:` | A① 两个兜底用例 | **必失败**（2 failed） |
| 上限判断 `if retries < limit:` 改 `if True:` | A② `test_resync_at_limit_settles_and_removes_pending` | **必失败** |
| 批量限流等待循环删除（改 `pass`） | B③ `test_rate_limit_keeps_inflight_at_most_two` | **必失败** |

## 5. 真机验收清单（未执行，待用户线上验证）

1. 观察 `plugins/cloudsubscribe.log`：`历史记录长时间未落盘，放行文件后处理` 与 `历史记录尚未落盘，保留后处理任务` 每文件最多各 1 条；不再出现 `历史记录长时间缺失，放弃写入终态` 的 ERROR 刷屏（至多偶发一条 WARNING）。
2. `/api/v1/history/transfer` 中对应记录终态由「处理中」翻转为成功/失败；`pending_offline_strm` 数据里不再残留对应 pending 项。
3. 历史页「重试全部失败」「重试选中」按钮：确认弹窗含条数；提交后 toast 显示 `已提交 X 条，跳过 Y 条`；列表经运行态刷新更新。
4. `POST /plugin/CloudSubscribe/history/retry_batch` 单次超过 20 条返回错误文案；历史页按钮会自动按 20 条分批，不触发该错误。

## 6. 主 agent 独立验收（不依赖委派方用例）

复跑时间：2026-10-15 23:5x；脚本 `/tmp/verify_1646.py`（自写，复用仓库测试 harness，直接驱动真实 `PostprocessService._resolve_history_record` / `HistoryService._mark_offline_history_status_batch`）。

### 6.1 真实循环路径（从计数 0 开始，逐轮重读存储，模拟监控周期）

构造「pending 项存在但历史记录永远匹配不上」的最坏情形，连续 15 轮调用（每轮先 `_history_ready_for_finalize` 再 `_resolve_history_record`，并把 pending 写回存储）：

```
计数序列=[1,2,3,4,5,6,7,8,9,10,10] 终止于第 11 轮
宽限WARN=1  未落盘WARN=1  ERROR=0
```

| 断言 | 结果 |
| --- | --- |
| A1 循环会终止（pending 项最终被移除，≤12 轮） | PASS（第 11 轮） |
| A2 计数逐轮递增并持久化到存储 | PASS（前三个 = 1,2,3） |
| A3 宽限放行 WARNING 只打一次 | PASS（1 次） |
| A4 尚未落盘 WARNING 只打一次 | PASS（1 次） |
| A5 不再用 ERROR 刷屏 | PASS（0 次） |

> 委派方用例是把计数**直接种在上限**上验结算，本脚本补的是从 0 递增的真实路径——线上刷 33 次正是因为计数没能递增到上限。

### 6.2 匹配正确性与不误伤

| 断言 | 结果 |
| --- | --- |
| B1 缺 `finalize_key` 时按 `(file_name, cloud_dir)` 兜底命中 | PASS（matched=1） |
| B2 终态已写入历史记录 | PASS（处理中 → 成功） |
| C1 key 匹配正常路径未回归 | PASS |
| C2 key 命中后 `finalize_key` 被清理 | PASS |
| D1 同名不同 `cloud_dir` 不误匹配 | PASS（matched=0，状态保持处理中） |
| E1 成功记录不被「失败」覆盖 | PASS |

### 6.3 变异反证（我亲手注入并恢复）

| 变异 | 结果 |
| --- | --- |
| 兜底匹配 `if record_pair[0] and record_pair[1]:` → `if False:` | B1/B2 **必失败** ✓ |
| 上限判断 `if retries < limit:` → `if True:` | A1 **必失败**（计数跑到 15 仍不终止）✓ |
| 批量限流 `while True: … time.sleep` → `pass` | `test_rate_limit_keeps_inflight_at_most_two` **必失败** ✓ |

恢复后复跑：全量 **382 passed, 2 failed**（同基线），`tests/test_history_retry_batch.py` 5 passed。

### 6.4 前端独立核验与主 agent 修正

- 提取 `Page.vue` / `AppPage.vue` / `HistoryTable.vue` 的 `<script setup>` 块，`node --check`（Node v25.6.0）**三份全部通过**（委派方因 pnpm 版本不符未能本机构建）。
- **发现并修正委派方一处误改**：`Page.vue` 单条重试弹窗被加上 `:error-text="retryError"`（`retryError` 全仓未定义，仅存在于 `OfflineTasksDialog.vue` 的 catch 变量）且 `persistent` 被删除 → 已恢复为原状（`persistent` 保留、去掉未定义属性）。
- **补一处健壮性**：前端批量提交按 20 条分批串行（`RETRY_BATCH_CHUNK_SIZE`），避免失败记录超过 20 条时整批被后端拒绝（接口契约仍保持「超 20 报错不截断」）。

## 7. 残留风险

1. 兜底匹配依赖 pending 项与历史记录两侧都有非空 `file_name` 与 `cloud_dir`；任一侧缺失时仍走 key 匹配与上限结算兜底（不会无限循环，但终态可能写不进历史）。
2. 批量重试的限流在飞计数基于模块级 `_retry_inflight_keys`（与单条共用）；与用户手动单条重试并发时共享同一护栏，语义一致。
3. 前端按钮统计基于当前页筛选结果（服务端分页），与「删除所选」同口径；未做全库扫描。
4. 前端构建未在本机验证（pnpm/node 版本不符），`dist/` 由 CI 构建；字段/事件均为既有模式。
