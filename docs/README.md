# 文档索引（本 fork 自有文档）

- `plan/` 计划（面向未来，完成后保留并标注状态）
  - [2026-10-11-p115strmhelper-跳过而非失败.md](plan/2026-10-11-p115strmhelper-跳过而非失败.md) — 方案与用户决策
  - [2026-10-11-p115strmhelper-跳过而非失败-实施任务书.md](plan/2026-10-11-p115strmhelper-跳过而非失败-实施任务书.md) — 行号级实施任务书（委派外部 runner 用）
  - [2026-10-11-待生效CI工作流-test-cloudsubscribe.md](plan/2026-10-11-待生效CI工作流-test-cloudsubscribe.md) — 已有状态：已完成（workflow scope 补齐后已推送）
- `test/` 验收证据
  - [2026-10-11-p115strmhelper-跳过语义-单测与静态检查.md](test/2026-10-11-p115strmhelper-跳过语义-单测与静态检查.md) — 单测/lint/CI/Release 证据 + 真机核对清单
  - [2026-10-11-CI工作流验证.md](test/2026-10-11-CI工作流验证.md) — 单测/发布工作流各轮 run id、130 tests OK、pnpm 前端 Release 资产核对
- `summary/` 结论与复盘
  - [2026-10-11-p115strmhelper-跳过语义-总结.md](summary/2026-10-11-p115strmhelper-跳过语义-总结.md) — 问题、决策、验证、教训
  - [2026-10-11-单仓库插件市场合并-总结.md](summary/2026-10-11-单仓库插件市场合并-总结.md) — 为什么两个仓库、怎么合成一个地址
  - [2026-10-11-合并仓库CI三件事.md](summary/2026-10-11-合并仓库CI三件事.md) — workflow scope、pnpm 前端分流、测试桩滞后的定性与教训

命名与归类规则：`YYYY-MM-DD-<短名>.md`；没做的 → `plan`，做过的证据 → `test`，下结论 → `summary`；同一主题跨天追加进同一篇。
