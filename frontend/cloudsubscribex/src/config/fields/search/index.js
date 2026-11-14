import {createCommonSearchGroups} from "./common.js";

function resolveDynamicField(field, options) {
  const descriptor = field?.dynamicOptions;
  if (!descriptor?.scope || !descriptor?.key) return field;
  const scope = String(descriptor.scope);
  const scopeOptions = options?.[scope];
  return {
    ...field,
    items: Array.isArray(scopeOptions?.[descriptor.key]) ? scopeOptions[descriptor.key] : [],
    loading: Boolean(options?.dynamicOptionLoading?.[scope]),
    loadError: String(options?.dynamicOptionErrors?.[scope] || ""),
  };
}

export function createSearchSection(resourceTypeItems, options = {}) {
  const dynamicSchemas = Array.isArray(options.searchSchemas) ? options.searchSchemas : [];

  return {
    value: "search",
    title: "搜索渠道",
    icon: "mdi-magnify",
    subtabs: [
      {value: "common", title: "通用设置", icon: "mdi-tune"},
      ...dynamicSchemas.map((s) => s.subtab).filter(Boolean),
    ],
    groups: [
      ...createCommonSearchGroups(resourceTypeItems),
      ...dynamicSchemas.flatMap((schema) =>
        (Array.isArray(schema.groups) ? schema.groups : []).map((group) => ({
          ...group,
          fields: (Array.isArray(group.fields) ? group.fields : []).map((field) =>
            resolveDynamicField(field, options),
          ),
        })),
      ),
    ],
  };
}
