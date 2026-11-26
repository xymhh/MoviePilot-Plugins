# MoviePilot-Plugins

个人 MoviePilot **v3** 插件市场：网盘订阅助手（CloudSubscribeX）与网盘订阅 Lite（CloudSubscribeLite）。

插件在 MoviePilot v3 的插件市场中安装和使用，版本与更新记录以 [`package.v3.json`](package.v3.json) 为准。

## 插件列表

| 插件 | 版本 | 适用 MoviePilot | 说明 |
| --- | --- | --- | --- |
| 网盘订阅助手（`CloudSubscribeX`） | 3.0.0 | ≥ 3.0.0 | 订阅缺失的影视，自动去网盘搜源、转存、整理入库 |
| 网盘订阅 Lite（`CloudSubscribeLite`） | 3.0.0 | ≥ 3.0.0 | 基于最新 CloudSubscribeX 的简化版：只保留搜索、转存、签到与榜单订阅 |

两个插件是同一代码基的两种发行形态（入口文件相似度 98.65%），不是两个独立实现。**Lite 是基于最新 X 的简化版**：底层代码与 X 相同，精简发生在注册/暴露层——不注册 STRM 全量/增量校准与 115 整理服务，不暴露 `/strm`、`/duplicates`、`/recyclebin`、`/funnel`、`/organize` 路由与对应侧边栏；整理与 STRM 交给 115 助手插件处理。其他能力（多网盘转存、秒传、TG 频道监控、榜单自动订阅、签到）两者相同。X 另有运行时 Lite 模式（`cs_lite_mode`），与 Lite 插件不是同一个东西，不要混淆。

### 网盘订阅助手（CloudSubscribeX）

订阅缺失的影视，自动去网盘搜源、转存、整理入库。

- **自动补缺**：按媒体库缺集搜网盘资源，命中即转存，追更订阅持续补齐
- **多网盘**：115 / 夸克 / 阿里 / 天翼 / 移动 / 123 / UC / 迅雷资源，支持跨盘转存（如夸克 → 115）
- **秒传优先**：命中秒传零上传；未命中可配置为中止，不吃 115 流量
- **整理入库**：自动重命名、洗版择优、按媒体类型分层建目录
- **STRM + Emby 302**：全量/增量生成 `.strm`，播放时实时换 115 直链，流量不过 MoviePilot
- **多渠道**：TG 频道监控、手动粘贴分享链接、片名对话订阅
- **榜单订阅**：豆瓣 / TMDB / Bangumi / AniList / 猫眼 / Netflix / Mikan 自动订阅

### 网盘订阅 Lite（CloudSubscribeLite）

基于最新 CloudSubscribeX 的简化版，只保留搜索渠道、跨盘转存、渠道签到与榜单订阅；整理与 STRM 交给 115 助手插件。

适合已经用 115 助手插件做整理，只需要“搜源 + 转存 + 签到 + 榜单”的用户。Lite 的 `system_version >=3.0.0` 不是笔误——它就是 v3 版本。

## 安装

在 MoviePilot v3 的插件市场中添加以下仓库地址：

```text
https://github.com/xymhh/MoviePilot-Plugins
```

刷新市场后搜索“网盘订阅助手”或“网盘订阅 Lite”安装。两个插件不要同时启用，避免重复转存；从旧插件 `CloudSubscribe` 迁移过来的配置，首次启动会自动复制（`enabled` 会被强制置为 `False`，需手动启用）。

> 旧版本说明：V2 历史实现仍保留在 `plugins.v2/`，清单见 [`package.v2.json`](package.v2.json)（X 1.36.1，适用 ≥ 2.14.6；Lite 1.0.3，适用 ≥ 3.0.0），供仍在用 MoviePilot v2 的用户安装。V3 用户请安装 v3 版本，不要混用。

> 注意：v3 迁移的目录、清单与版本声明已完成，但 V3 宿主合同适配（`app.sdk.*` 迁移、媒体身份 `media_source`/`media_id`、宿主数据库改走 Oper/Chain/SDK）尚未在 V3 宿主上实测验证。在实测通过之前，不要发版。

## 使用

使用指南见 [`docs/cloudsubscribe.md`](docs/cloudsubscribe.md)：运转流程、必填配置、网盘与搜索源设置、STRM 与整理、TG 频道监控、常见问题。指南按 X 3.0.0 / Lite 3.0.0 的当前行为编写。

命令行客户端见 [`cli.py`](cli.py)（仅 CloudSubscribeX），例如 `python3 cli.py doctor`、`python3 cli.py history --size 20`。

## 仓库结构

```text
MoviePilot-Plugins/
├── plugins.v3/          # MoviePilot v3 插件（当前版本）
│   ├── cloudsubscribex/ # 网盘订阅助手（CloudSubscribeX）
│   └── cloudsubscribelite/ # 网盘订阅 Lite（CloudSubscribeLite，基于最新 X 的简化版）
├── plugins.v2/          # MoviePilot v2 历史实现（仅维护，不再加新功能）
│   ├── cloudsubscribex/ # X 1.36.1
│   └── cloudsubscribelite/ # Lite 1.0.3
├── frontend/            # 插件前端源码（Vue 模块联邦）
├── icons/               # 插件图标
├── tests/               # 插件测试（v3 测试在 tests/v3/）
├── scripts/             # lint、版本等价性校验等脚本
├── docs/                # 用户指南与开发过程文档
├── package.v3.json      # v3 插件市场清单（版本、更新记录）
├── package.v2.json      # v2 历史清单
└── .github/workflows/   # lint、测试与自动发布
```

每个 v3 插件目录含 `__init__.py`（入口与元数据）、`version.py`（`VERSION`，`plugin_version` 引用它，不硬编码）、`pyproject.toml`（v3 依赖）、`README.md`（源码目录说明）与 `dist/assets/`（前端构建产物）。

## 开发与发布

- 发布一个版本时，版本号必须同时更新三处：插件 `version.py` 里的 `VERSION`（`__init__.py` 的 `plugin_version = VERSION`）、`package.v3.json` 里该插件的 `version`、以及 `package.v3.json` 里对应版本的 `history`（最新一条置顶，按语义版本降序）。
- 发布标签为 `<插件ID>_v<版本>`，例如 `CloudSubscribeX_v3.0.0`；压缩包为 `<小写目录>_v<版本>.zip`，例如 `cloudsubscribex_v3.0.0.zip`。改动 `package.v3.json` 即触发自动发布。
- V2 历史实现不再加新功能；修 V2 只改 `plugins.v2/` 与 `package.v2.json`，不要反向改坏 V3 实现。V2 旧条目已声明 `"v3": false`，V3 宿主不会回退加载它。
- **版本规则（按 DDSRem-Dev/MoviePilot-Plugins 的做法）**：V3 插件用 3.x.x，初始版本 3.0.0，不按“旧代主版本 +1”算；版本唯一来源是插件 `version.py` 里的 `VERSION`，`__init__.py` 写 `plugin_version = VERSION`，不硬编码；发布脚本 `get_plugin_meta.py` 也是先读 `version.py`，读不到才回退到 `plugin_version`，并在检测发布时比对源码版本与清单版本，不一致就报错。V3 的 `history` 只记 V3 自己的发布线，不抄 V2 的旧记录（V2 的记录留在 `package.v2.json` 里）。`system_version` 是宿主最低版本，不是插件版本。跟这套规则冲突的旧门禁脚本 `check_plugin_versions.py` 和 `verify_plugin_versions.py` 都已删除，仓库里不再有独立的版本门禁脚本。
- 开发过程文档（方案、验收、复盘）见 [`docs/`](docs/)，索引见 [`docs/README.md`](docs/README.md)。这些是开发记录，不是用户指南。

## 文档

- 用户指南：[`docs/cloudsubscribe.md`](docs/cloudsubscribe.md)
- 开发过程文档索引：[`docs/README.md`](docs/README.md)
- 插件源码目录说明：[`plugins.v3/cloudsubscribex/README.md`](plugins.v3/cloudsubscribex/README.md)、[`plugins.v3/cloudsubscribelite/README.md`](plugins.v3/cloudsubscribelite/README.md)

## 来源与致谢

本仓库 fork 自 [odomu/MoviePilot-Plugins](https://github.com/odomu/MoviePilot-Plugins)。上游只有一个插件 `CloudSubscribe`（目录 `plugins.v2/cloudsubscribe`，用户指南见上游 `docs/cloudsubscribe.md`）；本仓库在此基础上做了独立插件 ID（`CloudSubscribeX`，可与旧插件并存、各自升级）、新增基于最新 X 的简化版 `CloudSubscribeLite`，以及 2026-10-11 以来的大量自有开发（整理链、漏斗、STRM 校准、TG 频道监控等，见 `docs/`），现已迁移到 MoviePilot v3（`plugins.v3/` + `package.v3.json`）。

上游致谢（沿用上游 README）：本项目的部分设计与实现参考了 [Aqr-K/MoviePilot-Plugins](https://github.com/Aqr-K/MoviePilot-Plugins) 与 [mrtian2016/MoviePilot-Plugins](https://github.com/mrtian2016/MoviePilot-Plugins)，谨此致谢。

## 许可证

本仓库根据 [GNU General Public License v3.0](LICENSE) 许可证进行许可。
