<template>
  <div class="organize-panel">
    <v-card flat class="border rounded organize-shell">
      <v-card-text class="pa-3">
        <div class="d-flex flex-wrap align-center ga-2">
          <v-text-field
            v-model="sourcePath"
            class="organize-path"
            density="compact"
            variant="outlined"
            label="115 待整理目录"
            placeholder="/待整理"
            prepend-inner-icon="mdi-folder-cloud-outline"
            hide-details
            :disabled="busy" />
          <v-btn
            variant="tonal"
            size="small"
            prepend-icon="mdi-folder-search-outline"
            :disabled="busy"
            @click="directoryVisible = true">
            浏览
          </v-btn>
          <v-text-field
            v-model.number="maxFiles"
            class="organize-limit"
            density="compact"
            variant="outlined"
            type="number"
            min="1"
            max="5000"
            label="最多视频"
            hide-details
            :disabled="busy" />
          <v-btn
            color="primary"
            variant="flat"
            size="small"
            prepend-icon="mdi-magnify-scan"
            :loading="phase === 'scanning' || phase === 'recognizing'"
            :disabled="busy || !sourcePath.trim()"
            @click="startScan">
            扫描预览
          </v-btn>
          <v-btn
            v-if="busy"
            color="warning"
            variant="tonal"
            size="small"
            prepend-icon="mdi-stop-circle-outline"
            @click="stopTask">
            停止
          </v-btn>
        </div>

        <div class="organize-hint text-caption text-medium-emphasis mt-2">
          扫描只读；执行只做重命名 + 移动，不删除网盘文件。目标路径复用当前媒体库分类与命名模板。
        </div>

        <v-alert
          v-if="errorMessage"
          class="mt-3"
          type="error"
          variant="tonal"
          density="compact"
          closable
          @click:close="errorMessage = ''">
          {{ errorMessage }}
        </v-alert>

        <div class="organize-stats mt-3">
          <v-chip size="small" variant="tonal" prepend-icon="mdi-folder-outline">
            目录 {{ stats.directories || 0 }}
          </v-chip>
          <v-chip size="small" variant="tonal" prepend-icon="mdi-movie-open-outline">
            视频 {{ stats.videos || 0 }}
          </v-chip>
          <v-chip size="small" variant="tonal" prepend-icon="mdi-subtitles-outline">
            字幕 {{ stats.subtitles || 0 }}
          </v-chip>
          <v-chip size="small" :color="stats.ready ? 'success' : undefined" variant="tonal">
            可执行 {{ stats.ready || 0 }}
          </v-chip>
          <v-chip size="small" :color="stats.unmatched ? 'warning' : undefined" variant="tonal">
            待确认 {{ stats.unmatched || 0 }}
          </v-chip>
          <v-chip v-if="stats.truncated" size="small" color="warning" variant="tonal">
            已达扫描上限
          </v-chip>
        </div>

        <v-progress-linear
          v-if="busy"
          class="mt-3"
          indeterminate
          color="primary"
          rounded
          height="4" />

        <div class="organize-message text-caption text-medium-emphasis mt-2">
          {{ state.message || "尚未扫描" }}
        </div>

        <v-alert
          v-if="state.phase === 'idle'"
          class="mt-3"
          type="info"
          variant="tonal"
          density="compact">
          选择一个待整理目录后点「扫描预览」。识别成功的分组会显示最终目标目录和文件名，确认后再执行。
        </v-alert>

        <v-alert
          v-if="executionSummary"
          class="mt-3"
          :type="executionSummary.failed ? 'warning' : 'success'"
          variant="tonal"
          density="compact">
          {{ executionSummary.text }}
        </v-alert>

        <div v-if="groups.length" class="organize-toolbar mt-3">
          <div class="text-caption text-medium-emphasis">
            共 {{ groups.length }} 个分组，ready {{ readyCount }} 个
          </div>
          <v-spacer />
          <v-btn size="small" variant="text" :disabled="busy" @click="selectAllReady">
            全选可执行
          </v-btn>
          <v-btn size="small" variant="text" :disabled="busy" @click="selected = []">
            清空
          </v-btn>
          <v-btn
            color="primary"
            variant="flat"
            size="small"
            prepend-icon="mdi-folder-move-outline"
            :disabled="busy || !selected.length"
            @click="confirmVisible = true">
            执行选中（{{ selected.length }}）
          </v-btn>
        </div>

        <div v-if="groups.length" class="organize-table-wrap mt-2">
          <v-table density="compact" hover class="organize-table">
            <thead>
              <tr>
                <th class="check-col"></th>
                <th>作品 / 分组</th>
                <th>目标目录</th>
                <th class="num-col">集数</th>
                <th class="num-col">字幕</th>
                <th>状态</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="group in groups" :key="group.key">
                <td class="check-col">
                  <v-checkbox
                    v-model="selected"
                    :value="group.key"
                    density="compact"
                    hide-details
                    :disabled="group.status !== 'ready' || busy" />
                </td>
                <td>
                  <div class="d-flex align-center ga-2 flex-wrap">
                    <span class="font-weight-medium">{{ group.title || group.key }}</span>
                    <v-chip size="x-small" variant="outlined">
                      {{ group.media_type === "tv" ? "剧集" : "电影" }}
                    </v-chip>
                    <span v-if="group.year" class="text-caption text-medium-emphasis">{{ group.year }}</span>
                    <span v-if="group.media_type === 'tv' && group.season" class="text-caption text-medium-emphasis">
                      S{{ String(group.season).padStart(2, "0") }}
                    </span>
                  </div>
                  <div class="text-caption text-medium-emphasis organize-source">
                    {{ group.key }}
                  </div>
                  <div v-if="group.duplicate_count" class="text-caption text-warning">
                    重复集 {{ group.duplicate_count }} 个，已保留较大版本
                  </div>
                </td>
                <td>
                  <div class="target-path" :title="group.target_dir">{{ group.target_dir || "—" }}</div>
                  <div v-if="group.reason" class="text-caption text-medium-emphasis">{{ group.reason }}</div>
                </td>
                <td class="num-col">{{ group.episode_count || 0 }}</td>
                <td class="num-col">{{ group.subtitle_count || 0 }}</td>
                <td>
                  <v-chip size="small" :color="statusColor(group.status)" variant="tonal">
                    {{ statusLabel(group.status) }}
                  </v-chip>
                  <div v-if="group.result" class="text-caption text-medium-emphasis mt-1">
                    成功 {{ group.result.done }} / 跳过 {{ group.result.skipped }} / 失败 {{ group.result.failed }}
                  </div>
                </td>
              </tr>
            </tbody>
          </v-table>
        </div>

        <div v-if="latestHistory" class="text-caption text-medium-emphasis mt-3">
          最近：{{ latestHistory.trigger }} · {{ formatTime(latestHistory.finished_at) }} ·
          成功 {{ latestHistory.done }} / 跳过 {{ latestHistory.skipped }} / 失败 {{ latestHistory.failed }}
        </div>
      </v-card-text>
    </v-card>

    <v-dialog v-model="confirmVisible" max-width="520">
      <v-card>
        <v-card-title class="text-subtitle-1">确认执行整理</v-card-title>
        <v-card-text class="text-body-2">
          将把选中的 <strong>{{ selected.length }}</strong> 个分组移动到媒体库目标目录，并按平台模板重命名。
          目标端同名文件按「{{ conflictLabel }}」处理；不会删除任何文件。
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" :disabled="executing" @click="confirmVisible = false">取消</v-btn>
          <v-btn color="primary" variant="flat" :loading="executing" @click="executeSelected">
            开始整理
          </v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <DirectoryDialog
      v-model="directoryVisible"
      :api="api"
      mode="cloud"
      provider="115"
      :initial-path="sourcePath || '/'"
      @select="selectDirectory" />
  </div>
</template>

<script setup>
import {computed, onBeforeUnmount, onMounted, ref} from "vue"
import DirectoryDialog from "../dialogs/DirectoryDialog.vue"

const props = defineProps({
  api: {type: [Object, Function], required: true},
})

const state = ref({phase: "idle", groups: [], stats: {}, settings: {}, history: [], message: ""})
const sourcePath = ref("")
const maxFiles = ref(500)
const selected = ref([])
const directoryVisible = ref(false)
const confirmVisible = ref(false)
const executing = ref(false)
const errorMessage = ref("")
let pollTimer = null

const groups = computed(() => state.value.groups || [])
const stats = computed(() => state.value.stats || {})
const phase = computed(() => state.value.phase || "idle")
const busy = computed(() => ["scanning", "recognizing", "executing"].includes(phase.value))
const readyCount = computed(() => groups.value.filter(group => group.status === "ready").length)
const latestHistory = computed(() => {
  const items = state.value.history || []
  return items.length ? items[items.length - 1] : null
})
const conflictLabel = computed(() =>
  state.value.settings?.conflict_policy === "suffix" ? "自动加 (2) 后缀" : "跳过"
)
const executionSummary = computed(() => {
  const exec = state.value.execution
  if (!exec || !exec.started_at || busy.value) return null
  const done = exec.done || 0
  const failed = exec.failed || 0
  const skipped = exec.skipped || 0
  if (!done && !failed && !skipped) return null
  return {
    failed,
    text: `执行完成：成功 ${done}，跳过 ${skipped}，失败 ${failed}`,
  }
})

function unwrapResponse(raw) {
  if (!raw) return {}
  if (raw.data !== undefined) {
    const data = raw.data
    if (data && data.data !== undefined && (data.success !== undefined || data.phase !== undefined)) return data.data
    return data
  }
  return raw
}

async function request(method, path, payload) {
  if (method === "get") {
    return unwrapResponse(await props.api.get(`plugin/CloudSubscribeX/${path}`))
  }
  return unwrapResponse(await props.api.post(`plugin/CloudSubscribeX/${path}`, payload || {}))
}

async function loadStatus(applySettings = false) {
  try {
    const data = await request("get", "organize/status")
    if (!data || data.phase === undefined) return
    state.value = data
    if (applySettings && !sourcePath.value) {
      sourcePath.value = data.settings?.source_path || ""
      maxFiles.value = data.settings?.max_files || 500
    }
    if (data.phase === "ready" && !selected.value.length) selectAllReady()
  } catch (error) {
    // 轮询失败不打断页面，保留上一条状态。
    if (!pollTimer) errorMessage.value = String(error?.message || error)
  }
}

function selectAllReady() {
  selected.value = groups.value.filter(group => group.status === "ready").map(group => group.key)
}

async function startScan() {
  errorMessage.value = ""
  selected.value = []
  try {
    const result = await request("post", "organize/scan", {
      source_path: sourcePath.value.trim(),
      max_files: Number(maxFiles.value) || 500,
    })
    if (result.success === false) {
      errorMessage.value = result.message || "扫描启动失败"
      return
    }
    await loadStatus()
    startPolling()
  } catch (error) {
    errorMessage.value = `扫描失败：${error?.message || error}`
  }
}

function startPolling() {
  if (pollTimer) return
  pollTimer = setInterval(async () => {
    await loadStatus()
    if (!busy.value) stopPolling()
  }, 2000)
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

async function stopTask() {
  try {
    await request("post", "organize/stop")
    await loadStatus()
  } catch (error) {
    errorMessage.value = `停止失败：${error?.message || error}`
  }
}

async function executeSelected() {
  executing.value = true
  errorMessage.value = ""
  try {
    const result = await request("post", "organize/execute", {keys: selected.value})
    confirmVisible.value = false
    if (result.success === false) {
      errorMessage.value = result.message || "执行启动失败"
      return
    }
    await loadStatus()
    startPolling()
  } catch (error) {
    errorMessage.value = `执行失败：${error?.message || error}`
  } finally {
    executing.value = false
  }
}

function selectDirectory(path) {
  if (path) sourcePath.value = path
  directoryVisible.value = false
}

function statusLabel(status) {
  return ({
    ready: "可执行",
    unmatched: "待确认",
    conflict: "冲突",
    done: "已完成",
    partial: "部分失败",
  })[status] || status || "—"
}

function statusColor(status) {
  return ({
    ready: "success",
    unmatched: "warning",
    conflict: "error",
    done: "success",
    partial: "warning",
  })[status] || undefined
}

function formatTime(value) {
  if (!value) return "—"
  return new Date(Number(value) * 1000).toLocaleString()
}

onMounted(async () => {
  await loadStatus(true)
  if (busy.value) startPolling()
})

onBeforeUnmount(stopPolling)
</script>

<style scoped>
.organize-shell {
  background: rgb(var(--v-theme-surface));
}
.organize-path {
  flex: 1 1 320px;
  min-width: 240px;
}
.organize-limit {
  flex: 0 1 120px;
  min-width: 96px;
}
.organize-stats {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.organize-message {
  min-height: 18px;
}
.organize-toolbar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
}
.organize-table-wrap {
  max-height: 58vh;
  overflow: auto;
  border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
  border-radius: 8px;
}
.organize-table {
  min-width: 760px;
}
.check-col {
  width: 42px;
}
.num-col {
  width: 64px;
  text-align: right;
}
.organize-source {
  max-width: 360px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.target-path {
  max-width: 360px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: monospace;
  font-size: 12px;
}
</style>
