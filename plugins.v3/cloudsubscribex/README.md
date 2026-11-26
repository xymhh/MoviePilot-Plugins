# CloudSubscribeX 源码目录（v3）

本目录是 `CloudSubscribeX`（显示名「网盘订阅助手（CloudSubscribeX）」）的
MoviePilot v3 后端源码及 Release 前端产物目录。使用独立插件 ID，可与同名旧插件在 MP 里并存、各自独立升级。面向用户的安装、配置和功能说明见
[网盘订阅助手使用说明](../../docs/cloudsubscribe.md)。

当前版本：3.0.0（`version.py` 的 `VERSION`，`__init__.py` 的 `plugin_version = VERSION`）。

## 目录职责

```text
cloudsubscribex/
├── __init__.py          # 插件入口、元数据及生命周期代理
├── version.py           # 版本声明（VERSION，唯一事实来源）
├── pyproject.toml       # v3 依赖声明（dynamic = ["version"]）
├── requirements.txt     # v2 遗留依赖清单（v3 以 pyproject.toml 为准）
├── core/                # 领域模型、配置、平台适配和通用服务
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

## 前端与发布

前端源码位于 [`frontend/cloudsubscribex`](../../frontend/cloudsubscribex)。
[`plugins-release.yml`](../../.github/workflows/plugins-release.yml) 在发布时生成并打入插件 ZIP。

插件版本必须同时更新三处：

- `version.py` 中的 `VERSION`（`__init__.py` 的 `plugin_version = VERSION`，不得硬编码字符串）
- 仓库根目录 [`package.v3.json`](../../package.v3.json) 中的 `version`
- `package.v3.json` 中对应版本的 `history`（最新一条置顶，按语义版本降序）

发布标签与资产命名遵循 MoviePilot 约定：

```text
CloudSubscribeX_v<version>
cloudsubscribex_v<version>.zip
```

## 独立插件 ID 与旧 ID 迁移（1.6.49）

- 插件 ID：`CloudSubscribeX`；源码目录 `plugins.v3/cloudsubscribex`；前端目录 `frontend/cloudsubscribex`。
- `plugin_config_prefix` **仍是 `cloudsubscribe_`**：配置键与数据键（`history`、签到、预算等）保持原样，
  从旧 ID 迁移过来的配置无需重写。
- 首次启动（本插件配置为空、且未迁移过）会自动从旧 ID `CloudSubscribe` 读取并复制：
  1. 插件配置（写入本插件，`enabled` 强制置为 `False`，避免与旧插件同时转存）；
  2. 持久化数据键（历史/签到/预算/待转存队列等）；
  3. 私有 SQLite 库 `cloudsubscribe.db`（订阅登记、转存历史；目标文件已有内容时不覆盖）。
  迁移结果会记在数据键 `legacy_id_migration` 里，日志关键字：`已从旧插件 CloudSubscribe 迁移到独立 ID CloudSubscribeX`。
- 迁移只做一次；之后清空配置属于用户主动重置，不会回灌旧插件配置。
- 旧 ID 插件的数据目录是 `config/plugins/CloudSubscribe/`，本插件是 `config/plugins/CloudSubscribeX/`。

## 数据存储（1.7.98 起统一入库）

插件所有跨重启状态都在私有库 `cloudsubscribe.db`（`config/plugins/CloudSubscribeX/cloudsubscribe.db`），不再写 JSON 状态文件，共 11 张表：

| 数据 | 表 |
| --- | --- |
| 离线待终态任务 | `offline_pending_tasks` |
| 签到历史 / 签到调度状态 | `checkin_history`、`checkin_schedule_state` |
| 搜索源积分预算 | `point_budget_records` |
| 账号快照缓存 | `account_snapshots` |
| 运行期键值对（TG 监控池 / 游标 / 状态、STRM 续扫断点等） | `runtime_records`（按 `namespace` 隔离） |
| 源文件 SHA1（秒传未命中后零流量重试） | `source_sha1_cache` |
| 秒传未命中负缓存 | `source_miss_cache` |
| 认证会话 | `auth_sessions` |
| 失败与待修复的唯一事实来源 | `retry_index` |
| 整理漏斗队列 | `funnel_queue` |

注意：旧文档里的 `candidate_ledger_entries` 候选账本表已在 v1.13.0 的迁移（`20261117_03_drop_p4_ledgers.py`）中删除，`history` 相关表已在 v1.13.3 删除；`retry_index` 是失败与待修复的唯一事实来源，不要再按旧表名找数据。

老版本留下的 `source_sha1_cache.json`、`tg_monitor/*.json`、`strm_scan_state.json`
会在升级后第一次启动时自动导入，并原地改名为 `*.migrated`
（不删除，可回溯）。1.12.19 起下载器不再写任何 `.json` 伴随文件（缓存归属 / 完整性 /
续传改走进程内登记表）；旧版留下的 `*.meta.json` / `*.parts.json` 由启动残留清理直接删掉。

## v3 说明

本目录是从 `plugins.v2/cloudsubscribex/`（X 1.36.1）复制的 V3 专用实现，V2 目录原样保留、不再加新功能。v3 与 v2 的区别只有版本声明与清单（`version.py` + `pyproject.toml` + `package.v3.json`）；V3 宿主合同适配（`app.sdk.*` 迁移、媒体身份 `media_source`/`media_id`、宿主数据库改走 Oper/Chain/SDK）尚未在 V3 宿主上实测验证，在实测通过之前不要发版。另见根 README 的“未通过的版本门禁”说明。
