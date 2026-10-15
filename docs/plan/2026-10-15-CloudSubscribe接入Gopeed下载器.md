# CloudSubscribe 接入 Gopeed 作为外部下载器（实施任务书）

> 状态：已实施，本地验收通过（1.6.43，待推送/发布）→ 证据见 [../test/2026-10-15-Gopeed下载器单测.md](../test/2026-10-15-Gopeed下载器单测.md)
> 关联：上一轮「点选兜底类型相容性」已发布 1.6.42（commit a40fbb3 + 341f7d0）

## 背景

用户环境：MoviePilot v3 + 网盘订阅助手（CloudSubscribe），源盘夸克、目标盘 115。
夸克直链按**单连接**限速（约 100~200KB/s），提速只能靠多连接叠加；插件内置下载器
（`HttpFileDownloadService`）是「10MB 分段 × N 并发」模型，每段一次 Range 请求。
用户已在同一台 1Panel 服务器上部署 Gopeed（Docker，容器内端口 9999，与 MP 同
`1panel-network`），希望把「下载到本地」这一步交给 Gopeed 执行。

## 目标

把「夸克 → 本地中继 → 上传 115」链路里的**下载到本地**这一步，改为可选用外部
Gopeed（REST API）执行：

- 默认**关闭**（配置为 `builtin` 时行为与 1.6.42 逐字节一致）；
- Gopeed 侧任何异常**自动回落**内置下载器，主链路不因外部依赖挂掉；
- 其余环节（转存、取链、上传 115、重命名、通知、历史、STRM）**逐字节不变**。

## 现状锚点（已在源码中确认，勿再猜）

- 统一下载接口：`plugins.v2/cloudsubscribe/core/transfer.py:150`
  `HttpFileDownloadService.download_file(file_item, local_path, progress_callback=None, stop_requested=None, preserve_partial=False) -> str`
- 夸克实现：`plugins.v2/cloudsubscribe/drive/quark/files.py:74` `QuarkProvider.download_file(...)`
  （内部构造 `HttpFileDownloadService`，`concurrency=download_threads`，`part_size=10MB`）
- 取链：`drive/quark/files.py:96` `resolve_download_link(file_item) -> (url, headers)`；
  headers 来自 `client.download_headers({"user-agent": PC 客户端 UA})`，含 UA/Cookie/Referer；
  内部三通道回退（drive-pc/PC-UA → drive/PC-UA → drive/网页-UA）
- 跨盘流程调用点：`core/transfer.py:1248-1296`
  （`source_provider.require(CloudDriveCapability.FILE_DOWNLOAD)` → `downloader.download_file(...)`）
- 中继目录：`core/transfer.py:587 _cache_root`，配置键 `cross_transfer_download_path`（默认 `/tmp`）
- 任务管理器构造处：`core/drive_manager.py:37` `CrossTransferTaskManager(..., download_path=..., download_threads=...)`
- 配置默认值：`core/config.py:180` 起（`cross_transfer_*` 系列）
- 前端字段：`frontend/cloudsubscribe/src/config/fields/transfer.js`
- 版本号三处：`plugins.v2/cloudsubscribe/__init__.py:104`、`package.v2.json:6`、`package.v3.json`（若存在）

## Gopeed API 事实（已核实，v1.9.3 与 2.x 同源）

- 鉴权：请求头 `X-Api-Token: <token>`（带此头则跳过 basic auth）
- 建任务：`POST /api/v1/tasks`
  body `{"req": {"url": "...", "labels": {}, "extra": {"header": {...}}},
        "opts": {"name": "...", "path": "...", "selectFiles": []}}`
  → `{"code":0,"data":"<taskId>"}`
- 查任务：`GET /api/v1/tasks/{id}`（含进度）、`GET /api/v1/tasks/{id}/stats`、`GET /api/v1/tasks`
- 暂停/继续：`PUT /api/v1/tasks/{id}/pause`、`PUT /api/v1/tasks/{id}/continue`
- 删除：`DELETE /api/v1/tasks/{id}`
- 解析（不下载）：`POST /api/v1/resolve`

## 必须实现

### 1. 新增 `plugins.v2/cloudsubscribe/core/gopeed.py`

- `GopeedClient`
  - `X-Api-Token` 鉴权，超时可配（默认 15s）
  - `info()`、`create_task(url, headers, path, name) -> task_id`、`get_task(task_id)`、
    `pause_task(task_id)`、`delete_task(task_id)`
  - 网络异常 / 401 / `code != 0` 一律抛明确异常（异常消息**不得含 token**）
- `GopeedDownloadService`
  - **与 `HttpFileDownloadService.download_file` 同签名、同语义**（可被无差别替换）
  - 流程：
    1. 调 resolver 取 `(url, headers)` → 建任务：`req.extra.header = headers`、
       `opts.path = <local_path 的父目录>`、`opts.name = <local_path 的文件名>`
       （默认要求「Gopeed 与 MP 把同一宿主目录挂到同一容器路径」；配了
       `gopeed_path_prefix` 时按前缀映射换算）
    2. 每 1~2s 轮询任务，把已下载字节数/总量映射到 `progress_callback(done, total)`
    3. 完成 → 校验 `local_path` 存在且大小与 `file_item.size` 相符 → 返回 `str(local_path)`
    4. 任务失败 / 直链过期 → 重新取链并**重建任务**（最多 3 次，指数退避），
       语义对齐 `_LinkRefresher`（`core/transfer.py:104`）
    5. `stop_requested()` 为真 → 暂停并删除 Gopeed 任务，抛 `RuntimeError`
       （与内置下载器行为一致）
    6. Gopeed 侧异常 → 记 warning（含 task id 与原因，**不含 token**）后回落内置下载器
  - `preserve_partial=True` 时不得删除 Gopeed 侧任务/文件（与内置语义对齐）

### 2. 改 `drive/quark/files.py:74`

按配置分流：`builtin`（默认，原样）/ `gopeed`（走 `GopeedDownloadService`，
异常自动回落内置并记一次 warning）。**先只接夸克**，其它源盘行为不变。

### 3. 配置项

`core/config.py` 默认值 + `frontend/cloudsubscribe/src/config/fields/transfer.js` UI：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `cross_transfer_downloader` | `builtin` | `builtin` / `gopeed` |
| `gopeed_url` | `http://gopeed:9999` | 同 1panel-network 用容器名即可 |
| `gopeed_token` | 空 | 密码框；**禁止**写日志/历史/通知 |
| `gopeed_path_prefix` | 空 | 可选，Gopeed 侧路径前缀 → 本机路径前缀 |

UI 提示文案必须包含：① Gopeed 与 MP 需把同一宿主目录挂到同一容器路径；
② Token 在 Gopeed → 设置 → 高级 → 接口令牌；③ 关闭本项即完全走内置下载器。

### 4. 单测 `tests/test_gopeed_downloader.py`

unittest 风格，参考 `tests/plugin_env.py`。至少覆盖：

1. 正常完成 → 文件落在 `local_path`、返回该路径
2. 进度回调被调用且单调不减
3. 任务失败 → 重新取链 + 重建任务（断言 resolver 被调用 ≥2 次）
4. `stop_requested` → 暂停/删除任务并抛错
5. Gopeed 不可用（连不上/401）→ 回落内置下载器（断言内置服务被调用）
6. `gopeed_path_prefix` 映射生效

### 5. 版本与文档

- 版本 **1.6.43**，三处同步
- 本文件 + `docs/test/2026-10-15-Gopeed下载器单测.md`（测试与验收记录）
- `README.md` 文档索引加链

## 禁止事项

- 不改上传 115 链路（upload / `LocalRapidUploadAdapter` / 分片参数）
- 不改取链三通道与 UA 选择
- 不动 `_download_parallel` / `_download_serial` 内置实现
- 不新增第三方依赖（只用 `requests`，仓库已依赖）
- 不改 MoviePilot 宿主代码；只在 `moviepilot-plugins-cs` 仓库内改
- token 绝不落盘、落日志、进历史或通知
- **不要**执行任何 git 提交/推送（由主 agent 处理）

## 验收标准

- `python -m unittest discover -s tests -p "test_*.py" -v` 全绿（新增用例 + 既有基线）
- 新增用例必须在改动前（`git stash`）失败——反证断言有效
- `python scripts/lint.py --all` 0 error
- 报告：改动清单（`文件:行`）+ 测试输出 + 用户侧需做什么

## 运行方式

- 测试：仓库根目录 `python -m unittest discover -s tests -p "test_*.py" -v`
- lint：`python scripts/lint.py --all`
- 前端只改字段 js，**不要**跑构建（CI 会构建）；本机有 node 时用 `node --check` 自检语法
