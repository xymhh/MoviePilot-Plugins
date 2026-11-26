# CloudSubscribeLite 源码目录（v3）

本目录是 `CloudSubscribeLite`（显示名「网盘订阅 Lite」）的
MoviePilot v3 后端源码及 Release 前端产物目录。面向用户的安装、配置和功能说明见
[网盘订阅助手使用说明](../../docs/cloudsubscribe.md)。

当前版本：3.0.0（`version.py` 的 `VERSION`，`__init__.py` 的 `plugin_version = VERSION`）。

## Lite 是什么

**Lite 是基于最新 CloudSubscribeX 的简化版**，不是独立实现。它与 X 共用同一套代码基（入口文件相似度 98.65%），只保留搜索渠道、跨盘转存、渠道签到与榜单订阅；整理与 STRM 校准交给 115 助手插件。

精简已做到**代码层**，不只是不注册：重复功能的 API 模块、服务与工具已从 Lite 源码中删除，不再随包发出（2026-11-26 精简）：

| 被精简掉的 | 具体表现（以当前代码为准） |
| --- | --- |
| API 路由 | 整理 / STRM / 重复扫描 / 回收站 / 漏斗 / 整理台的路由一律不注册，包括 `/strm`、`/duplicates`、`/recyclebin`、`/funnel`、`/organize`；对应 API 模块代码已删除（`core/api/{duplicate,funnel,media_library,recyclebin_restore,strm_duplicates,season_rules}.py`、`core/api/emby_proxy*.py`） |
| 定时服务 | 不注册任何 STRM / 整理 / 115 整理定时服务（无 `CloudSubscribe_StrmFullScan`、`CloudSubscribe_StrmIncrement`、`CloudSubscribe_Organize`） |
| 重复扫描与回收站 | 重复文件扫描、回收站恢复的后端与工具已删除（`handlers/sync/{strm_duplicates,recyclebin_restore}.py`、`utils/deep_delete_*.py`） |
| 媒体库同步 | 媒体库 Webhook 同步、深度删除、Emby 302 反代已删除（`core/api/media_library.py`、`core/api/emby_proxy*.py`）；转存完成后的媒体服务器刷新保留 |
| 侧边栏 | 当前代码只返回 1 个入口：网盘订阅（`main`）。代码注释里写的“两个入口（网盘订阅 / 网盘资源）”是旧描述，以代码为准 |
| 115 存储 | 不注册“115 网盘 CS 版”存储适配器（`U115CSStorage` / `u115cs`，X 独有） |
| 跨盘下载默认引擎 | 与 X 相反：Lite 默认用内置 aria2（多连接 + 断点续传），失败自动回落内置下载器；X 默认关闭内置 aria2 |

**保留、但与 X 不同的细节**：转存管线内部仍会为已转存文件生成 `.strm`（供播放），走 Lite 自己的 `/redirect` 端点；生成时用的 STRM 模板已改指 `CloudSubscribeLite`（精简前写死 `CloudSubscribeX`，只装 Lite 时播不了）。转存管线内部的漏斗预检（`funnel_precommit_recheck`）与整理服务（`OrganizeService`，转存后整理上传文件用）也保留，因为删掉会断转存流程——它们不是对外暴露的整理功能。

保留的能力与 X 相同：订阅缺集搜索补缺、15 个搜索源、多网盘转存与跨盘转存、秒传、TG 频道监控、榜单自动订阅（7 个渠道）、渠道签到。

不要把 Lite 与 X 的运行时 Lite 模式（配置项 `cs_lite_mode`，默认关闭）混淆：那是 X 内部的一个开关，Lite 插件本身没有这个配置项。

## 目录职责

```text
cloudsubscribelite/
├── __init__.py          # 插件入口、元数据及生命周期代理
├── version.py           # 版本声明（VERSION，唯一事实来源）
├── pyproject.toml       # v3 依赖声明（dynamic = ["version"]）
├── requirements.txt     # v2 遗留依赖清单（v3 以 pyproject.toml 为准）
├── core/                # 领域模型、配置、平台适配和通用服务（与 X 相同）
├── drive/               # 网盘能力接口及提供方实现
├── handlers/            # 搜索、订阅、同步、通知和 API 编排
├── search/              # 搜索源客户端
├── utils/               # 文件解析、匹配、Magnet 和 STRM 工具
└── dist/assets/         # CI 构建后写入 Release ZIP 的 Vue 模块联邦产物
```

## 模块边界

- `core/` 定义插件自身业务模型与服务，不放具体网盘或搜索源实现。
- `drive/` 通过稳定能力协议接入网盘；调用方先检查能力，再获取对应服务。
- `search/` 只负责获取和标准化资源候选，不执行转存或文件处理。
- `handlers/` 组合领域服务并对接事件、API、通知和任务入口。
- `utils/` 仅保留无状态或低状态的通用辅助逻辑。

与 X 的差异在 `core/api/registration.py` 的注册层与已删除的模块：`get_api()` 不再构建整理 / STRM / 重复扫描 / 回收站 / 漏斗 / 整理台路由（对应模块已删除，不再是“建好再过滤”），`get_service()` 不注册 STRM 与整理定时服务，`get_sidebar_nav()` 只返回主入口；`__init__.py` 里也不再有 Emby 反代启动、媒体库 Webhook 处理与 STRM 校准方法。

## 前端与发布

前端源码位于 [`frontend/cloudsubscribelite`](../../frontend/cloudsubscribelite)。
[`plugins-release.yml`](../../.github/workflows/plugins-release.yml) 在发布时生成并打入插件 ZIP。

插件版本必须同时更新三处：

- `version.py` 中的 `VERSION`（`__init__.py` 的 `plugin_version = VERSION`，不得硬编码字符串）
- 仓库根目录 [`package.v3.json`](../../package.v3.json) 中的 `version`
- `package.v3.json` 中对应版本的 `history`（最新一条置顶，按语义版本降序）

发布标签与资产命名遵循 MoviePilot 约定：

```text
CloudSubscribeLite_v<version>
cloudsubscribelite_v<version>.zip
```

Lite 与 X 不要同时启用，避免重复转存。

## 独立插件 ID 与旧 ID 迁移

- 插件 ID：`CloudSubscribeLite`；源码目录 `plugins.v3/cloudsubscribelite`；前端目录 `frontend/cloudsubscribelite`。
- `plugin_config_prefix` **仍是 `cloudsubscribe_`**：配置键与数据键保持原样，从旧 ID 迁移过来的配置无需重写。
- 首次启动（本插件配置为空、且未迁移过）会自动从旧 ID `CloudSubscribe` 读取并复制插件配置、持久化数据键与私有 SQLite 库 `cloudsubscribe.db`（目标文件已有内容时不覆盖）。复制后 `enabled` 强制置为 `False`，需手动启用。
- 迁移只做一次；之后清空配置属于用户主动重置，不会回灌旧插件配置。
- 本插件的数据目录是 `config/plugins/CloudSubscribeLite/`。

## 数据存储

与 X 相同：插件所有跨重启状态都在私有库 `cloudsubscribe.db`（`config/plugins/CloudSubscribeLite/cloudsubscribe.db`），不再写 JSON 状态文件，共 11 张表，失败与待修复的唯一事实来源是 `retry_index`。旧文档里的 `candidate_ledger_entries` 候选账本表已在 v1.13.0 的迁移中删除，`history` 相关表已在 v1.13.3 删除，不要再按旧表名找数据。详见 [X 的源码目录说明](../cloudsubscribex/README.md)。

## v3 说明

本目录是从 `plugins.v2/cloudsubscribelite/`（Lite 1.0.3）复制的 V3 专用实现，V2 目录原样保留、不再加新功能。v3 与 v2 的区别只有版本声明与清单（`version.py` + `pyproject.toml` + `package.v3.json`）；V3 宿主合同适配尚未在 V3 宿主上实测验证，在实测通过之前不要发版。另见根 README 的“未通过的版本门禁”说明。
