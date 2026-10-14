# 复盘：清理插件缓存报 AnimeGardenSearchService 无 _lock（1.6.38）

> 任务书：[../plan/2026-10-14-清理插件缓存AnimeGarden.md](../plan/2026-10-14-清理插件缓存AnimeGarden.md)
> 证据：[../test/2026-10-14-AnimeGarden清缓存回归单测.md](../test/2026-10-14-AnimeGarden清缓存回归单测.md)

## 结论

AnimeGarden 搜索服务从「自建字典缓存」换成「平台 TTL 缓存 `_search_cache`」时，
`clear_cache()` 没跟着改，仍访问早已不存在的 `self._lock` / `self._cache`。
用户点「清理插件缓存」→ animegarden 渠道抛 `AttributeError` → 异常冒到
`core/api/runtime.py` 的唯一 except，**整轮清缓存中断**（首页搜索结果缓存与其它渠道缓存都没清）。

1.6.38 把 `clear_cache()` 改为按平台缓存 API 统计并清空（`items()` / `clear()`，返回清理条数），
并补 4 例回归单测（旧代码上必失败）。

## 教训

1. **换存储要连带扫一遍 `clear` / `close` / `reset` 这类"低频出口"。**
   检索路径（读+写）有测试与线上验证，反射式兜底方法没有——重构时最容易漏，
   漏了也只在用户点按钮时才炸。
2. **一个渠道坏了不该让整轮失败。** `runtime.py` 把 5 个分类 + N 个渠道的清理塞进同一个
   try/except，一个渠道的 `AttributeError` 直接吞掉整轮清理，日志只有一句「清理插件缓存失败」，
   既没说清哪个渠道、也没说清哪些缓存其实清了。本次按「最小改动」没动它，
   但**建议后续按渠道 try/except 并回传失败清单**（属于独立加固项，见任务书 §4）。
3. **"测试全绿"不覆盖没被测的方法名。** 该类没有单测，所以这处悬空属性从合并
   （`1ceedbc4`）一路活到线上；现在这 4 例把 provider → service 的清缓存链路钉住了。
4. **错误信息本身要能指路。** 线上那句 `'AnimeGardenSearchService' object has no attribute '_lock'`
   恰好把类名与缺失属性都写出来了，定位一次成功——保留原始异常文本比"友好化"更有用。
5. **本地跑不全全量时要说清是哪 2 例、为什么。** 本机 Python 3.11 装不了 `p115client`（要 ≥3.12），
   2 例 error 由此而来；把它写进证据文档，避免下次误判成回归。
