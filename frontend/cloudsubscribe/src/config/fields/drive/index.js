export function createDriveSection(options = {}) {
  const dynamicSchemas = Array.isArray(options.driverSchemas) ? options.driverSchemas : [];

  return {
    value: "drive",
    title: "网盘配置",
    icon: "mdi-cloud-cog-outline",
    subtabs: dynamicSchemas.map((s) => s.subtab).filter(Boolean),
    groups: dynamicSchemas.flatMap((s) => (Array.isArray(s.groups) ? s.groups : [])),
  };
}
