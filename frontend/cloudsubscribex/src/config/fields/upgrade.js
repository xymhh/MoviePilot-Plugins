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
            hint: "转存前按「季集」判重（不再依赖文件名）：新版本不差于已有版本时删旧写新，更差则跳过；旧文件含手动入库/老模板遗留也会删除。关闭后回退为仅按目标文件名判重。",
            cols: 4,
          },
          {
            key: "verify_landed_enabled",
            label: "转存后校验落盘（推荐开）",
            type: "switch",
            hint: "转存/后处理判定「成功」前，核对目标文件真的在目标季目录：查无实物则记「待确认」（不计成功、不写整理历史、不发成功通知），避免假成功变成下一轮的幻影版本。关闭后逐字节回退旧行为。",
            cols: 4,
          },
          {
            key: "prefer_template_named_version",
            label: "保留符合重命名格式的版本",
            type: "switch",
            hint: "现有文件名已等于当前「重命名格式」渲染结果时直接保留，不再被评分更高的候选替换（按现有重命名规则优先）；关闭后完全按评分/体积决定替换。",
            cols: 4,
          },
        ],
      },
      {
        title: "入库择优（按体积）",
        icon: "mdi-scale-balance",
        hint: "只影响「新入库」：转存某集时若库里已有同集文件，按「分辨率 > 体积」判断要不要换掉旧的；不做存量全量洗版。",
        fields: [
          {
            key: "quality_prefer_enabled",
            label: "入库时按体积择优",
            type: "switch",
            hint: "打开后：新候选分辨率更高（体积不低于旧版 60%）或同分辨率体积多出阈值，就删旧写新；否则跳过并记「现有版本更优」。",
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
          {
            key: "quality_ignore_rename_template",
            label: "择优时忽略命名模板拦截",
            type: "switch",
            hint: "本插件转存的文件名都符合重命名模板，不忽略的话「已符合模板就保留」会挡住所有替换；默认打开。",
            cols: 4,
          },
        ],
      },
      {
        title: "洗版范围",
        icon: "mdi-movie-filter-outline",
        hint: "仅处理已开启原生 best_version 的订阅；不选择时覆盖全部此类订阅，选择后仅处理指定订阅。",
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
