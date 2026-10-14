# AnimeGarden 清缓存回归单测（1.6.38）

> 任务书：[../plan/2026-10-14-清理插件缓存AnimeGarden.md](../plan/2026-10-14-清理插件缓存AnimeGarden.md)
> 环境：本容器 Python 3.11 + 仓库全量 `python -m unittest discover -s tests`

## 1. 新增用例（4 例）

文件：`tests/test_animegarden_cache_clear.py`
（加载**真实** `core/search.py`（SearchProvider/SearchCapability）、
`search/animegarden/service.py`、`search/animegarden/provider.py`；
只对 `app.*`、`utils.cache`、`magnet/matching/subs_filter/client` 做替身——
`utils.cache` 用按 region 共享存储的 `FakePlatformTTLCache` 复刻平台 TTL 缓存用到的部分）

| # | 用例 | 断言 |
| --- | --- | --- |
| 1 | `test_clear_cache_counts_and_empties_entries` | 两个关键词 → 缓存 2 条（第二次搜不重复请求）；`clear_cache()` 返回 2、缓存清空；清完再搜重新请求（请求数 2 → 3） |
| 2 | `test_clear_cache_on_empty_cache_returns_zero` | 空缓存返回 0，不抛异常 |
| 3 | `test_clear_cache_is_isolated_per_client` | 两个 base_url 的缓存区互不影响（清 A 后 B 仍有 1 条） |
| 4 | `test_provider_cache_maintenance_path` | 真实 `SearchProvider` 能力表里含 CACHE_MAINTENANCE；`provider.clear_cache()` 返回 1，二次调用返回 0（对齐线上 `runtime.py` → `search handler` → provider 的清理链路） |

## 2. 反证：用例在旧代码上必失败

把 `service.py` 的 `clear_cache` 临时改回旧实现（`with self._lock:` / `self._cache`）后重跑：

```
FAILED (errors=4)
File ".../core/search.py", line 327, in clear_cache
    return self.require(SearchCapability.CACHE_MAINTENANCE).clear_cache()
File ".../search/animegarden/service.py", line 198, in clear_cache
    with self._lock:
AttributeError: 'AnimeGardenSearchService' object has no attribute '_lock'
```

报错原文与线上日志（`media/ee1752281034.log`）逐字一致，4 例全 ERROR；随后已恢复修复版实现。

## 3. 全量结果

| 运行 | 结果 |
| --- | --- |
| 修复后 + 新用例 | `Ran 273 tests`，271 passed，**2 errors** |
| 基线（临时移走本用例文件） | `Ran 269 tests`，同样 **2 errors** |
| 修复后排除 `test_checkin_days.py` | `Ran 260 tests` — **OK** |

那 2 例 error 全部来自 `tests/test_checkin_days.py`，是**本机环境缺依赖**，与本次改动无关：

```
P115CheckinError: p115client 未安装，无法执行 115 签到
```

原因是 `plugins.v2/cloudsubscribe/requirements.txt` 的 `p115client>=0.0.9.6.5.1` 要求
Python ≥ 3.12，本容器只有 3.11（pip 明确报 "Requires-Python <4.0,>=3.12"）；
CI（`test-cloudsubscribe.yml`）用 Python 3.12 并 `pip install -r requirements.txt`，这条线上是绿的。

## 4. 静态检查

```
python scripts/lint.py plugins.v2/cloudsubscribe/search/animegarden/service.py \
                       tests/test_animegarden_cache_clear.py
Checked 2 file(s), 0 error(s), 0 warning(s)
```

## 5. 顺带的全仓复核

AST 扫描 `plugins.v2/cloudsubscribe` 下所有类，取「读过的 `self.*` 私有名 − 写过的 − 方法名」差集：
实例级悬空属性只有 `AnimeGardenSearchService -> ['_cache', '_lock']` 一处
（其它命中项均为类常量如 `_HEADERS`，或 OwnerDelegator 委托给 owner 的属性）。

## 6. CI / Release

待推送后补录（本地单测、反证、lint 已全绿）。
