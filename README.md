# MoviePilot-Plugins

个人 fork 的 MoviePilot 插件市场，两个插件：**网盘订阅助手（CloudSubscribeX）** 与 **115网盘STRM助手（P115StrmHelper）**。

上游：[`odomu/MoviePilot-Plugins`](https://github.com/odomu/MoviePilot-Plugins)。

**插件源（必须是仓库根地址，`main` 分支）**

```
https://github.com/xymhh/MoviePilot-Plugins
```

| 插件 | 用途 | MoviePilot 版本 | 源码 |
| --- | --- | --- | --- |
| 网盘订阅助手（CloudSubscribeX） | 订阅搜源 → 跨盘转存 → 整理入库，自带 STRM 全量/增量与 Emby 302 | >= 2.14.6（V2 线，V3 宿主可用） | `plugins.v2/cloudsubscribex` |
| 115网盘STRM助手（P115StrmHelper） | 115 STRM 生成、302 跳转、转移整理；本 fork 修「库内质量更好 → 整理失败」 | >= 3.0.0（V3 线） | `plugins.v3/p115strmhelper` |

## 安装

1. MoviePilot →「插件」→「插件市场」→ 右下角齿轮（仅管理员可见）
2. 添加上面的插件源地址 → 保存 →「同步插件源」
3. 搜插件名 → 安装 → 配置页按向导绑定网盘、设落地目录

## 只装一个就够

CloudSubscribeX 1.7.39+ 已自带 STRM 全量/增量与 302 播放，**不需要**再装 P115StrmHelper。把 STRM 模板改成：

```
{base_url}?pickcode={pickcode}&file_name={file_name}
```

其中 `base_url` = `http://<MP地址>/api/v1/plugin/CloudSubscribeX/redirect`。迁移：先手动改一部剧的 `.strm` 验证能播 → 改模板 → 卸载 STRM 助手。历史 `.strm` 不自动重写，新增集数自动用新模板。

P115StrmHelper 2.8.77 相对官方 2.8.76 的唯一功能改动：媒体库里已有同名更优版本时改为**跳过语义**（记日志与通知，不写失败记录、不发 `TransferFailed`），避免同一文件被反复重投。配置页「媒体库目录 → 覆盖模式」请用**按大小**；用「从不」也会命中「自动删除低质量源文件」的触发条件。

## 常用配置

| 配置 | 位置 | 说明 |
| --- | --- | --- |
| 跨盘缓存目录 | 基础设置 | **别用容器内 `/tmp`**，指向挂载的本地盘 |
| 115 上传分片并发 | 转存设置 | 0–16，默认关闭；开太大触发夸克风控 |
| 秒传未命中处理 | 转存控制 | 选「中止并清理」则不上传、不吃流量 |
| 外部下载器 Gopeed | 跨盘传输 | 夸克单连接限速约 100–200 KB/s；Gopeed 连接数填 256 实测 1.6 → 19.99 MB/s |
| 片名自动订阅 | 片名自动订阅 | 对话框发片名 → 平台出候选 → 点选即建订阅并搜源 |
| TG 频道监控 | 频道监控 | 填自己的 `api_id`/`api_hash` 登录，扫描频道网盘链接 |
| 忽略媒体库路径 | 提供方设置 | 每行一个前缀，排除 STRM 分享库干扰缺集判断 |

## 常见问题

| 现象 | 处理 |
| --- | --- |
| 市场里搜不到本仓库插件 | 确认填的是仓库根地址、用管理员账号、点过「同步插件源」；V3 读 `package.v3.json`，V2 读 `package.v2.json` |
| 整理失败「媒体库存在同名文件，且质量更好」反复重投 | 官方 2.8.76 会把它记成失败并覆盖原成功记录 → 升级本仓库 **2.8.77** |
| 跨盘转存慢到几十 KB/s | 升级到 1.6.43+ 接 Gopeed，并把「Gopeed 连接数」填 256 |
| 发片名后点候选没有反应 | 升级到 1.6.34+；日志搜 `媒体点选接管器已安装`，没有就重启 MP 容器 |
| 只订了第 1 季 | 1.6.29+ 默认按缺集季展开，检查「手动订阅补齐缺集季」开关 |
| 移动云盘资源预览为空 / 转存没反应 | 1.6.40+ 修了 `caiyun.139.com` 识别与 `caID`/`coID` 字段大小写 |
| 「清理插件缓存」报 `AnimeGardenSearchService ... '_lock'` | 1.6.38+ 已修 |
| 阿里云盘资源转存失败 | 阿里封了第三方分享直链（HTTP 410），已放弃该来源；阿里作为**目标盘**不受影响 |
| 想装回旧版本 | Releases 里按 `cloudsubscribex_v<版本>.zip` 下载（1.6.48 及更早为 `cloudsubscribe_v<版本>.zip`） |

更多现象与逐条根因见 [`docs/summary/`](docs/summary/) 与各版本复盘。

## 命令行

零依赖（标准库），插件侧无需改动：

```bash
export MP_URL=http://127.0.0.1:3000 MP_USER=<用户名> MP_PASSWORD=<密码>
python3 cli.py doctor                  # 体检：9 项红绿灯
python3 cli.py history --status failed # 失败记录
python3 cli.py sync start --keyword 三体
```

完整子命令见 [CLI 文档](docs/plan/2026-11-02-CLI-调研与架构.md)。

## 文档

- [`docs/README.md`](docs/README.md) — 文档索引：`plan/` 计划、`test/` 证据、`summary/` 结论
- [插件详细说明](plugins.v2/cloudsubscribex/README.md) — 配置项逐条解释
- [历史记录页改造（1.9.45–1.9.49）](docs/summary/2026-11-05-历史记录页改造.md)
- [移除任务中心（1.9.44）](docs/summary/2026-11-05-移除任务中心.md)
- [跨盘转存「深圳中继」方案](docs/plan/2026-10-17-跨盘转存深圳中继.md) — 海外 VPS 直传 115 只有 30–120 KB/s，内网上传实测 22 MB/s
