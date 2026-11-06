import {enabled} from "./helpers.js";

/** STRM 生成与播放：转存后写 .strm、增量补齐、全量校准。 */
export function createStrmSection() {
  return {
    value: "strm",
    title: "STRM 与播放",
    icon: "mdi-file-video-outline",
    groups: [
      {
        title: "生成设置",
        icon: "mdi-file-plus-outline",
        fields: [
          {
            key: "strm_generate_enabled",
            label: "转存后生成 STRM",
            type: "switch",
            hint: "转存后写同名 .strm；播放地址前缀为空则自动关闭",
            cols: 4,
          },
          {
            key: "strm_base_url",
            label: "播放地址前缀",
            type: "text",
            placeholder: "http://192.168.1.10:3000",
            hint: "必填，只填 协议://主机:端口；留空停用 STRM",
            cols: 8,
            show: enabled("strm_generate_enabled"),
          },
          {
            key: "strm_url_template",
            label: "STRM 内容模板",
            type: "textarea",
            hint: "支持 {base_url} {pickcode} {file_name}；留空用内置",
            cols: 12,
            show: enabled("strm_generate_enabled"),
          },
        ],
      },
      {
        title: "增量补齐",
        icon: "mdi-timer-sand",
        fields: [
          {
            key: "strm_increment_enabled",
            label: "定时补齐缺失 STRM",
            type: "switch",
            default: false,
            hint: "按间隔只补缺失 .strm；需先开「转存后生成 STRM」",
            cols: 4,
            show: enabled("strm_generate_enabled"),
          },
          {
            key: "strm_increment_minutes",
            label: "间隔（分钟）",
            type: "number",
            min: 1,
            max: 1440,
            default: 10,
            cols: 4,
            show: (config) =>
              Boolean(config.strm_generate_enabled) &&
              Boolean(config.strm_increment_enabled),
          },
          {
            key: "strm_increment_budget",
            label: "单轮时间预算（秒）",
            type: "number",
            min: 10,
            max: 3600,
            default: 180,
            hint: "超时即停，下一轮继续，避免长时间占锁",
            cols: 4,
            show: (config) =>
              Boolean(config.strm_generate_enabled) &&
              Boolean(config.strm_increment_enabled),
          },
        ],
      },
      {
        title: "全量校准",
        icon: "mdi-magnify-scan",
        fields: [
          {
            key: "strm_full_scan_enabled",
            label: "每天全量校准",
            type: "switch",
            default: false,
            hint: "每天 05:10 全量比对，补缺失并可选覆盖不一致",
            cols: 4,
            show: enabled("strm_generate_enabled"),
          },
          {
            key: "strm_full_scan_overwrite",
            label: "覆盖内容不一致的旧 STRM",
            type: "switch",
            default: false,
            hint: "关闭时只补缺失文件，已有 STRM 一律不动",
            cols: 4,
            show: enabled("strm_generate_enabled"),
          },
          {
            key: "strm_full_scan_now",
            label: "立即全量校准",
            type: "cleanup-residue",
            endpoint: "plugin/CloudSubscribeX/strm/full_scan",
            hint: "后台执行，进度与结果写入插件日志",
            cols: 4,
            show: enabled("strm_generate_enabled"),
          },
        ],
      },
    ],
  };
}
