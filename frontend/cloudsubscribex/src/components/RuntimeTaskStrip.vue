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
          <span class="item-title text-truncate">{{ taskLabel(task) }}</span>
          <v-chip
            size="x-small"
            variant="tonal"
            :color="taskStatusColor(task)"
            class="item-chip">
            {{ taskStatusLabel(task, {withProgress: true}) }}
          </v-chip>
        </div>
        <v-progress-linear
          :model-value="progressOf(task)"
          :indeterminate="!hasDeterminateProgress(task)"
          :color="taskStatusColor(task)"
          height="4"
          rounded
          class="item-bar" />
        <div class="item-sub text-truncate">{{ subText(task) }}</div>
      </li>
    </ul>

    <div v-if="!collapsed && hiddenCount > 0" class="strip-more">
      还有 {{ hiddenCount }} 个任务在排队或处理中
    </div>
  </div>
</template>

<script setup>
import {computed, ref} from "vue";
import {
  displayTotal,
  displayTransferred,
  hasDeterminateProgress,
  isTaskActive,
  taskPhaseText,
  taskProgress,
  taskStatusColor,
  taskStatusLabel,
} from "../utils/taskUtils.js";

const props = defineProps({
  tasks: {type: Array, default: () => []},
  max: {type: Number, default: 4},
});

const collapsed = ref(false);

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

function subText(task) {
  const phase = String(taskPhaseText(task) || "").trim();
  const total = displayTotal(task);
  const transferred = displayTransferred(task);
  const countText = total > 0 ? `已处理 ${transferred}/${total}` : "";
  return [phase, countText].filter(Boolean).join(" · ");
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
  gap: 8px;
}

.strip-item {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.item-head {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
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

.item-bar {
  margin: 0;
}

.item-sub {
  font-size: 11.5px;
  color: rgba(var(--v-theme-on-surface), 0.62);
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
}
</style>
