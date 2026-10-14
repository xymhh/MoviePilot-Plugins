# 清理插件缓存报 AnimeGardenSearchService 无 _lock（1.6.38 实施任务书）

> 状态：已完成 2026-10-14 → [../summary/2026-10-14-清理插件缓存AnimeGarden.md](../summary/2026-10-14-清理插件缓存AnimeGarden.md)

## 1. 现象

用户在插件页面点「清理插件缓存」，宿主机日志报：

```
【ERROR】... runtime.py - 清理插件缓存失败：'AnimeGardenSearchService' object has no attribute '_lock'
```

（`media/ee1752281034.log` 中 2026-10-09 起共 5 次：13:43、17:23 ×3、20:25）

## 2. 影响面

`core/api/runtime.py` 的清理入口把 5 个分类（search / cloud / sync / interface / platform）与
所有搜索渠道的 `clear_cache()` 放在**同一个 try** 里，只有一条 `except` 打日志。
所以 animegarden 渠道一抛异常，**整轮清缓存中断**：

- 搜索结果缓存（`search_results`）与 mikan / pansu / pianku 等其它渠道缓存**没清**；
- 接口返回 `{"success": False}`，前端看不到任何缓存被清理。

## 3. 根因

`plugins.v2/cloudsubscribe/search/animegarden/service.py` 的 `clear_cache()` 是**重构遗留**：

```python
def clear_cache(self) -> int:
    with self._lock:            # ← 不存在
        count = len(self._cache)  # ← 不存在
        self._cache.clear()
        return count
```

该类现在用的是平台 TTL 缓存 `self._search_cache = create_platform_ttl_cache(...)`
（`__init__` 里创建，`_cached_search` 读写），自建字典缓存 `_cache` 与锁 `_lock`
早已不存在。对照参考：`search/mikan/service.py` 是真自建字典缓存，`self._cache` /
`self._lock` 在 `__init__` 里都有定义，所以 mikan 的 `clear_cache` 没问题。

全仓库复核（AST 扫描 `self.*` 读写差集）：实例级悬空属性**只有这一处**
（`AnimeGardenSearchService -> ['_cache', '_lock']`），其余命中项均为类常量或
OwnerDelegator 委托给 owner 的属性。

## 4. 改法（最小改动）

```python
def clear_cache(self) -> int:
    """清空关键词检索 TTL 缓存，返回清理前条目数。"""
    count = len(list(self._search_cache.items()))
    self._search_cache.clear()
    return count
```

- 用 `items()` 统计条数，与 `handlers/search/service.py:385` 清理搜索结果缓存的写法一致
  （同一平台缓存类型，线上已跑通，不引入新 API 依赖）；
- 不加新锁：写路径（`_cached_search`）本来也没有锁，平台缓存后端自带并发保护，
  只给 clear 加锁是假保护；
- 不改 `core/api/runtime.py` 的 try/except 粒度（一个渠道坏了不该让整轮失败）——
  属于额外加固，本次不做，避免扩大改动面。

## 5. 验收标准

1. `clear_cache()` 返回清理条数、真清空（清完再搜会重新请求）、空缓存返回 0；
2. 不同客户端（不同 base_url）的缓存区互不影响；
3. 走真实 `SearchProvider` 的 CACHE_MAINTENANCE 能力调用不抛异常；
4. 新增用例在旧代码上**必须失败**（用临时回退反证）；
5. 全量单测 + `scripts/lint.py` 通过；版本升到 1.6.38 并发布 Release。

## 6. 禁止事项

- 不重命名/不引入新的缓存抽象，不顺手重构 animegarden 检索链路；
- 不动 `docs/` 以外的历史文档，不改 mikan 等其它渠道（它们没有此缺陷）；
- 不把 `core/api/runtime.py` 的异常吞掉（那只会让同类缺陷再次静默）。
