# MoviePilot-Plugins

MoviePilot 自用插件市场（个人 fork 版）——**本仓库只提供一个插件：网盘订阅助手（CloudSubscribe）**。

插件市场源地址（仓库根地址，`main` 分支）：

```
https://github.com/xymhh/MoviePilot-Plugins
```

上游：[`odomu/MoviePilot-Plugins`](https://github.com/odomu/MoviePilot-Plugins)，本 fork 在其基础上做跨盘转存（尤其夸克→115）优化。

---

## 沿革：2026-10-11 缩为单插件仓库

| 原内容 | 现状 | 改用 |
| --- | --- | --- |
| 115网盘STRM助手（P115StrmHelper，本 fork 的 V3 线 2.7.26） | **已撤下**（源码、`package.v3.json` 索引、Release、CI 工作流全部移除） | 上游 [`DDSRem-Dev/MoviePilot-Plugins`](https://github.com/DDSRem-Dev/MoviePilot-Plugins)（2.8.76，本机 MoviePilot V3 实测可装可用） |
| 配置备份 / 保种空间守护 / Telegram 自动签到 / 综艺正片守卫 | **已撤下**（索引此前已剔除，本次连源码一并删除） | 上游 [`zkmydgth/MoviePilot-Plugins`](https://github.com/zkmydgth/MoviePilot-Plugins) |

为什么撤下 P115StrmHelper：自维护 fork 只为修「媒体库已存在更高质量文件时被记成整理失败」，实测**上游用配置项就能覆盖**，不值得再养一个分叉：

- **媒体库目录的「覆盖模式」= 按大小**：决定"库里同名文件更好时是否跳过"。该值由上游从 MoviePilot 的目录（媒体库）配置读取（取值 `always` / `size` / `never` / `latest`），不在插件配置页。
- **插件内「自动删除低质量源文件」**（`auto_delete_inferior_source_enabled`，默认关）：按大小裁决失败时，把源盘那份低质量文件删掉。
- **插件内「媒体库已存在时拦截整理」**（`transfer_intercept_exists_enabled`，默认关）：首次入库场景下，媒体服务器里已有就不再整理。

> ⚠️ 坑：`从不`（`never`）模式的失败消息同样是「媒体库存在同名文件…」，**也会命中「自动删除低质量源文件」的触发条件**（消息含「同名文件」即删源），这一路径不比较质量。要"只在库里更好时才删源"，覆盖模式请用**按大小**。

历史决策与验收证据保留在 `docs/`（见文末文档索引），不再随仓库分发任何 P115 相关内容。

---

## 插件列表

| 插件 | 用途 | 适用版本 | 源码目录 |
| --- | --- | --- | --- |
| **网盘订阅助手**（CloudSubscribe） | 整合网盘能力与多渠道资源搜索，自动查找并补充订阅缺失的影视内容 | MoviePilot >= 2.14.6（V2 插件线，MoviePilot V3 实测可用） | `plugins.v2/cloudsubscribe` |

功能特性、配置项与注意事项见 [`plugins.v2/cloudsubscribe/README.md`](plugins.v2/cloudsubscribe/README.md)；
本 fork 逐版本改动见 `package.v2.json` 的 `history` 字段。

---

## 安装

1. 打开 MoviePilot 的插件市场设置：V3 在「插件」页 →「插件市场」标签 → 右下角齿轮（`mdi-store-cog`，仅 admin 可见）；V2 在「设定 → 插件市场」
2. 添加插件源 `https://github.com/xymhh/MoviePilot-Plugins`（必须是仓库根地址，raw 链接无效）→ 保存 → 点「同步插件源」
3. 在市场里搜「网盘订阅助手」安装，然后到配置页完成：扫码绑夸克、绑目标网盘 115、填中继缓存目录（别用容器内 `/tmp`，改成挂载的本地盘路径）、开需要的搜索渠道，跑一条订阅验证

## 运维与排障

| 现象 | 处理 |
| --- | --- |
| 市场里搜不到本仓库插件 | 本仓库只有 `package.v2.json`（没有 `package.v3.json`，MoviePilot V3 会回退读 v2 索引）；确认填的是仓库根地址、齿轮按钮要用管理员账号可见 |
| 跨盘转存（夸克→115）慢到几十 KB/s | 1.6.22 起固定 16MB 分片 + 115 分片并发 PUT（配置项「115 上传分片并发」0–16，`0` = 关闭并回退单流顺序上传，失败自动回退）；并发不要开太大，夸克有风控 |
| 转存"实际成功却报失败" | 1.6.18 起修复后处理终态未落库导致的假失败；升级到最新版 |
| 想换旧版本 | Release 资产按 `cloudsubscribe_v<版本>.zip` 命名，可在本仓库 Releases 里下载对应版本 |

## 文档

- [`docs/README.md`](docs/README.md) — 文档索引（`plan/` 计划、`test/` 证据、`summary/` 结论）
- 关键总结：
  - [撤下 P115 分叉与仓库瘦身](docs/summary/2026-10-11-撤下P115分叉与仓库瘦身.md)
  - [单仓库插件市场合并](docs/summary/2026-10-11-单仓库插件市场合并-总结.md)
  - [合并仓库 CI 三件事](docs/summary/2026-10-11-合并仓库CI三件事.md)

如果本文档信息与插件实际表现不符，以插件仓库代码为准。
