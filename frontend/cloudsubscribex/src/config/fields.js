import {createBasicSection} from "./fields/basic.js";
import {createDriveSection} from "./fields/drive/index.js";
import {createCloudDriveItems, createResourceTypeItems} from "./fields/helpers.js";
import {createNotifySection} from "./fields/notify.js";
import {createOrganizeSection} from "./fields/organize.js";
import {createSearchSection} from "./fields/search/index.js";
import {createTransferSection} from "./fields/transfer.js";
import {createUpgradeSection} from "./fields/upgrade.js";
import {createSubscribeSection} from "./fields/subscribe.js";
import {createCheckinSection} from "./fields/checkin.js";
import {createTgMonitorSection} from "./fields/tg.js";
import {createStrmSection} from "./fields/strm.js";
import {createEmbyProxySection} from "./fields/emby.js";

export function createConfigSections(options, config = {}) {
  const cloudDriveItems = createCloudDriveItems(options);
  const resourceTypeItems = createResourceTypeItems(options, config);
  // 1.10.3：配置页默认只露常用 6 组；其余 6 组折叠进“高级设置”。
  // 向导按 key 取字段时读全量（见 Config.vue 的 allSections），不受折叠影响。
  const ADVANCED_SECTIONS = new Set(["organize", "checkin", "strm", "emby", "drive", "upgrade"]);
  return [
    createBasicSection(cloudDriveItems, options),
    createTransferSection(options),
    createOrganizeSection(),
    createSubscribeSection(options),
    createCheckinSection(options),
    createTgMonitorSection(),
    createStrmSection(),
    createEmbyProxySection(),
    createDriveSection(options),
    createSearchSection(resourceTypeItems, options),
    createUpgradeSection(options),
    createNotifySection(options),
  ].map((section) => ({ ...section, advanced: ADVANCED_SECTIONS.has(section.value) }));
}
