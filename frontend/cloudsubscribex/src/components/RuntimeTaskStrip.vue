<template>
  <div v-if="activeTasks.length" class="runtime-task-strip" role="status" aria-live="polite">
    <div class="strip-head">
      <v-icon icon="mdi-progress-clock" size="15" class="strip-icon" />
      <span class="strip-title">运行中 {{ activeTasks.length }}</span>
      <v-spacer />
      <v-btn
        variant="text"
        size="x-small"
        density="comfortable"
        :title="collapsed ? '展开任务进度' : '收起任务进度'"
        :aria-label="collapsed ? '展开任务进度' : '收起任务进度'"
        @click="collapsed = !collapsed">
        <v-icon :icon="collapsed ? 'mdi-chevron-down' : 'mdi-chevron-up'" size="16" />
      </v-btn>
    </div>

    <ul v-if="!collapsed" class="strip-list">
      <li v-for="task in visibleTasks" :key="task.id" class="strip-item">
        <div class="item-head">
          <v-icon :icon="statusIcon(task)" :color="taskStatusColor(task)" size="15" class="item-icon" />
          <span class="item-title text-truncate">{{ taskLabel(task) }}</span>
          <v-chip
            size="x-small"
            variant="tonal"
            :color="taskStatusColor(task)"
            class="item-chip">
            <v-progress-circular
              v-if="task.search_active"
              indeterminate
              size="10"
              width="2"
              class="mr-1" />
            {{ taskStatusLabel(task, {withProgress: true}) }}
          </v-chip>
        </div>

        <div class="item-meta">
          <span class="meta-phase text-truncate">{{ phaseText(task) }}</span>
          <span v-if="elapsedText(task)" class="meta-elapsed">{{ elapsedText(task) }}</span>
          <v-spacer />
          <v-btn
            v-if="canStop(task)"
            variant="text"
            size="x-small"
            density="comfortable"
            color="error"
            icon="mdi-stop-circle-outline"
            title="停止此任务"
            aria-label="停止此任务"
            @click.stop="stopTask(task)" />
          <v-btn
            v-if="hasDetails(task)"
            variant="text"
            size="x-small"
            density="comfortable"
            :icon="isExpanded(task.id) ? 'mdi-chevron-up' : 'mdi-chevron-down'"
            :title="isExpanded(task.id) ? '收起明细' : '展开明细'"
            :aria-label="isExpanded(task.id) ? '收起明细' : '展开明细'"
            @click.stop="toggleExpanded(task.id)" />
        </div>

        <v-progress-linear
          :model-value="progressOf(task)"
          :indeterminate="isIndeterminate(task)"
          :color="taskStatusColor(task)"
          height="4"
          rounded
          class="item-bar" />
        <div v-if="hasDeterminate(task)" class="item-percent">
          {{ progressOf(task) }}%
        </div>

        <!-- 搜索渠道明细：横向胶囊，逐渠道显示状态/结果数/耗时 -->
        <div v-if="channelsOf(task).length" class="item-channels">
          <span
            v-for="channel in channelsOf(task)"
            :key="channel.key"
            class="channel-badge"
            :class="`channel-${channel.status || 'pending'}`"
            :title="channelTooltip(channel)">
            <v-progress-circular
              v-if="channel.status === 'searching'"
              indeterminate
              size="9"
              width="1.5" />
            <v-icon v-else :icon="channelIcon(channel)" size="12" />
            <span class="channel-name">{{ channelLabel(channel) }}</span>
            <span v-if="channel.status === 'success'" class="channel-count">{{ channel.count }}</span>
          </span>
        </div>

        <!-- 展开明细：当前文件 / 后处理分步 / 字节与速度 -->
        <div v-if="isExpanded(task.id) && hasDetails(task)" class="item-details">
          <div v-if="currentFile(task)" class="detail-row">
            <span class="detail-key">当前文件</span>
            <span class="detail-value text-truncate">{{ currentFile(task) }}</span>
          </div>
          <div v-if="stepsOf(task).length" class="detail-row detail-steps">
            <span class="detail-key">后处理</span>
            <span class="detail-steps-list">
              <span
                v-for="(step, index) in stepsOf(task)"
                :key="step.key"
                class="detail-step"
                :class="`step-${postprocessStepState(task, index)}`">
                <v-icon :icon="postprocessStepIcon(task, index)" size="12" />
                {{ step.label }}
              </span>
            </span>
          </div>
          <div v-if="bytesText(task)" class="detail-row">
            <span class="detail-key">进度</span>
            <span class="detail-value">{{ bytesText(task) }}</span>
          </div>
          <div v-if="speedText(task)" class="detail-row">
            <span class="detail-key">速度</span>
            <span class="detail-value">{{ speedText(task) }}</span>
          </div>
        </div>
      </li>
    </ul>

    <div v-if="!collapsed && hiddenCount > 0" class="strip-more">
      还有 {{ hiddenCount }} 个任务在排队或处理中
    </div>
  </div>
</template>

<script setup>
import {computed, onBeforeUnmount, onMounted, ref} from "vue";
import {
  channelIcon,
  channelLabel,
  channelTooltip,
  displayTotal,
  displayTransferred,
  formatDuration,
  formatSize,
  formatSpeed,
  hasDeterminateProgress,
  isTaskActive,
  postprocessStepIcon,
  postprocessStepState,
  postprocessStepsOf,
  searchChannelsOf,
  taskElapsedSeconds,
  taskPhaseText,
  taskProgress,
  taskStatusColor,
  taskStatusLabel,
} from "../utils/taskUtils.js";

const props = defineProps({
  tasks: {type: Array, default: () => []},
  max: {type: Number, default: 4},
});

const emit = defineEmits(["stop-task"]);

const collapsed = ref(false);
const expandedTaskIds = ref(new Set());
// 耗时每秒刷新：只在有运行中任务时才起定时器，卸载即清
const now = ref(Date.now());
let timer = null;

onMounted(() => {
  timer = setInterval(() => {
    if (activeTasks.value.length) now.value = Date.now();
  }, 1000);
});
onBeforeUnmount(() => {
  if (timer) clearInterval(timer);
  timer = null;
});

const activeTasks = computed(() => (props.tasks || []).filter((task) => isTaskActive(task)));

const visibleTasks = computed(() => activeTasks.value.slice(0, Math.max(1, Number(props.max) || 4)));

const hiddenCount = computed(() => Math.max(0, activeTasks.value.length - visibleTasks.value.length));

function taskLabel(task) {
  const title = String(task?.title || "").trim() || "未命名任务";
  const season = Number(task?.season || 0);
  const seasonText = season > 0 ? ` S${String(season).padStart(2, "0")}` : "";
  const mediaType = String(task?.media_type || "").trim();
  const typeText = mediaType === "电视剧" || mediaType === "电影" ? `[${mediaType}] ` : "";
  return `${typeText}${title}${seasonText}`;
}

function progressOf(task) {
  return Math.round(taskProgress(task));
}

function phaseText(task) {
  return String(taskPhaseText(task) || "").trim();
}

function elapsedText(task) {
  return formatDuration(taskElapsedSeconds(task, now.value));
}

function hasDeterminate(task) {
  return hasDeterminateProgress(task);
}

function isIndeterminate(task) {
  // 搜索阶段没有确定进度，用 indeterminate 表示"在动但不知道多久"
  if (task?.search_active) return true;
  return !hasDeterminateProgress(task);
}

function statusIcon(task) {
  if (task?.search_active) return "mdi-magnify";
  const status = String(task?.status || "");
  if (status === "stopping") return "mdi-stop-circle-outline";
  if (status === "failed") return "mdi-alert-circle-outline";
  if (status === "completed" || status === "success") return "mdi-check-circle-outline";
  return "mdi-progress-clock";
}

function canStop(task) {
  const status = String(task?.status || "");
  return ["queued", "running", "downloading", "transferring", "postprocessing"].includes(status);
}

function stopTask(task) {
  emit("stop-task", task);
}

function isExpanded(taskId) {
  return expandedTaskIds.value.has(taskId);
}

function toggleExpanded(taskId) {
  const next = new Set(expandedTaskIds.value);
  if (next.has(taskId)) next.delete(taskId);
  else next.add(taskId);
  expandedTaskIds.value = next;
}

function channelsOf(task) {
  return searchChannelsOf(task);
}

function stepsOf(task) {
  return postprocessStepsOf(task);
}

function currentFile(task) {
  return String(task?.current_file || "").trim();
}

function hasDetails(task) {
  return Boolean(
    currentFile(task) ||
    stepsOf(task).length ||
    displayTotal(task) > 0 ||
    channelsOf(task).length,
  );
}

function bytesText(task) {
  const total = displayTotal(task);
  if (total <= 0) return "";
  const transferred = displayTransferred(task);
  return `${formatSize(transferred)} / ${formatSize(total)}`;
}

function speedText(task) {
  const download = Number(task?.download_speed_bytes_per_second || 0);
  const upload = Number(task?.upload_speed_bytes_per_second || 0);
  const speed = Number(task?.speed_bytes_per_second || 0);
  if (upload > 0) return `上传 ${formatSpeed(upload)}`;
  if (download > 0) return `下载 ${formatSpeed(download)}`;
  if (speed > 0) return formatSpeed(speed);
  return "";
}
</script>

<style scoped>
.runtime-task-strip {
  margin: 0 0 12px;
  padding: 8px 12px 10px;
  border-radius: 10px;
  border: 1px solid rgba(var(--v-theme-primary), 0.24);
  background: rgba(var(--v-theme-primary), 0.06);
}

.strip-head {
  display: flex;
  align-items: center;
  gap: 6px;
  min-height: 24px;
}

.strip-icon {
  color: rgb(var(--v-theme-primary));
}

.strip-title {
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.02em;
  color: rgb(var(--v-theme-primary));
}

.strip-list {
  list-style: none;
  margin: 6px 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.strip-item {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding-bottom: 8px;
  border-bottom: 1px solid rgba(var(--v-theme-on-surface), 0.06);
}

.strip-item:last-child {
  border-bottom: none;
  padding-bottom: 0;
}

.item-head {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}

.item-icon {
  flex: 0 0 auto;
}

.item-title {
  flex: 1 1 auto;
  min-width: 0;
  font-size: 12.5px;
  font-weight: 600;
  color: rgba(var(--v-theme-on-surface), 0.88);
}

.item-chip {
  flex: 0 0 auto;
}

.item-meta {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  font-size: 11.5px;
  color: rgba(var(--v-theme-on-surface), 0.62);
}

.meta-phase {
  min-width: 0;
  flex: 0 1 auto;
}

.meta-elapsed {
  flex: 0 0 auto;
  font-variant-numeric: tabular-nums;
  color: rgba(var(--v-theme-on-surface), 0.5);
}

.item-bar {
  margin: 0;
}

.item-percent {
  font-size: 11px;
  font-weight: 700;
  text-align: right;
  font-variant-numeric: tabular-nums;
  color: rgba(var(--v-theme-on-surface), 0.7);
}

/* 搜索渠道胶囊 */
.item-channels {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 2px;
}

.channel-badge {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  padding: 1px 6px;
  border-radius: 999px;
  font-size: 10.5px;
  line-height: 1.6;
  border: 1px solid rgba(var(--v-theme-on-surface), 0.14);
  color: rgba(var(--v-theme-on-surface), 0.7);
  background: rgba(var(--v-theme-on-surface), 0.03);
}

.channel-badge.channel-success {
  border-color: rgba(var(--v-theme-success), 0.4);
  color: rgb(var(--v-theme-success));
}

.channel-badge.channel-searching {
  border-color: rgba(var(--v-theme-info), 0.45);
  color: rgb(var(--v-theme-info));
}

.channel-badge.channel-failed {
  border-color: rgba(var(--v-theme-error), 0.4);
  color: rgb(var(--v-theme-error));
}

.channel-badge.channel-timeout,
.channel-badge.channel-circuit_break {
  border-color: rgba(var(--v-theme-warning), 0.4);
  color: rgb(var(--v-theme-warning));
}

.channel-count {
  font-variant-numeric: tabular-nums;
  opacity: 0.85;
}

/* 展开明细 */
.item-details {
  margin-top: 4px;
  padding: 6px 8px;
  border-radius: 8px;
  background: rgba(var(--v-theme-on-surface), 0.04);
  display: flex;
  flex-direction: column;
  gap: 3px;
  font-size: 11px;
}

.detail-row {
  display: flex;
  gap: 8px;
  min-width: 0;
}

.detail-key {
  flex: 0 0 auto;
  color: rgba(var(--v-theme-on-surface), 0.5);
}

.detail-value {
  flex: 1 1 auto;
  min-width: 0;
  color: rgba(var(--v-theme-on-surface), 0.78);
  font-variant-numeric: tabular-nums;
}

.detail-steps-list {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  min-width: 0;
}

.detail-step {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  color: rgba(var(--v-theme-on-surface), 0.5);
}

.detail-step.step-completed {
  color: rgb(var(--v-theme-success));
}

.detail-step.step-active {
  color: rgb(var(--v-theme-info));
  font-weight: 600;
}

.strip-more {
  margin-top: 6px;
  font-size: 11.5px;
  color: rgba(var(--v-theme-on-surface), 0.55);
}

@media (max-width: 600px) {
  .runtime-task-strip {
    margin: 0 0 8px;
    padding: 7px 10px 9px;
  }

  .item-title {
    font-size: 12px;
  }

  .channel-badge {
    font-size: 10px;
  }
}
</style>
