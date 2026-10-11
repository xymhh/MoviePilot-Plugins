# 文档索引（本 fork 自有文档）

> 2026-10-11 起：**P115StrmHelper 的 fork 线已撤下**（源码/索引/Release/CI 全部移除，改用上游 DDSRem 线 + 插件自带配置项）。
> 下列 `plan/` `test/` `summary/` 中与 P115StrmHelper 相关的 4 篇为**历史记录**，只用于回溯当时的决策与验证，不再对应当前仓库内容；原因见 [撤下 P115 分叉与仓库瘦身](summary/2026-10-11-撤下P115分叉与仓库瘦身.md)。

- `plan/` 计划（面向未来，完成后保留并标注状态）
  - [2026-10-11-p115strmhelper-跳过而非失败.md](plan/2026-10-11-p115strmhelper-跳过而非失败.md) — 方案与用户决策
  - [2026-10-11-p115strmhelper-跳过而非失败-实施任务书.md](plan/2026-10-11-p115strmhelper-跳过而非失败-实施任务书.md) — 行号级实施任务书（委派外部 runner 用）
  - [2026-10-11-待生效CI工作流-test-cloudsubscribe.md](plan/2026-10-11-待生效CI工作流-test-cloudsubscribe.md) — 已有状态：已完成（workflow scope 补齐后已推送）
- `test/` 验收证据
  - [2026-10-11-p115strmhelper-跳过语义-单测与静态检查.md](test/2026-10-11-p115strmhelper-跳过语义-单测与静态检查.md) — 单测/lint/CI/Release 证据 + 真机核对清单
  - [2026-10-11-CI工作流验证.md](test/2026-10-11-CI工作流验证.md) — 单测/发布工作流各轮 run id、130 tests OK、pnpm 前端 Release 资产核对
- `summary/` 结论与复盘
  - [2026-10-11-撤下P115分叉与仓库瘦身.md](summary/2026-10-11-撤下P115分叉与仓库瘦身.md) — 缩为单插件仓库；上游本来就有配置项（覆盖模式 / 自动删除低质量源文件），不必改代码
  - [2026-10-11-p115strmhelper-跳过语义-总结.md](summary/2026-10-11-p115strmhelper-跳过语义-总结.md) — 问题、决策、验证、教训（历史）
  - [2026-10-11-单仓库插件市场合并-总结.md](summary/2026-10-11-单仓库插件市场合并-总结.md) — 为什么两个仓库、怎么合成一个地址
  - [2026-10-11-合并仓库CI三件事.md](summary/2026-10-11-合并仓库CI三件事.md) — workflow scope、pnpm 前端分流、测试桩滞后的定性与教训

命名与归类规则：`YYYY-MM-DD-<短名>.md`；没做的 → `plan`，做过的证据 → `test`，下结论 → `summary`；同一主题跨天追加进同一篇。
