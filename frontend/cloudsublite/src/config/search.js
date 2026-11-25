// v1.29.5：设置搜索——按 label / key / hint 在静态 227 项里实时过滤。
// 只搜静态字段：网盘驱动与搜索渠道的后端动态 schema 不进搜索（已定，
// 以后端能返回可搜索 label 清单为前提再扩展）。命中字段按所属任务组
// 展示；常用设置是各任务组字段的复用视图，不重复搜。搜索不受“显示高级
// 设置”折叠影响——它是显式查找，不是浏览。

export const SEARCH_RESULT_SECTION_VALUE = "search-results";

function normalizeSearchText(value) {
  return String(value || "").trim().toLowerCase();
}

function fieldSearchText(field) {
  return [field?.label, field?.key, field?.hint]
    .map((part) => String(part || ""))
    .join(" ");
}

/** 单个字段是否命中查询（label / key / hint 子串匹配，不区分大小写）。 */
export function matchesConfigSearch(field, query) {
  const keyword = normalizeSearchText(query);
  if (!keyword) return false;
  return normalizeSearchText(fieldSearchText(field)).includes(keyword);
}

/**
 * 按任务组过滤静态 sections：返回 [{value, title, icon, groups: [...]}]，
 * 只保留含命中字段的分组与任务组；常用设置（common）不参与——它是复用视图。
 */
export function filterConfigSections(sections, query) {
  const keyword = normalizeSearchText(query);
  if (!keyword) return [];
  const matched = [];
  for (const section of sections || []) {
    if (!section || section.value === "common") continue;
    const groups = [];
    for (const group of section.groups || []) {
      const fields = (group.fields || []).filter(
        (field) => field?.key && matchesConfigSearch(field, keyword),
      );
      if (fields.length) groups.push({...group, fields});
    }
    if (groups.length) {
      matched.push({value: section.value, title: section.title, icon: section.icon, groups});
    }
  }
  return matched;
}

/**
 * 把过滤结果拼成一个可直接交给 ConfigSection 渲染的 section：组标题带
 * 任务组名前缀（“追更 · 运行设置”），无 subtabs（命中组全展示）；
 * matchCount / matchGroupCount 供结果计数行用。无命中时返回 null。
 */
export function buildSearchResultSection(sections, query) {
  const matched = filterConfigSections(sections, query);
  if (!matched.length) return null;
  const groups = [];
  let fieldCount = 0;
  for (const section of matched) {
    for (const group of section.groups) {
      fieldCount += group.fields.length;
      groups.push({...group, title: `${section.title} · ${group.title}`});
    }
  }
  return {
    value: SEARCH_RESULT_SECTION_VALUE,
    title: "搜索结果",
    icon: "mdi-magnify",
    groups,
    matchCount: fieldCount,
    matchGroupCount: matched.length,
  };
}
