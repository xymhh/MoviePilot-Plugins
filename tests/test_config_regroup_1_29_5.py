"""v1.29.5：设置页按任务重排（6 组 + 常用设置 20 项 + 搜索框 + 字段级高级折叠）。

分组表：~/workspace/figma-settings-redesign/GROUPING.md（227 项零丢失零新增）。
v1.29.7 阶段 1 在转存组新增 6 个漏斗键（funnel_*），总数 233；
v1.29.8 阶段 2 再加 funnel_phase2_enabled（TG 进漏斗），总数 234。
v1.29.9 阶段 3 再加 funnel_phase3_enabled（订阅与洗版进漏斗），总数 235。
v1.30.0 阶段 4：转存组删 organize_after_transfer、加 funnel_enabled
与 funnel_scan_dry_run（+1 净），网盘整理组删 organize_source_path
（转存组 -1）；转存组仍 74、总数仍 235。常用设置 20 项里两处退役键
由两个漏斗键顶替。
静态接线测试 + node 真实组装/搜索测试；不拼 UI 行为，行为由 vite 构建 +
全量套件保证。
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIELDS_JS = os.path.join(
    REPO_ROOT, "frontend", "cloudsubscribex", "src", "config", "fields.js"
)
SEARCH_JS = os.path.join(
    REPO_ROOT, "frontend", "cloudsubscribex", "src", "config", "search.js"
)
CONFIG_VUE = os.path.join(
    REPO_ROOT, "frontend", "cloudsubscribex", "src", "components", "Config.vue"
)
SIDEBAR_VUE = os.path.join(
    REPO_ROOT,
    "frontend",
    "cloudsubscribex",
    "src",
    "components",
    "config",
    "ConfigSidebar.vue",
)
SECTION_VUE = os.path.join(
    REPO_ROOT,
    "frontend",
    "cloudsubscribex",
    "src",
    "components",
    "config",
    "ConfigSection.vue",
)

TASK_COUNTS = {
    "tracking": 91,
    "transfer": 76,
    "playback": 18,
    "notify": 21,
    "channels": 24,
    "drive": 7,
}

TASK_TITLES = {
    "tracking": "追更",
    "transfer": "转存",
    "playback": "播放",
    "notify": "通知与签到",
    "channels": "渠道",
    "drive": "网盘与基础",
}


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _run_node(script):
    """跑一段 node 脚本，返回其最后一行 stdout 解析后的 JSON。"""
    with tempfile.NamedTemporaryFile(
        "w", suffix=".mjs", delete=False, encoding="utf-8"
    ) as tmp:
        tmp.write(script)
        tmp_path = tmp.name
    try:
        result = subprocess.run(
            ["node", tmp_path],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
    finally:
        os.unlink(tmp_path)
    if result.returncode != 0:
        raise AssertionError(f"node 脚本失败：{result.stderr}")
    return json.loads(result.stdout.strip().splitlines()[-1])


def _sections_script(extra):
    return (
        "import {createConfigSections} from %r;\n"
        "import {filterConfigSections, buildSearchResultSection,"
        " matchesConfigSearch} from %r;\n"
        "const options = new Proxy({cloud_drive: \"\"}, "
        "{ get: (t, k) => (k in t ? t[k] : []) });\n"
        "const sections = createConfigSections(options, {});\n"
        + extra
    ) % (FIELDS_JS, SEARCH_JS)


class TestTaskGroupLayout(unittest.TestCase):
    def test_six_task_groups_with_exact_values_and_titles(self):
        source = _read(FIELDS_JS)
        layout = re.search(
            r"const TASK_LAYOUT = \[(.*?)\n\];", source, re.S
        )
        self.assertIsNotNone(layout, "缺少 TASK_LAYOUT")
        body = layout.group(1)
        self.assertEqual(len(re.findall(r'value: "', body)), 6)
        for value, title in TASK_TITLES.items():
            self.assertIn(f'value: "{value}"', body)
            self.assertIn(f'title: "{title}"', body)

    def test_split_sources_list_keys_without_duplicates(self):
        # 跨组拆分的源（basic / notify）按 key 挑字段：同一源内各任务组
        # 的 key 列表无重叠、无重复，合起来等于该源的全部静态 key。
        source = _read(FIELDS_JS)
        basic_keys = []
        notify_keys = []
        for match in re.finditer(
            r'\{\s*from: "(basic|notify)",\s*keys: \[(.*?)\],', source, re.S
        ):
            keys = re.findall(r'"([a-z_0-9]+)"', match.group(2))
            if match.group(1) == "basic":
                basic_keys.extend(keys)
            else:
                notify_keys.extend(keys)
        self.assertEqual(len(basic_keys), len(set(basic_keys)))
        self.assertEqual(len(notify_keys), len(set(notify_keys)))
        self.assertEqual(len(basic_keys), 20)
        self.assertEqual(len(notify_keys), 21)

    def test_no_section_level_advanced_flag(self):
        # v1.29.5：折叠是字段级，不再有 section 级 advanced 标记。
        source = _read(FIELDS_JS)
        self.assertNotIn("advanced: ADVANCED", source)
        self.assertIn("ADVANCED_FIELD_ORIGINS", source)
        self.assertIn("advanced: true", source)


class TestCommon20(unittest.TestCase):
    def test_common_has_20_keys_in_grouping_order(self):
        config = _read(CONFIG_VUE)
        keys_block = re.search(
            r"const COMMON_FIELD_KEYS = Object\.freeze\(\[(.*?)\]\)",
            config,
            re.S,
        )
        self.assertIsNotNone(keys_block, "缺少 COMMON_FIELD_KEYS")
        found = re.findall(r'"([a-z_]+)"', keys_block.group(1))
        self.assertEqual(
            found,
            [
                "enabled",
                "cloud_drive",
                "cron",
                "platform_download_policy",
                "cross_transfer_enabled",
                "funnel_enabled",
                "funnel_scan_dry_run",
                "enable_cloud_upgrade",
                "tg_monitor_enabled",
                "strm_generate_enabled",
                "notify",
                "auto_subscribe_enabled",
                "media_server_refresh_enabled",
                "emby_proxy_enabled",
                "search_concurrency",
                "cross_transfer_daily_upload_limit_gb",
                "transfer_risk_cooldown",
                "upgrade_episode_limit",
                "checkin_auto_retry",
                "batch_size",
            ],
        )

    def test_common_groups_cover_all_20_keys(self):
        config = _read(CONFIG_VUE)
        picks = re.findall(r"pick\(\[(.*?)\]\)", config, re.S)
        covered = []
        for block in picks:
            covered.extend(re.findall(r'"([a-z_]+)"', block))
        self.assertEqual(len(covered), 20)
        self.assertEqual(len(set(covered)), 20)


class TestSearchBox(unittest.TestCase):
    def test_search_input_and_results_wiring(self):
        config = _read(CONFIG_VUE)
        self.assertIn('v-model="searchQuery"', config)
        self.assertIn('placeholder="搜索设置：名称 / 键名 / 说明，如 洗版、cron、strm"', config)
        self.assertIn("const searchQuery = ref(\"\")", config)
        self.assertIn("const isSearching = computed(", config)
        self.assertIn("const searchResultSection = computed(", config)
        self.assertIn("buildSearchResultSection(baseSections.value, searchQuery.value)", config)
        self.assertIn('class="config-search-bar"', config)
        self.assertIn('class="config-search-results"', config)
        self.assertIn('class="config-search-empty"', config)
        self.assertIn("没有找到与", config)
        self.assertIn("仅搜索静态设置项", config)
        self.assertIn('import {buildSearchResultSection} from "../config/search.js";', config)

    def test_search_helper_exports(self):
        source = _read(SEARCH_JS)
        for name in (
            "matchesConfigSearch",
            "filterConfigSections",
            "buildSearchResultSection",
        ):
            self.assertIn(f"export function {name}", source)
        self.assertIn('value === "common"', source)


class TestFieldLevelFolding(unittest.TestCase):
    def test_sections_filter_fields_not_sections(self):
        config = _read(CONFIG_VUE)
        self.assertIn("function filterAdvancedFields(section)", config)
        self.assertIn("!field?.advanced", config)
        # 常用设置不参与折叠：sections computed 只对非 common 分组过滤。
        self.assertIn('section.value === "common" ? section : filterAdvancedFields(section)', config)

    def test_advanced_switch_and_storage_key_unchanged(self):
        sidebar = _read(SIDEBAR_VUE)
        self.assertIn("显示高级设置", sidebar)
        config = _read(CONFIG_VUE)
        self.assertIn('const SHOW_ADVANCED_KEY = "csx-config-show-advanced";', config)


class TestNavGroups(unittest.TestCase):
    def test_nav_is_common_plus_six_task_groups(self):
        config = _read(CONFIG_VUE)
        nav = re.search(
            r"const navGroups = computed\(\(\) => \{(.*?)\n\}\);", config, re.S
        )
        self.assertIsNotNone(nav)
        body = nav.group(1)
        for name in ("常用设置", "追更", "转存", "播放", "通知与签到", "渠道", "网盘与基础"):
            self.assertIn(f'name: "{name}"', body)
        for old in ("核心配置", "渠道配置", "播放与媒体", "服务通知"):
            self.assertNotIn(f'name: "{old}"', body)
        self.assertEqual(len(re.findall(r"name: ", body)), 7)


class TestOptionScopes(unittest.TestCase):
    def test_option_scopes_follow_new_tabs(self):
        config = _read(CONFIG_VUE)
        mapping = re.search(
            r"const optionScopeByTab = Object\.freeze\(\{(.*?)\}\)", config, re.S
        )
        self.assertIsNotNone(mapping)
        body = mapping.group(1)
        for tab in ("common", "tracking", "transfer", "playback", "notify", "channels", "drive"):
            self.assertIn(f"{tab}:", body)
        self.assertIn('notify: ["notify", "checkin"]', body)
        self.assertIn("function optionScopesForTab(", config)
        self.assertNotIn("function optionScopeForTab(", config)


class TestSectionValueFollowUp(unittest.TestCase):
    def test_source_names_read_channels(self):
        config = _read(CONFIG_VUE)
        self.assertIn('s.value === "channels"', config)
        self.assertIn('s.value === "tracking"', config)

    def test_config_section_search_check_follows_channels(self):
        section = _read(SECTION_VUE)
        self.assertIn('props.section?.value === "channels"', section)
        self.assertNotIn('props.section?.value === "search"', section)


@unittest.skipIf(shutil.which("node") is None, "node 不可用")
class TestAssemblyRuntime(unittest.TestCase):
    def test_counts_and_uniqueness(self):
        data = _run_node(
            _sections_script(
                "const allKeys = sections.flatMap((s) => (s.groups || [])"
                ".flatMap((g) => (g.fields || []).map((f) => f.key))).filter(Boolean);\n"
                "const groupTitles = Object.fromEntries(sections.map((s) => [s.value, "
                "(s.groups || []).map((g) => g.title)]));\n"
                "const advanced = sections.flatMap((s) => (s.groups || [])"
                ".flatMap((g) => (g.fields || []).filter((f) => f.advanced).map((f) => f.key)));\n"
                "console.log(JSON.stringify({\n"
                "  counts: Object.fromEntries(sections.map((s) => [s.value, "
                "(s.groups || []).flatMap((g) => g.fields || []).length])),\n"
                "  totalKeys: allKeys.length,\n"
                "  uniqueKeys: new Set(allKeys).size,\n"
                "  groupTitles,\n"
                "  advancedCount: advanced.length,\n"
                "}));\n"
            )
        )
        self.assertEqual(data["counts"], TASK_COUNTS)
        self.assertEqual(data["totalKeys"], 237)
        self.assertEqual(data["uniqueKeys"], 237)
        # v1.30.0 阶段 4：organize_source_path（advanced 字段）随源目录退役
        # 删除，advanced 37→36；organize_after_transfer 不是 advanced 字段，
        # 其删除不影响该计数；新增的 funnel_enabled / funnel_scan_dry_run
        # 均不是 advanced，总数 235 不变。
        self.assertEqual(data["advancedCount"], 36)
        # 同一任务组内子分组标题不重复（ConfigSection 用标题做 v-for key 与折叠键）。
        for value, titles in data["groupTitles"].items():
            self.assertEqual(len(titles), len(set(titles)), f"{value} 有重复子分组标题")
        self.assertEqual(
            data["groupTitles"]["tracking"].count("运行设置"), 1
        )

    def test_spot_check_field_homes(self):
        data = _run_node(
            _sections_script(
                "const home = {};\n"
                "for (const s of sections) for (const g of s.groups || []) "
                "for (const f of g.fields || []) home[f.key] = s.value;\n"
                "console.log(JSON.stringify({home}));\n"
            )
        )
        home = data["home"]
        # 分歧已定项与跨组移动项的归属（GROUPING.md）。
        self.assertEqual(home["platform_download_policy"], "tracking")
        self.assertEqual(home["cloud_drive"], "transfer")
        self.assertEqual(home["media_servers"], "playback")
        self.assertEqual(home["media_server_refresh_enabled"], "playback")
        self.assertEqual(home["emby_mediainfo_enabled"], "playback")
        self.assertEqual(home["media_server_path_mappings"], "playback")
        self.assertEqual(home["checkin_auto_retry"], "notify")
        self.assertEqual(home["checkin_timeline"], "notify")
        self.assertEqual(home["tg_monitor_enabled"], "channels")
        self.assertEqual(home["search_concurrency"], "channels")
        self.assertEqual(home["enabled"], "drive")
        self.assertEqual(home["cron"], "tracking")
        self.assertEqual(home["enable_cloud_upgrade"], "transfer")
        self.assertEqual(home["funnel_enabled"], "transfer")
        self.assertEqual(home["funnel_scan_dry_run"], "transfer")
        self.assertEqual(home["auto_subscribe_douban_enabled"], "tracking")
        self.assertEqual(home["strm_generate_enabled"], "playback")


@unittest.skipIf(shutil.which("node") is None, "node 不可用")
class TestSearchRuntime(unittest.TestCase):
    def test_label_key_hint_hits(self):
        data = _run_node(
            _sections_script(
                "const keys = (q) => filterConfigSections(sections, q)"
                ".flatMap((s) => s.groups.flatMap((g) => g.fields.map((f) => f.key)));\n"
                "console.log(JSON.stringify({\n"
                "  labelHit: keys(\"洗版\"),\n"
                "  keyHit: keys(\"cron\"),\n"
                "  keyHitUpper: keys(\"CRON\"),\n"
                "  hintOnlyHit: keys(\"24 小时\"),\n"
                "  noHit: filterConfigSections(sections, \"不存在的设置xyz\"),\n"
                "  blankHit: filterConfigSections(sections, \"   \"),\n"
                "}));\n"
            )
        )
        # label 命中：洗版相关字段跨 3 个任务组。
        # v1.29.9 阶段 3：funnel_phase3_enabled 的 label 含“洗版”
        # （订阅与洗版进漏斗），洗版命中由 8 增至 9。
        self.assertEqual(len(data["labelHit"]), 9)
        self.assertIn("enable_cloud_upgrade", data["labelHit"])
        self.assertIn("notify_template_upgrade", data["labelHit"])
        self.assertIn("funnel_phase3_enabled", data["labelHit"])
        # key 命中（大小写不敏感）。
        self.assertEqual(
            sorted(data["keyHit"]),
            [
                "auto_subscribe_cron",
                "checkin_cron",
                "cron",
                "organize_schedule_cron",
            ],
        )
        self.assertEqual(data["keyHitUpper"], data["keyHit"])
        # hint 命中：只有 cross_transfer_rapid_miss_action 的 hint 含“24 小时”。
        self.assertEqual(data["hintOnlyHit"], ["cross_transfer_rapid_miss_action"])
        self.assertEqual(data["noHit"], [])
        self.assertEqual(data["blankHit"], [])

    def test_common_not_double_counted_and_result_section(self):
        data = _run_node(
            _sections_script(
                "const found = buildSearchResultSection(sections, \"启用插件\");\n"
                "const strm = buildSearchResultSection(sections, \"strm\");\n"
                "console.log(JSON.stringify({\n"
                "  pluginKeys: found ? found.groups.flatMap((g) => g.fields.map((f) => f.key)) : [],\n"
                "  strmValue: strm ? strm.value : null,\n"
                "  strmMatchCount: strm ? strm.matchCount : 0,\n"
                "  strmMatchGroupCount: strm ? strm.matchGroupCount : 0,\n"
                "  strmGroupTitles: strm ? strm.groups.map((g) => g.title) : [],\n"
                "  noneResult: buildSearchResultSection(sections, \"不存在的设置xyz\"),\n"
                "}));\n"
            )
        )
        # “启用插件”只在任务组“网盘与基础”出现一次，不因常用设置复用而翻倍。
        self.assertEqual(data["pluginKeys"], ["enabled"])
        self.assertEqual(data["strmValue"], "search-results")
        # v1.29.8 阶段 2：funnel_phase2_enabled 的 hint 含“strm”
        # （直出 strm 路），strm 命中由 11 增至 12；转存组此前无 strm
        # 命中，本字段把它带入结果，分组数由 2 增至 3。
        self.assertEqual(data["strmMatchCount"], 12)
        self.assertEqual(data["strmMatchGroupCount"], 3)
        for title in data["strmGroupTitles"]:
            self.assertIn(" · ", title)
            self.assertTrue(
                title.startswith("播放 · ")
                or title.startswith("网盘与基础 · ")
                # v1.29.8 阶段 2：funnel_phase2_enabled 在转存组“整理队列”。
                or title.startswith("转存 · "),
                title,
            )
        self.assertIsNone(data["noneResult"])


if __name__ == "__main__":
    unittest.main()
