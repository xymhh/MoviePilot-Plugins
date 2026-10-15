# 跨盘中继下载接入 Gopeed（1.6.43）—— 结论与复盘

> 日期：2026-10-15　状态：**已发布**（Release `CloudSubscribe_v1.6.43`，CI 三绿）
> 任务书：[../plan/2026-10-15-CloudSubscribe接入Gopeed下载器.md](../plan/2026-10-15-CloudSubscribe接入Gopeed下载器.md)
> 验收证据：[../test/2026-10-15-Gopeed下载器单测.md](../test/2026-10-15-Gopeed下载器单测.md)

## 一句话

「夸克 → 本地中继」这一步现在可选用同机 Gopeed（REST + `X-Api-Token`）多连接下载；默认关（`builtin` 行为与 1.6.42 逐字节一致），Gopeed 侧任何异常自动回落内置，其余链路（转存、取链、上传 115、重命名、通知、历史、STRM）不变。

## 决策

| 决策 | 为什么 |
| --- | --- |
| 走插件自己的下载层（`drive/quark/files.py:74`），不动 MP 下载器抽象 | MP v3 下载器类型是硬枚举（Qbittorrent/Transmission/Rtorrent，`app/schemas/types.py:570`），插件注册不了新类型；胁持 `get_module()` 的 downloader family（14 个方法）代价大，且跨盘链路根本不经过 MP 下载器 |
| 共享目录，不用 HTTP 回拉 | Gopeed 与 MP 把同一宿主目录挂到同一容器路径 → 文件直接落在中继目录，无二次搬运；两边路径不同时用 `gopeed_path_prefix` 前缀映射 |
| 默认关 + 失败回落内置 | 外部依赖不进主链路的关键路径；不配置就没有任何行为变化 |
| 先只接夸克 | 用户的实际瓶颈在夸克（单连接限速 100~200KB/s）；架构通用，其它源盘按需一行接入 |

## 发布与核验（2026-10-15）

- commit `534dd3d`（本地 HEAD 与远端 `refs/heads/main` 同 hash）
- CI 三绿：Lint Python `38695903537`、Test cloudsubscribe (V2) `38695903633`、Auto Release `38695903693`
- Release `CloudSubscribe_v1.6.43`（published 2026-10-15T07:01:17Z），zip sha256 `9404920f773ba96b4a17f14d28b87498d66d290ac065b312e5166c0da2661f3b`，与发布资产 `.sha256` 逐字一致
- 包内读回：`plugin_version = "1.6.43"`、`core/gopeed.py` 与本地修复版 sha256 一致（`7181e85a…`）、前端产物 `cloudsubscribe/dist/assets/__federation_expose_Config-*.js` 含 `gopeed_token`

## 教训

1. **外部 API 的字段名必须拿源码/真实响应核对，不能信"按文档写的"**：委派首版三处全错 —— 探测路由写成 `/api/v1/version`（实际 `/api/v1/info`）、进度读 `progress.read` / `progress.total`（实际 `progress.downloaded`，总量在 `task.meta.res.size`）、终态漏了 `pause`（外部暂停会让轮询永久空转）。**三处都不会让单测失败**，因为假客户端是按首版的错误假设写的 —— 只有对上游源码才能发现。规矩：接口对接类任务，主 agent 必须自己核对契约，并要求测试的假客户端喂**真实字段名**。
2. **"删任务"与"删文件"必须分清**：`DELETE /api/v1/tasks/{id}` 不带 `force=true` 只删记录、保留文件（`pkg/rest/api.go:155-168`）。搞反的话下载完成即自删，表现为「下载成功但上传找不到文件」——最难查的那类。
3. **停止路径不能走回落**：停止时若触发回落，内置下载器会从头再下一遍（用户点了停止却还在下载）。所以 `InterruptedError` 单独放行、不进 fallback。
4. **无人值守的外部依赖要有卡死兜底**：Gopeed 任务长时间零进度会永久占住中继并发位（`cross_transfer_max_concurrent` 默认 2）→ 加 `stall_timeout`（默认 900s）。
5. **默认关 = 可回退**：`builtin` 分支逐字节走旧代码，回归面被限制在"用户主动打开"这一条路上。

## 后续

- 用户侧待办（唯一未验证的一环）：Gopeed compose 加共享挂载 + 插件设置填地址/Token → 真机速度对比
- 若提速不足：暴露任务级连接数 `opts.extra.connections`（HTTP 协议支持，当前用 Gopeed 默认值）
- 其它源盘接入：按需在各自 `files.py` 加同样的分流
