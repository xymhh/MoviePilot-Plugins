# 任务书：postprocess 循环修复 + 历史失败记录批量重试（CloudSubscribe 1.6.46）

> 状态：已完成 2026-10-15（证据：[../test/2026-10-15-postprocess修复+批量重试单测.md](../test/2026-10-15-postprocess修复+批量重试单测.md)，复盘：[../summary/2026-10-15-postprocess循环与批量重试.md](../summary/2026-10-15-postprocess循环与批量重试.md)）
> 背景：用户线上（MP v3 + 本插件 1.6.45）出现两类问题，要求一次解决。
> 注：§3.5 提到的 package.v3.json 实为另一插件（P115StrmHelper）的清单，按仓库既有约定未改；
> 版本升级落在 package.v2.json + __init__.py（与 1.6.44/1.6.45 一致）。

## 0. 线上证据（主 agent 从用户 MP 实例读到，非推测）

用户 MP：`https://mp.xymhh.eu.org`，插件 `CloudSubscribe` 版本 1.6.45，绑定源 `github:xymhh/moviepilot-plugins`。
日志文件 `plugins/cloudsubscribe.log`（2026-10-15 当天统计）：

- `postprocess.py` 刷屏：`历史记录长时间未落盘，放行文件后处理：<文件名>` 约 1730 条/天、`历史记录尚未落盘，保留后处理任务稍后重写终态（成功）：<文件名>` 约 870 条/天、`历史记录长时间缺失，放弃写入终态（成功）：<文件名>` 约 40 条/天。
- 涉及约 **65 个文件**，单个文件名最多出现 **33 次**，仍在持续（23:29 仍在刷）。
- 该刷屏 **2026-10-14 09:32 就开始**（208 条），**早于 1.6.45**，不是本次新增功能引入的。
- 同时 MP 侧整理**是成功的**（`/api/v1/history/transfer` 这些文件 `status=True`），所以影响是「日志刷屏 + 插件自身历史记录终态写不进去 + 反复重排队」，不是数据丢失。

## 1. 需求 A：修 postprocess 死循环

### 1.1 现状链路（已核实，行号为当前 main `ba50846` 状态）

1. 后处理任务入队时带 `pending_key`（`handlers/sync/service.py:1646`：`f"{prefix}:{token}:{subscribe_id}"`），并写进历史记录字段 `finalize_key`（`handlers/sync/television.py:677`、`movie.py:660`、`upgrade.py:606`、`retry.py:397`）。
2. `PostprocessService._history_ready_for_finalize`（`handlers/sync/postprocess.py:293-304`）：`item["history_ready"]`/`skip_history` 为真才放行；否则宽限 600s，超时后 **每次都** 打 WARNING「历史记录长时间未落盘，放行文件后处理」并放行。
3. `_resolve_history_record`（`postprocess.py:823-850`）：调 `_mark_offline_history_status`，命中则完成；未命中时 `history_resync_count` 累加、`next_check_at=now+60`、打 WARNING「历史记录尚未落盘，保留后处理任务稍后重写终态」；超过 `_HISTORY_RESYNC_LIMIT`（10）才打 ERROR「历史记录长时间缺失，放弃写入终态」并放行。
4. 匹配逻辑 `_mark_offline_history_status_batch`（`handlers/sync/history.py:1083-1130`）：只认两种匹配——`record["finalize_key"] == pending_key`，或 `pending_key.upper() in record["share_url"].upper()`。**`pending_key` 形如 `pansou:ABCDEF:123`，永远不可能是 share_url 的子串**，所以实际只有 `finalize_key` 一条路。
5. 结论：**历史记录缺 `finalize_key`（或值不同）→ 永远匹配不上 → 循环**。单个文件名 33 次说明 `history_resync_count` 上限没起到终止作用（要么计数没落盘、要么 pending 项被反复重建），必须查清并修掉。

### 1.2 必须做的修改

1. **匹配兜底（治本）**：`_mark_offline_history_status_batch` 在 key/share_url 匹配失败时，增加「文件级」兜底匹配——用 `(file_name, cloud_dir)` 精确相等匹配（两侧字段都存在：pending 项有 `file_name`/`cloud_dir`，历史记录也有 `file_name`/`cloud_dir`）。只做精确相等，**禁止模糊/包含匹配**（避免误改他人记录）。
2. **终止保证（治本）**：确认并修复 `history_resync_count` 的持久化——计数必须写回 `_OFFLINE_PENDING_KEY` 数据；达到上限后必须真正「结算」该项：从 pending 移除、更新前端任务态、历史记录按兜底匹配写终态；**如果确实找不到任何历史记录，也必须移除 pending 项**（不能无限重排队）。
3. **日志降噪**：同一 pending 项的两条 WARNING（宽限放行 / 尚未落盘）**各自最多打一次**（用 item 上的标记位，如 `history_grace_warned`、`history_resync_warned`），后续降为 `logger.debug`。达到上限那次保留一条 WARNING（不要 ERROR 级刷屏）。
4. **不改语义**：正常路径（能匹配上）行为、`skip_history`、`成功` 覆盖 `失败` 的保护（`history.py:1113`）、平台历史镜像逻辑，一律不变。

## 2. 需求 B：历史记录「失败」批量重试

### 2.1 现状（已核实）

- 单条重试已存在：前端 `frontend/cloudsubscribe/src/components/Page.vue:518-535` → `api.post("plugin/CloudSubscribe/history/retry", {time, share_url, file_name})`；`AppPage.vue` 同理。
- 后端路由 `core/api/registration.py:398` `/history/retry` → `api_vue_retry_history`（`core/api/runtime.py:142`）→ `submit_history_retry`（`handlers/sync/retry.py:52`）。
- `submit_history_retry` 已有：记录查找（time+share_url+file_name 三元组）、`_history_retry_state` 可重试判定、`_retry_inflight_keys` 去重（模块级 set + lock）、标记为「下载中/处理中」、起 daemon 线程跑 `retry_history_record`。
- 可重试判定 `_history_retry_state`（`handlers/sync/history.py:572-595`）：`失败` 可重试；`成功`+跨盘 不可重试；`成功` 且 `source_file_name == file_name` 可「修复」。
- 前端 `HistoryTable.vue` 已支持多选（`@selection-change`）与批量删除（`@delete-groups`）——批量重试照这个模式做。

### 2.2 必须做的修改

1. **后端新路由**：`POST /history/retry_batch`，注册进 `core/api/registration.py`（照 `/history/delete_batch` 的写法，`auth: "bear"`，summary「批量重试失败转存记录」）。
2. **后端实现**：新增 `submit_history_retry_batch(records)`（放 `handlers/sync/retry.py`，复用 `submit_history_retry` 的既有校验/去重/入队逻辑，**不要复制粘贴一份**）。
   - 入参：`{"records": [{"time": "...", "share_url": "...", "file_name": "..."}, ...]}`，**单次最多 20 条**（超出直接返回错误，不静默截断）。
   - 逐条：找不到记录 → 计入 `skipped`（原因「记录不存在」）；`_history_retry_state` 不可重试 → `skipped`（原因用其 `retry_title`）；已在重试中（`_retry_inflight_keys`）→ `skipped`（原因「正在重试中」）；否则提交并计入 `submitted`。
   - **并发护栏**：批量提交必须限流——串行提交 + 每两条之间 sleep（建议 1.0s，可用模块常量），并保证整批总在飞数量不超过 2；**禁止**一次起 20 个 daemon 线程。
   - 返回：`{"submitted": N, "skipped": M, "details": [{"file_name": "...", "ok": bool, "message": "..."}]}`。
   - 批量提交本身放到一个后台线程里执行（接口立即返回，语义与单条一致：先返回、后台跑）。
3. **前端**（`HistoryTable.vue` + `Page.vue` + `AppPage.vue`，v2/v3 两个入口都要有）：
   - 历史页工具栏新增按钮「**重试全部失败**」：统计当前筛选结果中 `status == "失败"` 且 `can_retry == true` 的记录数，为 0 时禁用并提示「没有可重试的失败记录」。
   - 若已有勾选记录，按钮旁提供「**重试选中**」（勾选项里 `can_retry` 为真的部分）。
   - 点击后弹确认框（复用现有确认弹窗组件与风格），文案含条数，例如「将重试 12 条失败记录，期间会产生新的搜索与转存任务，是否继续？」。
   - 提交后 toast 展示后端返回的 `submitted/skipped` 摘要；列表状态依赖现有运行态刷新机制自动更新，不新增轮询。
4. **不改语义**：单条 `/history/retry` 行为、`can_retry`/`retry_title` 计算、历史筛选与分页，一律不变。

## 3. 验收标准（主 agent 会独立复跑，不看汇报）

1. 单测：
   - A 部分：`tests/test_sync_postprocess.py` 新增用例——① 历史记录无 `finalize_key` 但 `(file_name, cloud_dir)` 相同 → 能写终态；② `history_resync_count` 达上限后 pending 项被移除、不再重复处理；③ 两条 WARNING 各只出现一次；④ 正常路径（key 匹配）行为不变。
   - B 部分：新增 `tests/test_history_retry_batch.py`——① 混合批次（可重试 / 不可重试 / 不存在 / 在飞）计数正确；② 超 20 条报错；③ 提交限流生效（断言不会同时提交超过 2 条）；④ 单条 `submit_history_retry` 行为不变。
2. 全量 `pytest tests/ -q` 通过（当前基线：370 passed，另有 2 例既有 p115client 环境失败，允许保持）。
3. 变异测试（主 agent 会做）：把兜底匹配改成恒假、把上限判断改成恒假、把限流去掉，对应用例必须失败。
4. lint（仓库既有 lint 脚本）0 error。
5. 版本号：`package.v2.json` / `package.v3.json` / 插件内版本声明统一升到 **1.6.46**（照 1.6.45 的改法）。
6. docs：`docs/test/2026-10-15-postprocess修复+批量重试单测.md`、`docs/summary/2026-10-15-postprocess循环与批量重试.md`，并在 `README.md` 文档索引补链。

## 4. 禁止事项

- **不要 push、不要动 remote、不要打 tag**（发布由主 agent 做）。
- 不要改动 `fallback`/`abort`（1.6.45）相关语义、不要动 Gopeed/跨盘转存既有逻辑。
- 不要顺手重构无关代码、不要改测试基线数字、不要删既有用例。
- 不要碰前端构建产物（dist 由 CI 生成）。
- 不确定的地方先在回报里写清，不要猜着改。

## 5. 交付回报格式

改了哪些文件（路径 + 一句话）、新增/修改用例数、`pytest` 具体输出数字、A/B 两部分各自「为什么这样修」「怎么证明修好了」、遗留问题。
