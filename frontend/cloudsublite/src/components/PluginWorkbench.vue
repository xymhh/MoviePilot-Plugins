<template>
  <div :class="['cloud-app-page', { 'is-host-dialog-page': isHostDialogPage }]">
    <header class="app-header">
      <v-btn
        v-if="isHostDialogPage"
        class="close-action"
        variant="tonal"
        color="primary"
        icon="mdi-arrow-left"
        title="返回"
        aria-label="返回"
        @click="closeHostDialog" />
      <div class="app-identity">
        <v-avatar color="primary" variant="tonal" size="38">
          <v-icon icon="mdi-cloud-sync-outline" size="20" />
        </v-avatar>
        <div class="identity-text">
          <div class="identity-title-row">
            <h1 class="text-truncate">网盘订阅助手</h1>
            <v-chip
              size="x-small"
              variant="tonal"
              color="primary"
              class="version-chip font-weight-bold ml-1 cursor-pointer"
              title="点击清除缓存并重新拉取最新前端包"
              @click="forceReloadApp">
              v{{ currentVersion }}
            </v-chip>
            <span
              :class="['status-badge', 'status-badge-clickable', active ? 'status-badge-active' : 'status-badge-idle']"
              :title="active ? '正在同步（点击体检）' : '就绪（点击体检）'"
              role="button"
              tabindex="0"
              @click="doctorVisible = true"
              @keydown.enter.prevent="doctorVisible = true">
              <span class="status-dot"></span>
              <span class="status-text">{{ active ? "同步中" : "就绪" }}</span>
            </span>
          </div>
          <p class="text-truncate">{{ runtimeSummary }}</p>
        </div>
      </div>
      <div class="app-actions">
        <v-btn
          class="manual-action"
          variant="tonal"
          color="secondary"
          prepend-icon="mdi-link-variant-plus"
          title="手动添加"
          @click="openManualDialog()">
          <span class="action-label">手动添加</span>
        </v-btn>
        <v-btn
          :class="['sync-action', { 'sync-action-active': active }]"
          :color="active ? 'warning' : 'primary'"
          variant="flat"
          :prepend-icon="active ? 'mdi-stop-circle-outline' : 'mdi-magnify'"
          :disabled="runtime.status === 'stopping'"
          :loading="runtime.status === 'starting' || searchStarting"
          :title="active ? '停止全部任务' : immediateSearchTitle"
          @click="active ? confirmStopSync() : openImmediateSearchConfirm()">
          <span class="action-label">{{ active ? "停止全部" : immediateSearchLabel }}</span>
        </v-btn>
        <v-btn
          class="settings-action"
          prepend-icon="mdi-cog-outline"
          variant="tonal"
          color="success"
          :loading="configLoading"
          title="配置"
          @click="openConfig">
          <span class="action-label">设置</span>
        </v-btn>
        <v-menu location="bottom end" @update:model-value="handleMoreMenu">
          <template #activator="{ props: menuProps }">
            <v-btn
              v-bind="menuProps"
              class="more-action"
              variant="tonal"
              color="default"
              icon="mdi-dots-vertical"
              title="更多工具" />
          </template>
          <v-list density="compact" class="more-action-list">
            <v-list-item
              :prepend-icon="transferPaused ? 'mdi-play-circle-outline' : 'mdi-pause-circle-outline'"
              :title="transferPaused ? '恢复转存' : '暂停转存'"
              :subtitle="transferPaused ? '已暂停：不再认领新任务' : '正在写文件不打断'"
              :disabled="transferPauseLoading"
              @click="toggleTransferPause" />
            <v-divider class="my-1" />
            <v-list-item
              class="d-md-none"
              prepend-icon="mdi-link-variant-plus"
              title="手动添加"
              @click="openManualDialog()" />
            <v-list-item
              class="d-md-none"
              prepend-icon="mdi-cog-outline"
              title="插件设置"
              @click="openConfig" />
            <v-list-item
              v-if="offlineSupported"
              prepend-icon="mdi-cloud-download-outline"
              title="离线任务"
              @click="offlineVisible = true" />
            <v-list-item
              prepend-icon="mdi-cached"
              title="清理缓存"
              @click="cacheVisible = true" />
          </v-list>
        </v-menu>
      </div>
    </header>

    <section class="history-workspace" aria-label="失败记录">
      <HistoryTable
        :api="api"
        :items="failures"
        :page="historyPage.page"
        :page-size="historyPage.pageSize"
        :total="historyPage.total"
        :total-pages="historyPage.totalPages"
        :loading="loading"
        :retrying-key="retryingHistoryKey"
        :deleting-key="deletingHistoryKey"
        :status-filter="historyStatus"
        @refresh="loadPage"
        @query-change="onHistoryQueryChange"
        @clear="openClearHistory"
        @retry="confirmRetryHistory"
        @retry-failed="confirmRetryFailedHistory"
        @retry-selected="confirmRetrySelectedHistory"
        @mark-success="confirmMarkHistorySuccess"
        @delete="confirmDeleteHistory"
        @delete-records="confirmDeleteRecords"
        @selection-change="updateHistorySelection"
        @open-media="openMediaDetail" />
    </section>

    <OfflineTasksDialog v-if="offlineSupported" v-show="offlineVisible" v-model="offlineVisible" :api="api" :plugin-id="pluginId" />
    <ManualResourceDialog
      v-show="manualVisible"
      v-model="manualVisible"
      :api="api"
      :plugin-id="pluginId"
      @started="manualStarted" />
    <ConfirmDialog
      v-model="searchConfirmVisible"
      :title="immediateSearchDialogTitle"
      :loading="searchStarting"
      :confirm-text="selectedHistoryCount ? '搜索所选' : '搜索全部'"
      persistent
      @confirm="confirmImmediateSearch">
      <template v-if="selectedHistoryCount">
        确认立即搜索所选 {{ selectedHistoryCount }} 条失败记录的媒体？
        <v-alert type="info" variant="tonal" density="compact" class="mt-3">
          本次仅搜索所选失败记录的媒体，不会搜索其他订阅。
        </v-alert>
      </template>
      <template v-else>确认立即搜索全部订阅？</template>
    </ConfirmDialog>
    <ConfirmDialog
      v-model="retryVisible"
      title="恢复历史任务"
      alert-text="将重新检查目标文件、本地缓存和原分享，必要时重新执行跨盘转存及后处理。"
      confirm-text="确认恢复"
      :loading="Boolean(retryingHistoryKey)"
      persistent
      @confirm="retryHistory">
      确认恢复“{{ historyUpgradeLabel(retryingRecord ? {records: [retryingRecord]} : null) }}”的转存任务？
    </ConfirmDialog>
    <ConfirmDialog
      v-if="retryBatchVisible"
      v-model="retryBatchVisible"
      title="批量重试失败记录"
      icon="mdi-replay"
      icon-color="primary"
      :body="`将重试 ${retryBatchRecords.length} 条失败记录，期间会产生新的搜索与转存任务，是否继续？`"
      confirm-text="确认重试"
      confirm-color="primary"
      :loading="retryingHistoryKey === 'batch'"
      @confirm="retryFailedBatch" />
    <ConfirmDialog
      v-if="markSuccessVisible"
      v-model="markSuccessVisible"
      title="批量标记为成功"
      icon="mdi-check-decagram-outline"
      icon-color="warning"
      :body="`将把 ${markSuccessRecords.length} 条「待修复 / 待确认」记录直接标记为成功；之后落盘自检和后处理终态都不会再改写这些记录，是否继续？`"
      confirm-text="确认标记"
      confirm-color="warning"
      :loading="markingHistoryKey === 'batch'"
      @confirm="markHistorySuccess" />
    <ConfirmDialog
      v-if="stopVisible"
      v-model="stopVisible"
      :title="stoppingTask ? '停止任务' : '停止全部任务'"
      icon="mdi-stop-circle-outline"
      icon-color="warning"
      alert-type="warning"
      :alert-text="['downloading', 'transferring', 'postprocessing'].includes(stoppingTask?.status)
        ? '将停止插件文件后处理；离线任务、已下载文件和STRM均会保留。'
        : '任务将在安全节点停止，已完成的处理不会回退。'"
      confirm-text="确认停止"
      confirm-color="warning"
      :loading="stopping"
      persistent
      @confirm="stopConfirmed">
      <div v-if="stoppingTask">确认停止“{{ stoppingTask.title || "此任务" }}”？</div>
      <div v-else-if="stoppableTaskCount > 0">确认停止当前 {{ stoppableTaskCount }} 个等待或运行中的任务？</div>
      <div v-else>确认停止当前订阅任务？</div>
    </ConfirmDialog>
    <DoctorDialog v-model="doctorVisible" :api="api" :plugin-id="pluginId" />
    <v-dialog v-model="configVisible" attach="body" max-width="62rem" :fullscreen="isMobile" class="config-dialog">
      <Config
        v-if="configVisible"
        :api="api"
        :initial-config="configData"
        :show-switch="false"
        @close="configVisible = false" />
    </v-dialog>

    <ConfirmDialog
      v-if="clearVisible"
      v-model="clearVisible"
      title="清空失败记录"
      icon="mdi-delete-sweep-outline"
      icon-color="error"
      body="确认清理失败记录页的全部失败与待修复记录？处理中的记录不会被清理，网盘文件和 STRM 均会保留，此操作不可逆。"
      :options="[
        { key: 'clearPoints', label: '同时清空 HDHive/Dian115 已花费积分记录', color: 'warning' },
      ]"
      confirm-text="确认清空"
      confirm-color="error"
      :loading="clearing"
      @confirm="clearHistory" />

    <ConfirmDialog
      v-if="cacheVisible"
      v-model="cacheVisible"
      title="清理缓存"
      icon="mdi-cached"
      icon-color="primary"
      :options="CACHE_CATEGORIES"
      :default-selected="['search']"
      show-select-all
      show-selected-count
      confirm-text="确认清理"
      :loading="clearingCache"
      @confirm="clearCache" />

    <ConfirmDialog
      v-if="deleteVisible"
      v-model="deleteVisible"
      title="删除失败记录"
      icon="mdi-delete-outline"
      icon-color="error"
      :body="deletingRecordCount
        ? `确认删除所选 ${deletingRecordCount} 条失败记录？网盘文件和 STRM 均会保留。`
        : `确认删除“${deletingRecord?.file_name || '此记录'}”的失败记录？网盘文件和 STRM 均会保留。`"
      confirm-text="确认删除"
      confirm-color="error"
      :loading="Boolean(deletingHistoryKey)"
      @confirm="deleteHistory" />

    <v-snackbar v-model="fallbackVisible" :color="fallbackType" :style="snackbarTextStyle" location="top" timeout="3000">
      {{ fallbackMessage }}
    </v-snackbar>
  </div>
</template>

<script setup>
import {computed, defineAsyncComponent, onMounted, onUnmounted, ref} from "vue";
import {useDisplay} from "vuetify";

import HistoryTable from "./dashboard/HistoryTable.vue";
import DoctorDialog from "./dialogs/DoctorDialog.vue";
import {useHistoryPageData} from "../composables/usePageData.js";
import {useRuntimeData} from "../composables/useRuntimeData.js";
import {CACHE_CATEGORIES, useCacheActions} from "../composables/useCacheActions.js";
import {formatBytes} from "../composables/resourceUtils.js";

const Config = defineAsyncComponent(() => import("./Config.vue"))
const ConfirmDialog = defineAsyncComponent(() => import("./dialogs/ConfirmDialog.vue"));
const ManualResourceDialog = defineAsyncComponent(() => import("./dialogs/ManualResourceDialog.vue"))
const OfflineTasksDialog = defineAsyncComponent(() => import("./dialogs/OfflineTasksDialog.vue"))

const props = defineProps({
  api: { type: [Object, Function], default: () => ({}) },
  pluginId: { type: String, default: "CloudSubscribeX" },
  navKey: { type: String, default: "main" },
  // MoviePilot 宿主以全屏弹窗打开插件页时传入；独立页面路由不传。
  onClose: { type: Function, default: null },
})

// 宿主弹窗模式：手机端宿主弹窗盖住自己的底部导航，页面需自带返回入口。
const isHostDialogPage = computed(() => typeof props.onClose === "function")

function closeHostDialog() {
  try {
    props.onClose?.()
  } catch (e) {
    console.warn("关闭插件页失败", e)
  }
}

const currentVersion = typeof __PLUGIN_VERSION__ !== "undefined" ? __PLUGIN_VERSION__ : "1.9.36";

// 全局转存暂停开关（原任务中心按钮，2026-11-05 随任务中心移除后收进「更多」菜单）
const transferPaused = ref(false)
const transferPauseLoading = ref(false)

async function loadTransferPause() {
  try {
    const result = await api.get(`plugin/${props.pluginId}/sync/pause`)
    if (result?.success === false) return
    transferPaused.value = Boolean(result?.data?.paused)
  } catch (e) {
    console.warn("读取转存暂停状态失败", e)
  }
}

function handleMoreMenu(open) {
  if (open) void loadTransferPause()
}

async function toggleTransferPause() {
  if (transferPauseLoading.value) return
  transferPauseLoading.value = true
  try {
    const result = await api.post(`plugin/${props.pluginId}/sync/pause`, {
      paused: !transferPaused.value,
    })
    if (result?.success === false) throw new Error(result?.message || "切换暂停失败")
    transferPaused.value = Boolean(result?.data?.paused)
    notify(result?.message || (transferPaused.value ? "已暂停转存" : "已恢复转存"))
  } catch (e) {
    notify(e?.message || "切换暂停失败", "error")
  } finally {
    transferPauseLoading.value = false
  }
}

async function forceReloadApp() {
  if (typeof window !== "undefined") {
    try {
      if ("caches" in window) {
        const keys = await window.caches.keys();
        await Promise.all(keys.map((k) => window.caches.delete(k)));
      }
    } catch {}
    const url = new URL(window.location.href);
    url.searchParams.set("_reload", Date.now().toString());
    window.location.replace(url.toString());
  }
}

const api = props.api
const display = useDisplay()
const isMobile = computed(() => display.xs.value)
const fallbackVisible = ref(false)
const fallbackMessage = ref("")
const fallbackType = ref("success")

const snackbarTextStyle = computed(() => ({
  color: `rgb(var(--v-theme-on-${fallbackType.value || "success"}))`,
}))
const historyStatus = ref("失败")
const offlineVisible = ref(false)
const manualVisible = ref(false)
const stopVisible = ref(false)
const stoppingTask = ref(null)
const stopping = ref(false)
const configVisible = ref(false)
const doctorVisible = ref(false)
const configLoading = ref(false)
const configData = ref({})
const clearVisible = ref(false)
const clearing = ref(false)
const cacheVisible = ref(false)
const clearingCache = ref(false)
const retryingHistoryKey = ref("")
const retryVisible = ref(false)
const retryingRecord = ref(null)
const retryBatchVisible = ref(false)
const retryBatchRecords = ref([])
const markSuccessVisible = ref(false)
const markSuccessRecords = ref([])
const markingHistoryKey = ref("")
const deleteVisible = ref(false)
const deletingRecord = ref(null)
const deletingRecords = ref([])
const deletingRecordCount = ref(0)
const deletingHistoryKey = ref("")
const searchConfirmVisible = ref(false)
const searchStarting = ref(false)
const historyDirty = ref(true)
const historySelection = ref({ groupCount: 0, subscribeIds: [], targets: [] })

function notify(text, type = "success") {
  const method = ["success", "info", "warning", "error"].includes(type) ? type : "success"
  fallbackMessage.value = text
  fallbackType.value = method
  fallbackVisible.value = true
}

const {
  failures,
  historyPage,
  loading,
  loadPage,
  updateHistoryQuery,
  clearHistory: clearHistoryRequest,
  deleteHistory: deleteHistoryRequest,
  deleteHistoryBatch: deleteHistoryBatchRequest,
} = useHistoryPageData(api, notify, props.pluginId)
const { clearCache: clearCacheRequest } = useCacheActions(api, props.pluginId)
let historyRefreshTimer = null

function scheduleHistoryRefresh() {
  historyDirty.value = true
  if (historyRefreshTimer !== null) {
    window.clearTimeout(historyRefreshTimer)
  }
  historyRefreshTimer = window.setTimeout(async () => {
    historyRefreshTimer = null
    if (historyDirty.value) {
      historyDirty.value = false
      await loadPage(false)
    }
  }, 180)
}

const {
  offlineSupported,
  runtime,
  active,
  startSync,
  stopSync: stopSyncRequest,
  stopTask: stopTaskRequest,
} = useRuntimeData(api, notify, props.pluginId, {
  onSettled: scheduleHistoryRefresh,
  onHistoryChanged: scheduleHistoryRefresh,
})

onMounted(() => {
  // v1.23.0：插件主页直接进失败记录页，首屏就按状态查服务端，默认只看失败。
  void updateHistoryQuery({status: "失败", page: 1})
  void loadTransferPause()
})
onUnmounted(() => {
  if (historyRefreshTimer !== null) window.clearTimeout(historyRefreshTimer)
})

// HistoryTable 的筛选 chip（× 清除筛选）走 query-change 回来时，
// 同步 historyStatus，否则 chip 上仍显示旧筛选。
function onHistoryQueryChange(query) {
  historyStatus.value = String(query?.status ?? "")
  return updateHistoryQuery(query)
}

const stoppableTaskCount = computed(
  () => (runtime.tasks || []).filter((task) => ["queued", "running"].includes(task.status)).length,
)
const selectedHistoryCount = computed(() => Math.max(0, Number(historySelection.value.groupCount || 0)))
const immediateSearchLabel = computed(() =>
  selectedHistoryCount.value ? `搜索所选（${selectedHistoryCount.value}）` : "搜索全部",
)
const immediateSearchTitle = computed(() =>
  selectedHistoryCount.value ? `立即搜索所选 ${selectedHistoryCount.value} 条失败记录的媒体` : "立即搜索全部订阅",
)
const immediateSearchDialogTitle = computed(() => (selectedHistoryCount.value ? "搜索所选失败记录" : "搜索全部订阅"))
const runtimeSummary = computed(() => {
  if (runtime.status === "starting") return "正在准备订阅任务"
  if (runtime.status === "stopping") return "正在停止当前任务"
  if (active.value) return runtime.task || "正在处理订阅任务"
  return "统一管理订阅搜索、资源转存与运行记录"
})

function updateHistorySelection(selection) {
  historySelection.value = {
    groupCount: Math.max(0, Number(selection?.groupCount || 0)),
    subscribeIds: Array.isArray(selection?.subscribeIds) ? [...selection.subscribeIds] : [],
    targets: Array.isArray(selection?.targets) ? [...selection.targets] : [],
  }
}

function openImmediateSearchConfirm() {
  searchConfirmVisible.value = true
}

async function confirmImmediateSearch() {
  if (searchStarting.value) return
  searchStarting.value = true
  try {
    const success = await startSync(historySelection.value)
    if (success) searchConfirmVisible.value = false
  } finally {
    searchStarting.value = false
  }
}

async function manualStarted(text) {
  notify(text)
  await loadPage(false)
}

function openManualDialog() {
  manualVisible.value = true
}

function confirmStopSync() {
  stoppingTask.value = null
  stopVisible.value = true
}

async function stopConfirmed() {
  if (stopping.value) return
  stopping.value = true
  try {
    const success = stoppingTask.value ? await stopTaskRequest(stoppingTask.value.id) : await stopSyncRequest()
    if (success) {
      stopVisible.value = false
      stoppingTask.value = null
    }
  } finally {
    stopping.value = false
  }
}

async function openConfig() {
  if (configLoading.value) return
  configLoading.value = true
  try {
    const response = await api.get(`plugin/form/${props.pluginId}`)
    const data = response?.data?.model ? response.data : response
    if (!data?.model || typeof data.model !== "object") {
      throw new Error("未能读取当前插件配置")
    }
    configData.value = JSON.parse(JSON.stringify(data.model))
    configVisible.value = true
  } catch (error) {
    notify(error.message || "加载配置失败", "error")
  } finally {
    configLoading.value = false
  }
}

function openMediaDetail(link) {
  window.location.hash = String(link).replace(/^#/, "")
}

function openClearHistory() {
  clearVisible.value = true
}

async function clearHistory({clearPoints}) {
  clearing.value = true
  try {
    const message = await clearHistoryRequest(false, clearPoints);
    clearVisible.value = false
    notify(message)
  } catch (e) {
    notify(e.message || "清空失败", "error")
  } finally {
    clearing.value = false
  }
}

async function clearCache(categories) {
  clearingCache.value = true
  try {
    const message = await clearCacheRequest(categories)
    cacheVisible.value = false
    notify(message)
  } catch (e) {
    notify(e.message || "清理缓存失败", "error")
  } finally {
    clearingCache.value = false
  }
}

function historyKey(record) {
  return [record.time, record.share_url, record.file_name].join("|")
}

function confirmRetryHistory(record) {
  retryingRecord.value = record
  retryVisible.value = true
}

async function retryHistory() {
  const record = retryingRecord.value
  if (!record || retryingHistoryKey.value) return
  retryingHistoryKey.value = historyKey(record)
  try {
    const result = await api.post(`plugin/${props.pluginId}/history/retry`, {
      time: record.time,
      share_url: record.share_url,
      file_name: record.file_name,
    })
    if (!result?.success) throw new Error(result?.message || "重试失败")
    await loadPage(false)
    retryVisible.value = false
    retryingRecord.value = null
    notify(result.message || "已重新提交处理")
  } catch (e) {
    notify(e.message || "重试失败", "error")
  } finally {
    retryingHistoryKey.value = ""
  }
}

function retryBatchIdentity(record) {
  return {
    time: record?.time,
    share_url: record?.share_url,
    file_name: record?.file_name,
  }
}

function confirmRetryFailedHistory(payload) {
  const records = (payload?.records || []).map(retryBatchIdentity)
  if (!records.length) {
    notify("没有可重试的失败记录", "warning")
    return
  }
  retryBatchRecords.value = records
  retryBatchVisible.value = true
}

function confirmRetrySelectedHistory(payload) {
  const records = (payload?.records || []).map(retryBatchIdentity)
  if (!records.length) {
    notify("所选记录没有可重试项", "warning")
    return
  }
  retryBatchRecords.value = records
  retryBatchVisible.value = true
}

// 后端单次上限 20 条，超出时前端分批串行提交，避免整批被拒。
const RETRY_BATCH_CHUNK_SIZE = 20

async function retryFailedBatch() {
  const records = retryBatchRecords.value
  if (!records.length || retryingHistoryKey.value) return
  retryingHistoryKey.value = "batch"
  try {
    let submitted = 0
    let skipped = 0
    for (let index = 0; index < records.length; index += RETRY_BATCH_CHUNK_SIZE) {
      const chunk = records.slice(index, index + RETRY_BATCH_CHUNK_SIZE)
      const result = await api.post(`plugin/${props.pluginId}/history/retry_batch`, { records: chunk })
      if (!result?.success) throw new Error(result?.message || "批量重试失败")
      const data = result?.data || {}
      submitted += Number(data.submitted ?? 0)
      skipped += Number(data.skipped ?? 0)
    }
    notify(`已提交 ${submitted} 条，跳过 ${skipped} 条`)
    retryBatchVisible.value = false
    retryBatchRecords.value = []
    await loadPage(false)
  } catch (e) {
    notify(e.message || "批量重试失败", "error")
  } finally {
    retryingHistoryKey.value = ""
  }
}

// 后端单次上限 2000 条，前端按 200 条串行提交，避免整批被拒。
const MARK_SUCCESS_CHUNK_SIZE = 200

function markSuccessIdentity(record) {
  return {
    record_id: record?.record_id,
    time: record?.time,
    share_url: record?.share_url,
    file_name: record?.file_name,
    tmdb_id: record?.tmdb_id,
    season: record?.season,
    episode: record?.episode,
  }
}

function confirmMarkHistorySuccess(payload) {
  const records = (payload?.records || []).map(markSuccessIdentity)
  if (!records.length) {
    notify("所选记录没有可标记的待修复/待确认项", "warning")
    return
  }
  markSuccessRecords.value = records
  markSuccessVisible.value = true
}

async function markHistorySuccess() {
  const records = markSuccessRecords.value
  if (!records.length || markingHistoryKey.value) return
  markingHistoryKey.value = "batch"
  try {
    let updated = 0
    let skipped = 0
    for (let index = 0; index < records.length; index += MARK_SUCCESS_CHUNK_SIZE) {
      const chunk = records.slice(index, index + MARK_SUCCESS_CHUNK_SIZE)
      const result = await api.post(`plugin/${props.pluginId}/history/mark_success`, { records: chunk })
      if (!result?.success) throw new Error(result?.message || "批量标记成功失败")
      const data = result?.data || {}
      updated += Number(data.updated ?? 0)
      skipped += Number(data.skipped ?? 0)
    }
    notify(`已标记 ${updated} 条为成功${skipped ? `，跳过 ${skipped} 条` : ""}`)
    markSuccessVisible.value = false
    markSuccessRecords.value = []
    await loadPage(false)
  } catch (error) {
    notify(error.message || "批量标记成功失败", "error")
  } finally {
    markingHistoryKey.value = ""
  }
}

function confirmDeleteHistory(record) {
  if (record?.status === "处理中") {
    notify("任务仍在处理，完成后才能删除失败记录", "warning")
    return
  }
  deletingRecord.value = record
  deletingRecords.value = []
  deletingRecordCount.value = 0
  deleteVisible.value = true
}

function confirmDeleteRecords({ records, recordCount }) {
  const deletableRecords = (records || []).filter(
    (record) => record?.status !== "处理中",
  )
  if (!deletableRecords.length) {
    notify("所选记录没有可删除的失败记录", "warning")
    return
  }
  deletingRecord.value = null
  deletingRecords.value = deletableRecords
  deletingRecordCount.value = Number(recordCount || deletableRecords.length)
  deleteVisible.value = true
}

async function deleteHistory() {
  const batch = deletingRecordCount.value > 0
  const record = deletingRecord.value
  if (!batch && !record) return
  deletingHistoryKey.value = batch ? "batch" : historyKey(record)
  try {
    const message = batch
      ? await deleteHistoryBatchRequest(deletingRecords.value)
      : await deleteHistoryRequest(record)
    deleteVisible.value = false
    deletingRecord.value = null
    deletingRecords.value = []
    deletingRecordCount.value = 0
    notify(message)
  } catch (e) {
    notify(e.message || "删除失败", "error")
  } finally {
    deletingHistoryKey.value = ""
  }
}

</script>

<style scoped>
@import "../styles/tokens.css";
.cloud-app-page {
  display: flex;
  box-sizing: border-box;
  min-width: 0;
  height: calc(100dvh - 64px);
  max-height: calc(100dvh - 64px);
  min-height: 0;
  flex-direction: column;
  gap: 16px;
  overflow: hidden;
  padding: 16px;
  color: rgb(var(--v-theme-on-background));
}

/* 宿主全屏弹窗内没有宿主顶栏，按整屏高度铺满，避免底部留出 64px 空白 */
.cloud-app-page.is-host-dialog-page {
  height: 100dvh;
  max-height: 100dvh;
}

.close-action {
  flex: 0 0 auto;
}

.app-header,
.app-identity,
.app-actions {
  display: flex;
  align-items: center;
}

.app-header {
  flex: 0 0 auto;
  justify-content: space-between;
  gap: 16px;
  min-width: 0;
}

.app-identity {
  min-width: 0;
  gap: 10px;
  flex: 1 1 auto;
}

.identity-text {
  min-width: 0;
  flex: 1 1 auto;
}

.identity-title-row {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.version-chip {
  flex-shrink: 0;
  font-size: 11px !important;
  letter-spacing: -0.2px;
  border-radius: 4px;
}

.app-identity h1 {
  margin: 0;
  font-size: 1.15rem;
  font-weight: 600;
  line-height: 1.3;
  letter-spacing: 0;
  white-space: nowrap;
}

.status-badge-clickable {
  cursor: pointer;
  transition: filter 0.15s ease;
}

.status-badge-clickable:hover { filter: brightness(1.08); }

.status-badge {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 1px 7px;
  border-radius: 999px;
  font-size: 0.72rem;
  font-weight: 500;
}

.status-badge-active {
  background: rgba(var(--v-theme-warning), 0.15);
  color: rgb(var(--v-theme-warning));
  border: 1px solid rgba(var(--v-theme-warning), 0.3);
}

.status-badge-idle {
  background: rgba(var(--v-theme-success), 0.12);
  color: rgb(var(--v-theme-success));
  border: 1px solid rgba(var(--v-theme-success), 0.25);
}

.status-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: currentColor;
}

.status-badge-active .status-dot {
  animation: pulse-dot 1.5s infinite;
}

@keyframes pulse-dot {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.4; transform: scale(0.85); }
}

.app-identity p {
  margin: 2px 0 0;
  color: rgba(var(--v-theme-on-surface), 0.62);
  font-size: 0.875rem;
  line-height: 1.4;
}

.app-actions {
  flex: 0 0 auto;
  gap: 6px;
}

.app-actions :deep(.v-btn) {
  min-height: 34px;
  padding-inline: 10px;
  font-size: 0.8rem;
}

.workspace-window,









:global(html:has(.cloud-app-page)),
:global(body:has(.cloud-app-page)) {
  overflow: hidden !important;
  overscroll-behavior: none;
}

@media (max-width: 959px) {
  .cloud-app-page {
    gap: 12px;
    padding: 12px;
  }

  .app-header {
    align-items: flex-start;
  }

  .app-actions {
    display: flex;
    align-items: center;
    gap: 4px;
  }

  .app-actions :deep(.v-btn) {
    min-width: 36px;
    padding-inline: 6px;
  }

  .app-actions :deep(.v-btn__prepend) {
    margin: 0;
  }

  .action-label {
    display: none;
  }

  .app-actions :deep(.settings-action) {
    min-width: 36px;
    padding-inline: 6px;
  }
}

.dialog-card {
  box-shadow: 0 16px 40px rgba(0, 0, 0, 0.16) !important;
}


@media (max-width: 600px) {
  .cloud-app-page {
    overflow-x: hidden;
    overflow-y: auto;
    overscroll-behavior-y: contain;
    padding: 10px;
    padding-bottom: 88px !important;
  }

  /* 弹窗模式下宿主底部导航被弹窗盖住，不再为它预留 88px */
  .cloud-app-page.is-host-dialog-page {
    padding-bottom: calc(12px + env(safe-area-inset-bottom)) !important;
  }

  .app-header {
    align-items: center;
    gap: 8px;
  }

  .app-identity {
    flex: 1 1 auto;
    gap: 6px;
    overflow: hidden;
    min-width: 0;
  }

  .app-identity :deep(.v-avatar) {
    width: 32px !important;
    height: 32px !important;
    flex-shrink: 0;
  }

  .identity-title-row {
    gap: 6px;
  }

  .app-identity h1 {
    font-size: 0.95rem;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .status-text {
    display: none;
  }

  .status-badge {
    padding: 3px;
    width: 12px;
    height: 12px;
    border-radius: 50%;
    justify-content: center;
  }

  .status-dot {
    width: 6px;
    height: 6px;
    margin: 0;
  }

  .app-identity p {
    display: none;
  }

  .manual-action,
  .settings-action {
    display: none !important;
  }

  .app-actions {
    display: flex !important;
    align-items: center;
    gap: 6px;
  }

  .app-actions :deep(.v-btn) {
    min-width: 32px;
    width: 32px;
    height: 32px;
    padding: 0 !important;
    border-radius: 50%;
    background: transparent !important;
    box-shadow: none !important;
  }

  .app-actions :deep(.v-btn__overlay),
  .app-actions :deep(.v-btn__underlay) {
    opacity: 0 !important;
  }

  .app-actions :deep(.sync-action) {
    color: rgb(var(--v-theme-primary)) !important;
  }

  .app-actions :deep(.sync-action-active) {
    color: rgb(var(--v-theme-warning)) !important;
  }

  .app-actions :deep(.settings-action) {
    min-width: 34px;
    width: 34px;
    padding-inline: 0;
  }

  .app-actions :deep(.settings-action .v-btn__prepend) {
    margin: 0;
  }

  .settings-action .action-label {
    display: none;
  }
}
.history-workspace {
  display: flex;
  min-width: 0;
  min-height: 0;
  flex: 1 1 auto;
  flex-direction: column;
  overflow: hidden;
  border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
  border-radius: 8px;
  background: rgb(var(--v-theme-surface));
}

.history-workspace :deep(.history-table-root) {
  flex: 1 1 auto;
  min-height: 0;
}
</style>
