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
