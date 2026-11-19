"""v1.25.0 Issue B1/B2/B3：频道监控加载态 + 统计卡 + 渠道卡片摘要。

TgMonitor.vue 只做静态断言（与 test_frontend_split_1_24_0.py 同款做法；
行为由 vite 构建 + 全量套件兜底）。

channelLine 是纯函数：从 ChannelManager.vue 里把它与三个辅助函数
抽出来，在 node 里真跑四类输入（布尔/对象/正则/长串）断言输出。
"""
import json
import os
import subprocess
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TG_MONITOR = os.path.join(
    REPO_ROOT, "frontend", "cloudsubscribex", "src",
    "components", "dashboard", "TgMonitor.vue",
)
CHANNEL_MANAGER = os.path.join(
    REPO_ROOT, "frontend", "cloudsubscribex", "src",
    "components", "dashboard", "ChannelManager.vue",
)


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


class TgMonitorLoadingTests(unittest.TestCase):
    def test_initial_loading_state_exists(self):
        """B1：首次加载状态存在，默认 true，只覆盖首次加载。"""
        source = _read(TG_MONITOR)
        self.assertIn("const initialLoading = ref(true)", source)

    def test_loading_hides_not_enabled_hint_and_zero_cards(self):
        """B1：加载中不显示未启用提示与统计卡，显示骨架占位。"""
        source = _read(TG_MONITOR)
        self.assertIn('v-if="!status.enabled && !initialLoading"', source)
        self.assertIn('<v-skeleton-loader v-if="initialLoading"', source)
        self.assertIn('<div v-if="!initialLoading" class="tg-stats', source)

    def test_loading_badge(self):
        """B1：加载中徽标显示“加载中”，不是默认的“未启用”。"""
        source = _read(TG_MONITOR)
        self.assertIn('initialLoading ? "加载中"', source)
        self.assertIn(".status-badge-loading", source)

    def test_loading_failure_exits_loading(self):
        """B1：加载失败也要退出加载态（finally 置 false，refreshAll 吞异常）。"""
        source = _read(TG_MONITOR)
        self.assertIn("initialLoading.value = false", source)
        self.assertIn("} finally {", source)

    def test_ignored_and_in_flight_cards(self):
        """B3：统计卡含已忽略与进行中两张，口径与 FILTERS / 后端状态一致。"""
        source = _read(TG_MONITOR)
        self.assertIn('"ignored", title: "已忽略", tone: "muted"', source)
        self.assertIn('i.status === "ignored"', source)
        self.assertIn('"in-flight",', source)
        self.assertIn('["transferring", "processing"].includes(i.status)', source)
        self.assertIn('{ value: "in-flight", title: "进行中"', source)

    def test_failed_counted_in_todo_note(self):
        """B3：统计卡下方注明“失败已计入待处理”。"""
        source = _read(TG_MONITOR)
        self.assertIn("失败已计入待处理", source)


def _extract_channel_line(source):
    """从 ChannelManager.vue 抽出 channelLine 及其辅助函数（花括号配对）。"""
    start = source.index("const _CHANNEL_LINE_SKIP_TYPES")
    end_marker = "function channelLine(item) {"
    fn_start = source.index(end_marker)
    depth = 0
    index = source.index("{", fn_start)
    while True:
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
        index += 1


class ChannelLineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = _read(CHANNEL_MANAGER)
        helpers = _extract_channel_line(source)
        harness = """
const SCHEMAS = {
  mikan: { groups: [{ fields: [
    { key: "mikan_base_url", type: "text" },
    { key: "mikan_fansub_order", type: "text" },
    { key: "mikan_exclude_re", type: "text" },
    { key: "mikan_result_limit", type: "number" },
  ] }] },
  pansou: { groups: [{ fields: [
    { key: "pansou_url", type: "text" },
    { key: "pansou_auth_enabled", type: "switch" },
    { key: "pansou_username", type: "text" },
  ] }] },
};
function schemaFor(type) { return SCHEMAS[type]; }
""" + helpers + """
const cases = JSON.parse(process.argv[2]);
for (const item of cases) {
    console.log(JSON.stringify(channelLine(item)));
}
"""
        cls._temp = tempfile.NamedTemporaryFile(
            "w", suffix=".mjs", delete=False, encoding="utf-8",
        )
        cls._temp.write(harness)
        cls._temp.close()

    @classmethod
    def tearDownClass(cls):
        os.unlink(cls._temp.name)

    def _run(self, cases):
        proc = subprocess.run(
            ["node", self._temp.name, json.dumps(cases)],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return [json.loads(line) for line in proc.stdout.splitlines()]

    def test_boolean_false_not_shown(self):
        """B2：布尔 false 不再显示成 "false"，优先取后面的地址。"""
        (result,) = self._run([
            {"type": "pansou", "config": {
                "pansou_auth_enabled": False, "pansou_url": "http://pansou:80",
            }},
        ])
        self.assertEqual(result, "http://pansou:80")

    def test_boolean_only_is_unconfigured(self):
        """B2：只有布尔值可取时显示“未配置”，不显示 "false"/"true"。"""
        (result,) = self._run([
            {"type": "pansou", "config": {"pansou_auth_enabled": False}},
        ])
        self.assertEqual(result, "未配置")

    def test_object_array_shows_doc_count(self):
        """B2：在线文档的对象数组不再显示 "[object Object]"，显示文档数。"""
        results = self._run([
            {"type": "online_docs", "config": {"online_docs": [
                {"url": "https://a.example/doc", "resource_types": ["115"]},
                {"url": "https://b.example/doc", "resource_types": ["quark"]},
            ]}},
            {"type": "online_docs", "config": {"online_docs": []}},
        ])
        self.assertEqual(results, ["2 个文档", "未配置"])

    def test_regex_skipped_in_favor_of_scalar(self):
        """B2：正则（_re 字段）跳过，取字幕组偏好；长过滤词串不截断显示。"""
        (result,) = self._run([
            {"type": "mikan", "config": {
                "mikan_base_url": "",
                "mikan_fansub_order": ["LoliHouse", "喵萌奶茶屋"],
                "mikan_exclude_re": "生肉|无字幕|合集|1080p|4K|HDR|杜比|内封|外挂|特效|修正|v2|v3|END",
            }},
        ])
        self.assertEqual(result, "LoliHouse, 喵萌奶茶屋")

    def test_long_string_truncated(self):
        """B2：超长标量仍截断 60 字 + 省略号。"""
        long_text = "关" * 100
        (result,) = self._run([
            {"type": "mikan", "config": {"mikan_base_url": long_text}},
        ])
        self.assertEqual(result, "关" * 60 + "…")

    def test_channel_line_still_feeds_search(self):
        """B2：channelLine 仍是 filtered 搜索的 haystack 组成部分。"""
        source = _read(CHANNEL_MANAGER)
        self.assertIn("channelLine(item),", source)


if __name__ == "__main__":
    unittest.main()
