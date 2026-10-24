# MoviePilot 运维补丁脚本（非插件代码）

这些脚本改的是 **MoviePilot 容器内的 `/app`**，与插件本身无关，不进插件 Release。

## apply_uv_lock_timeout.py

**治什么**：插件市场点「更新」后永久「正在更新」，日志停在
`package.py - <插件> 存在依赖，开始尝试安装依赖` 之后零输出。

**根因**：`app/adapters/system/plugin/health.py` 里抢全局依赖安装锁是
`while not _package_install_lock.acquire(blocking=False): await asyncio.sleep(0.01)` ——
无超时、无日志。持锁线程一旦卡死（uv 子进程不可中断 / 线程池占满），
之后**所有**插件的依赖安装都永久静默排队。

**补丁**：抢锁加 180s 超时（超时返回失败而不是死等）、等待期间每 15s 打 INFO、
登记锁持有者（哪个插件的 requirements）、900s 看门狗强制释放、
`[UV] 尝试使用策略` 日志 debug→info。

**用法**（在 MP 宿主机执行）：

```bash
curl -fsSL -o /tmp/apply_uv.py   https://raw.githubusercontent.com/xymhh/MoviePilot-Plugins/main/scripts/mp-patches/apply_uv_lock_timeout.py

# 1. 先试跑，只读不改
docker exec -i moviepilot-v3 python3 - --dry-run < /tmp/apply_uv.py

# 2. 应用（幂等，锚点缺失会自动放弃并打印实际片段）
docker exec -i moviepilot-v3 python3 - < /tmp/apply_uv.py

# 3. 生效
docker restart moviepilot-v3
```

镜像更新会覆盖 `/app`，补丁需重打。
