/**
 * 展示层统一状态词（方案 C）。
 *
 * 插件有四个页面各自持有一套状态词：任务中心用「处理中 / 下载中」、历史用
 * 「成功 / 失败 / 跳过」、频道监控用 queued / new / transferred、账本用原因
 * 词。同一个东西在不同页面叫不同名字，用户要自己在脑子里翻译。
 *
 * 这里只统一「展示」：把内部原始值收敛成七个词，颜色也一并给出。筛选仍然
 * 传后端认识的原始值（见 STATUS_QUERY），不改变任何接口语义。
 */

//: 统一后的七个状态词。
export const STATUS = {
  QUEUED: "排队",
  RUNNING: "进行中",
  PENDING: "待确认",
  SUCCESS: "成功",
  FAILED: "失败",
  SKIPPED: "已跳过",
  IGNORED: "忽略",
};

//: 原始值 → 统一词。未命中的值原样返回，避免新增状态被吞掉。
const RAW_TO_STATUS = {
  queued: STATUS.QUEUED,
  new: STATUS.QUEUED,
  pending: STATUS.QUEUED,
  waiting: STATUS.QUEUED,
  running: STATUS.RUNNING,
  transferring: STATUS.RUNNING,
  downloading: STATUS.RUNNING,
  postprocessing: STATUS.RUNNING,
  stopping: STATUS.RUNNING,
  transferred: STATUS.SUCCESS,
  failed: STATUS.FAILED,
  skipped: STATUS.SKIPPED,
  ignored: STATUS.IGNORED,
  排队: STATUS.QUEUED,
  排队中: STATUS.QUEUED,
  等待重试: STATUS.QUEUED,
  处理中: STATUS.RUNNING,
  下载中: STATUS.RUNNING,
  进行中: STATUS.RUNNING,
  待确认: STATUS.PENDING,
  成功: STATUS.SUCCESS,
  失败: STATUS.FAILED,
  秒传失败: STATUS.FAILED,
  跳过: STATUS.SKIPPED,
  已跳过: STATUS.SKIPPED,
  忽略: STATUS.IGNORED,
};

const STATUS_COLORS = {
  [STATUS.QUEUED]: "info",
  [STATUS.RUNNING]: "primary",
  [STATUS.PENDING]: "warning",
  [STATUS.SUCCESS]: "success",
  [STATUS.FAILED]: "error",
  [STATUS.SKIPPED]: "warning",
  [STATUS.IGNORED]: "grey",
};

/** 原始状态值 → 统一展示词。空值返回 fallback。 */
export function statusText(raw, fallback = "未知") {
  const text = String(raw ?? "").trim();
  if (!text) return fallback;
  return RAW_TO_STATUS[text] || text;
}

/** 原始状态值 → 统一展示词对应的 v-theme 颜色。 */
export function statusColor(raw) {
  return STATUS_COLORS[statusText(raw)] || "grey";
}

/** 统一词 → 后端筛选值集合（历史页 statuses 参数是多值）。 */
export const STATUS_QUERY = {
  [STATUS.QUEUED]: ["处理中"],
  [STATUS.RUNNING]: ["处理中", "下载中"],
  [STATUS.PENDING]: ["待确认"],
  [STATUS.SUCCESS]: ["成功"],
  [STATUS.FAILED]: ["失败"],
  [STATUS.SKIPPED]: ["跳过"],
};

//: 历史页可筛选的统一状态词（历史库里没有独立的「排队」与「忽略」）。
export const HISTORY_STATUS_OPTIONS = [
  STATUS.RUNNING,
  STATUS.PENDING,
  STATUS.SUCCESS,
  STATUS.FAILED,
  "秒传失败",
  STATUS.SKIPPED,
];

/** 把后端原始筛选值反向收敛成统一词，用于回显已选筛选项。 */
export function statusFilterLabel(raw) {
  const text = String(raw ?? "").trim();
  if (text === "秒传失败") return text;
  return statusText(text);
}

/** 统一词 → 提交给后端的状态数组。 */
export function statusQueryValues(labels) {
  const values = [];
  for (const label of labels || []) {
    const mapped = STATUS_QUERY[label];
    if (Array.isArray(mapped)) values.push(...mapped);
    else if (label) values.push(label);
  }
  return [...new Set(values)];
}
