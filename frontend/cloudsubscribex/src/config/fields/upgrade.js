export function createUpgradeSection(options) {
  return {
    value: "upgrade",
    title: "洗版设置",
    icon: "mdi-auto-fix",
    groups: [
      {
        title: "洗版功能",
        icon: "mdi-source-branch",
        hint: "候选资源继续使用优先级规则组筛选和排序；插件不附加清晰度规则。",
        fields: [
          {
            key: "enable_cloud_upgrade",
            label: "启用网盘洗版",
            type: "switch",
            hint: "从网盘候选中查找更优版本；关闭后订阅不会进入网盘洗版流程。",
            cols: 4,
          },
          {
            key: "enable_pt_upgrade",
            label: "启用 PT 洗版",
            type: "switch",
            hint: "完成 PT 下载和本地整理后，将符合洗版范围及评分条件的文件上传到当前网盘。",
            cols: 4,
          },
          {
            key: "upgrade_mode",
            label: "洗版模式",
            type: "select",
            items: [
              {title: "保留最大文件", value: "largest"},
              {title: "保留最小文件", value: "smallest"},
              {title: "直接替换", value: "replace"},
              {title: "新旧共存", value: "coexist"},
            ],
            hint: "评分更高时执行洗版；同评分时最大/最小模式按文件大小决定，默认保留最大文件。",
            cols: 4,
          },
          {
            key: "episode_dedup_enabled",
            label: "按季集判重替换",
            type: "switch",
            hint: "按「季集」判重：新不差于旧则删旧写新",
            disabled: (config) => config.upgrade_mode === "coexist",
            cols: 4,
          },
          {
            key: "verify_landed_enabled",
            label: "转存后核对入库（推荐开）",
            type: "switch",
            hint: "判成功前核对文件真在季目录，查无记「待确认」",
            cols: 4,
          },
          {
            key: "prefer_template_named_version",
            label: "保留符合重命名格式的版本",
            type: "switch",
            hint: "文件名已符合重命名格式时，仅评分或体积提升才替换",
            cols: 4,
          },
        ],
      },
      {
        title: "入库择优（按体积）",
        icon: "mdi-scale-balance",
        hint: "只管新入库：库里已有同集时按「分辨率>体积」决定替换",
        fields: [
          {
            key: "quality_prefer_enabled",
            label: "入库时按体积择优",
            type: "switch",
            hint: "分辨率更高或同分辨率体积超出阈值时替换",
            disabled: (config) =>
              (config.upgrade_mode === "coexist") ||
              (config.episode_dedup_enabled === false),
            cols: 4,
          },
          {
            key: "quality_min_gain_percent",
            label: "体积增益阈值（%）",
            type: "number",
            min: 0,
            hint: "同分辨率时，新文件体积至少比旧文件多这个百分比才算更好，默认 10。",
            cols: 4,
          },
          {
            key: "quality_min_gain_mb",
            label: "体积增益阈值（MB）",
            type: "number",
            min: 0,
            hint: "同分辨率时，新文件至少比旧文件多这么多 MB（与百分比同时满足），默认 200。",
            cols: 4,
          },
        ],
      },
      {
        title: "洗版范围",
        icon: "mdi-movie-filter-outline",
        hint: "仅对开启原生 best_version 的订阅生效；留空=全部",
        fields: [
          {
            key: "upgrade_subscribe_ids",
            label: "单独开启洗版的订阅",
            type: "select",
            items: options.subscribes,
            multiple: true,
            searchable: true,
            hint: "所选电影或电视剧订阅参与网盘洗版。",
            cols: 12,
          },
        ],
      },
      {
        title: "洗版参数",
        icon: "mdi-tune-vertical",
        fields: [
          {
            key: "upgrade_episode_limit",
            label: "每轮洗版集数上限",
            type: "number",
            min: 0,
            hint: "每轮最多洗版这么多集，0 不限；追更剧建议 3-5",
            cols: 4,
          },
          {
            key: "self_heal_interval",
            label: "评分自愈间隔（分钟）",
            type: "number",
            min: 0,
            hint: "按真实整理、转存和媒体库记录刷新评分；0 表示关闭。",
            cols: 4,
          },
        ],
      },
    ],
  }
}
