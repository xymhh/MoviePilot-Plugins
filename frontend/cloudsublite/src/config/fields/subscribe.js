export function createSubscribeSection(options = {}) {
  return {
    value: "subscribe",
    title: "榜单订阅",
    icon: "mdi-format-list-bulleted-square",
    groups: [
      {
        title: "运行设置",
        icon: "mdi-filter-cog-outline",
        fields: [
          {key: "auto_subscribe_enabled", label: "启用榜单自动订阅", type: "switch", cols: 4},
          {
            key: "auto_subscribe_onlyonce",
            label: "保存后立即运行一次",
            type: "switch",
            hint: "立即执行一次并复位",
            cols: 4,
          },
          {key: "auto_subscribe_notify", label: "发送运行结果通知", type: "switch", cols: 4},
          {
            key: "auto_subscribe_cron",
            label: "榜单订阅执行周期",
            type: "cron",
            hint: "自动抓取周期",
            cols: 12,
          },
          {
            key: "auto_subscribe_media_types",
            label: "订阅媒体类型",
            type: "select",
            multiple: true,
            items: [
              {title: "全部", value: "all"},
              {title: "电影", value: "movie"},
              {title: "电视剧", value: "tv"},
              {title: "动漫电影", value: "anime_movie"},
              {title: "动漫番剧", value: "anime_tv"},
            ],
            default: ["all"],
            hint: "所有订阅渠道通用的媒体类型限制。支持多选，保留“全部”默认订阅所有类型。",
            cols: 12,
          },
          {
            key: "auto_subscribe_skip_season_zero",
            label: "跳过第 0 季",
            type: "switch",
            hint: "跳过特别篇 (S00)",
            cols: 4,
          },
          {key: "auto_subscribe_skip_subscribed", label: "跳过已有活动订阅", type: "switch", cols: 4},
          {key: "auto_subscribe_skip_history", label: "跳过历史订阅", type: "switch", cols: 4},
          {key: "auto_subscribe_skip_library", label: "跳过媒体库已有内容", type: "switch", cols: 4},
          {
            key: "auto_subscribe_username",
            label: "订阅用户名",
            hint: "写入系统订阅记录",
            cols: 12,
          },
        ],
      },
      {
        title: "代理服务器",
        icon: "mdi-lan-connect",
        fields: [
          {
            key: "auto_subscribe_proxy",
            label: "代理地址",
            type: "auto-subscribe-proxy",
            hint: "留空直连，支持 http/socks5",
            placeholder: "http://127.0.0.1:7890",
            cols: 12,
          },
          {key: "auto_subscribe_proxy_username", label: "代理用户名", cols: 6},
          {key: "auto_subscribe_proxy_password", label: "代理密码", type: "password", cols: 6},
        ],
      },
    ],
  }
}
