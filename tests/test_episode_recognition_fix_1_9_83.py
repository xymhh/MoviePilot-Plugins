"""1.9.83 集号识别口径 + 未播出守卫修复。

事故：玫瑰战争分享有 E14（`14 DV.mkv`），识别计划 ready 选中它，但转存阶段
`share_episodes` 只到 E13 → `missing={14} ∩ share_episodes` 为空 → 转存 0 个文件。
同时 TMDB 日期错位把库里已有的 E05 判为未播出、从订阅 note 剔除。

A: `utils/share_recognize.py` 暴露同源解析 `episode_from_name`；
   `handlers/sync/naming.py` 在部分文件认不出时补判（不再只在整批无集号时兜底）。

B: `handlers/sync/television.py` 媒体库已存在的集不再按 TMDB 日期判未播出。
"""
import ast
import importlib.util
import os
import sys
import types
import unittest

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
PKG = os.path.join(project_root, "plugins.v2", "cloudsubscribex")


def read(rel):
    with open(os.path.join(PKG, rel), encoding="utf-8") as handle:
        return handle.read()


def load_share_recognize():
    """只加载纯逻辑模块，绕过 utils/__init__ 的 app.* 依赖。"""
    pkg = types.ModuleType("u_fix")
    pkg.__path__ = []
    sys.modules["u_fix"] = pkg

    def load(name, path):
        spec = importlib.util.spec_from_file_location("u_fix." + name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules["u_fix." + name] = module
        spec.loader.exec_module(module)
        return module

    load("file_parser", os.path.join(PKG, "utils", "file_parser.py"))
    return load("share_recognize", os.path.join(PKG, "utils", "share_recognize.py"))


class TestEpisodeFromNameA(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sr = load_share_recognize()

    def test_14_dv_mkv_is_episode_14(self):
        """事故主角：开头数字 + 空格 + 画质词。"""
        self.assertEqual(self.sr.episode_from_name("14 DV.mkv", 1), 14)
        self.assertEqual(self.sr.classify_path("M 坆螝战爭/14 DV.mkv", 1_499_753_077)["ep"], 14)

    def test_plain_number_names(self):
        self.assertEqual(self.sr.episode_from_name("14.mkv", 1), 14)
        self.assertEqual(self.sr.episode_from_name("01.mkv", 1), 1)
        self.assertEqual(self.sr.episode_from_name("第14集.mkv", 1), 14)

    def test_years_and_resolutions_are_not_episodes(self):
        for name in ("2024.mkv", "1080p.mkv", "2160p.mkv", "720.mkv"):
            self.assertIsNone(
                self.sr.episode_from_name(name, 1),
                f"{name} 不应被当集号",
            )

    def test_agreement_with_recognition_plan(self):
        """转存阶段复用的口径必须与识别计划一致。"""
        names = ["01.mkv", "13.mkv", "14.mkv", "14 DV.mkv"]
        for name in names:
            plan_ep = self.sr.classify_path(name, 1_000_000_000)["ep"]
            self.assertEqual(self.sr.episode_from_name(name, 1), plan_ep, f"{name} 口径不一致")

    def test_rescue_tail_episodes_still_only_tail_numbers(self):
        """rescue 的 3 集门槛保持不动（不降低）。"""
        self.assertEqual(
            self.sr.rescue_tail_episodes_from_names(["01.mkv", "02.mkv", "03.mkv"]), {1, 2, 3}
        )
        self.assertEqual(
            self.sr.rescue_tail_episodes_from_names(["01.mkv"]), set()
        )


class TestASummarizeWiring(unittest.TestCase):
    def setUp(self):
        self.source = read("handlers/sync/naming.py")

    def test_imports_episode_from_name(self):
        self.assertIn("from ...utils.share_recognize import episode_from_name", self.source)

    def test_partial_unresolved_is_recovered(self):
        """关键：episodes 非空时也要补判 unresolved（原来的 if not episodes 只在整批无集号时兜底）。"""
        self.assertIn("elif unresolved:", self.source)
        self.assertIn("episode_from_name(name, season)", self.source)
        self.assertIn("episodes.add(episode)", self.source)

    def test_rescue_threshold_untouched(self):
        self.assertIn("rescue_tail_episodes_from_names(unresolved)", self.source)


class TestShareEpisodesInclude14(unittest.TestCase):
    """行为级：按 naming.py 的新逻辑，喂真实分享文件树（13 集 + 14 DV.mkv），
    FileMatcher 认不出 `14 DV.mkv`，补判后 share_episodes 必须含 14，交集才有 E14 可转。
    """

    @classmethod
    def setUpClass(cls):
        cls.sr = load_share_recognize()

    def test_real_share_tree_yields_e14(self):
        # 模拟 _summarize_share_episodes：FileMatcher 已认出 01..13.mkv → 1..13，
        # `14 DV.mkv` 认不出 → unresolved。用新的补判分支（naming.py 的 elif unresolved）。
        names = [f"{i:02d}.mkv" for i in range(1, 14)] + ["14 DV.mkv"]
        episodes = set(range(1, 14))
        unresolved = ["14 DV.mkv"]  # FileMatcher 认不出的那一个
        self.assertEqual(len(names), 14)
        # 事故前：episodes 非空 → 不进 rescue → E14 丢失
        before = set(episodes)
        self.assertNotIn(14, before, "事故复现前提：E14 应缺失")
        # 修复后：对 unresolved 用同源解析补判
        for name in unresolved:
            episode = self.sr.episode_from_name(name, 1)
            if episode is not None:
                episodes.add(episode)
        self.assertIn(14, episodes, "补判后必须含 E14")
        self.assertEqual(sorted(episodes), list(range(1, 15)))
        missing_episode_set = {14}
        matched = missing_episode_set & episodes
        self.assertEqual(matched, {14}, "交集必须算出 E14，才能转存第 14 集")

    def test_intersection_would_be_empty_without_fix(self):
        self.assertEqual({14} & set(range(1, 14)), set())


class TestRealMethodBody(unittest.TestCase):
    """直接用 AST 抽出 naming.py 的 `_summarize_share_episodes` 真实函数体执行，
    注入可控的 FileMatcher / MediaFileParser，验证修复后 E14 不再丢。
    变异反证：把补判调用替换成 None 必须让本测试挂掉。
    """

    @classmethod
    def setUpClass(cls):
        cls.sr = load_share_recognize()
        source = read("handlers/sync/naming.py")
        tree = ast.parse(source)
        fn = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "_summarize_share_episodes"
        )
        module = ast.Module(body=[fn], type_ignores=[])
        ast.fix_missing_locations(module)

        class FakeFileMatcher:
            @staticmethod
            def episode_from_file(item, season, mediainfo):
                name = str((item or {}).get("name") or "")
                # 模拟真实行为：01..13.mkv 认得出，14 DV.mkv 认不出。
                stem = name.rsplit(".", 1)[0] if "." in name else name
                return int(stem) if stem.isdigit() and 1 <= int(stem) <= 13 else None

        class FakeParser:
            @staticmethod
            def is_video(name):
                return str(name or "").lower().endswith((".mkv", ".mp4"))

        import typing

        namespace = {
            "Set": typing.Set,
            "Tuple": typing.Tuple,
            "List": typing.List,
            "Dict": typing.Dict,
            "Optional": typing.Optional,
            "MediaInfo": typing.Any,
            "MediaFileParser": FakeParser,
            "FileMatcher": FakeFileMatcher,
            "rescue_tail_episodes_from_names": cls.sr.rescue_tail_episodes_from_names,
            "episode_from_name": cls.sr.episode_from_name,
        }
        exec(compile(module, "<naming._summarize_share_episodes>", "exec"), namespace)
        cls.fn = namespace["_summarize_share_episodes"]

    def test_e14_recovered_by_real_body(self):
        files = [
            {"name": f"{i:02d}.mkv"} for i in range(1, 14)
        ] + [{"name": "14 DV.mkv"}]
        video_count, episodes = self.fn(files, 1, None)
        self.assertEqual(video_count, 14)
        self.assertIn(14, episodes, "真实函数体必须把 E14 补进 share_episodes")

    def test_year_file_not_false_positive(self):
        files = [{"name": f"{i:02d}.mkv"} for i in range(1, 4)] + [{"name": "2024.mkv"}]
        _, episodes = self.fn(files, 1, None)
        self.assertNotIn(2024, episodes)
        self.assertEqual(episodes, {1, 2, 3})


class TestBAiredInLibrary(unittest.TestCase):
    def setUp(self):
        self.source = read("handlers/sync/television.py")
        self.tree = ast.parse(self.source)

    def test_library_episodes_override_unaired(self):
        """AST 断言真实赋值语句，避免注释里的字符串造成假通过。"""
        found = False
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.AugAssign):
                continue
            target = node.target
            if isinstance(target, ast.Name) and target.id == "unaired_episodes":
                if isinstance(node.op, ast.Sub):
                    found = True
                    self.assertIn(
                        "library_episodes",
                        ast.unparse(node.value),
                        "必须从 unaired_episodes 减去媒体库实际集",
                    )
        self.assertTrue(found, "未播出守卫缺少「库里已存在的集放行」语句")
        self.assertIn("set(media_server_episodes) & expected_episodes", self.source)

    def test_guard_still_requires_valid_media_server(self):
        self.assertIn("if unaired_episodes and media_server_valid and media_server_episodes:", self.source)

    def test_guyu_protection_comment_kept(self):
        self.assertIn("谷雨事故防护不变", self.source)


if __name__ == "__main__":
    unittest.main()
