export function createBasicSection(cloudDriveItems, options = {}) {
  return {
    value: "basic",
    title: "基础设置",
    icon: "mdi-cog-outline",
    groups: [
      {
        title: "运行设置",
        icon: "mdi-play-circle-outline",
        fields: [
          {key: "enabled", label: "启用插件", type: "switch", cols: 4},
          {
            key: "show_sidebar_nav",
            label: "启用左侧导航",
            type: "switch",
            hint: "刷新主界面生效",
            cols: 4,
          },
          {
            key: "agent_enabled",
            label: "启用智能体工具",
            type: "switch",
            hint: "允许智能体查询与调度",
            cols: 4,
          },
          {
            key: "direct_transfer_enabled",
            label: "链接直达转存",
            type: "switch",
            hint: "消息中的网盘链接直接转存",
            cols: 4,
          },
          {
            key: "platform_transfer_history_enabled",
            label: "写入整理历史",
            type: "switch",
            cols: 4,
          },
          {
            key: "takeover_new_subscribes",
            label: "拦截新增订阅",
            type: "switch",
            hint: "新增订阅由插件统一调度",
            cols: 4,
          },
          {
            key: "cron",
            label: "订阅执行周期",
            type: "cron",
            hint: "自动搜索调度执行周期",
            placeholder: "30 2,10,18 * * *",
            cols: 8,
          },
        ],
      },
      {
        title: "订阅接管时段",
        icon: "mdi-clock-outline",
        fields: [
          {
            key: "block_system_subscribe",
            label: "始终接管系统订阅",
            hint: "全天由插件接管调度",
            type: "switch",
            cols: 4,
          },
          {
            key: "platform_download_policy",
            label: "平台下载策略",
            type: "select",
            items: [
              {title: "允许下载并整理", value: "allow"},
              {title: "阻止搜索及下载", value: "block"},
              {title: "转为网盘离线下载", value: "cloud"},
            ],
            hint: "接管期内 PT/RSS 下载行为",
            cols: 8,
          },
          {
            key: "block_start_time",
            label: "接管开始",
            type: "time",
            cols: 4,
            show: (config) => !config.block_system_subscribe,
          },
          {
            key: "block_end_time",
            label: "接管结束",
            type: "time",
            cols: 4,
            show: (config) => !config.block_system_subscribe,
          },
        ],
      },
      {
        title: "提供方设置",
        icon: "mdi-cloud-outline",
        fields: [
          {
            key: "cloud_drive",
            label: "当前转存网盘",
            type: "select",
            items: cloudDriveItems,
            cols: 4,
          },
          {
            key: "media_servers",
            label: "启用媒体库",
            type: "select",
            items: options.mediaservers || [],
            multiple: true,
            hint: "用于入库检查与洗版基线读取",
            cols: 8,
          },
        ],
      },
    ],
  }
}
