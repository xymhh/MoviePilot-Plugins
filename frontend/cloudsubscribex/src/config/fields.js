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

function collectFields(section) {
  const map = new Map();
  (section?.groups || []).forEach((group) => {
    (group.fields || []).forEach((field) => map.set(field.key, field));
  });
  return map;
}

function pickFields(fields, keys) {
  return keys.map((key) => fields.get(key)).filter(Boolean);
}

export function createConfigSections(options, config = {}) {
  const cloudDriveItems = createCloudDriveItems(options);
  const resourceTypeItems = createResourceTypeItems(options, config);
  const basic = createBasicSection(cloudDriveItems, options);
  const transfer = createTransferSection(options);
  const organize = createOrganizeSection();
  const subscribe = createSubscribeSection(options);
  const checkin = createCheckinSection(options);
  const tg = createTgMonitorSection();
  const strm = createStrmSection();
  const emby = createEmbyProxySection();
  const drive = createDriveSection(options);
  const search = createSearchSection(resourceTypeItems, options);
  const upgrade = createUpgradeSection(options);
  const notify = createNotifySection(options);

  const basicFields = collectFields(basic);
  const mediaGroup = {
    title: "媒体库",
    icon: "mdi-library-shelves",
    hint: "网盘选择、Emby 库与期集换算",
    fields: pickFields(basicFields, [
      "cloud_drive",
      "media_servers",
      "period_episode_rules",
      "media_server_ignore_paths",
    ]),
  };

  return [
    {
      value: "overview",
      title: "概览",
      icon: "mdi-view-dashboard-outline",
      groups: [
        {
          title: "插件运行",
          icon: "mdi-play-circle-outline",
          hint: "总开关、导航入口与扫描周期",
          fields: pickFields(basicFields, ["enabled", "show_sidebar_nav", "agent_enabled", "cron"]),
        },
        {
          title: "订阅行为",
          icon: "mdi-playlist-check",
          hint: "订阅生成与记录保留策略",
          fields: pickFields(basicFields, [
            "title_subscribe_enabled",
            "direct_transfer_enabled",
            "subscribe_all_missing_seasons",
            "subscribe_skip_season_zero",
            "subscribe_season_limit",
            "platform_transfer_history_enabled",
            "sync_job_ledger_enabled",
          ]),
        },
        {
          title: "调度与接管",
          icon: "mdi-calendar-clock",
          hint: "接管现有订阅，并设定禁止系统订阅的时段",
          fields: pickFields(basicFields, [
            "takeover_new_subscribes",
            "block_system_subscribe",
            "block_start_time",
            "block_end_time",
            "platform_download_policy",
          ]),
        },
      ],
    },
    {
      ...drive,
      value: "storage",
      title: "网盘与媒体库",
      icon: "mdi-cloud-outline",
      groups: [mediaGroup, ...(drive.groups || [])],
    },
    {...subscribe, title: "订阅与榜单"},
    {...search, title: "搜源"},
    {
      ...transfer,
      value: "transfer",
      title: "转存与整理",
      icon: "mdi-cloud-download-outline",
      groups: [...(transfer.groups || []), ...(organize.groups || []), ...(upgrade.groups || [])],
    },
    {
      ...strm,
      value: "playback",
      title: "STRM 与播放",
      icon: "mdi-play-box-outline",
      groups: [...(strm.groups || []), ...(emby.groups || [])],
    },
    {
      ...notify,
      value: "notify",
      title: "通知与监控",
      icon: "mdi-bell-outline",
      groups: [...(notify.groups || []), ...(checkin.groups || []), ...(tg.groups || [])],
    },
  ];
}
