import os
import re
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel_path: str) -> str:
    with open(os.path.join(REPO_ROOT, rel_path), encoding="utf-8") as f:
        return f.read()


class TestOverviewServerSideFilter(unittest.TestCase):
    def test_stat_select_pushes_filter_to_server(self):
        source = _read(
            "frontend/cloudsubscribex/src/components/PluginWorkbench.vue"
        )
        handle = re.search(
            r"function handleStatSelect\(stat\) \{(.*?)\n\}",
            source,
            re.S,
        )
        self.assertIsNotNone(handle)
        body = handle.group(1)
        self.assertIn("overviewFilter.value = filter", body)
        self.assertIn("updateHistoryQuery", body)
        # v1.13.3：单个 status 字符串直传服务端，不再经 statuses 数组；
        # 秒传失败伪状态已删，失败就是失败，不再映射成两类。
        self.assertIn("status: filter", body)
        self.assertNotIn("overviewStatusesFor", source)
        self.assertNotIn("秒传失败", source)

    def test_overview_mount_queries_failed_on_server(self):
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
        self.assertIn('props.mode === "overview"', body)
        self.assertIn('updateHistoryQuery({status: "失败", page: 1})', body)
        # v1.13.3：首屏拉数走 loadSummary + updateHistoryQuery（其内部
        # 触发 loadPage），不再在 onMounted 里直接调 loadPage(false)。
        self.assertIn("loadSummary()", body)

    def test_overview_media_hub_hides_subscribe_chips(self):
        source = _read(
            "frontend/cloudsubscribex/src/components/PluginWorkbench.vue"
        )
        mount = re.search(
            r"<MediaHub(.*?) />",
            source,
            re.S,
        )
        self.assertIsNotNone(mount)
        attrs = mount.group(1)
        self.assertIn(':show-filter-chips="false"', attrs)
        self.assertIn(':empty-title="overviewEmptyTitle"', attrs)
        self.assertIn(':empty-hint="overviewEmptyHint"', attrs)
        self.assertIn('暂无失败记录', source)
        self.assertIn('失败的转存会出现在这里，可直接重试', source)


class TestMediaHubOverviewProps(unittest.TestCase):
    def test_filter_chips_gated_by_prop(self):
        source = _read(
            "frontend/cloudsubscribex/src/components/dashboard/MediaHub.vue"
        )
        self.assertIn(
            'showFilterChips: { type: Boolean, default: true }', source
        )
        self.assertIn('v-if="showFilterChips"', source)

    def test_release_strip_only_in_subscribe_mode(self):
        source = _read(
            "frontend/cloudsubscribex/src/components/dashboard/MediaHub.vue"
        )
        self.assertIn('v-if="isSubscribeMode && hasTvGroups"', source)

    def test_empty_state_uses_props(self):
        source = _read(
            "frontend/cloudsubscribex/src/components/dashboard/MediaHub.vue"
        )
        self.assertIn(
            'emptyTitle: { type: String, default: "暂无匹配的追更订阅" }',
            source,
        )
        self.assertIn("emptyHint", source)
        self.assertIn(
            '{{ searchKeyword.trim() ? "没有匹配的记录" : emptyTitle }}',
            source,
        )


if __name__ == "__main__":
    unittest.main()
