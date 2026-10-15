# 复盘：postprocess 循环修复 + 历史批量重试（1.6.46）

日期：2026-10-15
任务书：[../plan/2026-10-15-postprocess循环修复+历史批量重试.md](../plan/2026-10-15-postprocess循环修复+历史批量重试.md)
证据：[../test/2026-10-15-postprocess修复+批量重试单测.md](../test/2026-10-15-postprocess修复+批量重试单测.md)

## 起因

用户线上（MP v3 + 1.6.45）日志两类问题，10-14 起就存在（早于 1.6.45，非新功能引入）：

1. `postprocess.py` 刷屏：宽限放行 WARNING 约 1730 条/天、「尚未落盘」WARNING 约 870 条/天，约 65 个文件、单文件最高 33 次——插件自身历史终态写不进去、pending 项反复重排队（MP 侧整理本身是成功的，非数据丢失）。
2. 历史失败记录只能逐条重试，失败积压时操作成本高。

## 改动边界

- A（治本）：`handlers/sync/history.py` 批量匹配新增 `(file_name, cloud_dir)` 全等兜底（仅当两侧字段都非空，禁止模糊/包含）；`handlers/sync/postprocess.py` 新增 `_persist_history_resync_progress`（计数写回 pending 数据）与 `_settle_offline_pending_item`（达上限强制移除 pending，找不到历史记录也移除），上限路径由 ERROR 降为一条 WARNING。
- A（降噪）：宽限放行 / 尚未落盘两条 WARNING 各按 pending 项标记位只打一次，后续 `logger.debug`。
- B：`handlers/sync/retry.py` 新增 `submit_history_retry_batch`（分类同步、提交复用 `submit_history_retry`，后台线程串行 + 每条 sleep 1.0s + 整批在飞 ≤ 2，单次上限 20）；`core/api/registration.py` + `runtime.py` 新增 `POST /history/retry_batch`；前端 `HistoryTable.vue`/`Page.vue`/`AppPage.vue` 新增「重试全部失败」「重试选中」+ 确认弹窗 + toast 摘要；前端按 20 条自动分批串行提交（主 agent 补），避免失败积压超过 20 条时整批被拒。
- 不动：单条 `/history/retry`、`can_retry`/`retry_title`、筛选分页、1.6.45 fallback/abort 语义、Gopeed/跨盘转存。

## 教训

1. **「放行」不等于「结算」**：原逻辑到上限只 `return True` 放行给提交路径，依赖调用方 pop；一旦字段不匹配（monitor token/代际）pending 永不移除，`history_resync_count` 也永远到不了上限。终止保证必须自己写进存储层（写计数 + 到上限 pop），不能寄希望于调用链的其它副作用。
2. **不可能命中的分支是隐性死循环温床**：`pending_key.upper() in share_url` 对 `pansou:TOKEN:id` 形态永远为假，匹配只剩一条腿。修匹配之外必须同时修「匹配不上时怎么办」——终止 + 降噪两者缺一就会持续刷屏。
3. **每轮刷屏的 WARNING 要挂在持久标记位上，不是调用计数**：宽限放行发生在 `_due_pending_keys` 的筛选阶段，比 `_resolve_history_record` 更早、次数更多；标记位随 reconcile 写回存储，重启后也不会复发。
4. **批量接口的「立即返回」与「响应含计数」并不矛盾**：分类（查记录、判可重试、查在飞）是纯内存操作可同步完成并计入响应；真正耗时且需要限流的是提交动作，放后台线程。测试上分别锁「分类计数」与「在飞峰值 ≤ 2」两个断言面。
5. **任务书里写的文件不一定归属本插件**：package.v3.json 实为 P115StrmHelper 清单，按仓库既有约定（1.6.44/1.6.45）只升 `package.v2.json` + `__init__.py`，并在任务书状态里注明偏差，而不是机械照做。
6. **委派方会顺手改无关属性，验收必须逐行读 diff**：本次委派方在 `Page.vue` 单条重试弹窗上加了 `:error-text="retryError"`（该变量全仓不存在）并删掉了 `persistent`，属于越界改动 + 未定义引用。逐行读 diff 才发现，已恢复原状。
7. **「本机构建不了」不等于「没法验证前端」**：pnpm/node 版本不符确实装不上依赖，但提取 `<script setup>` 块用 `node --check` 就能挡住语法级错误，成本几乎为零。

## 核验

- 全量 `pytest tests/ -q`：**382 passed, 2 failed**（2 例为既有 p115client 环境失败；基线 370 → 382，+12 = A7 + B5）。
- `python scripts/lint.py --all`：**601 文件 0 error**。
- 变异反证：兜底匹配恒假 → A① 失败；上限判断恒假 → A② 失败；去掉限流 → B③ 失败；恢复后 26/26 全绿。
- **主 agent 独立复验**（脚本 `/tmp/verify_1646.py`，不依赖委派方用例）：从计数 0 递增的真实循环路径 11 轮终止、两条 WARNING 各 1 次、ERROR 0 次；兜底命中/不误匹配/成功保护 6 项断言全 PASS；三处变异亲手注入复现失败后恢复；前端三份 `<script setup>` `node --check` 通过。
- 版本：`__init__.py` `plugin_version = "1.6.46"`；`package.v2.json` version + history.v1.6.46 条目。
