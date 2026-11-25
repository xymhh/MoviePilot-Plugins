import {createBasicSection} from "./fields/basic.js";
import {createDriveSection} from "./fields/drive/index.js";
import {createCloudDriveItems, createResourceTypeItems} from "./fields/helpers.js";
import {createNotifySection} from "./fields/notify.js";
import {createOrganizeSection} from "./fields/organize.js";
import {createSearchSection} from "./fields/search/index.js";
import {createTransferSection} from "./fields/transfer.js";
import {createUpgradeSection} from "./fields/upgrade.js";
import {createSubscribeSection} from "./fields/subscribe.js";
import {createSubsourcesSection} from "./fields/subsources.js";
import {createCheckinSection} from "./fields/checkin.js";
import {createTgMonitorSection} from "./fields/tg.js";
import {createStrmSection} from "./fields/strm.js";
import {createEmbyProxySection} from "./fields/emby.js";

// v1.29.5：13 个技术分组按任务重排为 6 组（分组表见
// ~/workspace/figma-settings-redesign/GROUPING.md，227 项零丢失零新增）。
// 字段定义仍由各 create*Section 产出（每 key 只定义一次），这里只按 key
// 挑字段重组，不复制定义；同一任务组内同名子分组自动合并（追更的两个
// “运行设置”合为一组）。后端动态下发的网盘驱动 / 搜索渠道 schema 不动，
// 仍按原方式挂接（drive → 网盘与基础，search → 渠道）。
// origin 属于旧高级分组（organize/checkin/strm/emby/drive/upgrade）的
// 字段打上 advanced: true，由 Config.vue 在分组浏览视图里按“显示高级
// 设置”开关做字段级折叠；常用设置与搜索不受折叠影响。
const ADVANCED_FIELD_ORIGINS = new Set(["organize", "checkin", "strm", "emby", "drive", "upgrade"]);

// 每项 source：from = 旧 section value；keys 缺省时整组搬迁；groupTitle
// 缺省时沿用原分组标题（仅 cloud_drive / media_servers 两个单字段跨组时改名）。
const TASK_LAYOUT = [
  {
    value: "tracking",
    title: "追更",
    icon: "mdi-format-list-bulleted-square",
    sources: [
      {
        from: "basic",
        keys: [
          "cron",
          "takeover_new_subscribes",
          "block_system_subscribe",
          "block_start_time",
          "block_end_time",
          "platform_download_policy",
          "platform_transfer_history_enabled",
          "title_subscribe_enabled",
          "subscribe_all_missing_seasons",
          "subscribe_skip_season_zero",
          "subscribe_season_limit",
        ],
      },
      {from: "subscribe"},
      {from: "subsources"},
    ],
    subtabsFrom: "subsources",
  },
  {
    value: "transfer",
    title: "转存",
    icon: "mdi-cloud-download-outline",
    sources: [
      {from: "transfer"},
      {from: "organize"},
      {from: "upgrade"},
      {from: "basic", keys: ["cloud_drive"], groupTitle: "转存目标"},
    ],
  },
  {
    value: "playback",
    title: "播放",
    icon: "mdi-file-video-outline",
    sources: [
      {from: "strm"},
      {from: "emby"},
      {from: "basic", keys: ["media_servers"], groupTitle: "媒体库"},
      {
        from: "notify",
        keys: [
          "media_server_refresh_enabled",
          "emby_mediainfo_enabled",
          "media_server_refresh_delay",
          "media_server_path_mappings",
        ],
      },
    ],
  },
  {
    value: "notify",
    title: "通知与签到",
    icon: "mdi-bell-outline",
    sources: [
      {
        from: "notify",
        keys: [
          "platform_media_sync_enabled",
          "platform_deep_delete_enabled",
          "platform_deep_delete_title_keywords",
          "media_library_webhook_urls",
          "media_library_webhook_help",
          "notify",
          "notification_type",
          "notify_template_transfer",
          "notify_template_cross_transfer",
          "notify_template_upgrade",
          "notify_template_checkin",
          "notify_template_organize",
          "notify_template_delete",
          "webhook_enabled",
          "webhook_url",
          "webhook_method",
          "webhook_timeout",
        ],
      },
      {from: "checkin"},
    ],
  },
  {
    value: "channels",
    title: "渠道",
    icon: "mdi-magnify",
    sources: [{from: "tg"}, {from: "search"}],
    subtabsFrom: "search",
  },
  {
    value: "drive",
    title: "网盘与基础",
    icon: "mdi-cloud-cog-outline",
    sources: [
      {
        from: "basic",
        keys: [
          "enabled",
          "show_sidebar_nav",
          "agent_enabled",
          "direct_transfer_enabled",
          "mp_base_url",
          "period_episode_rules",
          "media_server_ignore_paths",
        ],
      },
      {from: "drive"},
    ],
    subtabsFrom: "drive",
  },
];

function assembleTaskSection(task, legacy) {
  const groupsByTitle = new Map();
  const groups = [];
  for (const source of task.sources) {
    const origin = legacy[source.from];
    const advanced = ADVANCED_FIELD_ORIGINS.has(source.from);
    const wanted = source.keys ? new Set(source.keys) : null;
    for (const group of origin?.groups || []) {
      const picked = (group.fields || []).filter(
        (field) => field?.key && (!wanted || wanted.has(field.key)),
      );
      if (!picked.length) continue;
      const title = source.groupTitle || group.title;
      let target = groupsByTitle.get(title);
      if (!target) {
        target = {title, icon: group.icon, fields: []};
        if (group.hint) target.hint = group.hint;
        if (group.tab) target.tab = group.tab;
        if (group.beforeTabs) target.beforeTabs = true;
        if (group.hideHeading) target.hideHeading = true;
        groupsByTitle.set(title, target);
        groups.push(target);
      }
      for (const field of picked) {
        target.fields.push(advanced ? {...field, advanced: true} : field);
      }
    }
  }
  const section = {value: task.value, title: task.title, icon: task.icon, groups};
  if (task.subtabsFrom) {
    const subtabs = legacy[task.subtabsFrom]?.subtabs;
    if (Array.isArray(subtabs) && subtabs.length) section.subtabs = subtabs;
  }
  return section;
}

export function createConfigSections(options, config = {}) {
  const cloudDriveItems = createCloudDriveItems(options);
  const resourceTypeItems = createResourceTypeItems(options, config);
  // 字段定义的唯一来源：13 个旧 section 创建函数（调用顺序与 v1.29.4 一致，
  // 只产出定义，不再直接决定页面分组）。
  const legacy = {
    basic: createBasicSection(cloudDriveItems, options),
    transfer: createTransferSection(options),
    organize: createOrganizeSection(),
    subscribe: createSubscribeSection(options),
    subsources: createSubsourcesSection(),
    checkin: createCheckinSection(options),
    tg: createTgMonitorSection(),
    strm: createStrmSection(),
    emby: createEmbyProxySection(),
    drive: createDriveSection(options),
    search: createSearchSection(resourceTypeItems, options),
    upgrade: createUpgradeSection(options),
    notify: createNotifySection(options),
  };
  return TASK_LAYOUT.map((task) => assembleTaskSection(task, legacy));
}
