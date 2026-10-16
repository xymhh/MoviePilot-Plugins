# CloudSubscribeX 源码目录

本目录是 `CloudSubscribeX`（显示名「网盘订阅助手（跨盘优化版）」，fork 自上游 `odomu` 的同名插件）的
MoviePilot v2 后端源码及 Release 前端产物目录。1.6.49 起使用独立插件 ID，与上游同名插件可在 MP 里并存、各自独立升级。面向用户的安装、配置和功能说明见
[网盘订阅助手使用说明](../../docs/cloudsubscribe.md)。

## 目录职责

```text
cloudsubscribex/
├── __init__.py          # 插件入口、元数据及生命周期代理
├── requirements.txt     # Python 运行依赖
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

插件版本必须同时更新：

- `__init__.py` 中的 `plugin_version`
- 仓库根目录 [`package.v2.json`](../../package.v2.json) 中的 `version`
- `package.v2.json` 中对应版本的 `history`

发布标签与资产命名遵循 MoviePilot 约定：

```text
CloudSubscribeX_v<version>
cloudsubscribex_v<version>.zip
```

## 独立插件 ID 与旧 ID 迁移（1.6.49）

- 插件 ID：`CloudSubscribeX`；源码目录 `plugins.v2/cloudsubscribex`；前端目录 `frontend/cloudsubscribex`。
- `plugin_config_prefix` **仍是 `cloudsubscribe_`**：配置键与数据键（`history`、签到、预算等）保持原样，
  从旧 ID 迁移过来的配置无需重写。
- 首次启动（本插件配置为空、且未迁移过）会自动从旧 ID `CloudSubscribe` 读取并复制：
  1. 插件配置（写入本插件，`enabled` 强制置为 `False`，避免与旧插件同时转存）；
  2. 持久化数据键（历史/签到/预算/待转存队列等）；
  3. 私有 SQLite 库 `cloudsubscribe.db`（订阅登记、转存历史；目标文件已有内容时不覆盖）。
  迁移结果会记在数据键 `legacy_id_migration` 里，日志关键字：`已从旧插件 CloudSubscribe 迁移到独立 ID CloudSubscribeX`。
- 迁移只做一次；之后清空配置属于用户主动重置，不会回灌旧插件配置。
- 旧 ID 插件的数据目录是 `config/plugins/CloudSubscribe/`，本插件是 `config/plugins/CloudSubscribeX/`。
