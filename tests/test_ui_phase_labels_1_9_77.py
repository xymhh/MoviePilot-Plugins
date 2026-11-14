"""1.9.77 下载中 UI 显示修复：英文 phase 中文化 + 进度体积易读化。

纯静态源码断言；JS 实际输出另由 Node 直接 import 验证（见 docs/test 验收记录）。

1.9.86：RuntimeTaskStrip.vue 已随「任务进度改走下载管理页」删除，
test_sub_text_uses_format_size（浮条专属的 bytesText 易读体积断言）一并退役；
PHASE_LABELS / taskPhaseText / formatSize 仍由 taskUtils.js 提供并被 MediaHub 使用，
其余断言保留。
"""
import os
import unittest

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)

TASK_UTILS = "frontend/cloudsubscribex/src/utils/taskUtils.js"
INIT = "plugins.v2/cloudsubscribex/__init__.py"
PACKAGE = "package.v2.json"


def read(rel):
    with open(os.path.join(project_root, rel), encoding="utf-8") as handle:
        return handle.read()


EXPECTED_LABELS = {
    "downloading": "下载中",
    "uploading": "上传中",
    "streaming": "边下边传",
    "rapid_upload": "秒传中",
    "hashing": "校验中",
    "cache_ready": "缓存就绪",
    "pending": "等待中",
    "stopping": "停止中",
    "done": "已完成",
    "aborted": "已中止",
    "canceled": "已取消",
    "failed": "失败",
}


class TestPhaseLabels(unittest.TestCase):
    def setUp(self):
        self.utils = read(TASK_UTILS)
        self.init = read(INIT)
        self.package = read(PACKAGE)

    def test_phase_mapping_table(self):
        self.assertIn("const PHASE_LABELS = {", self.utils)
        for phase, label in EXPECTED_LABELS.items():
            self.assertIn(f'{phase}: "{label}"', self.utils)

    def test_task_phase_text_uses_mapping(self):
        self.assertIn("return PHASE_LABELS[phase] || phase", self.utils)
        # 中文/未知 phase 仍原样返回，error 仍优先
        self.assertIn('phase !== "等待调度"', self.utils)
        self.assertIn("if (task.error) return task.error", self.utils)

    def test_format_size_reused_not_rewritten(self):
        self.assertIn("export function formatSize(value)", self.utils)

    def test_version_not_below_1_9_77(self):
        """1.9.77 起版本只往上走，这里只校验不低于本版。"""
        import re

        match = re.search(r'plugin_version = "(\d+)\.(\d+)\.(\d+)"', self.init)
        self.assertIsNotNone(match, "未找到 plugin_version")
        self.assertGreaterEqual(
            tuple(int(part) for part in match.groups()), (1, 9, 77)
        )
        self.assertIn('"v1.9.77":', self.package)


if __name__ == "__main__":
    unittest.main()
