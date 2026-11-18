"""失败记录页的服务端状态筛选（原 1.10.5 总览筛选）。

v1.23.0（§11 前端收敛第 1 项）：总览模式已删——插件主页直接进失败
记录页，mode prop、StatsGrid、MediaHub、overviewGroups/overviewFilter
与 handleStatSelect 一并删除；首屏仍默认只看失败（服务端 status 查询），
筛选 chip（× 清除）经 query-change 回写 historyStatus。用户 2026-11-18
定夺：MediaHub.vue / MediaDrawer.vue / StatsGrid.vue 三个孤儿文件随
v1.23.0 一并删除（抽屉的重试/删除本就是 HistoryTable 的行操作）。
本文件只守删完之后仍成立的契约：首屏默认失败筛选、筛选 chip 同步。
"""

import os
import re
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel_path: str) -> str:
    with open(os.path.join(REPO_ROOT, rel_path), encoding="utf-8") as f:
        return f.read()


class TestHistoryServerSideFilter(unittest.TestCase):
    def test_history_mount_queries_failed_on_server(self):
        source = _read(
            "frontend/cloudsubscribex/src/components/PluginWorkbench.vue"
        )
        mounted = re.search(
            r"onMounted\(\(\) => \{(.*?)\n\}\)",
            source,
            re.S,
        )
        self.assertIsNotNone(mounted)
        body = mounted.group(1)
        self.assertIn('updateHistoryQuery({status: "失败", page: 1})', body)
        # v1.23.0：总览已删，首屏不再拉 history/summary（统计卡片已删），
        # 也不再按 mode 分支。
        self.assertNotIn("loadSummary", body)
        self.assertNotIn("props.mode", body)

    def test_overview_mode_deleted(self):
        source = _read(
            "frontend/cloudsubscribex/src/components/PluginWorkbench.vue"
        )
        for gone in (
            "handleStatSelect",
            "overviewFilter",
            "overviewGroups",
            "overviewEmptyTitle",
            "overviewEmptyHint",
            "overviewListTitle",
            "StatsGrid",
            "MediaHub",
        ):
            self.assertNotIn(gone, source)
        self.assertNotIn("props.mode", source)

    def test_status_chip_syncs_on_query_change(self):
        # 统计卡片删掉后，HistoryTable 的筛选 chip（× 清除筛选）是唯一
        # 的筛选入口；query-change 回来时必须同步 historyStatus，否则
        # chip 上仍显示旧筛选。
        source = _read(
            "frontend/cloudsubscribex/src/components/PluginWorkbench.vue"
        )
        self.assertIn('@query-change="onHistoryQueryChange"', source)
        handler = re.search(
            r"function onHistoryQueryChange\(query\) \{(.*?)\n\}",
            source,
            re.S,
        )
        self.assertIsNotNone(handler)
        body = handler.group(1)
        self.assertIn("historyStatus.value", body)
        self.assertIn("updateHistoryQuery", body)


class TestDeletedOrphanFiles(unittest.TestCase):
    def test_orphan_files_are_deleted(self):
        # 用户 2026-11-18 定夺：MediaHub / MediaDrawer / StatsGrid
        # 随 v1.23.0 一并删除（不再挂载，抽屉的重试/删除本就是
        # HistoryTable 的行操作）。
        for rel_path in (
            "frontend/cloudsubscribex/src/components/dashboard/MediaHub.vue",
            "frontend/cloudsubscribex/src/components/dashboard/MediaDrawer.vue",
            "frontend/cloudsubscribex/src/components/dashboard/StatsGrid.vue",
        ):
            self.assertFalse(
                os.path.exists(os.path.join(REPO_ROOT, rel_path)),
                f"孤儿文件 {rel_path} 应已删除",
            )


if __name__ == "__main__":
    unittest.main()
