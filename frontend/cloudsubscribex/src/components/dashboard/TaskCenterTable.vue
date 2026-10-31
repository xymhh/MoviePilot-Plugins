<template>
  <section class="task-center-panel">
    <div class="task-center-head">
      <div class="d-flex align-center ga-2">
        <v-icon icon="mdi-format-list-bulleted-square" color="primary" size="small" />
        <span class="text-subtitle-2 font-weight-bold">任务中心</span>
        <v-chip size="x-small" variant="tonal">{{ tasks.length }}</v-chip>
      </div>
      <div class="d-flex align-center ga-2 flex-wrap">
        <v-btn-toggle
          v-model="filter"
          class="task-center-filters"
          mandatory
          density="compact"
          variant="outlined"
          divided>
          <v-btn v-for="item in filters" :key="item.value" :value="item.value" size="small">
            {{ item.title }}
          </v-btn>
        </v-btn-toggle>
        <v-btn
          icon="mdi-refresh"
          variant="text"
          size="small"
          :loading="loading"
          title="刷新任务中心"
          @click="loadTasks()" />
      </div>
    </div>

    <div v-if="error" class="text-caption text-error mb-2">{{ error }}</div>

    <v-data-table
      v-if="!isMobile"
      :headers="headers"
      :items="tasks"
      item-value="task_key"
      density="compact"
      hover
      fixed-header
      :loading="loading"
      loading-text="正在加载任务..."
      no-data-text="暂无任务"
      class="task-center-table">
      <template #item.status="{ item }">
        <div class="d-flex flex-column align-start">
          <v-chip :color="statusColor(item)" size="x-small" variant="tonal">
            {{ item.status_label || item.status || "-" }}
            <v-tooltip v-if="item.dead_letter" activator="parent" location="top">
              重试次数已耗尽，需要人工处理
            </v-tooltip>
          </v-chip>
          <span v-if="item.stage_label" class="text-caption text-medium-emphasis mt-1">
            {{ item.stage_label }}
          </span>
        </div>
      </template>
      <template #item.title="{ item }">
        <span class="font-weight-medium">{{ item.title || "-" }}</span>
      </template>
      <template #item.target="{ item }">
        <div class="task-target text-caption">
          <span>{{ item.target_provider || "-" }}</span>
          <span class="text-medium-emphasis" :title="item.target_path">
            {{ item.target_path || "-" }}
          </span>
          <span class="text-medium-emphasis" :title="item.target_name">{{ item.target_name || "" }}</span>
        </div>
      </template>
      <template #item.attempt="{ item }">
        <span>{{ item.max_attempts ? `${item.attempt}/${item.max_attempts}` : "-" }}</span>
      </template>
      <template #item.next_retry_at="{ item }">
        <span>{{ formatTime(item.next_retry_at) }}</span>
      </template>
      <template #item.error="{ item }">
        <span class="task-error text-caption" :title="item.error || ''">{{ item.error || "-" }}</span>
      </template>
      <template #item.actions="{ item }">
        <div class="d-flex gap-1">
          <v-btn
            v-if="item.actions.includes('retry')"
            size="x-small"
            variant="tonal"
            color="primary"
            prepend-icon="mdi-refresh"
            :loading="retryingKey === item.task_key"
            @click="retryTask(item)">重试</v-btn>
          <v-btn
            v-if="item.actions.includes('stop')"
            size="x-small"
            variant="tonal"
            color="warning"
            prepend-icon="mdi-stop-circle-outline"
            :loading="stoppingKey === item.task_key"
            @click="stopTask(item)">停止</v-btn>
          <span v-if="!item.actions.length" class="text-medium-emphasis">-</span>
        </div>
      </template>
    </v-data-table>

    <div v-else class="task-center-mobile">
      <v-progress-linear v-if="loading" indeterminate color="primary" class="mb-2" />
      <div v-if="!tasks.length" class="text-caption text-medium-emphasis py-4 text-center">暂无任务</div>
      <div v-for="item in tasks" :key="item.task_key" class="task-mobile-card">
        <div class="d-flex align-center flex-wrap ga-2">
          <v-chip :color="statusColor(item)" size="x-small" variant="tonal">
            {{ item.status_label || item.status || "-" }}
          </v-chip>
          <span v-if="item.stage_label" class="text-caption text-medium-emphasis">{{ item.stage_label }}</span>
          <v-chip v-if="item.dead_letter" size="x-small" color="error" variant="tonal">死信</v-chip>
          <v-spacer />
          <span class="text-caption text-medium-emphasis">
            {{ item.max_attempts ? `${item.attempt}/${item.max_attempts}` : "" }}
          </span>
        </div>
        <div class="task-mobile-title text-body-2 font-weight-medium">{{ item.title || "-" }}</div>
        <div class="text-caption text-medium-emphasis">
          {{ item.media_type || "-" }} · {{ item.target_provider || "-" }}
        </div>
        <div class="text-caption task-mobile-target" :title="item.target_path">
          {{ item.target_path || "-" }}{{ item.target_name ? " / " + item.target_name : "" }}
        </div>
        <div v-if="item.next_retry_at" class="text-caption text-warning">
          下次重试：{{ formatTime(item.next_retry_at) }}
        </div>
        <div v-if="item.error" class="text-caption text-error task-mobile-error">{{ item.error }}</div>
        <div v-if="item.actions.length" class="d-flex ga-2 mt-2">
          <v-btn
            v-if="item.actions.includes('retry')"
            size="x-small"
            variant="tonal"
            color="primary"
            :loading="retryingKey === item.task_key"
            @click="retryTask(item)">重试</v-btn>
          <v-btn
            v-if="item.actions.includes('stop')"
            size="x-small"
            variant="tonal"
            color="warning"
            :loading="stoppingKey === item.task_key"
            @click="stopTask(item)">停止</v-btn>
        </div>
      </div>
    </div>

    <v-snackbar v-model="messageVisible" :color="messageType" :style="snackbarTextStyle" location="top" timeout="3000">
      {{ message }}
    </v-snackbar>
  </section>
</template>

<script setup>
import {computed, onMounted, onUnmounted, ref, watch} from "vue";
import {useDisplay} from "vuetify";

const props = defineProps({
  api: { type: [Object, Function], default: () => ({}) },
  pluginId: { type: String, default: "CloudSubscribeX" },
  active: { type: Boolean, default: false },
})

const api = props.api
const display = useDisplay()
const isMobile = computed(() => display.xs.value)
const tasks = ref([])
const loading = ref(false)
const error = ref("")
const filter = ref("all")
const retryingKey = ref("")
const stoppingKey = ref("")
const message = ref("")
const messageType = ref("success")

const snackbarTextStyle = computed(() => ({
  color: `rgb(var(--v-theme-on-${messageType.value || "success"}))`,
}))
const messageVisible = ref(false)
let timer = null

const filters = [
  {title: "全部", value: "all"},
  {title: "运行中", value: "active"},
  {title: "等待重试", value: "waiting"},
  {title: "失败", value: "failed"},
  {title: "待确认", value: "pending"},
]
const headers = [
  {title: "状态", key: "status", sortable: false},
  {title: "标题", key: "title", sortable: false},
  {title: "类型", key: "media_type", sortable: false},
  {title: "目标", key: "target", sortable: false},
  {title: "尝试", key: "attempt", sortable: false},
  {title: "下次重试", key: "next_retry_at", sortable: false},
  {title: "错误摘要", key: "error", sortable: false},
  {title: "操作", key: "actions", sortable: false, align: "end"},
]

function notify(text, type = "success") {
  message.value = text
  messageType.value = type
  messageVisible.value = true
}

function statusColor(task) {
  if (task.status === "成功") return "success"
  if (task.status === "跳过") return "warning"
  if (task.status === "待确认") return "warning"
  if (task.status === "失败") return task.dead_letter ? "error" : "orange-lighten-1"
  return ["处理中", "下载中"].includes(task.status) ? "info" : "error"
}

function formatTime(value) {
  const seconds = Number(value || 0)
  if (!seconds) return "-"
  const date = new Date(seconds * 1000)
  if (Number.isNaN(date.getTime())) return "-"
  return date.toLocaleString("zh-CN", {hour12: false})
}

async function loadTasks(showLoading = true) {
  if (!props.active && !showLoading) return
  if (showLoading) loading.value = true
  error.value = ""
  try {
    const result = await api.get(`plugin/${props.pluginId}/tasks?state=${encodeURIComponent(filter.value)}`)
    if (!result?.success) throw new Error(result?.message || "加载任务失败")
    tasks.value = Array.isArray(result.data) ? result.data : []
  } catch (e) {
    error.value = e.message || "加载任务失败"
    if (showLoading) notify(error.value, "error")
  } finally {
    loading.value = false
  }
}

async function retryTask(task) {
  const hasHistory = Boolean(task.history_time && task.share_url && task.file_name)
  if (!hasHistory && !task.job_id) {
    notify("任务缺少重试所需标识", "error")
    return
  }
  retryingKey.value = task.task_key
  try {
    const result = hasHistory
      ? await api.post(`plugin/${props.pluginId}/history/retry`, {
        time: task.history_time,
        share_url: task.share_url,
        file_name: task.file_name,
      })
      : await api.post(`plugin/${props.pluginId}/tasks/retry`, {
        job_id: task.job_id,
      })
    if (!result?.success) throw new Error(result?.message || "重试失败")
    notify(result.message || "已重新提交处理")
    await loadTasks(false)
  } catch (e) {
    notify(e.message || "重试失败", "error")
  } finally {
    retryingKey.value = ""
  }
}

async function stopTask(task) {
  stoppingKey.value = task.task_key
  try {
    const result = await api.post(`plugin/${props.pluginId}/sync/task/stop`, {
      task_id: task.job_id || task.task_key,
    })
    if (!result?.success) throw new Error(result?.message || "停止失败")
    notify(result.message || "已请求停止")
    await loadTasks(false)
  } catch (e) {
    notify(e.message || "停止失败", "error")
  } finally {
    stoppingKey.value = ""
  }
}

watch(filter, () => void loadTasks())
watch(() => props.active, (active) => {
  if (active) void loadTasks()
})

onMounted(() => {
  if (props.active) void loadTasks()
  timer = window.setInterval(() => {
    if (props.active && !loading.value) void loadTasks(false)
  }, 15000)
})

onUnmounted(() => {
  if (timer !== null) window.clearInterval(timer)
})
</script>

<style scoped>
.task-center-panel {
  min-width: 0;
}
.task-center-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 8px;
}
.task-center-filters {
  max-width: 100%;
  overflow-x: auto;
  scrollbar-width: none;
}
.task-center-filters::-webkit-scrollbar {
  display: none;
}
.task-center-filters :deep(.v-btn) {
  white-space: nowrap;
}
.task-center-table {
  max-height: 420px;
}
.task-center-table :deep(.v-table__wrapper) {
  overflow-x: auto;
}
.task-center-table :deep(.v-chip) {
  white-space: nowrap;
}
.task-center-table :deep(th) {
  white-space: nowrap;
  font-size: 12px;
}
.task-center-table :deep(td) {
  font-size: 12px;
}
.task-target {
  display: flex;
  flex-direction: column;
  max-width: 240px;
  overflow: hidden;
}
.task-target span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.task-error {
  display: inline-block;
  max-width: 260px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  vertical-align: middle;
}
.task-center-mobile {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.task-mobile-card {
  border: 1px solid rgb(var(--v-border-color), var(--v-border-opacity));
  border-radius: 8px;
  padding: 10px;
  min-width: 0;
}
.task-mobile-card :deep(.v-chip) {
  white-space: nowrap;
}
.task-mobile-head {
  row-gap: 4px;
}
.task-mobile-title {
  margin-top: 4px;
  word-break: break-all;
}
.task-mobile-target,
.task-mobile-error {
  word-break: break-all;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
</style>
