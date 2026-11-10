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
            <v-list-item
              prepend-icon="mdi-folder-move-outline"
              title="网盘整理台"
              @click="mainTab = 'organize'" />
          </v-list>
        </v-menu>
      </div>
    </header>

    <!-- 全局运行中任务：非订阅影视手动转存没有订阅卡/历史卡可挂，靠这条浮条看进度 -->
    <RuntimeTaskStrip :tasks="runtime.tasks" />

    <section class="overview-band" aria-label="转存统计">
      <div class="overview-title d-flex align-center ga-2 mb-2">
        <v-icon icon="mdi-chart-box-outline" color="primary" size="small" />
        <span class="text-subtitle-2 font-weight-bold">转存统计</span>
      </div>
      <StatsGrid :stats="stats" class="overview-stats-grid" @select="handleStatSelect" />
    </section>

    <section :class="['workspace', { 'workspace-history': mainTab === 'history' }]">
      <div class="workspace-nav">
        <v-tabs v-model="mainTab" color="primary" class="workspace-tabs">
          <v-tab value="hub">
            <v-icon icon="mdi-filmstrip-box" size="small" class="mr-2" />
            追更列表
            <v-chip v-if="activeTaskCount" size="x-small" class="ml-2">
              {{ activeTaskCount }}
            </v-chip>
          </v-tab>
          <v-tab value="history">
            <v-icon icon="mdi-clipboard-text-clock-outline" size="small" class="mr-2" />
            历史记录
            <v-chip v-if="historyStats.total" size="x-small" class="ml-2">
              {{ historyStats.total }}
            </v-chip>
          </v-tab>
          <v-tab value="tg">
            <v-icon icon="mdi-television-guide" size="small" class="mr-2" />
            频道监控
          </v-tab>
          <v-tab value="organize">
            <v-icon icon="mdi-folder-move-outline" size="small" class="mr-2" />
            网盘整理
          </v-tab>
        </v-tabs>
      </div>

      <div class="workspace-window">
        <div v-show="mainTab === 'hub'" class="workspace-pane hub-pane">
          <MediaHub
            :items="subscribesProgress.length ? subscribesProgress : historyGroups"
            :tasks="runtime.tasks"
            :is-subscribe-mode="Boolean(subscribesProgress.length)"
            :api="api"
            :loading="loading"
            :retrying-key="retryingHistoryKey"
            @refresh="loadPage"
            @retry="confirmRetryHistory"
            @retry-failed="confirmRetryFailedHistory"
            @delete="confirmDeleteHistory" />
        </div>
        <div v-show="mainTab === 'tg'" class="workspace-pane tg-pane">
          <TgMonitor :api="api" :active="mainTab === 'tg'" />
        </div>
        <div v-show="mainTab === 'organize'" class="workspace-pane organize-pane">
          <OrganizePanel :api="api" />
        </div>
        <div v-show="mainTab === 'history'" class="workspace-pane history-pane">
          <HistoryTable
            :api="api"
            :items="historyGroups"
            :page="historyPage.page"
            :page-size="historyPage.pageSize"
            :total="historyPage.total"
            :total-pages="historyPage.totalPages"
            :filter-options="historyPage.filterOptions"
            :emby-play-items="embyPlayItems"
            :loading="loading"
            :retrying-key="retryingHistoryKey"
            :deleting-key="deletingHistoryKey"
            :notifying-key="notifyingHistoryKey"
            :upgrading-key="upgradingHistoryKey"
            :enable-cloud-upgrade="historyPage.enableCloudUpgrade"
            :initial-statuses="historyStatuses"
            @refresh="loadPage"
            @query-change="updateHistoryQuery"
            @clear="openClearHistory"
            @retry="confirmRetryHistory"
            @retry-failed="confirmRetryFailedHistory"
            @retry-selected="confirmRetrySelectedHistory"
            @mark-success="confirmMarkHistorySuccess"
            @delete="confirmDeleteHistory"
            @delete-groups="confirmDeleteGroups"
            @selection-change="updateHistorySelection"
            @notify="confirmNotifyHistory"
            @upgrade="handleHistoryUpgrade"
            @play="playHistory"
            @open-media="openMediaDetail" />
        </div>
      </div>
    </section>

    <OfflineTasksDialog v-if="offlineSupported" v-show="offlineVisible" v-model="offlineVisible" :api="api" />
    <ManualResourceDialog
      v-show="manualVisible"
      v-model="manualVisible"
      :api="api"
      :plugin-id="pluginId"
      :initial-mode="manualInitialMode"
      :initial-media="manualInitialMedia"
      @started="manualStarted" />
    <ConfirmDialog
      v-model="searchConfirmVisible"
      :title="immediateSearchDialogTitle"
      :loading="searchStarting"
      :confirm-text="selectedHistoryCount ? '搜索所选' : '搜索全部'"
      persistent
      @confirm="confirmImmediateSearch">
      <template v-if="selectedHistoryCount">
        确认立即搜索所选 {{ selectedHistoryCount }} 个历史媒体？
        <v-alert type="info" variant="tonal" density="compact" class="mt-3">
          本次仅搜索所选历史记录，不会搜索其他订阅。
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
      :body="`将把 ${markSuccessRecords.length} 条「待确认 / 待处理」记录直接标记为成功；之后落盘自检和后处理终态都不会再改写这些记录，是否继续？`"
      confirm-text="确认标记"
      confirm-color="warning"
      :loading="markingHistoryKey === 'batch'"
      @confirm="markHistorySuccess" />
    <ConfirmDialog
      v-model="upgradeVisible"
      title="确认历史洗版"
      alert-text="将继续使用该条历史记录作为现有版本基线，并按当前洗版设置搜索和比较候选资源。"
      alert-type="warning"
      confirm-text="确认洗版"
      confirm-color="warning"
      :loading="Boolean(upgradingHistoryKey)"
      persistent
      @confirm="confirmHistoryUpgrade">
      确认洗版“{{ historyUpgradeLabel(upgradingPayload) }}”？
    </ConfirmDialog>
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
    <DoctorDialog v-model="doctorVisible" :api="api" />
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
      title="清空历史记录"
      icon="mdi-delete-sweep-outline"
      icon-color="error"
      body="确认清理本插件的转存历史？此操作不可逆。"
      :options="[
        { key: 'forceClear', label: '同时终止并清理正在处理的记录', color: 'error' },
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
      title="删除历史记录"
      icon="mdi-delete-outline"
      icon-color="error"
      :body="deletingGroupCount
        ? `确认删除所选 ${deletingGroupCount} 个汇总项中的 ${deletingRecords.length} 条转存历史？`
        : `确认删除“${deletingRecord?.file_name || '此记录'}”的转存历史？`"
      :options="[{ key: 'deleteLinked', label: '同时删除关联的网盘文件和STRM', color: 'error' }]"
      confirm-text="确认删除"
      confirm-color="error"
      :loading="Boolean(deletingHistoryKey)"
      @confirm="deleteHistory" />

    <ConfirmDialog
      v-model="notifyVisible"
      title="补发汇总通知"
      confirm-text="确认发送"
      :error-text="notifyError"
      :loading="Boolean(notifyingHistoryKey)"
      @confirm="notifyHistory">
      将为“{{ notifyingSummaryTitle || notifyingRecord?.file_name || "此记录" }}”重新发送入库通知和Webhook。
    </ConfirmDialog>

    <v-snackbar v-model="fallbackVisible" :color="fallbackType" :style="snackbarTextStyle" location="top" timeout="3000">
      {{ fallbackMessage }}
    </v-snackbar>
  </div>
</template>

<script setup>
import {computed, defineAsyncComponent, onMounted, onUnmounted, ref, watch} from "vue";
import {useDisplay} from "vuetify";

import HistoryTable from "./dashboard/HistoryTable.vue";
import MediaHub from "./dashboard/MediaHub.vue";
import StatsGrid from "./dashboard/StatsGrid.vue";
import OrganizePanel from "./dashboard/OrganizePanel.vue";
import RuntimeTaskStrip from "./RuntimeTaskStrip.vue";
import DoctorDialog from "./dialogs/DoctorDialog.vue";
import {useHistoryPageData} from "../composables/usePageData.js";
import {useRuntimeData} from "../composables/useRuntimeData.js";
import {CACHE_CATEGORIES, useCacheActions} from "../composables/useCacheActions.js";
import {formatBytes} from "../composables/resourceUtils.js";

const Config = defineAsyncComponent(() => import("./Config.vue"))
const TgMonitor = defineAsyncComponent(() => import("./dashboard/TgMonitor.vue"))
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
const mainTab = ref("hub")
const historyStatuses = ref([])
const offlineVisible = ref(false)
const manualVisible = ref(false)
const manualInitialMode = ref("links")
const manualInitialMedia = ref(null)
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
const deletingGroupCount = ref(0)
const deletingHistoryKey = ref("")
const notifyVisible = ref(false)
const notifyingRecord = ref(null)
const notifyingSummaryTitle = ref("")
const notifyingHistoryKey = ref("")
const upgradingHistoryKey = ref("")
const upgradeVisible = ref(false)
const upgradingPayload = ref(null)
const notifyError = ref("")
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
  historyGroups,
  subscribesProgress,
  historyPage,
  historyStats,
  embyPlayItems,
  loading,
  stats,
  loadPage,
  loadSummary,
  updateHistoryQuery,
  clearHistory: clearHistoryRequest,
  deleteHistory: deleteHistoryRequest,
  deleteHistoryBatch: deleteHistoryBatchRequest,
  notifyHistory: notifyHistoryRequest,
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
    await loadSummary(false)
    if ((mainTab.value === "history" || mainTab.value === "hub") && historyDirty.value) {
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
  upgradeHistory: upgradeHistoryRequest,
} = useRuntimeData(api, notify, props.pluginId, {
  onSettled: scheduleHistoryRefresh,
  onHistoryChanged: scheduleHistoryRefresh,
})

onMounted(() => {
  void loadSummary()
  void loadPage(false)
  void loadTransferPause()
})
onUnmounted(() => {
  if (historyRefreshTimer !== null) window.clearTimeout(historyRefreshTimer)
})

watch(mainTab, (tab) => {
  if ((tab !== "history" && tab !== "hub") || !historyDirty.value) return
  historyDirty.value = false
  void loadPage()
})

function handleStatSelect(stat) {
  const filter = String(stat?.filter || "").trim()
  historyStatuses.value = filter ? [filter] : []
  mainTab.value = "history"
}

const activeTaskCount = computed(
  () =>
    (runtime.tasks || []).filter((task) => ["queued", "running", "stopping", "downloading", "transferring", "postprocessing"].includes(task.status))
      .length,
)
const stoppableTaskCount = computed(
  () => (runtime.tasks || []).filter((task) => ["queued", "running"].includes(task.status)).length,
)
const selectedHistoryCount = computed(() => Math.max(0, Number(historySelection.value.groupCount || 0)))
const immediateSearchLabel = computed(() =>
  selectedHistoryCount.value ? `搜索所选（${selectedHistoryCount.value}）` : "搜索全部",
)
const immediateSearchTitle = computed(() =>
  selectedHistoryCount.value ? `立即搜索所选 ${selectedHistoryCount.value} 个历史媒体` : "立即搜索全部订阅",
)
const immediateSearchDialogTitle = computed(() => (selectedHistoryCount.value ? "搜索所选历史记录" : "搜索全部订阅"))
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
  await loadSummary(false)
  if (mainTab.value === "history") await loadPage(false)
  else historyDirty.value = true
}

function openManualDialog(mode = "links", media = null) {
  manualInitialMode.value = mode === "upgrade" ? "upgrade" : "links"
  manualInitialMedia.value = media ? { ...media } : null
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

async function playHistory(itemId) {
  const popup = window.open("", "_blank")
  try {
    const result = await api.get(`plugin/${props.pluginId}/history/play/${encodeURIComponent(itemId)}`)
    const url = result?.data?.url
    if (!result?.success || !url) throw new Error(result?.message || "未找到播放地址")
    if (popup) popup.location.href = url
    else window.open(url, "_blank", "noopener,noreferrer")
  } catch (error) {
    if (popup) popup.close()
    notify(error.message || "打开 Emby 失败", "error")
  }
}

function openClearHistory() {
  clearVisible.value = true
}

async function clearHistory({forceClear, clearPoints}) {
  clearing.value = true
  try {
    const message = await clearHistoryRequest(forceClear, clearPoints);
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
    await Promise.all([loadPage(false), loadSummary(false)])
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
    await Promise.all([loadPage(false), loadSummary(false)])
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
    notify("所选记录没有可标记的待确认/待处理项", "warning")
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
      const result = await api.post("plugin/CloudSubscribeX/history/mark_success", { records: chunk })
      if (!result?.success) throw new Error(result?.message || "批量标记成功失败")
      const data = result?.data || {}
      updated += Number(data.updated ?? 0)
      skipped += Number(data.skipped ?? 0)
    }
    notify(`已标记 ${updated} 条为成功${skipped ? `，跳过 ${skipped} 条` : ""}`)
    markSuccessVisible.value = false
    markSuccessRecords.value = []
    await Promise.all([loadPage(false), loadSummary(false)])
  } catch (error) {
    notify(error.message || "批量标记成功失败", "error")
  } finally {
    markingHistoryKey.value = ""
  }
}

function handleHistoryUpgrade(payload) {
  if (payload?.scope === "group") {
    openManualDialog("upgrade", payload.media || null)
    return
  }
  if (!(payload?.records || []).length) return
  upgradingPayload.value = payload
  upgradeVisible.value = true
}

function historyUpgradeLabel(payload) {
  const record = payload?.records?.[0] || {}
  const title = String(record.title || record.file_name || "此记录").trim()
  const season = Number(record.season || 0)
  const episode = Number(record.episode || 0)
  if (season > 0 && episode > 0) {
    return `${title} S${String(season).padStart(2, "0")}E${String(episode).padStart(2, "0")}`
  }
  return title
}

async function confirmHistoryUpgrade() {
  const payload = upgradingPayload.value
  const records = payload?.records || []
  if (!records.length) return
  upgradingHistoryKey.value = String(payload?.key || "")
  try {
    const message = await upgradeHistoryRequest(records, payload?.scope || "record")
    upgradeVisible.value = false
    upgradingPayload.value = null
    notify(message)
  } catch (e) {
    notify(e.message || "洗版任务提交失败", "error")
  } finally {
    upgradingHistoryKey.value = ""
  }
}

function confirmDeleteHistory(record) {
  if (!["成功", "失败", "跳过"].includes(record?.status) && !record?.finalize_key) {
    notify("任务仍在处理，完成后才能删除历史记录", "warning")
    return
  }
  deletingRecord.value = record
  deletingRecords.value = []
  deletingGroupCount.value = 0
  deleteVisible.value = true
}

function confirmDeleteGroups({ records, groupCount }) {
  const deletableRecords = (records || []).filter(
    (record) => ["成功", "失败", "跳过"].includes(record?.status) || Boolean(record?.finalize_key),
  )
  if (!deletableRecords.length) {
    notify("所选汇总项没有可删除的历史记录", "warning")
    return
  }
  deletingRecord.value = null
  deletingRecords.value = deletableRecords
  deletingGroupCount.value = Number(groupCount || 0)
  deleteVisible.value = true
}

async function deleteHistory({deleteLinked}) {
  const batch = deletingGroupCount.value > 0
  const record = deletingRecord.value
  if (!batch && !record) return
  deletingHistoryKey.value = batch ? "batch" : historyKey(record)
  try {
    const message = batch
      ? await deleteHistoryBatchRequest(deletingRecords.value, deleteLinked)
      : await deleteHistoryRequest(record, deleteLinked)
    deleteVisible.value = false
    deletingRecord.value = null
    deletingRecords.value = []
    deletingGroupCount.value = 0
    notify(message)
  } catch (e) {
    notify(e.message || "删除失败", "error")
  } finally {
    deletingHistoryKey.value = ""
  }
}

function confirmNotifyHistory(payload) {
  const record = payload?.record || payload
  if (record?.status !== "成功" || record?.finalize_key) {
    notify("文件尚未成功完成，不能发送通知", "warning")
    return
  }
  notifyingRecord.value = record
  notifyingSummaryTitle.value = String(payload?.summaryTitle || record?.title || record?.file_name || "此记录")
  notifyError.value = ""
  notifyVisible.value = true
}

async function notifyHistory() {
  const record = notifyingRecord.value
  if (!record) return
  notifyingHistoryKey.value = historyKey(record)
  try {
    const message = await notifyHistoryRequest(record)
    notifyVisible.value = false
    notifyingRecord.value = null
    notifyingSummaryTitle.value = ""
    notifyError.value = ""
    notify(message)
  } catch (e) {
    notifyError.value = e.message || "通知失败"
  } finally {
    notifyingHistoryKey.value = ""
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
.app-actions,
.overview-title,
.overview-metric {
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

.overview-band {
  display: flex;
  flex: 0 0 auto;
  flex-direction: column;
  gap: 8px;
  min-width: 0;
}

.overview-title {
  gap: 8px;
  padding-inline: 2px;
  font-size: 0.875rem;
  font-weight: 600;
}

.overview-metrics {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  overflow: hidden;
  border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
  border-radius: 8px;
  background: rgb(var(--v-theme-surface));
}

.overview-metric {
  min-width: 0;
  gap: 10px;
  padding: 12px 16px;
}

.overview-metric + .overview-metric {
  border-left: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
}

.overview-metric > div,
.overview-metric span,
.overview-metric strong {
  display: block;
  min-width: 0;
}

.overview-metric span {
  color: rgba(var(--v-theme-on-surface), 0.62);
  font-size: 0.75rem;
}

.overview-metric strong {
  margin-top: 2px;
  font-size: 1.15rem;
  line-height: 1.2;
}

.workspace {
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

.workspace-nav {
  display: flex;
  align-items: center;
  min-width: 0;
  flex: 0 0 auto;
  border-bottom: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
}

.workspace-tabs {
  min-width: 0;
  flex: 1 1 auto;
}

.workspace-tabs :deep(.v-tab) {
  min-width: auto;
  padding-inline: 14px;
  letter-spacing: normal;
  text-transform: none;
  font-size: 0.86rem;
  gap: 4px;
}

.workspace-tabs :deep(.v-tab .v-icon) {
  margin-right: 2px !important;
}

.workspace-tabs :deep(.v-tab .v-chip) {
  margin-left: 4px !important;
}

.workspace-window,
.workspace-pane {
  min-height: 0;
  height: 100%;
}

.workspace-window {
  display: flex;
  flex: 1 1 auto;
}

.workspace-pane {
  min-width: 0;
  flex: 1 1 auto;
  overflow: hidden;
}

.hub-pane {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 12px;
}

.hub-pane :deep(.media-hub-root) {
  flex: 1 1 auto;
  min-height: 0;
}

.runtime-panel {
  flex: 0 0 auto;
  width: 100%;
  min-width: 0;
}

.history-pane {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.history-view-switch {
  display: flex;
  flex: 0 0 auto;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
  padding: 4px 2px 0;
}

.history-pane :deep(.history-table-root) {
  flex: 1 1 auto;
  min-height: 0;
}

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
  .overview-metric--desktop-only {
    display: none;
  }

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

  .overview-metrics {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .workspace {
    min-height: max(460px, calc(100dvh - 210px));
    flex: 0 0 auto;
  }

  .workspace-window,
  .workspace-pane {
    height: auto;
    overflow: visible;
  }

  .hub-pane :deep(.runtime-panel),
  .history-view-switch,
  .hub-pane :deep(.task-list),
  .workspace-history .history-pane :deep(.history-table-root),
  .workspace-history .history-pane :deep(.history-mobile-list),
  .workspace-history .history-pane :deep(.history-mobile-scroll) {
    min-height: 0;
    flex: 0 0 auto;
    overflow: visible;
    overscroll-behavior: auto;
    touch-action: pan-y;
  }

  .workspace-history .history-pane :deep(.history-table-root),
  .workspace-history .history-pane :deep(.history-mobile-list) {
    width: 100%;
    max-width: 100%;
  }

  .hub-pane :deep(.runtime-panel) {
    display: flex;
    width: 100%;
    min-height: 0;
  }

  .overview-metric {
    padding: 9px 12px;
  }

  .workspace-nav {
    overflow-x: auto;
    scrollbar-width: none;
    -webkit-overflow-scrolling: touch;
  }

  .workspace-nav::-webkit-scrollbar {
    display: none;
  }

  .workspace-tabs {
    flex: 0 0 auto;
    width: auto;
  }

  .workspace-tabs :deep(.v-tab) {
    min-width: auto;
    flex: 0 0 auto;
    padding-inline: 10px;
    font-size: 0.8rem;
    text-transform: none;
  }

  .hub-pane {
    padding: 8px;
  }
}
</style>
