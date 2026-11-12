export const ACTIVE_TASK_STATUSES = [
  "queued",
  "running",
  "stopping",
  "downloading",
  "transferring",
  "postprocessing",
]

export function isTaskActive(task) {
  return Boolean(task) && (
    Boolean(task.search_active) ||
    ACTIVE_TASK_STATUSES.includes(task.status)
  )
}

export function taskStatusLabel(task, {withProgress = false} = {}) {
  if (!task) return ""
  let label
  if (task.search_active) {
    label = "搜索中"
  } else if (task.status === "running") {
    const phase = String(task.phase || "")
    label = phase.includes("校验候选") || phase.includes("检查候选") ? "校验中" : "运行中"
  } else {
    label = {
      queued: "排队中",
      stopping: "停止中",
      downloading: "离线中",
      transferring: "转存中",
      postprocessing: "后处理中",
      completed: "完成",
      success: "完成",
      failed: "失败",
      stopped: "已停止",
      canceled: "已取消",
    }[task.status] || "运行中"
  }
  if (withProgress) {
    const percent = Math.round(taskProgress(task))
    if (percent > 0 && percent < 100 && !["queued", "stopping", "failed"].includes(task.status)) {
      return `${label} ${percent}%`
    }
  }
  return label
}

export function taskStatusColor(task) {
  if (!task) return "secondary"
  if (task.search_active) return "info"
  if (task.status === "running") {
    const phase = String(task.phase || "")
    return phase.includes("校验候选") || phase.includes("检查候选") ? "primary" : "info"
  }
  return {
    queued: "secondary",
    stopping: "warning",
    downloading: "info",
    transferring: "info",
    postprocessing: "primary",
    completed: "success",
    success: "success",
    failed: "error",
    stopped: "warning",
    canceled: "warning",
  }[task.status] || "secondary"
}

// 后端运行时 phase 是英文标识，UI 统一显示中文；未知/已中文化的值原样保留。
const PHASE_LABELS = {
  downloading: "下载中",
  uploading: "上传中",
  streaming: "边下边传",
  rapid_upload: "秒传中",
  hashing: "校验中",
  cache_ready: "缓存就绪",
  pending: "等待中",
  stopping: "停止中",
  done: "已完成",
  aborted: "已中止",
  canceled: "已取消",
  failed: "失败",
}

export function taskPhaseText(task) {
  if (!task) return ""
  if (task.error) return task.error
  const phase = String(task.phase || "").trim()
  if (phase && phase !== "等待调度") return PHASE_LABELS[phase] || phase
  if (task.status === "downloading") {
    const pending = Number(task.download_pending_count || 0)
    return pending > 0 ? `等待离线下载 ${pending} 个文件` : "等待离线下载"
  }
  if (task.status === "transferring") {
    const pending = Number(task.transfer_pending_count || 0)
    return pending > 0 ? `等待网盘转存 ${pending} 个文件` : "等待网盘转存"
  }
  if (task.postprocess_active) {
    return task.current_file || "正在处理文件"
  }
  return task.message || phase || ""
}

export function taskProgress(task) {
  if (!task) return 0
  if (task.transfer_active || ["pt_upgrade", "cross_transfer"].includes(task.task_kind)) {
    const percent = Number(task.progress)
    if (Number.isFinite(percent) && percent > 0) {
      return Math.max(0, Math.min(100, percent))
    }
    if (displayTotal(task) > 0) {
      return Math.max(0, Math.min(100, (displayTransferred(task) / displayTotal(task)) * 100))
    }
  }
  if (["downloading", "transferring", "postprocessing"].includes(task.status)) {
    const postProgress = Number(task.postprocess_progress || 0)
    if (postProgress > 0) return Math.max(0, Math.min(100, postProgress))
    const total = Number(task.postprocess_file_total || 0)
    if (total > 0) {
      return Math.max(0, Math.min(100, Math.round((Number(task.postprocess_file_completed || 0) / total) * 100)))
    }
  }
  const value = Number(task.progress || 0)
  return Math.max(0, Math.min(100, Number.isFinite(value) ? value : 0))
}

export function hasDeterminateProgress(task) {
  if (!task || task.status === "queued" || task.phase === "等待调度") return false
  if ((task.transfer_active || ["pt_upgrade", "cross_transfer"].includes(task.task_kind)) && displayTotal(task) > 0) {
    return true
  }
  if (["downloading", "transferring", "postprocessing"].includes(task.status)) {
    return Boolean(
      task.postprocess_active ||
      Number(task.postprocess_progress || 0) > 0 ||
      Number(task.postprocess_file_total || 0) > 0,
    )
  }
  return Number(task.progress || 0) > 0
}

export function displayTransferred(task) {
  return Number(task?.stage_total || 0) > 0
    ? Number(task?.stage_transferred || 0)
    : Number(task?.transferred || 0)
}

export function displayTotal(task) {
  return Number(task?.stage_total || 0) > 0
    ? Number(task?.stage_total || 0)
    : Number(task?.total || 0)
}

export function formatSize(value) {
  let size = Math.max(0, Number(value || 0))
  const units = ["B", "KB", "MB", "GB", "TB"]
  let unit = 0
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024
    unit += 1
  }
  return `${size.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`
}

export function formatSpeed(value) {
  return `${formatSize(value)}/s`
}

// ── 1.9.81 任务卡片增强：耗时 / 搜索渠道 / 后处理分步 ────────────────
// 后端字段早已存在（core/services/runtime.py 的 _idle_search_state /
// _idle_postprocess_state、handlers/search/service.py 的 channels_state），
// 这里只做展示层格式化，不改后端。

export function formatDuration(seconds) {
  const sec = Math.max(0, Number(seconds || 0))
  if (!Number.isFinite(sec) || sec <= 0) return ""
  if (sec < 60) return `${sec < 10 ? sec.toFixed(1) : Math.round(sec)}s`
  const mins = Math.floor(sec / 60)
  const remainSec = Math.round(sec % 60)
  if (mins < 60) return `${mins}m ${remainSec}s`
  const hours = Math.floor(mins / 60)
  return `${hours}h ${mins % 60}m`
}

export function formatChannelElapsed(ms) {
  const value = Math.max(0, Number(ms || 0))
  if (value <= 0) return ""
  if (value < 1000) return `${Math.round(value)}ms`
  return `${(value / 1000).toFixed(1)}s`
}

// 渠道 key -> 展示名；未知渠道回落大写 key（与后端 get_source_display_name 口径一致）
const CHANNEL_LABELS = {
  quark: "夸克网盘",
  alipan: "阿里云盘",
  "115": "115网盘",
  xunlei: "迅雷云盘",
  uc: "UC网盘",
  pansou: "盘搜",
  tg: "Telegram",
  mikan: "蜜柑计划",
  animegarden: "动漫花园",
  manual: "手动资源",
  seedhub: "SeedHub",
  hdhive: "HDHive",
  dian115: "Dian115",
}

export function channelLabel(channel) {
  // 允许传渠道对象或纯 key 字符串；空对象/空串一律返回空，避免 String({}) 得到 "[object Object]"
  const raw = typeof channel === "string" ? channel : channel?.key
  const key = String(raw || "").trim()
  if (!key) return ""
  const named = typeof channel === "object" ? String(channel?.name || "").trim() : ""
  return named || CHANNEL_LABELS[key.toLowerCase()] || key.toUpperCase()
}

export function channelIcon(channel) {
  const status = String(channel?.status || "").trim()
  if (status === "success") {
    return Number(channel?.count || 0) > 0 ? "mdi-check-circle" : "mdi-minus-circle-outline"
  }
  if (status === "searching") return "mdi-magnify"
  if (status === "timeout") return "mdi-clock-alert-outline"
  if (status === "circuit_break") return "mdi-flash-off"
  if (status === "failed") return "mdi-alert-circle-outline"
  return "mdi-clock-outline"
}

export function channelColor(channel) {
  const status = String(channel?.status || "").trim()
  if (status === "success") return Number(channel?.count || 0) > 0 ? "success" : "secondary"
  if (status === "searching") return "info"
  if (status === "timeout") return "warning"
  if (status === "circuit_break") return "warning"
  if (status === "failed") return "error"
  return "secondary"
}

export function channelTooltip(channel) {
  if (!channel) return ""
  const name = channelLabel(channel)
  const status = String(channel?.status || "").trim()
  const parts = [name]
  if (status === "success") {
    parts.push(Number(channel?.count || 0) > 0 ? `找到 ${channel.count} 条` : "无结果")
  } else if (status === "searching") {
    parts.push("搜索中")
  } else if (status === "failed") {
    parts.push("失败")
  } else if (status === "timeout") {
    parts.push("超时")
  } else if (status === "circuit_break") {
    parts.push("熔断冷却")
  } else if (status === "skipped") {
    parts.push("已跳过")
  } else {
    parts.push("等待中")
  }
  const elapsed = formatChannelElapsed(channel?.elapsed_ms)
  if (elapsed) parts.push(elapsed)
  const error = String(channel?.error || "").trim()
  if (error) parts.push(error)
  return parts.join(" · ")
}

export function searchChannelsOf(task) {
  const channels = task?.search_channels
  return Array.isArray(channels) ? channels.filter((item) => item && item.key) : []
}

export function postprocessStepsOf(task) {
  const steps = task?.postprocess_steps
  return Array.isArray(steps) ? steps.filter((step) => step?.key && step?.label) : []
}

export function postprocessStepState(task, index) {
  const currentIndex = Math.max(0, Number(task?.postprocess_step_index || 0))
  const position = Math.max(0, Number(index || 0))
  if (position < currentIndex) return "completed"
  if (position === currentIndex) {
    return String(task?.postprocess_step || "") ? "active" : "pending"
  }
  return "pending"
}

export function postprocessStepIcon(task, index) {
  const state = postprocessStepState(task, index)
  if (state === "completed") return "mdi-check-circle"
  if (state === "active") return "mdi-progress-clock"
  return "mdi-circle-outline"
}

export function taskElapsedSeconds(task, nowMs = Date.now()) {
  const explicit = Number(task?.elapsed_seconds)
  if (Number.isFinite(explicit) && explicit > 0) return explicit
  // 后端 started_at / queued_at 是**秒**级时间戳（time.time()），不能当毫秒用
  const started = Number(task?.started_at || task?.queued_at || 0)
  if (!started) return 0
  const current = Number(nowMs || 0) / 1000
  return Math.max(0, current - started)
}
