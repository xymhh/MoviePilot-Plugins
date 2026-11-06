# MoviePilot-Plugins

个人 MoviePilot 插件市场（fork 自 [`odomu/MoviePilot-Plugins`](https://github.com/odomu/MoviePilot-Plugins)）。

插件源：`https://github.com/xymhh/MoviePilot-Plugins`

## 网盘订阅助手（CloudSubscribeX）

订阅缺失的影视，自动去网盘搜源、转存、整理入库。

- **自动补缺**：按媒体库缺集搜网盘资源，命中即转存，追更订阅持续补齐
- **多网盘**：115 / 夸克 / 阿里 / 天翼 / 移动 / 123 / UC / 迅雷 资源，支持跨盘转存（如夸克 → 115）
- **秒传优先**：命中秒传零上传；未命中可配置为中止，不吃 115 流量
- **整理入库**：自动重命名、洗版择优、按媒体类型分层建目录
- **STRM + Emby 302**：全量/增量生成 `.strm`，播放时实时换 115 直链，流量不过 MoviePilot
- **多渠道**：TG 频道监控、手动粘贴分享链接、片名对话订阅

## 115网盘STRM助手（P115StrmHelper）

115 网盘的 STRM 与整理一条龙。

- 全量/增量生成 STRM，302 跳转播放
- 接管 115 整理：自动改名 + 移动到位
- 生活事件监控，新文件进库后自动补 STRM
- 本 fork 修补：媒体库已有更优版本时按**跳过**处理，不再记失败、不再反复重投
