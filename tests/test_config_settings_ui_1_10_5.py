"""1.10.5：设置 UI 优化——榜单源拆分组 + 分组折叠。

* 订阅源拆分：豆瓣/猫眼/TMDB/Mikan/Bangumi/AniList/Netflix 7 个榜单
  源从“榜单订阅”拆成独立分组「订阅源」（新文件 fields/subsources.js，
  21 组 67 个字段）；订阅只留运行设置与代理（2 组 13 个字段）。
  字段 key 一个不少、无重叠；向导只按 key 取字段，不受影响；
  测试榜单的提供方名改从“订阅源”的 subtabs 取。
* 分组折叠：ConfigSection 每组默认只展开首组与有修改的组，点标题
  切换；有修改的组标题旁有圆点；展开状态按 section 记 localStorage；
  另加全部展开/收起。恢复默认值改用后端下发的默认值表（以前
  options.defaults 从没赋值过，文本框会被错恢复成空串）。

运行：``cd <repo> && python -m pytest tests/test_config_settings_ui_1_10_5.py -q``
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel_path: str) -> str:
    with open(os.path.join(REPO_ROOT, rel_path), encoding="utf-8") as f:
        return f.read()


def _keys(source: str):
    return re.findall(r'key:\s*"([^"]+)"', source)


class TestSubsourcesSplit(unittest.TestCase):
    def test_subscribe_keeps_only_global_groups(self):
        source = _read("frontend/cloudsubscribex/src/config/fields/subscribe.js")
        self.assertNotIn("subtabs", source)
        titles = re.findall(r'        title: "([^"]+)",', source)
        self.assertEqual(titles, ["运行设置", "代理服务器"])
        keys = _keys(source)
        self.assertEqual(len(keys), 13)
        self.assertIn("auto_subscribe_enabled", keys)
        self.assertIn("auto_subscribe_proxy", keys)
        self.assertNotIn("auto_subscribe_douban_enabled", keys)
        self.assertNotIn("monthItems", source)
        self.assertNotIn("netflixCountries", source)

    def test_subsources_has_all_providers(self):
        source = _read("frontend/cloudsubscribex/src/config/fields/subsources.js")
        self.assertIn("export function createSubsourcesSection()", source)
        self.assertIn('value: "subsources"', source)
        self.assertIn('title: "订阅源"', source)
        for provider in ("douban", "maoyan", "tmdb", "mikan", "bangumi", "anilist", "netflix"):
            self.assertIn(f'value: "{provider}"', source)
        self.assertNotIn('value: "global"', source)
        titles = re.findall(r'        title: "([^"]+)",', source)
        self.assertEqual(len(titles), 21)
        keys = _keys(source)
        self.assertEqual(len(keys), 67)
        self.assertTrue(all(k.startswith("auto_subscribe_") for k in keys))
        for key in (
            "auto_subscribe_douban_enabled",
            "auto_subscribe_tmdb_ranks",
            "auto_subscribe_bangumi_enabled",
            "auto_subscribe_anilist_ranks",
            "auto_subscribe_maoyan_base_url",
            "auto_subscribe_netflix_global",
            "auto_subscribe_mikan_base_urls",
        ):
            self.assertIn(key, keys)

    def test_no_key_lost_or_duplicated_by_split(self):
        sub = _read("frontend/cloudsubscribex/src/config/fields/subscribe.js")
        src = _read("frontend/cloudsubscribex/src/config/fields/subsources.js")
        sub_keys = set(_keys(sub))
        src_keys = set(_keys(src))
        self.assertEqual(sub_keys & src_keys, set())
        self.assertEqual(len(sub_keys) + len(src_keys), 80)

    def test_fields_registers_subsources_after_subscribe(self):
        source = _read("frontend/cloudsubscribex/src/config/fields.js")
        self.assertIn(
            'import {createSubsourcesSection} from "./fields/subsources.js";',
            source,
        )
        self.assertLess(
            source.index("createSubscribeSection(options),"),
            source.index("createSubsourcesSection(),"),
        )
        self.assertLess(
            source.index("createSubsourcesSection(),"),
            source.index("createCheckinSection(options),"),
        )
        advanced = re.search(
            r"ADVANCED_SECTIONS = new Set\(\[(.*?)\]\)", source, re.S
        ).group(1)
        self.assertNotIn("subsources", advanced)

    def test_config_nav_and_provider_names_follow_split(self):
        source = _read("frontend/cloudsubscribex/src/components/Config.vue")
        self.assertIn('"subscribe", "subsources", "upgrade"', source)
        provider_names = re.search(
            r"const autoSubscribeProviderNames = computed\(\(\) => \{(.*?)\n\}\);",
            source,
            re.S,
        )
        self.assertIsNotNone(provider_names)
        body = provider_names.group(1)
        self.assertIn('s.value === "subsources"', body)
        self.assertIn('s.value === "subscribe"', body)

    def test_config_stores_backend_defaults(self):
        source = _read("frontend/cloudsubscribex/src/components/Config.vue")
        self.assertIn("defaults: {},", source)
        self.assertIn("options.defaults = data.defaults", source)


@unittest.skipIf(shutil.which("node") is None, "node 不可用")
class TestSectionAssemblyRuntime(unittest.TestCase):
    """真实跑一遍 createConfigSections：13 个分组、key 无重叠。"""

    def test_sections_assemble(self):
        script = (
            "import {createConfigSections} from %r;\n"
            "const options = new Proxy({cloud_drive: \"\"}, "
            "{ get: (t, k) => (k in t ? t[k] : []) });\n"
            "const sections = createConfigSections(options, {});\n"
            "const sub = sections.find((s) => s.value === \"subscribe\");\n"
            "const src = sections.find((s) => s.value === \"subsources\");\n"
            "const allKeys = sections.flatMap((s) => (s.groups || [])"
            ".flatMap((g) => (g.fields || []).map((f) => f.key))).filter(Boolean);\n"
            "console.log(JSON.stringify({\n"
            "  sections: sections.length,\n"
            "  valuesUnique: new Set(sections.map((s) => s.value)).size === sections.length,\n"
            "  subscribeGroups: sub.groups.length,\n"
            "  subscribeSubtabs: (sub.subtabs || []).length,\n"
            "  subsourcesGroups: src.groups.length,\n"
            "  subsourcesSubtabs: (src.subtabs || []).map((t) => t.value),\n"
            "  totalKeys: allKeys.length,\n"
            "  uniqueKeys: new Set(allKeys).size,\n"
            "}));\n"
            % (
                os.path.join(
                    REPO_ROOT,
                    "frontend/cloudsubscribex/src/config/fields.js",
                )
            )
        )
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
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(data["sections"], 13)
        self.assertTrue(data["valuesUnique"])
        self.assertEqual(data["subscribeGroups"], 2)
        self.assertEqual(data["subscribeSubtabs"], 0)
        self.assertEqual(data["subsourcesGroups"], 21)
        self.assertEqual(
            data["subsourcesSubtabs"],
            ["douban", "maoyan", "tmdb", "mikan", "bangumi", "anilist", "netflix"],
        )
        self.assertEqual(data["totalKeys"], data["uniqueKeys"])


class TestGroupFolding(unittest.TestCase):
    def test_fold_state_and_toggle(self):
        source = _read(
            "frontend/cloudsubscribex/src/components/config/ConfigSection.vue"
        )
        self.assertIn(
            'const GROUP_FOLD_STORAGE_PREFIX = "csx-config-expanded-groups:"',
            source,
        )
        self.assertIn("const expandedGroups = ref(new Set())", source)
        self.assertIn("function seedExpandedGroups()", source)
        self.assertIn("function isGroupExpanded(group)", source)
        self.assertIn("function toggleGroup(group)", source)
        self.assertIn("function expandAllGroups()", source)
        self.assertIn("function collapseAllGroups()", source)
        self.assertIn("function groupHasChanges(group)", source)
        self.assertIn("function fieldDefaultValue(field)", source)
        # 恢复默认值与“有修改”圆点共用同一默认值口径。
        reset = re.search(
            r"function executeResetSubtab\(\) \{(.*?)\n\}",
            source,
            re.S,
        )
        self.assertIsNotNone(reset)
        self.assertIn("fieldDefaultValue(field)", reset.group(1))
        self.assertNotIn("let defVal = field.default;", reset.group(1))

    def test_heading_toggles_and_fields_collapse(self):
        source = _read(
            "frontend/cloudsubscribex/src/components/config/ConfigSection.vue"
        )
        self.assertIn("group-heading--collapsible", source)
        self.assertIn('role="button"', source)
        self.assertIn('@click="toggleGroup(group)"', source)
        self.assertIn('@click.stop="promptResetSubtab"', source)
        self.assertIn(
            '<v-row v-show="isGroupExpanded(group)" dense class="config-fields-row">',
            source,
        )
        self.assertIn("group-modified-dot", source)
        self.assertIn("group-field-count", source)
        self.assertIn("group-fold-toolbar", source)
        self.assertIn("全部展开", source)
        self.assertIn("全部收起", source)
        self.assertIn(
            ':icon="isGroupExpanded(group) ? \'mdi-chevron-down\' : \'mdi-chevron-right\'"',
            source,
        )

    def test_first_group_opens_on_first_subtab_visit(self):
        source = _read(
            "frontend/cloudsubscribex/src/components/config/ConfigSection.vue"
        )
        self.assertIn("function ensureFirstGroupExpanded()", source)
        self.assertIn("visitedSubtabs", source)


if __name__ == "__main__":
    unittest.main()
