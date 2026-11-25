import {enabled} from "./helpers.js";

export function createCheckinSection(options = {}) {
  const dynamicProviders = Array.isArray(options.checkinSchemas?.providers)
    ? options.checkinSchemas.providers
    : [];
  const dynamicGroups = Array.isArray(options.checkinSchemas?.groups)
    ? options.checkinSchemas.groups
    : [];

  const baseGroups = [
    {
      tab: "checkin",
      title: "签到详情",
      icon: "mdi-history",
      hint: "集中显示全部签到渠道最近 7 天状态，并在对应渠道行内执行立即签到。",
      fields: [
        {
          key: "checkin_timeline",
          type: "checkin-timeline",
          providers: dynamicProviders,
          cols: 12,
        },
      ],
    },
    {
      tab: "checkin",
      title: "统一调度",
      icon: "mdi-calendar-clock-outline",
      hint: "所有渠道共用一个任务；每天首次全量签到，后续只重试当天失败的渠道。",
      fields: [
        {
          key: "checkin_auto_retry",
          label: "自动重试",
          type: "switch",
          hint: "自动签到失败后，在当天 9-23 点内随机重试失败渠道。",
          cols: 12,
        },
        {
          key: "checkin_cron",
          label: "签到执行周期",
          type: "cron",
          hint: "默认每天 08:00 执行；修改后统一应用到全部签到渠道。",
          placeholder: "0 8 * * *",
          cols: 6,
        },
        {
          key: "checkin_retry_count",
          label: "自动重试次数",
          type: "number",
          min: 1,
          max: 10,
          suffix: "次",
          hint: "默认重试 2 次，已签到成功的渠道不会再次执行。",
          cols: 6,
          show: enabled("checkin_auto_retry"),
        },
      ],
    },
  ];

  return {
    value: "checkin",
    title: "签到服务",
    icon: "mdi-calendar-check-outline",
    groups: [...baseGroups, ...dynamicGroups],
  };
}
