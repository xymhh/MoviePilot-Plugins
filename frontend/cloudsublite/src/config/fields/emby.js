import {enabled} from "./helpers.js";

/** Emby 302 反代：插件内起独立端口反向代理 Emby，播放请求 302 到网盘直链。 */
export function createEmbyProxySection() {
  return {
    value: "emby",
    title: "Emby 302 反代",
    icon: "mdi-call-split",
    groups: [
      {
        title: "反代设置",
        icon: "mdi-server-network",
        fields: [
          {
            key: "emby_proxy_enabled",
            label: "启用内置反代",
            type: "switch",
            hint: "播放 STRM 时把客户端 302 到网盘 CDN，客户端直连、不占服务器带宽。开启前先把 Emby 的访问入口指到本反代端口",
            cols: 12,
          },
          {
            key: "emby_proxy_port",
            label: "监听端口",
            type: "number",
            min: 1,
            max: 65535,
            default: 3010,
            cols: 6,
            show: enabled("emby_proxy_enabled"),
          },
          {
            key: "emby_proxy_upstream",
            label: "Emby 上游地址",
            type: "text",
            placeholder: "http://embyserver:8096",
            hint: "留空则用「基础设置 → 媒体服务器」里的地址",
            cols: 6,
            show: enabled("emby_proxy_enabled"),
          },
          {
            key: "emby_proxy_allowed_clients",
            label: "仅对这些客户端 302",
            type: "text",
            placeholder: "Infuse, Emby for Android",
            hint: "留空=全部客户端；填 UA 子串，逗号分隔。网页播放器建议不填，避免浏览器直连失败",
            cols: 12,
            show: enabled("emby_proxy_enabled"),
          },
        ],
      },
    ],
  };
}
