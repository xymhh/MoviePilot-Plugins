import {enabled} from "../helpers.js";

export function createCommonSearchGroups(resourceTypeItems, sourceItems = []) {
  return [
    {
      tab: "common",
      title: "搜索顺序",
      icon: "mdi-sort",
      fields: [
        {
          key: "search_source_order",
          label: "搜索资源优先级",
          type: "priority-order",
          items: sourceItems,
          cols: 12,
        },
        {
          key: "resource_type_order",
          label: "资源类型优先级",
          type: "priority-order",
          items: resourceTypeItems,
          cols: 12,
        },
        {
          key: "magnet_metadata_url_template",
          label: "Magnet元数据地址模板",
          hint: "必须包含 {info_hash}",
          placeholder: "https://itorrents.org/torrent/{info_hash}.torrent",
          cols: 12,
        },
      ],
    },
    {
      tab: "common",
      title: "搜索代理",
      icon: "mdi-lan-connect",
      fields: [
        {
          key: "search_proxy",
          label: "代理地址",
          type: "proxy",
          hint: "留空直连，支持 http/socks5",
          placeholder: "http://127.0.0.1:7890",
          cols: 12,
        },
        {
          key: "search_proxy_username",
          label: "代理用户名",
          cols: 6,
        },
        {
          key: "search_proxy_password",
          label: "代理密码",
          type: "password",
          cols: 6,
        },
      ],
    },
    {
      tab: "common",
      title: "搜索性能",
      icon: "mdi-speedometer",
      fields: [
        {
          key: "search_concurrency",
          label: "搜索并发数",
          hint: "跨渠道并发数，建议 2，最大 5",
          type: "number",
          min: 1,
          max: 5,
          cols: 6,
        },
        {
          key: "search_source_timeout",
          label: "搜索超时",
          hint: "渠道默认等待超时，默认 60 秒",
          type: "number",
          min: 5,
          max: 120,
          suffix: "秒",
          cols: 6,
        },
        {
          key: "search_cache_enabled",
          label: "启用搜索缓存",
          hint: "避免短时间内重复检索",
          type: "switch",
          cols: 6,
        },
        {
          key: "search_cache_ttl_minutes",
          label: "缓存时间（分钟）",
          hint: "本地缓存有效期，默认 30 分钟",
          type: "number",
          min: 1,
          max: 1440,
          cols: 6,
          show: enabled("search_cache_enabled"),
        },
      ],
    },
    {
      tab: "common",
      title: "故障熔断",
      icon: "mdi-shield-alert-outline",
      fields: [
        {
          key: "search_circuit_breaker_enabled",
          label: "启用故障熔断机制",
          hint: "故障时自动跳过，期满自动探测恢复",
          type: "switch",
          cols: 4,
        },
        {
          key: "search_circuit_breaker_threshold",
          label: "连续失败熔断阈值",
          hint: "连续失败此次数后触发熔断",
          type: "number",
          min: 1,
          max: 10,
          suffix: "次",
          cols: 4,
          show: enabled("search_circuit_breaker_enabled"),
        },
        {
          key: "search_circuit_breaker_cooldown",
          label: "熔断冷却时间",
          hint: "熔断后跳过时长，期满自动恢复",
          type: "number",
          min: 10,
          max: 600,
          suffix: "秒",
          cols: 4,
          show: enabled("search_circuit_breaker_enabled"),
        },
      ],
    },
  ]
}
