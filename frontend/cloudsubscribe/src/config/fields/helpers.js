export const enabled = (key) => (config) => Boolean(config[key]);

export function createCloudDriveItems(options) {
  return Array.isArray(options?.cloudDrives) ? options.cloudDrives : [];
}

/** 资源类型选项由后端下发（options.resourceTypes），这里只按当前网盘能力过滤。 */
export function createResourceTypeItems(options, config) {
  const cloudDriveItems = createCloudDriveItems(options);
  const catalog = Array.isArray(options.resourceTypes) ? options.resourceTypes : [];
  const activeDrive = cloudDriveItems.find((item) => item.value === (config.cloud_drive || "115"));
  const supportedTypes = new Set(activeDrive?.resource_types || ["115", "ed2k", "magnet"]);
  const targetCanUpload = activeDrive?.capabilities?.includes("local_upload");
  if (config.cross_transfer_enabled && targetCanUpload) {
    cloudDriveItems.forEach((drive) => {
      const capabilities = new Set(drive.capabilities || []);
      if (
        drive.value === activeDrive?.value ||
        !capabilities.has("share_transfer") ||
        !capabilities.has("file_download")
      ) {
        return;
      }
      (drive.resource_types || []).forEach((value) => {
        if (!["ed2k", "magnet"].includes(value)) supportedTypes.add(value);
      });
    });
  }
  return catalog
    .filter((item) => supportedTypes.has(item?.value))
    .map((item) => ({title: item.name, value: item.value, icon: item.icon}));
}

/** 搜索渠道选项由后端下发（options.sources）。 */
export function createSourceItems(options) {
  return (Array.isArray(options.sources) ? options.sources : []).map((item) => ({
    title: item.name,
    value: item.key,
    icon: item.icon,
  }));
}
