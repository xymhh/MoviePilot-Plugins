"""1.9.81 任务列表 UI 增强：耗时 / 搜索渠道 / 后处理分步。

纯前端改动，后端零修改。测试用 Node 实跑 taskUtils 的新函数，
再用静态断言确认卡片模板真的渲染了这些字段。
"""
import json
import os
import shutil
import subprocess
import unittest

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
FRONTEND = os.path.join(project_root, "frontend", "cloudsubscribex")
UTILS = os.path.join(FRONTEND, "src", "utils", "taskUtils.js")
STRIP = os.path.join(FRONTEND, "src", "components", "RuntimeTaskStrip.vue")


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def run_node(expr: str):
    """在 frontend 目录里跑一段 ESM，返回 stdout。"""
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("node 不可用，跳过 JS 行为断言")
    script = (
        "import * as u from './src/utils/taskUtils.js';\n"
        f"const out = (() => {{ {expr} }})();\n"
        "console.log(JSON.stringify(out));\n"
    )
    result = subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=FRONTEND, capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        raise AssertionError(f"node 执行失败: {result.stderr[:600]}")
    return json.loads(result.stdout.strip().splitlines()[-1])


class TestDurationFormatting(unittest.TestCase):
    def test_format_duration_buckets(self):
        values = run_node(
            "return [0, 5, 9.5, 65, 3661, -1, null, 'abc'].map((v) => u.formatDuration(v))"
        )
        self.assertEqual(values, ["", "5.0s", "9.5s", "1m 5s", "1h 1m", "", "", ""])

    def test_format_channel_elapsed(self):
        values = run_node(
            "return [0, 500, 1500, 65000, null].map((v) => u.formatChannelElapsed(v))"
        )
        self.assertEqual(values, ["", "500ms", "1.5s", "65.0s", ""])


class TestChannelHelpers(unittest.TestCase):
    def test_channel_label_fallbacks(self):
        values = run_node(
            "return [u.channelLabel({key:'pansou'}), u.channelLabel({key:'unknown_x'}),"
            " u.channelLabel({}), u.channelLabel(''), u.channelLabel(null),"
            " u.channelLabel({key:'x', name:'自定义'})]"
        )
        self.assertEqual(
            values, ["盘搜", "UNKNOWN_X", "", "", "", "自定义"],
            "空对象必须返回空串，不能出现 [object Object]",
        )

    def test_channel_label_no_object_string(self):
        """回归：旧实现 String({}) 会得到 "[object Object]" 显示在界面上。"""
        values = run_node(
            "return [u.channelLabel({}), u.channelLabel({name:'只有名字'}),"
            " u.channelLabel(undefined), u.channelLabel(0)]"
        )
        for value in values:
            self.assertNotIn(
                "[object Object]", value,
                f"渠道名不得出现 [object Object]，实际 {value!r}",
            )
        self.assertEqual(values, ["", "", "", ""])

    def test_channel_icon_and_color_by_status(self):
        icons = run_node(
            "return ['success','searching','failed','timeout','circuit_break','pending']"
            ".map((s) => u.channelIcon({status:s, count:1}))"
        )
        self.assertEqual(icons, [
            "mdi-check-circle", "mdi-magnify", "mdi-alert-circle-outline",
            "mdi-clock-alert-outline", "mdi-flash-off", "mdi-clock-outline",
        ])
        colors = run_node(
            "return ['success','searching','failed','timeout'].map((s) => u.channelColor({status:s, count:0}))"
        )
        self.assertEqual(colors, ["secondary", "info", "error", "warning"])

    def test_channel_tooltip_includes_count_and_error(self):
        tip = run_node(
            "return u.channelTooltip({key:'pansou', status:'success', count:12, elapsed_ms:1500})"
        )
        self.assertIn("盘搜", tip)
        self.assertIn("12", tip)
        self.assertIn("1.5s", tip)
        err_tip = run_node(
            "return u.channelTooltip({key:'seedhub', status:'failed', error:'超时 30s'})"
        )
        self.assertIn("失败", err_tip)
        self.assertIn("超时 30s", err_tip)

    def test_search_channels_guards_bad_payload(self):
        self.assertEqual(run_node("return u.searchChannelsOf(null)"), [])
        self.assertEqual(run_node("return u.searchChannelsOf({search_channels:'x'})"), [])
        kept = run_node(
            "return u.searchChannelsOf({search_channels:[{key:'a'},null,{name:'x'}]})"
        )
        self.assertEqual(kept, [{"key": "a"}], "只保留有 key 的渠道条目")


class TestPostprocessSteps(unittest.TestCase):
    def test_steps_filter_requires_key_and_label(self):
        steps = run_node(
            "return u.postprocessStepsOf({postprocess_steps:"
            "[{key:'a',label:'A'},{key:'b'},{label:'C'}]})"
        )
        self.assertEqual(steps, [{"key": "a", "label": "A"}])

    def test_step_state_progression(self):
        states = run_node(
            "const t = {postprocess_step_index:1, postprocess_step:'b'};"
            "return [0,1,2].map((i) => u.postprocessStepState(t, i))"
        )
        self.assertEqual(states, ["completed", "active", "pending"])
        icons = run_node(
            "const t = {postprocess_step_index:1, postprocess_step:'b'};"
            "return [0,1,2].map((i) => u.postprocessStepIcon(t, i))"
        )
        self.assertEqual(icons, ["mdi-check-circle", "mdi-progress-clock", "mdi-circle-outline"])


class TestElapsedSeconds(unittest.TestCase):
    def test_explicit_field_wins(self):
        self.assertEqual(run_node("return u.taskElapsedSeconds({elapsed_seconds:42})"), 42)

    def test_started_at_is_seconds_not_ms(self):
        """后端 started_at 是秒级 time.time()，当毫秒用会算出 0。"""
        value = run_node(
            "return u.taskElapsedSeconds({started_at: 1000}, 3700000)"
        )
        self.assertEqual(value, 2700, "started_at=1000s, now=3700s 应得 2700 秒")

    def test_missing_start_returns_zero(self):
        self.assertEqual(run_node("return u.taskElapsedSeconds({})"), 0)


class TestStripRemoved(unittest.TestCase):
    """1.9.86：任务进度脱离自有 UI，改走下载管理页独立标签。

    RuntimeTaskStrip / useRuntimeTasks 已删除；两处挂载点不再引用它，
    实时进度改由后端 list_torrents 投影到下载页。
    """

    def test_strip_component_and_hook_deleted(self):
        self.assertFalse(os.path.exists(STRIP), "RuntimeTaskStrip.vue 应已删除")
        self.assertFalse(
            os.path.exists(
                os.path.join(FRONTEND, "src", "composables", "useRuntimeTasks.js")
            ),
            "useRuntimeTasks.js 应已删除",
        )

    def test_no_mount_points_reference_strip(self):
        for name in ("AppPage.vue", "AppPageResource.vue"):
            page = read(os.path.join(FRONTEND, "src", "components", name))
            self.assertNotIn("RuntimeTaskStrip", page, f"{name} 不应再挂载任务条")
            self.assertNotIn("stopRuntimeTask", page, f"{name} 不应再有条带停止函数")

    def test_downloader_projection_replaces_strip(self):
        backend = read(
            os.path.join(
                project_root, "plugins.v2", "cloudsubscribex", "__init__.py"
            )
        )
        for token in (
            "def list_torrents",
            "def stop_torrents",
            "def remove_torrents",
            "def torrent_files",
            "_ensure_downloader_entry",
            "_remove_downloader_entry",
        ):
            self.assertIn(token, backend, f"后端缺少下载器投影方法：{token}")


if __name__ == "__main__":
    unittest.main()
