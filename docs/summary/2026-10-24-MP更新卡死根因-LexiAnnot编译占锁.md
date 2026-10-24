# 2026-10-24 MP「一直正在更新」根因：LexiAnnot 的 spacy 编译卡死占住全局依赖安装锁

> 状态：根因已定位并交叉验证；处置待用户执行（卸载 LexiAnnot + 重启容器）。

## 结论

不是网络问题，不是 `CloudSubscribeX` 本身的问题。**触发源是 LexiAnnot 的 `spacy` 依赖**：

1. MP 容器（v3.1.9）跑 **Python 3.14**（`/opt/venv/lib/python3.14`）。
2. `/config/plugins_backup/lexiannot/requirements.txt` = `pysubs2 / langdetect / pymediainfo / spacy~=3.8.11`。
3. `spacy 3.8.11` 强制 `thinc>=8.3.4,<8.4.0`；`thinc 8.3.6` 在 pypi 只有 cp310/311/312/313 wheel，**linux 上没有任何 cp314 wheel** → uv 只能源码编译（Cython + C++，需编译器，极慢或直接失败）。
4. `08:07:30` LexiAnnot 开始装依赖 → `08:08:14` 插件被停止；`plugins/` 里已没有 lexiannot（安装回滚），但**那次安装线程至今持有 MP 的全局依赖安装锁**。
5. `app/adapters/system/plugin/health.py`：`while not cls._package_install_lock.acquire(blocking=False): await asyncio.sleep(0.01)` —— **无超时、无 INFO 日志**。锁被占后，后续所有插件的依赖安装都在这里静默死等。
6. 于是 `10:25:51` 的 1.7.29 安装日志停在 `package.py - 存在依赖，开始尝试安装依赖` 之后 **零输出**（20+ 分钟），前端永远「正在更新」。1.7.28 同理（08:05 之后从未加载过 1.7.28）。

**为什么别人正常**：别人没装 LexiAnnot，没有线程去编译 thinc，锁一直空闲。

## 排除项（都查过）

| 假设 | 结论 |
| --- | --- |
| 网络 / 德国 VPS 访问 pypi 慢 | 否。若是网络，uv 有 `PLUGIN_DEPENDENCY_INSTALL_TIMEOUT=300` 会报错；实测 MP CPU 1.8%、无任何超时/解析报错 |
| 我的插件依赖要编译 | 否。`curl_cffi 0.16.3`、`numpy`、`pillow` 在 pypi 均有 cp314 与 cp314t manylinux wheel（逐个查过） |
| Release 资产/版本号问题 | 否。1.7.29 代码已落盘、元数据正确、插件业务日志 10:33–10:35 正常 |
| MP 通用 bug（所有人都会踩） | 部分是。锁无超时是放大器，但触发条件是「有插件依赖在 3.14 上无 wheel」 |

## 处置

```bash
# 1. 确认 LexiAnnot 不在插件目录（已回滚，正常）
ls /opt/1panel/apps/moviepilot/config/plugins

# 2. 重启清掉进程内锁
docker restart moviepilot-v3

# 3. 重启后在市场重装 CloudSubscribeX（依赖都有 wheel，秒过）
```

长期：Python 3.14 镜像上不要装带 `spacy` / `thinc` / `torch` 这类重依赖的插件；`scripts/mp-patches/apply_uv_lock_timeout.py` 是护栏（180s 超时 + 持有者日志 + 900s 看门狗），不装也能用，装了下次卡住能立刻在日志里看到是谁。
