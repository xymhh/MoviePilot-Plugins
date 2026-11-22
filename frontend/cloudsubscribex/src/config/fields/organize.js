import {enabled} from "./helpers.js";

/** 115 网盘整理台：扫描预览 + 批量重命名/移动，不删除文件。 */
export function createOrganizeSection() {
  return {
    value: "organize",
    title: "网盘整理",
    icon: "mdi-folder-move-outline",
    groups: [
      {
        title: "整理范围",
        icon: "mdi-folder-search-outline",
        hint: "扫描只读；执行时只重命名和移动，不删除任何网盘文件。目标路径复用当前媒体库分类与重命名模板。手动整理时在整理台页面上现选源目录。",
        fields: [
          {
            key: "organize_max_files",
            label: "单次最多视频数",
            type: "number",
            default: 500,
            min: 1,
            max: 5000,
            suffix: "个",
            hint: "达到上限后只处理已扫描部分，防止大库扫描过久",
            cols: 4,
          },
          {
            key: "organize_max_depth",
            label: "最大递归深度",
            type: "number",
            default: 8,
            min: 1,
            max: 20,
            suffix: "层",
            hint: "源目录下的层级；一般 5-8 足够",
            cols: 4,
          },
          {
            key: "organize_include_subtitles",
            label: "随视频搬字幕",
            type: "switch",
            default: true,
            hint: "同目录、同季集或同文件名的字幕跟视频一起改名并移动",
            cols: 4,
          },
          {
            key: "organize_conflict_policy",
            label: "目标同名文件",
            type: "select",
            default: "skip",
            options: [
              {title: "跳过（推荐）", value: "skip"},
              {title: "自动加 (2) 后缀", value: "suffix"},
            ],
            hint: "绝不覆盖或删除目标端已有文件；视频被跳过时，对应字幕也跳过",
            cols: 12,
          },
        ],
      },
      {
        title: "定时整理",
        icon: "mdi-calendar-clock-outline",
        hint: "默认关闭。开启后按 Cron 跑漏斗扫描对账（dry-run：只分类记日志，不入队、不删文件）；漏斗关时不跑。旧整理台定时扫描随源目录退役而停用，手动整理仍可在整理台页面上现选源目录。",
        fields: [
          {
            key: "organize_schedule_enabled",
            label: "启用定时整理",
            type: "switch",
            default: false,
            cols: 4,
          },
          {
            key: "organize_schedule_cron",
            label: "执行周期",
            type: "cron",
            default: "0 4 * * *",
            placeholder: "0 4 * * *",
            hint: "默认每天 04:00；修改后需保存并重启插件生效",
            cols: 4,
            show: enabled("organize_schedule_enabled"),
          },
          {
            key: "organize_schedule_auto_execute",
            label: "扫描后自动执行",
            type: "switch",
            default: true,
            hint: "仅整理台定时扫描用；04:00 现在跑漏斗扫描对账（dry-run，不执行），本开关不影响它",
            cols: 4,
            show: enabled("organize_schedule_enabled"),
          },
          {
            key: "organize_schedule_notify",
            label: "完成后通知",
            type: "switch",
            default: true,
            hint: "定时任务结束后发送成功/跳过/失败汇总",
            cols: 12,
            show: enabled("organize_schedule_enabled"),
          },
        ],
      },
    ],
  };
}
