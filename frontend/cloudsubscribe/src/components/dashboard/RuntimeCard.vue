<template>
  <div class="runtime-panel">
    <div v-if="tasks.length" class="task-list">
      <div v-for="task in tasks" :key="task.id" class="task-row">
        <v-icon
          class="task-leading-icon"
          :icon="
            task.task_kind === 'cross_transfer'
              ? 'mdi-swap-horizontal-bold'
              : task.media_type === '电影'
                ? 'mdi-movie-outline'
                : 'mdi-television-classic'
          "
          :color="taskColor(task.status)"
          size="small" />
        <div class="task-content">
          <div class="task-line">
            <span class="task-name text-body-2 font-weight-medium">{{ task.title }}</span>
            <span v-if="task.season" class="text-caption text-medium-emphasis">
              S{{ String(task.season).padStart(2, "0") }}
            </span>
            <v-chip
              v-if="task.task_kind === 'pt_upgrade' || task.task_kind === 'cloud_upgrade'"
              size="x-small"
              variant="outlined"
              :color="task.task_kind === 'pt_upgrade' ? 'warning' : 'primary'">
              {{ task.task_kind === "pt_upgrade" ? "PT 洗版" : "网盘洗版" }}
            </v-chip>
            <v-chip :color="taskStatusColor(task)" size="x-small" variant="tonal" class="task-status-chip">
              <v-progress-circular
                v-if="task.search_active"
                indeterminate
                size="10"
                width="2"
                class="mr-1" />
              <span class="task-status-label">{{ taskStatusBaseText(task) }}</span>
            </v-chip>
          </div>
          <div class="task-meta">
            <div class="task-phase-wrap">
              <span class="task-phase text-caption">
                {{
                  task.postprocess_active || ["downloading", "transferring", "postprocessing"].includes(task.status)
                    ? postprocessingSummary(task)
                    : task.task_kind === "cross_transfer"
                      ? task.error || task.message || task.phase
                      : task.status === "failed"
                        ? task.message || task.phase || "处理失败"
                        : task.phase || "等待调度"
                }}
              </span>
              <v-btn
                v-if="hasTaskDetails(task)"
                class="task-detail-toggle ml-1"
                :icon="isTaskExpanded(task.id) ? 'mdi-chevron-up' : 'mdi-information-outline'"
                variant="text"
                size="x-small"
                :title="isTaskExpanded(task.id) ? '收起详情' : '查看详情'"
                :aria-label="isTaskExpanded(task.id) ? '收起详情' : '查看详情'"
                @click="toggleTaskDetails(task.id)" />
            </div>
            <div
              v-if="
                (task.transfer_active || ['pt_upgrade', 'cross_transfer'].includes(task.task_kind)) &&
                displayTotal(task) > 0
              "
              class="task-transfer flex-shrink-0 text-caption">
              {{ formatSize(displayTransferred(task)) }} / {{ formatSize(displayTotal(task)) }} ·
              {{ formatSpeed(task.speed_bytes_per_second || task.upload_speed) }}
            </div>
          </div>
          <!-- 进度条与百分比：横向一体化显示 -->
          <div v-if="shouldShowProgress(task)" class="task-progress-container d-flex align-center ga-2">
            <v-progress-linear
              :class="[
                'task-progress',
                'flex-grow-1',
                {
                  'task-progress--active':
                    task.postprocess_active ||
                    ['downloading', 'transferring', 'postprocessing'].includes(task.status) ||
                    Boolean(task.transfer_active) ||
                    Boolean(task.search_active),
                },
              ]"
              :model-value="taskProgress(task)"
              :style="progressStyle(task)"
              :indeterminate="isProgressIndeterminate(task)"
              :color="taskColor(task.status)"
              height="3"
              rounded />
            <span
              v-if="hasDeterminateProgress(task)"
              class="task-progress-percent text-caption font-weight-bold flex-shrink-0"
              :style="{ color: `rgb(var(--v-theme-${taskColor(task.status)}))` }">
              {{ Math.round(taskProgress(task)) }}%
            </span>
          </div>
          <v-expand-transition>
            <div v-if="hasTaskDetails(task) && isTaskExpanded(task.id)" class="task-details text-caption">
              <!-- 搜索渠道详情：全宽流式横向胶囊排布 -->
              <div v-if="hasSearchDetails(task)" class="task-search-details">
                <div class="task-search-channels">
                  <div
                    v-for="ch in task.search_channels"
                    :key="ch.key"
                    class="task-channel-badge"
                    :class="[
                      `task-channel-badge--${ch.status}`,
                      { 'task-channel-badge--empty': ch.status === 'success' && ch.count === 0 }
                    ]"
                    :title="channelTooltip(task, ch)">
                    <v-progress-circular
                      v-if="ch.status === 'searching'"
                      indeterminate
                      size="10"
                      width="1.6"
                      class="task-channel-icon task-channel-spinner" />
                    <v-icon v-else :icon="channelIcon(ch)" size="11" class="task-channel-icon" />
                    <span class="task-channel-name">{{ ch.name }}</span>

                    <span
                      v-if="ch.status === 'success'"
                      class="task-channel-tag"
                      :class="ch.count > 0 ? 'task-channel-tag--count' : 'task-channel-tag--zero'">
                      {{ ch.count }}条
                    </span>
                    <span v-else-if="ch.status === 'searching'" class="task-channel-tag task-channel-tag--searching">
                      搜索中
                    </span>
                    <span v-else-if="ch.status === 'timeout'" class="task-channel-tag task-channel-tag--warn">
                      超时
                    </span>
                    <span v-else-if="ch.status === 'circuit_break'" class="task-channel-tag task-channel-tag--warn">
                      熔断
                    </span>
                    <span v-else-if="ch.status === 'failed'" class="task-channel-tag task-channel-tag--err">
                      失败
                    </span>
                    <span v-else-if="ch.status === 'pending'" class="task-channel-tag task-channel-tag--pending">
                      等待
                    </span>

                    <span v-if="channelDisplayElapsed(task, ch)" class="task-channel-time">
                      {{ channelDisplayElapsed(task, ch) }}
                    </span>
                  </div>
                </div>
              </div>
              <div v-if="task.current_file" class="task-detail-row">
                <span class="task-detail-label">当前文件</span>
                <span class="task-current-file" :title="task.current_file">{{ task.current_file }}</span>
              </div>
              <div v-if="Number(task.postprocess_file_total || 0) > 0" class="task-detail-row">
                <span class="task-detail-label">文件进度</span>
                <span>
                  已完成 {{ Number(task.postprocess_file_completed || 0) }} /
                  {{ Number(task.postprocess_file_total || 1) }} 个
                </span>
              </div>
              <div v-if="postprocessSteps(task).length" class="task-detail-row task-process-row">
                <span class="task-detail-label">处理过程</span>
                <div class="task-process">
                  <div
                    v-for="(step, index) in postprocessSteps(task)"
                    :key="step.key"
                    class="task-process-step"
                    :class="`task-process-step--${postprocessStepState(task, index)}`">
                    <v-icon :icon="postprocessStepIcon(task, index)" size="14" />
                    <span>{{ step.label }}</span>
                  </div>
                </div>
              </div>
            </div>
          </v-expand-transition>
        </div>
        <v-btn
          v-if="canStop(task)"
          icon="mdi-stop-circle-outline"
          color="warning"
          variant="text"
          size="x-small"
          class="task-stop-btn"
          :loading="task.status === 'stopping'"
          title="停止此任务"
          @click="emit('stop-task', task.id)" />
        <v-icon v-else :icon="resultIcon(task.status, task.task_kind)" :color="taskColor(task.status)" size="small" />
      </div>
    </div>
    <div v-else class="idle-state text-medium-emphasis">
      <v-progress-circular v-if="active" indeterminate size="44" width="3" color="primary" />
      <v-icon v-else icon="mdi-check-circle-outline" color="success" size="44" />
      <div class="text-subtitle-2 font-weight-medium mt-3">
        {{ active ? runtime.task || "正在准备订阅任务" : "当前没有订阅任务" }}
      </div>
      <div class="text-caption mt-1">
        {{ active ? "正在加载订阅任务列表" : runtime.task || "等待下一次订阅搜索" }}
      </div>
    </div>
  </div>
</template>
<script setup>
import {computed, onMounted, onUnmounted, ref} from "vue";

const props = defineProps({
  runtime: {type: Object, required: true},
  active: Boolean,
})
const emit = defineEmits(["stop-task"]);
const active = computed(() => props.active);
const expandedTaskIds = ref(new Set());
const tasks = computed(() =>
  (props.runtime.tasks || []).filter(
    (task) =>
      task.task_kind === "cross_transfer" || ["queued", "running", "stopping", "downloading", "transferring", "postprocessing"].includes(task.status),
  ),
)

const now = ref(Date.now());
let timer = null;

onMounted(() => {
  timer = setInterval(() => {
    if (tasks.value.some((t) => t.search_active || t.status === "running")) {
      now.value = Date.now();
    }
  }, 200);
});

onUnmounted(() => {
  if (timer) {
    clearInterval(timer);
    timer = null;
  }
});

function postprocessingSummary(task) {
  if (task?.status === "downloading") {
    const pendingCount = Number(task?.download_pending_count || 0);
    return pendingCount > 0 ? `等待离线下载 ${pendingCount} 个文件` : "等待离线下载";
  }
  if (task?.status === "transferring") {
    const pendingCount = Number(task?.transfer_pending_count || 0);
    return pendingCount > 0 ? `等待网盘转存 ${pendingCount} 个文件` : "等待网盘转存";
  }
  if (task?.postprocess_active) {
    return currentPostprocessStep(task)?.label || "正在处理文件";
  }
  if (task?.download_pending_count > 0) {
    return `等待离线下载 ${task.download_pending_count} 个文件`;
  }
  if (task?.transfer_pending_count > 0) {
    return `等待网盘转存 ${task.transfer_pending_count} 个文件`;
  }
  const pendingCount = Number(task?.pending_count || 0);
  return pendingCount > 0 ? `${pendingCount} 个文件待完成后处理` : "正在完成文件后处理";
}

function isTransferringOrPostprocessing(task) {
  if (!task) return false;
  return Boolean(
    task.postprocess_active ||
    task.transfer_active ||
    task.current_file ||
    Number(task.postprocess_file_total || 0) > 0 ||
    ["downloading", "transferring", "postprocessing"].includes(task.status) ||
    /转存|下载|后处理|整理/.test(String(task.phase || "")),
  );
}

function hasTaskDetails(task) {
  return hasPostprocessDetails(task) || hasSearchDetails(task);
}

function hasSearchDetails(task) {
  // 进入转存或后处理阶段后，自动隐藏搜索渠道信息，聚焦转存与处理进度
  if (isTransferringOrPostprocessing(task)) {
    return false;
  }
  return Array.isArray(task?.search_channels) && task.search_channels.length > 0;
}

function channelIcon(ch) {
  if (!ch) return "mdi-circle-outline";
  if (ch.status === "success") {
    return ch.count > 0 ? "mdi-check-circle" : "mdi-minus-circle-outline";
  }
  if (ch.status === "searching") {
    return "mdi-magnify";
  }
  if (ch.status === "timeout") {
    return "mdi-clock-alert-outline";
  }
  if (ch.status === "circuit_break") {
    return "mdi-flash-off";
  }
  if (ch.status === "failed") {
    return "mdi-alert-circle-outline";
  }
  return "mdi-clock-outline";
}

function channelTooltip(task, ch) {
  if (!ch) return "";
  const name = ch.name || ch.key;
  const elapsedStr = channelDisplayElapsed(task, ch);
  const elapsedPart = elapsedStr ? ` · 耗时 ${elapsedStr}` : "";
  if (ch.status === "success") {
    return `${name}：成功返回 ${ch.count} 条候选数据${elapsedPart}`;
  }
  if (ch.status === "searching") {
    const liveElapsed = elapsedStr ? ` (已耗时 ${elapsedStr})` : "";
    return `${name}：正在并发查询中${liveElapsed}...`;
  }
  if (ch.status === "timeout") {
    return `${name}：搜索请求超时已跳过${elapsedPart}`;
  }
  if (ch.status === "circuit_break") {
    return `${name}：${ch.error || "多次失败熔断保护中"}`;
  }
  if (ch.status === "failed") {
    return `${name}：搜索失败 (${ch.error || "未知异常"})${elapsedPart}`;
  }
  return `${name}：等待检索`;
}

function hasPostprocessDetails(task) {
  return Boolean(
    task?.postprocess_active ||
    task?.current_file ||
    Number(task?.postprocess_file_total || 0) > 0 ||
    postprocessSteps(task).length,
  )
}

function postprocessSteps(task) {
  return Array.isArray(task?.postprocess_steps) ? task.postprocess_steps.filter((step) => step?.key && step?.label) : [];
}

function currentPostprocessStep(task) {
  const steps = postprocessSteps(task);
  const currentKey = String(task?.postprocess_step || "");
  return steps.find((step) => step.key === currentKey) || null;
}

function postprocessStepState(task, index) {
  const currentIndex = Math.max(0, Number(task?.postprocess_step_index || 0));
  if (index < currentIndex) return "completed";
  if (index === currentIndex) return "active";
  return "pending";
}

function postprocessStepIcon(task, index) {
  const state = postprocessStepState(task, index);
  if (state === "completed") return "mdi-check-circle";
  if (state === "active") return "mdi-progress-clock";
  return "mdi-circle-outline";
}

function isTaskExpanded(taskId) {
  return expandedTaskIds.value.has(taskId);
}

function toggleTaskDetails(taskId) {
  const nextIds = new Set(expandedTaskIds.value);
  if (nextIds.has(taskId)) {
    nextIds.delete(taskId);
  } else {
    nextIds.add(taskId);
  }
  expandedTaskIds.value = nextIds;
}

function canStop(task) {
  return ["queued", "running", "stopping", "downloading", "transferring", "postprocessing"].includes(task?.status);
}

function formatDuration(seconds) {
  const sec = Math.max(0, Number(seconds || 0));
  if (sec <= 0) return "";
  if (sec < 60) {
    return `${sec < 10 ? sec.toFixed(1) : Math.round(sec)}s`;
  }
  const mins = Math.floor(sec / 60);
  const remainSec = Math.round(sec % 60);
  if (mins < 60) {
    return `${mins}m ${remainSec}s`;
  }
  const hours = Math.floor(mins / 60);
  const remainMins = mins % 60;
  return `${hours}h ${remainMins}m`;
}

function formatChannelElapsed(ms) {
  const m = Number(ms || 0);
  if (m <= 0) return "";
  const sec = m / 1000;
  if (sec < 0.1) return "<0.1s";
  return `${sec < 10 ? sec.toFixed(1) : Math.round(sec)}s`;
}

const localChannelStartTimes = new Map();

function getChannelElapsedMs(task, ch) {
  if (!ch) return 0;
  if (ch.status === "searching") {
    const key = `${task?.id || ""}:${ch.key || ch.name}`;
    if (ch.started_at && Number(ch.started_at) > 0) {
      const startMs = Number(ch.started_at) * 1000;
      return Math.max(0, now.value - startMs);
    }
    if (!localChannelStartTimes.has(key)) {
      const initialElapsed = Number(ch.elapsed_ms || 0);
      localChannelStartTimes.set(key, now.value - initialElapsed);
    }
    const startMs = localChannelStartTimes.get(key);
    return Math.max(0, now.value - startMs);
  }
  return Number(ch.elapsed_ms || 0);
}

function channelDisplayElapsed(task, ch) {
  const ms = getChannelElapsedMs(task, ch);
  return formatChannelElapsed(ms);
}

function taskElapsedText(task) {
  if (!task) return "";
  if (task.status === "queued" || task.phase === "等待调度") return "";
  const sec = Number(task.elapsed_seconds || 0);
  if (sec > 0) {
    return formatDuration(sec);
  }
  return "";
}

function taskStatusBaseText(task) {
  if (!task) return "未知";
  if (task.search_active) {
    return "搜索中";
  }
  const status = task.status;
  if (status === "running") {
    const phase = String(task.phase || "");
    if (phase.includes("校验候选") || phase.includes("检查候选")) {
      return "校验中";
    }
    return "运行中";
  }
  return (
    {
      queued: "排队中",
      running: "运行中",
      stopping: "停止中",
      downloading: "离线中",
      transferring: "转存中",
      postprocessing: "后处理中",
      completed: "完成",
      success: "完成",
      failed: "失败",
      stopped: "已停止",
      canceled: "已取消",
    }[status] || "未知"
  );
}

function taskStatusElapsed(task) {
  if (!task) return "";
  if (task.search_active) {
    return taskElapsedText(task);
  }
  if (task.status === "running") {
    const phase = String(task.phase || "");
    if (phase.includes("校验候选") || phase.includes("检查候选")) {
      return taskElapsedText(task);
    }
  }
  return "";
}

function taskStatusText(task) {
  const base = taskStatusBaseText(task);
  const elapsed = taskStatusElapsed(task);
  return elapsed ? `${base} (${elapsed})` : base;
}

function taskStatusColor(task) {
  if (!task) return "secondary";
  if (task.search_active) return "info";
  const status = task.status;
  if (status === "running") {
    const phase = String(task.phase || "");
    if (phase.includes("校验候选") || phase.includes("检查候选")) {
      return "primary";
    }
    return "info";
  }
  return (
    {
      queued: "secondary",
      running: "info",
      stopping: "warning",
      downloading: "info",
      transferring: "info",
      postprocessing: "primary",
      completed: "success",
      success: "success",
      failed: "error",
      stopped: "warning",
      canceled: "warning",
    }[status] || "secondary"
  );
}

function taskStatus(status) {
  return (
    {
      queued: "排队中",
      running: "运行中",
      stopping: "停止中",
      downloading: "离线中",
      transferring: "转存中",
      postprocessing: "后处理中",
      completed: "完成",
      success: "完成",
      failed: "失败",
      stopped: "已停止",
      canceled: "已取消",
    }[status] || "未知"
  );
}

function taskColor(status) {
  return (
    {
      queued: "secondary",
      running: "info",
      stopping: "warning",
      downloading: "info",
      transferring: "info",
      postprocessing: "primary",
      completed: "success",
      success: "success",
      failed: "error",
      stopped: "warning",
      canceled: "warning",
    }[status] || "secondary"
  )
}

const progressColors = [
  {progress: 0, color: [66, 165, 245]},
  {progress: 35, color: [38, 198, 218]},
  {progress: 65, color: [255, 179, 0]},
  {progress: 85, color: [102, 187, 106]},
  {progress: 100, color: [46, 125, 50]},
]

function progressColor(progress) {
  const value = Math.max(0, Math.min(100, Number(progress || 0)));
  const upperIndex = progressColors.findIndex((item) => value <= item.progress);
  if (upperIndex <= 0) return `rgb(${progressColors[0].color.join(", ")})`;
  const lower = progressColors[upperIndex - 1];
  const upper = progressColors[upperIndex];
  const ratio = (value - lower.progress) / (upper.progress - lower.progress);
  const color = lower.color.map((channel, index) => Math.round(channel + (upper.color[index] - channel) * ratio));
  return `rgb(${color.join(", ")})`;
}

function progressStyle(task) {
  if (["failed"].includes(task?.status)) {
    return {"--task-progress-gradient": "linear-gradient(90deg, #ff8a80, #d32f2f)"};
  }
  if (["stopping", "stopped", "canceled"].includes(task?.status)) {
    return {"--task-progress-gradient": "linear-gradient(90deg, #ffd54f, #fb8c00)"};
  }
  const progress = taskProgress(task);
  return {
    "--task-progress-gradient": `linear-gradient(90deg, ${progressColor(progress)}, ${progressColor(Math.min(100, progress + 18))})`,
  }
}

function shouldShowProgress(task) {
  if (!task) return false;
  // 等待调度或排队中不显示进度条和动画，解决大量排队任务时的严重卡顿
  if (task.status === "queued" || task.phase === "等待调度") {
    return false;
  }
  return true;
}

function hasDeterminateProgress(task) {
  if (!task) return false;
  if (task.status === "queued" || task.phase === "等待调度") {
    return false;
  }
  // 1. 跨盘或 PT 传输有字节数
  if (
    (task.transfer_active || ["pt_upgrade", "cross_transfer"].includes(task.task_kind)) &&
    displayTotal(task) > 0
  ) {
    return true;
  }
  // 2. 转存中、下载中或后处理中：只要有步骤活跃、或有文件进度、或有非零百分比
  if (["downloading", "transferring", "postprocessing"].includes(task.status)) {
    return Boolean(
      task.postprocess_active ||
      Number(task.postprocess_progress || 0) > 0 ||
      Number(task.postprocess_file_total || 0) > 0,
    );
  }
  // 3. 运行中且具备阶段进度
  if (["running"].includes(task.status) && Number(task.progress || 0) > 0) {
    return true;
  }
  return false;
}

function isProgressIndeterminate(task) {
  if (!task) return false;
  // 等待调度和排队状态绝不运行动画
  if (task.status === "queued" || task.phase === "等待调度") {
    return false;
  }
  if (["completed", "success", "failed", "stopped", "canceled"].includes(task.status)) {
    return false;
  }
  if (hasDeterminateProgress(task)) {
    return false;
  }
  return true;
}

function taskProgress(task) {
  if (!task) return 0;
  if (
    (task.transfer_active || ["pt_upgrade", "cross_transfer"].includes(task.task_kind)) &&
    displayTotal(task) > 0
  ) {
    const total = displayTotal(task);
    const transferred = displayTransferred(task);
    return Math.max(0, Math.min(100, (transferred / total) * 100));
  }
  if (["downloading", "transferring", "postprocessing"].includes(task.status)) {
    const postProgress = Number(task.postprocess_progress || 0);
    if (postProgress > 0) {
      return Math.max(0, Math.min(100, postProgress));
    }
    const total = Number(task.postprocess_file_total || 0);
    if (total > 0) {
      const completed = Number(task.postprocess_file_completed || 0);
      return Math.max(0, Math.min(100, Math.round((completed / total) * 100)));
    }
  }
  const value = Number(task.progress || 0);
  return Math.max(0, Math.min(100, Number.isFinite(value) ? value : 0));
}

function resultIcon(status, taskKind) {
  if (taskKind === "cross_transfer" && status === "running") {
    return "mdi-cloud-upload-outline";
  }
  if (taskKind === "pt_upgrade" && status === "running") {
    return "mdi-cloud-upload-outline";
  }
  return ["downloading", "transferring", "postprocessing"].includes(status)
    ? "mdi-cog-sync-outline"
    : ["completed", "success"].includes(status)
      ? "mdi-check-circle"
      : status === "failed"
        ? "mdi-alert-circle"
        : "mdi-stop-circle"
}

function formatSize(value) {
  let size = Math.max(0, Number(value || 0));
  const units = ["B", "KB", "MB", "GB", "TB"];
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
}

function formatSpeed(value) {
  return `${formatSize(value)}/s`;
}

function displayTransferred(task) {
  return Number(task?.stage_total || 0) > 0 ? Number(task?.stage_transferred || 0) : Number(task?.transferred || 0);
}

function displayTotal(task) {
  return Number(task?.stage_total || 0) > 0 ? Number(task?.stage_total || 0) : Number(task?.total || 0);
}
</script>

<style scoped>
.runtime-panel {
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: rgb(var(--v-theme-surface));
}

.runtime-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 10px 12px;
}

.min-width-0,
.task-content {
  min-width: 0;
}

.task-list {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  border-top: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
}

.task-row {
  display: grid;
  grid-template-columns: 20px minmax(0, 1fr) 26px;
  align-items: flex-start;
  gap: 8px;
  min-height: 34px;
  padding: 5px 10px 4px;
}

.task-leading-icon {
  margin-top: 1.5px;
  flex-shrink: 0;
}

.task-row + .task-row {
  border-top: 1px solid rgba(var(--v-border-color), 0.08);
}

.task-line {
  display: flex;
  align-items: center;
  gap: 5px;
  line-height: 1.32;
}

.task-name {
  font-size: 13px;
  font-weight: 500;
  line-height: 1.32;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.task-line :deep(.v-chip) {
  height: 17px !important;
  font-size: 9.5px !important;
  padding: 0 4px !important;
}

.runtime-header > .d-flex {
  min-width: 0;
}

.task-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 2.5px;
  line-height: 1.32;
}

.task-phase {
  font-size: 11.5px;
  line-height: 1.32;
  color: rgba(var(--v-theme-on-surface), 0.82) !important;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.task-phase-wrap {
  display: flex;
  min-width: 0;
  align-items: center;
  gap: 2px;
}

.task-detail-toggle {
  flex: 0 0 auto;
  width: 16px !important;
  height: 16px !important;
  min-width: 16px !important;
  padding: 0 !important;
  color: rgba(var(--v-theme-on-surface), 0.72) !important;
  opacity: 0.85;
}

.task-detail-toggle :deep(.v-icon) {
  font-size: 13px !important;
}

.task-detail-toggle:hover {
  opacity: 1;
  color: rgb(var(--v-theme-primary)) !important;
}

.task-stop-btn {
  width: 22px !important;
  height: 22px !important;
  min-width: 22px !important;
  padding: 0 !important;
  margin-top: -1px;
}

.task-details {
  display: grid;
  gap: 2px;
  margin-top: 4px;
  padding: 3px 0 1px;
  border-top: 1px dashed rgba(var(--v-border-color), 0.14);
  color: rgba(var(--v-theme-on-surface), 0.8);
}

.task-detail-row {
  display: grid;
  grid-template-columns: 56px minmax(0, 1fr);
  gap: 8px;
  line-height: 1.45;
  overflow-wrap: anywhere;
}

.task-detail-label {
  color: rgba(var(--v-theme-on-surface), var(--v-high-emphasis-opacity));
  font-weight: 500;
}

.task-current-file {
  min-width: 0;
  overflow-wrap: anywhere;
}

.task-process-row {
  align-items: start;
}

.task-process {
  display: flex;
  min-width: 0;
  flex-wrap: wrap;
  gap: 5px 12px;
}

.task-process-step {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  color: rgba(var(--v-theme-on-surface), var(--v-disabled-opacity));
  white-space: nowrap;
}

.task-process-step--completed {
  color: rgb(var(--v-theme-success));
}

.task-process-step--active {
  color: rgb(var(--v-theme-primary));
  font-weight: 500;
}

.search-phase-chip {
  flex: 0 0 auto;
  height: 18px !important;
  font-size: 10px !important;
  padding: 0 6px !important;
}

.task-search-details {
  width: 100%;
  padding: 1px 0 0;
}

.task-search-channels {
  display: flex;
  min-width: 0;
  width: 100%;
  flex-wrap: wrap;
  align-items: center;
  gap: 5px 8px;
}

.task-channel-badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  height: 22px;
  border-radius: 5px;
  padding: 0 7px;
  font-size: 11px;
  line-height: 1;
  white-space: nowrap;
  background: rgba(var(--v-theme-surface-variant), 0.28);
  color: rgba(var(--v-theme-on-surface), 0.88);
  border: 1px solid rgba(var(--v-border-color), 0.14);
  transition: all 0.18s cubic-bezier(0.4, 0, 0.2, 1);
  user-select: none;
}

.task-channel-badge:hover {
  transform: translateY(-1px);
  border-color: rgba(var(--v-border-color), 0.32);
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.05);
}

.task-channel-icon {
  flex-shrink: 0;
}

.task-channel-name {
  font-weight: 550;
  letter-spacing: 0.1px;
}

.task-channel-tag {
  display: inline-flex;
  align-items: center;
  font-size: 10.5px;
  font-weight: 600;
}

.task-channel-tag--count {
  color: rgb(var(--v-theme-success));
}

.task-channel-tag--zero {
  opacity: 0.75;
  font-weight: 500;
}

.task-channel-tag--searching {
  color: rgb(var(--v-theme-info));
}

.task-channel-tag--warn {
  color: rgb(var(--v-theme-warning));
}

.task-channel-tag--err {
  color: rgb(var(--v-theme-error));
}

.task-channel-tag--pending {
  color: rgba(var(--v-theme-on-surface), 0.5);
}

.task-channel-time {
  display: inline-flex;
  align-items: center;
  font-size: 9.5px;
  font-variant-numeric: tabular-nums;
  opacity: 0.75;
  color: rgba(var(--v-theme-on-surface), 0.75);
  letter-spacing: -0.2px;
}

.task-channel-time::before {
  content: "·";
  margin-right: 3px;
  opacity: 0.55;
  font-weight: normal;
}

.task-channel-badge--success {
  background: rgba(var(--v-theme-success), 0.1);
  color: rgb(var(--v-theme-success));
  border-color: rgba(var(--v-theme-success), 0.28);
}

.task-channel-badge--empty {
  background: rgba(var(--v-theme-on-surface), 0.035);
  color: rgba(var(--v-theme-on-surface), 0.72);
  border-color: rgba(var(--v-border-color), 0.12);
}

.task-channel-badge--searching {
  background: rgba(var(--v-theme-info), 0.08);
  color: rgb(var(--v-theme-info));
  border-color: rgba(var(--v-theme-info), 0.28);
}

.task-channel-badge--timeout,
.task-channel-badge--circuit_break {
  background: rgba(var(--v-theme-warning), 0.08);
  color: rgb(var(--v-theme-warning));
  border-color: rgba(var(--v-theme-warning), 0.26);
}

.task-channel-badge--failed {
  background: rgba(var(--v-theme-error), 0.08);
  color: rgb(var(--v-theme-error));
  border-color: rgba(var(--v-theme-error), 0.28);
}

.task-channel-badge--pending {
  color: rgba(var(--v-theme-on-surface), 0.45);
  background: rgba(var(--v-theme-on-surface), 0.03);
  border-color: rgba(var(--v-border-color), 0.08);
}

.task-elapsed {
  font-variant-numeric: tabular-nums;
  user-select: none;
}

.task-transfer {
  flex: 0 0 auto;
  white-space: nowrap;
}

.task-progress {
  margin-top: 0;
}

.task-progress :deep(.v-progress-linear__determinate) {
  background: var(--task-progress-gradient) !important;
  transition: width 0.35s ease,
  background 0.35s ease;
}

.task-progress--active :deep(.v-progress-linear__determinate) {
  background-image: linear-gradient(110deg, transparent 22%, rgba(255, 255, 255, 0.72) 48%, transparent 72%),
  var(--task-progress-gradient) !important;
  background-position: -120px 0,
  0 0;
  background-size: 120px 100%,
  100% 100%;
  background-repeat: no-repeat;
  animation: task-progress-shimmer 1.15s linear infinite;
}

@keyframes task-progress-shimmer {
  to {
    background-position: 120px 0,
    0 0;
  }
}

.idle-state {
  display: flex;
  flex: 1 1 auto;
  min-height: 220px;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 24px;
  text-align: center;
}

.task-progress-container {
  width: 100%;
  margin-top: 3.5px;
  margin-bottom: 1px;
  line-height: 1;
}

.task-progress-percent {
  font-size: 11px !important;
  font-variant-numeric: tabular-nums;
  min-width: 32px;
  text-align: right;
  letter-spacing: -0.2px;
}

@media (max-width: 600px) {
  .runtime-header > .d-flex {
    width: 100%;
    white-space: nowrap;
  }

  .task-line {
    flex-wrap: nowrap;
    min-width: 0;
  }

  .task-name {
    min-width: 0;
    flex: 0 1 auto;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .task-meta {
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-direction: row;
    gap: 6px;
    width: 100%;
    min-width: 0;
  }

  .task-phase-wrap {
    min-width: 0;
    flex: 1 1 auto;
  }

  .task-phase {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .task-transfer {
    flex-shrink: 0;
    white-space: nowrap;
    font-size: 11px;
  }

  /* 手机端搜索渠道紧凑布局，简化瘦身并自适应拉伸占满一行 */
  .task-search-details {
    padding: 2px 0 1px;
  }

  .task-search-channels {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
    width: 100%;
  }

  .task-channel-badge {
    flex: 1 1 auto;
    min-width: 0;
    height: 22px;
    padding: 0 6px;
    font-size: 10.5px;
    gap: 3px;
    justify-content: center;
    border-radius: 6px;
    white-space: nowrap;
  }

  .task-channel-badge :deep(.v-icon) {
    font-size: 11px !important;
    margin-right: 0 !important;
    flex-shrink: 0;
  }

  .task-channel-badge :deep(.v-progress-circular) {
    width: 9px !important;
    height: 9px !important;
    margin-right: 0 !important;
    flex-shrink: 0;
  }

  /* 驱动名称完整显示，不强制截断 */
  .task-channel-name {
    max-width: none !important;
    overflow: visible !important;
    text-overflow: clip !important;
    white-space: nowrap !important;
    flex-shrink: 0;
  }

  .task-channel-tag {
    font-size: 10px;
    margin-left: 0 !important;
    flex-shrink: 0;
  }

  /* 手机端省略“搜索中”文本，只保留加载动效，为完整显示驱动名称腾出空间 */
  .task-channel-tag--searching {
    display: none !important;
  }

  .task-channel-time {
    display: none !important; /* 手机端隐藏耗时，大幅简化并节省空间 */
  }
}
</style>
