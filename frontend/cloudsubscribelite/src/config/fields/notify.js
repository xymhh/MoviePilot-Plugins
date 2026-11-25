import { enabled } from "./helpers.js"

export function createNotifySection(options) {
  return {
    value: "notify",
    title: "通知设置",
    icon: "mdi-bell-outline",
    groups: [
      {
        title: "入库通知",
        icon: "mdi-database-arrow-up-outline",
        fields: [
          {
            key: "media_server_refresh_enabled",
            label: "启用入库通知",
            type: "switch",
            cols: 6,
          },
          {
            key: "emby_mediainfo_enabled",
            label: "Emby 媒体信息提取",
            type: "switch",
            hint: "入库后触发 Emby 提取信息",
            cols: 6,
            show: enabled("media_server_refresh_enabled"),
          },
          {
            key: "media_server_refresh_delay",
            label: "延迟通知（秒）",
            hint: "延迟触发入库通知秒数",
            type: "number",
            min: 0,
            cols: 6,
            show: enabled("media_server_refresh_enabled"),
          },
          {
            key: "media_server_path_mappings",
            label: "媒体服务器路径映射",
            type: "textarea",
            placeholder: "/媒体服务器/strm#/strm",
            hint: "每行一条：媒体服务器路径#本地路径",
            rows: 4,
            monospace: true,
            cols: 12,
            show: enabled("media_server_refresh_enabled"),
          },
        ],
      },
      {
        title: "媒体库通知",
        icon: "mdi-server-network-outline",
        fields: [
          {
            key: "platform_media_sync_enabled",
            label: "接收媒体库通知",
            hint: "开启后接收媒体库新增/删除通知",
            type: "switch",
            cols: 6,
          },
          {
            key: "platform_delete_linkage_enabled",
            label: "媒体库删除联动",
            hint: "媒体库删除通知到达后，联动清理插件历史、网盘文件与本地 STRM",
            type: "switch",
            cols: 6,
            show: enabled("platform_media_sync_enabled"),
          },
          {
            key: "media_library_webhook_urls",
            label: "Emby 媒体库通知地址",
            type: "media-library-webhook",
            items: options.mediaservers.filter((item) => String(item.type || "").toLowerCase() === "emby"),
            urls: options.mediaLibraryWebhookUrls || {},
            cols: 12,
            show: enabled("platform_media_sync_enabled"),
          },
          {
            key: "media_library_webhook_help",
            type: "info",
            label: "Emby Webhook 配置",
            lines: [
              "上方地址为插件入口，已包含 API Token，直接复制到 Emby 的 Webhooks 插件即可",
              "Emby 请求方法选 POST，按 Emby 默认以原始 JSON 投递，无需表单 data 模式",
              "事件勾选「媒体库新增 / 媒体库删除」；删除通知将联动清理插件历史、网盘文件与本地 STRM",
              "source 参数必须与平台中配置的 Emby 媒体服务器名称一致",
            ],
            cols: 12,
            show: enabled("platform_media_sync_enabled"),
          },
        ],
      },
      {
        title: "消息通知",
        icon: "mdi-message-badge-outline",
        fields: [
          { key: "notify", label: "发送消息通知", type: "switch", cols: 4 },
          {
            key: "notification_type",
            label: "消息通知类型",
            type: "select",
            items: options.notificationTypes,
            hint: "分发到已启用的通知渠道",
            cols: 8,
            show: enabled("notify"),
          },
        ],
      },
      {
        title: "Webhook",
        icon: "mdi-webhook",
        fields: [
          {
            key: "webhook_enabled",
            label: "启用 Webhook",
            type: "switch",
            cols: 4,
          },
          {
            key: "webhook_url",
            label: "Webhook 地址",
            cols: 8,
            show: enabled("webhook_enabled"),
          },
          {
            key: "webhook_method",
            label: "请求方法",
            type: "select",
            items: ["POST", "GET"],
            cols: 4,
            show: enabled("webhook_enabled"),
          },
          {
            key: "webhook_timeout",
            label: "超时（秒）",
            type: "number",
            min: 1,
            max: 120,
            cols: 4,
            show: enabled("webhook_enabled"),
          },
        ],
      },
    ],
  }
}
