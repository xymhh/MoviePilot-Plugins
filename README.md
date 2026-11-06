# MoviePilot-Plugins

个人 MoviePilot 插件市场，两个插件：**网盘订阅助手（CloudSubscribeX）**、**115网盘STRM助手（P115StrmHelper）**。

上游：[`odomu/MoviePilot-Plugins`](https://github.com/odomu/MoviePilot-Plugins)

**插件源（仓库根地址，`main` 分支）**

```
https://github.com/xymhh/MoviePilot-Plugins
```

| 插件 | 用途 | MoviePilot | 源码 |
| --- | --- | --- | --- |
| 网盘订阅助手（CloudSubscribeX） | 订阅搜源 → 跨盘转存 → 整理入库，自带 STRM 与 Emby 302 | >= 2.14.6 | `plugins.v2/cloudsubscribex` |
| 115网盘STRM助手（P115StrmHelper） | 115 STRM 生成、302、整理；本 fork 修「库内质量更好 → 整理失败」 | >= 3.0.0 | `plugins.v3/p115strmhelper` |

## 安装

1. MoviePilot →「插件」→「插件市场」→ 齿轮（管理员可见）
2. 添加上面的插件源 → 保存 →「同步插件源」
3. 搜插件名安装，配置页按向导绑定网盘、设落地目录

## 只装一个就够

CloudSubscribeX 1.7.39+ 已自带 STRM 全量/增量与 302，不需要再装 P115StrmHelper。把 STRM 模板改成：

```
http://<MP地址>/api/v1/plugin/CloudSubscribeX/redirect?pickcode={pickcode}&file_name={file_name}
```

先手动改一部剧的 `.strm` 验证能播，再改模板、卸载 STRM 助手。历史 `.strm` 不重写，新增集数自动用新模板。

## 三个必须知道的配置

| 配置 | 说明 |
| --- | --- |
| 跨盘缓存目录 | 不要用容器内 `/tmp`，指向挂载的本地盘 |
| 秒传未命中处理 | 选「中止并清理」则不上传、不耗 115 流量 |
| 覆盖模式（P115StrmHelper） | 用「按大小」；用「从不」会误触发「自动删除低质量源文件」 |

## 常见问题

| 现象 | 处理 |
| --- | --- |
| 市场里搜不到本仓库插件 | 确认填的是仓库根地址、管理员账号、点过「同步插件源」 |
| 整理失败「媒体库存在同名文件，且质量更好」反复重投 | 官方 2.8.76 会覆盖原成功记录 → 升级本仓库 **2.8.78**（含 2.8.77 的跳过语义） |
| 发片名后点候选没反应 | 升级到 1.6.34+；日志搜 `媒体点选接管器已安装`，没有就重启 MP |
| 阿里云盘资源转存失败 | 阿里封了第三方分享直链（410），已放弃该来源；阿里作为目标盘不受影响 |

## 命令行

```bash
export MP_URL=http://127.0.0.1:3000 MP_USER=<用户名> MP_PASSWORD=<密码>
python3 cli.py doctor && python3 cli.py history --status failed
```

## 文档

- [`docs/README.md`](docs/README.md) — 索引：`plan/` 计划、`test/` 证据、`summary/` 结论
- [插件配置项说明](plugins.v2/cloudsubscribex/README.md)
- [历史记录页改造（1.9.45–1.9.49）](docs/summary/2026-11-05-历史记录页改造.md)
