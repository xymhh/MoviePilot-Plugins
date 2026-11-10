<template>
  <v-dialog
    v-model="visible"
    max-width="820"
    scrollable
    class="media-detail-dialog">
    <v-card v-if="group" class="rounded-lg elevation-8 overflow-hidden">
      <!-- 头部：海报 + 基础元信息 + 关闭按钮 -->
      <v-card-title class="drawer-header pa-4 bg-surface-variant d-flex align-start ga-3">
        <div class="drawer-cover">
          <img
            v-if="coverUrl && !coverError"
            :src="coverUrl"
            class="cover-img rounded elevation-2"
            alt=""
            loading="lazy"
            @error="coverError = true" />
          <div v-else class="cover-badge-art rounded elevation-2">
            <span class="art-letter">{{ getInitial(group.title || group.name) }}</span>
          </div>
        </div>

        <div class="drawer-title-box flex-grow-1 min-w-0">
          <div class="d-flex align-center justify-space-between">
            <h2 class="text-h6 font-weight-bold drawer-title text-truncate" :title="group.title || group.name">
              {{ group.title || group.name }}
              <span v-if="group.year" class="text-medium-emphasis text-body-1">({{ group.year }})</span>
            </h2>
            <v-btn icon="mdi-close" variant="text" size="small" @click="visible = false" />
          </div>

          <div class="drawer-meta-tags mt-2 d-flex flex-wrap align-center ga-1">
            <v-chip size="x-small" color="primary" variant="tonal">
              {{ group.type || "电视剧" }}<template v-if="group.season"> · S{{ String(group.season).padStart(2, "0") }}</template>
            </v-chip>
            <v-chip v-if="airedCount !== null" size="x-small" variant="tonal" color="info">
              已播 {{ airedCount }}<template v-if="totalCount > 0"> / 共 {{ totalCount }}</template> 集
            </v-chip>
            <v-chip v-if="task" size="x-small" :color="taskStatusColor(task)" variant="flat" :class="['font-weight-bold', solidChipClass(taskStatusColor(task))]">
              {{ taskStatusLabel(task, { withProgress: true }) }}
            </v-chip>
            <v-chip v-else-if="group.subscribe_id" size="x-small" color="info" variant="tonal">
              监控中
            </v-chip>
            <v-chip size="x-small" variant="flat" color="success" :class="['font-weight-bold', solidChipClass('success')]">
              已入库 {{ libraryCount }}
            </v-chip>
            <v-chip v-if="pendingMissingCount > 0" size="x-small" variant="flat" color="warning" :class="['font-weight-bold', solidChipClass('warning')]">
              待补 {{ pendingMissingCount }}
            </v-chip>
            <v-chip v-if="unairedCount > 0" size="x-small" variant="tonal" color="info">
              未播 {{ unairedCount }}
            </v-chip>
          </div>

          <div class="drawer-actions mt-3 d-flex align-center ga-2">
            <v-btn
              v-if="hasFailedRecords"
              size="small"
              color="warning"
              variant="tonal"
              prepend-icon="mdi-refresh"
              :loading="retrying"
              @click="$emit('retry-failed', group)">
              重试失败集
            </v-btn>
            <v-btn
              v-if="api"
              size="small"
              color="primary"
              variant="tonal"
              prepend-icon="mdi-magnify"
              @click="openSearch()">
              搜索资源
            </v-btn>
            <v-btn
              v-if="mediaDetailLink"
              size="small"
              variant="text"
              color="primary"
              prepend-icon="mdi-open-in-new"
              :href="mediaDetailLink"
              target="_blank">
              TMDB / 详情
            </v-btn>
          </div>
        </div>
      </v-card-title>

      <v-divider />

      <!-- 主体内容 -->
      <v-card-text class="drawer-body pa-4" style="max-height: 65vh;">
        <!-- 资源搜索：按剧名或 SxxExx 精确搜，结果可一键转存 -->
        <div v-if="searchOpen" class="resource-search-panel mb-4 pa-3 rounded border">
          <div class="search-bar d-flex align-center ga-2 flex-wrap">
            <v-text-field
              v-model="searchKeyword"
              density="compact"
              variant="outlined"
              hide-details
              placeholder="剧名 或 剧名 S01E07"
              class="search-keyword"
              @keyup.enter="runSearch" />
            <v-btn size="small" color="primary" variant="flat" :loading="searching" @click="runSearch">搜索</v-btn>
            <v-btn size="small" variant="text" @click="closeSearch">收起</v-btn>
          </div>

          <div v-if="pendingEpisodeNumbers.length" class="search-ep-chips mt-2 d-flex align-center flex-wrap ga-1">
            <span class="text-caption text-medium-emphasis">按集搜：</span>
            <v-chip
              v-for="ep in pendingEpisodeNumbers.slice(0, 15)"
              :key="ep"
              size="x-small"
              variant="tonal"
              color="primary"
              @click="searchEpisode(ep)">
              E{{ String(ep).padStart(2, "0") }}
            </v-chip>
          </div>

          <!-- 搜索源切换：默认「全部」混排，点某个渠道只看它 -->
          <div v-if="searchSourceTabs.length > 1" class="search-source-tabs mt-2 d-flex align-center flex-wrap ga-1">
            <span class="text-caption text-medium-emphasis">搜索源：</span>
            <v-chip
              v-for="tab in searchSourceTabs"
              :key="tab.key"
              size="x-small"
              :variant="activeSearchSource === tab.key ? 'flat' : 'tonal'"
              :color="activeSearchSource === tab.key ? 'primary' : undefined"
              @click="activeSearchSource = tab.key">
              {{ tab.name }}<span class="source-count"> {{ tab.count }}</span>
            </v-chip>
          </div>

          <div v-if="searching" class="text-caption text-medium-emphasis mt-2">搜索中…只查后台启用的渠道，首次可能 1-2 分钟，之后走缓存很快</div>
          <div v-else-if="searchError" class="text-caption text-error mt-2">{{ searchError }}</div>
          <div v-else-if="searchNote" class="text-caption text-medium-emphasis mt-2">{{ searchNote }}</div>

          <div v-if="visibleSearchItems.length" class="search-results mt-3">
            <div v-for="(item, idx) in visibleSearchItems" :key="item.url || idx" class="search-result-item">
              <div class="result-main">
                <div class="result-title" :title="item.title">{{ item.title }}</div>
                <div class="result-meta">
                  <span v-if="item.source_name || item.source" class="meta-source">{{ item.source_name || item.source }}</span>
                  <span v-if="item.resource_type_name" class="meta-pan">{{ item.resource_type_name }}</span>
                  <span v-if="item.size_formatted">{{ item.size_formatted }}</span>
                  <span v-if="item.seeders">{{ item.seeders }} 做种</span>
                  <span
                    v-if="item.cost_label"
                    class="meta-cost"
                    :class="{ 'meta-cost--paid': Number(item.unlock_points) > 0 && !item.is_unlocked }">
                    {{ item.cost_label }}
                  </span>
                </div>
              </div>
              <v-btn
                size="x-small"
                color="primary"
                variant="flat"
                :loading="transferringUrl === (item.url || item.resource_ref)"
                :disabled="!item.url && !item.resource_ref"
                @click="transferItem(item)">
                转存
              </v-btn>
            </div>
          </div>
        </div>

        <div v-if="task" class="task-progress-panel mb-4 pa-3 rounded border">
          <div class="d-flex align-center flex-wrap ga-2">
            <v-chip size="small" :color="taskStatusColor(task)" variant="tonal" class="font-weight-bold">
              {{ taskStatusLabel(task, { withProgress: true }) }}
            </v-chip>
            <span class="text-caption text-medium-emphasis">{{ taskPhaseText(task) }}</span>
            <v-spacer />
            <span v-if="taskPercent > 0" class="text-caption font-weight-medium">{{ taskPercent }}%</span>
          </div>
          <v-progress-linear
            v-if="showTaskProgress"
            class="mt-2"
            :model-value="taskPercent"
            :color="taskStatusColor(task)"
            height="6"
            rounded />
          <div class="d-flex flex-wrap align-center ga-4 mt-2 text-caption text-medium-emphasis">
            <span v-if="taskSizeText">已转 {{ taskSizeText }}</span>
            <span v-if="taskSpeedText">{{ taskSpeedText }}</span>
            <span
              v-if="task.current_file"
              class="text-truncate task-current-file"
              :title="task.current_file">
              当前：{{ task.current_file }}
            </span>
          </div>
          <div v-if="task.error" class="text-caption text-error mt-1">失败原因：{{ task.error }}</div>
        </div>

        <!-- 1. 分集选集方块矩阵 (B站/爱奇艺风格) -->
        <div v-if="episodesList.length" class="episode-matrix-section mb-4">
          <div class="d-flex align-center justify-space-between mb-2">
            <div class="d-flex align-center ga-2">
              <span class="text-subtitle-2 font-weight-bold">选集进度</span>
              <span class="text-caption text-medium-emphasis">点单集看状态</span>
            </div>
            <!-- 状态图例 -->
            <div class="matrix-legend d-flex align-center ga-2 text-caption">
              <span class="legend-item"><span class="legend-dot bg-success"></span>已入库</span>
              <span class="legend-item"><span class="legend-dot bg-warning"></span>转存中</span>
              <span class="legend-item"><span class="legend-dot bg-surface-variant border"></span>待补齐</span>
              <span class="legend-item"><span class="legend-dot ep-dot-unaired"></span>未播</span>
            </div>
          </div>

          <div class="episode-grid">
            <button
              v-for="ep in episodesList"
              :key="ep.number"
              type="button"
              :class="['ep-box', `ep-box--${ep.status}`, { 'ep-box--selected': selectedEpisode === ep.number }]"
              @click="onSelectEp(ep)">
              <span class="ep-num">{{ ep.number }}</span>
              <span class="ep-badge">{{ ep.statusText }}</span>
            </button>
          </div>

          <!-- 选中某一集后的快捷卡片 -->
          <v-expand-transition>
            <div v-if="selectedEpData" class="selected-ep-card mt-3 pa-3 rounded border">
              <div class="selected-ep-row">
                <div class="selected-ep-main">
                  <v-chip size="small" :color="selectedEpData.statusColor" variant="tonal" class="font-weight-bold">
                    第 {{ selectedEpData.number }} 集 · {{ selectedEpData.statusText }}
                  </v-chip>
                  <span v-if="selectedEpData.matchedRecord?.file_name" class="text-caption ep-file-name" :title="selectedEpData.matchedRecord.file_name">
                    {{ selectedEpData.matchedRecord.file_name }}
                  </span>
                </div>
                <div class="selected-ep-actions">
                  <v-btn
                    v-if="selectedEpData.matchedRecord"
                    size="x-small"
                    variant="tonal"
                    color="primary"
                    prepend-icon="mdi-refresh"
                    @click="$emit('retry', selectedEpData.matchedRecord)">
                    重新转存此集
                  </v-btn>
                </div>
              </div>
            </div>
          </v-expand-transition>
        </div>

        <!-- 2. 追更记录：成功 / 跳过 / 失败都在同一条时间线上 -->
        <div v-if="(group.records || []).length" class="records-log-section mt-4">
          <div class="d-flex align-center justify-space-between mb-2">
            <span class="text-subtitle-2 font-weight-bold">追更记录 ({{ (group.records || []).length }})</span>
            <span class="text-caption text-medium-emphasis">点单集可重新转存</span>
          </div>

          <div class="record-list">
            <v-card
              v-for="(rec, idx) in group.records || []"
              :key="recordKey(rec, idx)"
              variant="outlined"
              :class="['record-item', 'mb-2', 'pa-3', recordToneClass(rec)]">
              <div class="record-layout">
                <div class="record-main min-w-0">
                  <div class="record-head">
                    <v-chip
                      size="x-small"
                      :color="recordStatusColor(rec)"
                      variant="flat"
                      :class="['font-weight-bold', 'record-status-chip', solidChipClass(recordStatusColor(rec))]">
                      {{ recordStatusLabel(rec) }}
                    </v-chip>
                    <span v-if="rec.season || rec.episode" class="record-ep">
                      {{ formatSeasonEpisode(rec) }}
                    </span>
                    <span class="record-time">{{ rec.time || "-" }}</span>
                    <v-spacer />
                    <span v-if="rec.size" class="record-size">{{ formatSize(rec.size) }}</span>
                  </div>

                  <div class="record-filename text-body-2" :title="rec.file_name || rec.title">
                    {{ rec.file_name || rec.title || "未知文件" }}
                  </div>

                  <div class="record-meta">
                    <span v-if="sourceLabel(rec)" class="record-meta-item">
                      <v-icon icon="mdi-source-branch" size="12" class="mr-1" />
                      {{ sourceLabel(rec) }}
                    </span>
                    <span v-if="rec.resource_type" class="record-meta-item">
                      <v-icon icon="mdi-cloud-outline" size="12" class="mr-1" />
                      {{ resourceTypeLabel(rec.resource_type) }}
                    </span>
                    <a
                      v-if="rec.source_link"
                      :href="rec.source_link"
                      target="_blank"
                      rel="noopener"
                      class="record-meta-link"
                      @click.stop>
                      查看来源
                    </a>
                    <a
                      v-if="rec.share_url"
                      :href="rec.share_url"
                      target="_blank"
                      rel="noopener"
                      class="record-meta-link"
                      @click.stop>
                      打开分享
                    </a>
                  </div>

                  <div v-if="rec.reason" :class="['record-reason', recordReasonTone(rec)]">
                    <v-icon :icon="recordReasonIcon(rec)" size="13" class="mr-1" />
                    <span>{{ rec.reason }}</span>
                  </div>
                </div>

                <div class="record-actions d-flex flex-column ga-1">
                  <v-btn
                    icon="mdi-refresh"
                    size="x-small"
                    variant="text"
                    color="primary"
                    title="重试此条"
                    :loading="retryingKey === retryKey(rec)"
                    @click="$emit('retry', rec)" />
                  <v-btn
                    icon="mdi-delete-outline"
                    size="x-small"
                    variant="text"
                    color="error"
                    title="删除记录"
                    @click="$emit('delete', rec)" />
                </div>
              </div>
            </v-card>
          </div>
        </div>

        <!-- 3. 空提示（如果既无分集也无流水） -->
        <div v-else-if="!episodesList.length" class="empty-drawer text-center py-8 text-medium-emphasis">
          <v-icon icon="mdi-information-outline" size="40" class="mb-2" />
          <div>该订阅当前没有分集与追更记录</div>
        </div>
      </v-card-text>

      <v-divider />
      <v-card-actions class="pa-3 bg-surface-variant d-flex justify-end">
        <v-btn variant="text" size="small" @click="visible = false">关闭</v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>
</template>

<script setup>
import { ref, computed, watch } from "vue"
import {
  displayTotal,
  displayTransferred,
  formatSize as formatTaskSize,
  formatSpeed,
  hasDeterminateProgress,
  taskPhaseText,
  taskProgress,
  taskStatusColor,
  taskStatusLabel,
} from "../../utils/taskUtils.js"
import { getSourceName } from "../../composables/resourceUtils.js"

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  group: { type: Object, default: null },
  task: { type: Object, default: null },
  retrying: { type: Boolean, default: false },
  retryingKey: { type: String, default: "" },
  api: { type: Object, default: null },
})

const emit = defineEmits(["update:modelValue", "retry", "retry-failed", "delete", "refresh"])

const selectedEpisode = ref(null)
const coverError = ref(false)

const taskPercent = computed(() => Math.round(taskProgress(props.task)))
const showTaskProgress = computed(() => Boolean(props.task) && hasDeterminateProgress(props.task) && taskPercent.value > 0)
const taskSizeText = computed(() => {
  const total = displayTotal(props.task)
  if (!total) return ""
  return `${formatTaskSize(displayTransferred(props.task))} / ${formatTaskSize(total)}`
})
const taskSpeedText = computed(() => {
  const speed = Number(props.task?.speed_bytes_per_second || props.task?.upload_speed || 0)
  return speed > 0 ? formatSpeed(speed) : ""
})

const visible = computed({
  get: () => props.modelValue,
  set: (val) => {
    emit("update:modelValue", val)
    if (!val) {
      selectedEpisode.value = null
      coverError.value = false
    }
  },
})

const coverUrl = computed(() => {
  if (!props.group) return ""
  const raw = (
    props.group.poster_url ||
    props.group.poster ||
    props.group.image ||
    props.group.poster_path ||
    props.group.backdrop_path ||
    (props.group.records && props.group.records[0] && (props.group.records[0].poster_url || props.group.records[0].image || props.group.records[0].poster || props.group.records[0].poster_path)) ||
    ""
  )
  if (!raw) return ""
  const str = String(raw).trim()
  if (str.startsWith("data:") || str.startsWith("blob:")) {
    return str
  }
  if (str.startsWith("/api/v1/system/cache/image") || str.startsWith("api/v1/plugin/")) {
    return str
  }
  let target = str
  if (target.startsWith("/") && !target.startsWith("//")) {
    target = `https://image.tmdb.org/t/p/w500${target}`
  }
  return `/api/v1/system/cache/image?url=${encodeURIComponent(target)}`
})

function getInitial(title) {
  const clean = String(title || "").trim().replace(/^[^\w\u4e00-\u9fa5]+/, "")
  return clean ? clean.slice(0, 1) : "片"
}

const mediaDetailLink = computed(() => {
  const tmdbId = props.group?.tmdb_id
  const type = props.group?.type === "电影" ? "movie" : "tv"
  return tmdbId ? `https://www.themoviedb.org/${type}/${tmdbId}` : ""
})

const totalCount = computed(() => {
  if (props.group?.total_episodes !== undefined) {
    return Number(props.group.total_episodes) || 0
  }
  return (props.group?.records || []).length
})

const airedCount = computed(() => {
  const value = props.group?.aired_episodes
  if (value === null || value === undefined || value === "") return null
  const number = Number(value)
  return Number.isFinite(number) ? Math.max(0, number) : null
})

const libraryCount = computed(() => {
  if (props.group?.library_count !== undefined) {
    return Number(props.group.library_count) || (props.group.library_episodes || []).length
  }
  return (props.group?.records || []).filter((r) => !r.status || r.status === "成功" || r.status === "完成").length
})

const missingCount = computed(() => {
  if (props.group?.missing_count !== undefined) {
    return Number(props.group.missing_count) || 0
  }
  return (props.group?.records || []).filter((r) => r.status && r.status !== "成功" && r.status !== "完成").length
})

/** 已播到第几集：优先用最新已播集号，其次用已播集数；0 表示 TMDB 还没给数据 */
const airedBoundary = computed(() => {
  const latest = Number(props.group?.latest_air_episode || 0)
  const aired = Number(props.group?.aired_episodes || 0)
  return Math.max(latest, aired)
})

/** 还没播出的集数：总集数里超过已播边界、且本地确实没有的部分 */
const unairedCount = computed(() => {
  const total = totalCount.value
  const boundary = airedBoundary.value
  if (total <= 0 || boundary <= 0) return 0
  const libSet = new Set(props.group?.library_episodes || [])
  const transferringSet = new Set(props.group?.transferring_episodes || [])
  let count = 0
  for (let i = boundary + 1; i <= total; i += 1) {
    if (!libSet.has(i) && !transferringSet.has(i)) count += 1
  }
  return count
})

/** 真该补的：缺集里已经播出的部分 */
const pendingMissingCount = computed(() => Math.max(0, missingCount.value - unairedCount.value))

const hasFailedRecords = computed(() => {
  return (props.group?.records || []).some((r) => r.status && r.status !== "成功" && r.status !== "完成")
})

// 生成 1..N 的分集列表
const episodesList = computed(() => {
  if (!props.group) return []
  const total = totalCount.value
  const libSet = new Set(props.group.library_episodes || [])
  const transferringSet = new Set(props.group.transferring_episodes || [])
  const recs = props.group.records || []

  // 有后端聚合口径（total_episodes）时，已入库只认 library_episodes，
  // 否则会出现「头部说缺 17 集、矩阵却把 93-106 全涂成已入库」的分裂。
  const aggregateMode = total > 0
  const boundary = airedBoundary.value
  // 聚合模式下集数上界就是全剧集数；否则按流水里的最大集号推断
  const maxEp = aggregateMode ? total : Math.max(0, ...recs.map((r) => Number(r.episode || 0)))
  if (maxEp <= 0) return []

  const list = []
  for (let i = 1; i <= maxEp; i++) {
    let status = "missing"
    let statusText = "待补"

    if (libSet.has(i)) {
      status = "success"
      statusText = "已入库"
    } else if (transferringSet.has(i)) {
      status = "transferring"
      statusText = "转存中"
    } else if (boundary > 0 && i > boundary) {
      // TMDB 说这一集还没播：不算待补，标「未播」
      status = "unaired"
      statusText = "未播"
    } else {
      // 检查 records 中是否有该集的记录
      const rec = recs.find((r) => Number(r.episode) === i)
      if (rec) {
        if (!rec.status || rec.status === "成功" || rec.status === "完成") {
          // 聚合模式下后端说没入库就是没入库，不让单条流水把状态改绿
          if (!aggregateMode) {
            status = "success"
            statusText = "已入库"
          }
        } else if (rec.status.includes("跳过") || rec.status.includes("未命中")) {
          status = "skipped"
          statusText = "已跳过"
        } else if (rec.status.includes("失败")) {
          status = "failed"
          statusText = "失败"
        }
      }
    }

    list.push({
      number: i,
      status,
      statusText,
    })
  }
  return list
})

const selectedEpData = computed(() => {
  if (!selectedEpisode.value) return null
  const ep = episodesList.value.find((e) => e.number === selectedEpisode.value)
  if (!ep) return null
  const matchedRecord = (props.group?.records || []).find((r) => Number(r.episode) === ep.number)
  return {
    ...ep,
    statusColor: ep.status === "success" ? "success" : ep.status === "transferring" ? "warning" : "default",
    matchedRecord,
  }
})

function onSelectEp(ep) {
  selectedEpisode.value = selectedEpisode.value === ep.number ? null : ep.number
}

// ---------------- 资源搜索 / 一键转存 ----------------
const PLUGIN_ID = "CloudSubscribeX"
const searchOpen = ref(false)
const searchKeyword = ref("")
const searching = ref(false)
const searchItems = ref([])
/** 后台启用的渠道（后端 enabled_sources 下发），用于「搜索源」切换 */
const searchSources = ref([])
/** all = 混排；其它值 = 只看该渠道 */
const activeSearchSource = ref("all")
const searchError = ref("")
const searchNote = ref("")
const transferringUrl = ref("")
const transferredUrl = ref("")

const isMovieGroup = computed(() => {
  const type = String(props.group?.media_type || props.group?.type || "")
  return type === "movie" || type === "电影"
})

const seasonNumber = computed(() => {
  const value = props.group?.season
  if (value === null || value === undefined || value === "") return 1
  const season = Number(value)
  return Number.isFinite(season) && season > 0 ? season : 1
})

/** 真该补的集号（已播但没入库），用于「按集搜」快捷按钮 */
const pendingEpisodeNumbers = computed(() =>
  episodesList.value.filter((ep) => ep.status === "missing").map((ep) => ep.number),
)

/** 每个渠道在当前结果里的条数，用于「搜索源」标签上的计数 */
const searchSourceCounts = computed(() => {
  const counts = {}
  for (const item of searchItems.value) {
    const key = String(item?.source || "").trim().toLowerCase()
    if (!key) continue
    counts[key] = (counts[key] || 0) + 1
  }
  return counts
})

/** 「全部 + 有结果的渠道」切换标签；只有一个渠道时不显示（没得切） */
const searchSourceTabs = computed(() => {
  const tabs = searchSources.value.map((src) => ({
    key: src.key,
    name: src.name || src.key,
    count: searchSourceCounts.value[src.key] || 0,
  }))
  if (tabs.length <= 1) return []
  return [
    { key: "all", name: "全部", count: searchItems.value.length },
    ...tabs,
  ]
})

const visibleSearchItems = computed(() => {
  if (activeSearchSource.value === "all") return searchItems.value
  return searchItems.value.filter(
    (item) => String(item?.source || "").trim().toLowerCase() === activeSearchSource.value,
  )
})

function groupTitle() {
  return String(props.group?.title || props.group?.name || "").trim()
}

function seasonTag() {
  if (isMovieGroup.value) return ""
  return ` S${String(seasonNumber.value).padStart(2, "0")}`
}

/** 默认关键词只用剧名：带季号会被各站拼成「剧名 S01」而搜不到东西 */
function defaultKeyword() {
  return groupTitle()
}

/** 换剧（或换季）时把搜索状态清干净，否则关键词还停在上一步那部剧 */
watch(
  () => `${props.group?.subscribe_id || 0}|${props.group?.tmdb_id || 0}|${props.group?.season ?? ""}|${props.group?.title || ""}`,
  () => {
    searchKeyword.value = searchOpen.value ? defaultKeyword() : ""
    searchItems.value = []
    searchSources.value = []
    activeSearchSource.value = "all"
    searchError.value = ""
    searchNote.value = ""
    transferringUrl.value = ""
    transferredUrl.value = ""
    selectedEpisode.value = null
    coverError.value = false
  },
)

function closeSearch() {
  searchOpen.value = false
}

function openSearch() {
  searchOpen.value = true
  if (!searchKeyword.value) searchKeyword.value = defaultKeyword()
  // 不自动搜：全渠道首搜约 1-2 分钟，交给用户自己按「搜索」或点集号
}

function searchEpisode(episode) {
  searchOpen.value = true
  searchKeyword.value = `${groupTitle()}${seasonTag()}E${String(episode).padStart(2, "0")}`.trim()
  runSearch()
}

async function runSearch() {
  if (!props.api || searching.value) return
  const keyword = String(searchKeyword.value || "").trim()
  if (!keyword) {
    searchError.value = "请输入关键词"
    return
  }
  searching.value = true
  searchError.value = ""
  searchNote.value = ""
  searchItems.value = []
  activeSearchSource.value = "all"
  try {
    const result = await props.api.post(`plugin/${PLUGIN_ID}/resource/search_resources`, {
      title: keyword,
      year: props.group?.year || "",
      media_type: isMovieGroup.value ? "movie" : "tv",
      season: isMovieGroup.value ? undefined : seasonNumber.value,
      tmdb_id: props.group?.tmdb_id || undefined,
      // 追更列表只看后台「搜索渠道优先级」里启用的渠道（资源页不受影响）
      enabled_only: true,
    })
    if (!result?.success) {
      searchError.value = result?.message || "搜索失败"
      return
    }
    const items = result?.data?.items || []
    const enabledMeta = result?.data?.enabled_sources
    searchSources.value = Array.isArray(enabledMeta)
      ? enabledMeta.map((src) => ({key: String(src?.key || ""), name: src?.name || src?.key || ""}))
        .filter((src) => src.key)
      : []
    searchItems.value = items.slice(0, 20)
    searchNote.value = items.length
      ? (result?.message || `共 ${items.length} 条候选`)
      : (result?.message || "没搜到资源，换个关键词试试")
  } catch (error) {
    searchError.value = `搜索出错：${error?.message || error}`
  } finally {
    searching.value = false
  }
}

async function transferItem(item) {
  if (!props.api) return
  const key = item?.url || item?.resource_ref || ""
  if (!key) return
  transferringUrl.value = key
  transferredUrl.value = ""
  searchError.value = ""
  try {
    const payload = {
      subscribe_id: Number(props.group?.subscribe_id || 0),
      media: {
        tmdb_id: props.group?.tmdb_id || undefined,
        media_type: isMovieGroup.value ? "movie" : "tv",
        title: groupTitle(),
        year: props.group?.year || undefined,
        seasons: isMovieGroup.value ? [] : [seasonNumber.value],
      },
      resources: [item],
    }
    const result = await props.api.post(`plugin/${PLUGIN_ID}/sync/manual`, payload)
    if (result?.success) {
      transferredUrl.value = key
      searchNote.value = result?.message || "已提交转存"
      emit("refresh")
    } else {
      searchError.value = result?.message || "转存失败"
    }
  } catch (error) {
    searchError.value = `转存出错：${error?.message || error}`
  } finally {
    transferringUrl.value = ""
  }
}

function formatSize(bytes) {
  let value = Number(bytes || 0)
  if (!value) return "-"
  for (const unit of ["B", "KB", "MB", "GB", "TB"]) {
    if (value < 1024 || unit === "TB") return `${value.toFixed(value >= 100 ? 0 : 1)} ${unit}`
    value /= 1024
  }
  return "-"
}

function formatSeasonEpisode(rec) {
  const parts = []
  if (rec.season !== undefined && rec.season !== null && rec.season !== "") {
    parts.push(`S${String(rec.season).padStart(2, "0")}`)
  }
  if (rec.episode !== undefined && rec.episode !== null && rec.episode !== "") {
    parts.push(`E${String(rec.episode).padStart(2, "0")}`)
  }
  return parts.join("")
}

function isRecordSuccess(rec) {
  const status = String(rec?.status || "").trim()
  return !status || status === "成功" || status === "完成"
}

function isRecordSkipped(rec) {
  const status = String(rec?.status || "")
  return status.includes("跳过") || status.includes("未命中")
}

function recordStatusColor(rec) {
  if (isRecordSuccess(rec)) return "success"
  if (isRecordSkipped(rec)) return "warning"
  return "error"
}

/**
 * 实心徽标的可读性类名。
 * Vuetify 实心 chip 的前景色取自主题的 on-* 色，暗色主题下 on-success 会被算成深色，
 * 于是绿底上出现黑字（对比度不足）。这里强制按底色反算：亮底用深字，暗底用白字。
 */
function solidChipClass(color) {
  const name = String(color || "success").trim().toLowerCase()
  return `solid-chip-${name}`
}

function recordStatusLabel(rec) {
  const status = String(rec?.status || "").trim()
  if (!status) return "已转存"
  if (status === "完成") return "成功"
  return status
}

function recordToneClass(rec) {
  if (isRecordSuccess(rec)) return "is-success"
  if (isRecordSkipped(rec)) return "is-warning"
  return "is-error"
}

function recordReasonTone(rec) {
  return recordToneClass(rec)
}

function recordReasonIcon(rec) {
  if (isRecordSuccess(rec)) return "mdi-check-circle-outline"
  if (isRecordSkipped(rec)) return "mdi-skip-next-circle-outline"
  return "mdi-alert-circle-outline"
}

function tgChannelText(channel) {
  const text = String(channel || "").trim()
  if (!text) return "TG频道"
  if (/^https?:\/\//i.test(text)) {
    // 频道配置允许直接填 t.me 链接，取不到可读频道名时统一显示「私有频道」，
    // 否则会把 "https:" 当成频道名显示出来。
    const invite = text.match(/t\.me\/\+([^/?#]+)/i)
    if (invite) return "TG频道 私有频道"
    const named = text.match(/t\.me\/([^/?#]+)/i)
    return named ? `TG频道 @${named[1]}` : "TG频道"
  }
  return `TG频道 ${text}`
}

function sourceLabel(rec) {
  const raw = String(rec?.source_type || rec?.source || "").trim()
  const lower = raw.toLowerCase()
  const via = String(rec?.discovered_via || "").trim()
  const viaLower = via.toLowerCase()
  // 频道推送的候选复用手动转存链路（source=manual），只有带 tg: 前缀才是 TG 来源；
  // 具体搜索渠道（pansou/seedhub…）即使带 discovered_via 也要显示自己的名字。
  const manualLike = ["", "manual", "手动添加", "手动资源"].includes(lower)
  if (viaLower.startsWith("tg:") && (manualLike || lower === "tg")) {
    const detail = via.replace(/^tg:/i, "")
    const messageId = (detail.match(/(\d+)\s*$/) || [])[1] || ""
    const channel = detail.replace(/\/+\d+\s*$/, "").replace(/^\/+/, "")
    const channelText = tgChannelText(channel)
    return messageId ? `${channelText} · 消息 ${messageId}` : channelText
  }
  if (lower === "tg") return "TG频道"
  if (["manual", "手动添加", "手动资源"].includes(lower)) return "手动添加"
  if (lower === "push") return "频道推送"
  if (lower === "subscribe" || lower.startsWith("subscribe|")) {
    const name = raw.split("|")[1]
    return name ? `订阅自动 · ${name}` : "订阅自动"
  }
  if (lower === "moviepilot") return "MoviePilot"
  if (lower === "hdhive") return "HDHive"
  return raw ? getSourceName(raw) : ""
}

function resourceTypeLabel(type) {
  const map = {
    "115": "115网盘",
    quark: "夸克网盘",
    alipan: "阿里云盘",
    tianyi: "天翼云盘",
    p123: "123云盘",
  }
  return map[String(type).toLowerCase()] || type
}

function recordKey(rec, index) {
  return `${rec.time || ""}:${rec.file_name || ""}:${index}`
}

function retryKey(rec) {
  return [rec.time, rec.share_url, rec.file_name].join("|")
}
</script>

<style scoped>
.task-progress-panel {
  background: rgba(var(--v-theme-primary), 0.04);
}

.task-current-file {
  max-width: 100%;
  min-width: 0;
}

.drawer-header {
  border-bottom: 1px solid rgba(var(--v-theme-on-surface), 0.08);
}

.drawer-cover {
  width: 72px;
  height: 108px;
  flex: 0 0 72px;
  border-radius: 6px;
  overflow: hidden;
}

.cover-img {
  width: 72px;
  height: 108px;
  border-radius: 6px;
}

.cover-img :deep(.v-img__img),
.cover-img :deep(img) {
  object-fit: cover !important;
  width: 100% !important;
  height: 100% !important;
}

.cover-placeholder {
  width: 72px;
  height: 108px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(var(--v-theme-surface-variant), 0.5);
}

.cover-badge-art {
  width: 72px;
  height: 108px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, rgba(var(--v-theme-primary), 0.25), rgba(var(--v-theme-surface-variant), 0.8));
  border: 1px solid rgba(var(--v-theme-primary), 0.2);
}

.art-letter {
  font-size: 1.6rem;
  font-weight: 700;
  color: rgb(var(--v-theme-primary));
  opacity: 0.9;
}

.drawer-title-box {
  min-width: 0;
}

.drawer-title {
  margin: 0;
  line-height: 1.3;
}

.drawer-body {
  overflow-y: auto;
  /* 长文件名/长链接一律不撑破弹窗宽度 */
  overflow-x: hidden;
}

/* 选集网格 */
.episode-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(56px, 1fr));
  gap: 8px;
  max-height: 240px;
  overflow-y: auto;
  padding: 4px;
}

.ep-box {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 48px;
  border-radius: 6px;
  border: 1px solid rgba(var(--v-theme-on-surface), 0.12);
  background: rgba(var(--v-theme-surface-variant), 0.2);
  cursor: pointer;
  transition: all 0.2s ease;
}

.ep-box:hover {
  border-color: rgb(var(--v-theme-primary));
  transform: translateY(-1px);
}

.ep-box--selected {
  outline: 2px solid rgb(var(--v-theme-primary));
}

.ep-box--success {
  background: rgba(var(--v-theme-success), 0.15);
  border-color: rgba(var(--v-theme-success), 0.4);
  color: rgb(var(--v-theme-success));
}

.ep-box--transferring {
  background: rgba(var(--v-theme-warning), 0.15);
  border-color: rgba(var(--v-theme-warning), 0.4);
  color: rgb(var(--v-theme-warning));
  animation: pulse-border 1.5s infinite;
}

.ep-box--skipped {
  background: rgba(var(--v-theme-warning), 0.08);
  border-color: rgba(var(--v-theme-warning), 0.3);
  color: rgba(var(--v-theme-warning), 0.85);
}

.ep-box--failed {
  background: rgba(var(--v-theme-error), 0.12);
  border-color: rgba(var(--v-theme-error), 0.4);
  color: rgb(var(--v-theme-error));
}

.ep-num {
  font-size: 0.95rem;
  font-weight: 700;
  line-height: 1.1;
}

.ep-badge {
  font-size: 0.65rem;
  opacity: 0.8;
  margin-top: 2px;
}

.legend-dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  margin-right: 4px;
}

/* 未播：虚线圆点 + 半透明，与「待补齐」实心区分 */
.ep-dot-unaired {
  background: transparent;
  border: 1px dashed rgba(var(--v-theme-on-surface), 0.45);
}

.resource-search-panel {
  border-color: rgba(148, 163, 184, 0.35) !important;
}

.search-keyword {
  flex: 1 1 180px;
  min-width: 0;
}

.search-results {
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-height: 260px;
  overflow-y: auto;
}

.search-result-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  border-radius: 8px;
  background: rgba(148, 163, 184, 0.08);
}

.result-main {
  flex: 1 1 auto;
  min-width: 0;
}

.result-title {
  font-size: 12px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.search-source-tabs .source-count {
  opacity: 0.6;
  font-size: 10px;
}

.result-meta {
  display: flex;
  gap: 8px;
  font-size: 11px;
  opacity: 0.72;
  flex-wrap: wrap;
}

.meta-pan {
  font-weight: 600;
}

/* 渠道标签：与后台启用的渠道一一对应，一眼看出这条来自哪个站 */
.meta-source {
  font-weight: 600;
  color: rgb(var(--v-theme-primary));
}

.meta-cost {
  font-weight: 600;
  color: rgb(var(--v-theme-success));
}

.meta-cost--paid {
  color: rgb(var(--v-theme-warning));
}

@media (max-width: 640px) {
  .search-results {
    max-height: 200px;
  }

  .result-title {
    white-space: normal;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
  }
}

/* 未播的集：虚线边框 + 降透明度，一眼看出不是「该补没补」 */
.ep-box--unaired {
  border-style: dashed;
  opacity: 0.55;
}

.record-item {
  border-color: rgba(var(--v-theme-on-surface), 0.12);
  border-left-width: 3px;
  background: rgba(var(--v-theme-surface-variant), 0.25);
  border-radius: 8px;
}

.record-item.is-success {
  border-left-color: rgba(var(--v-theme-success), 0.85);
}

.record-item.is-warning {
  border-left-color: rgba(var(--v-theme-warning), 0.85);
}

.record-item.is-error {
  border-left-color: rgba(var(--v-theme-error), 0.85);
}

.record-layout {
  display: flex;
  align-items: flex-start;
  gap: 8px;
}

.record-main {
  flex: 1 1 auto;
}

.record-head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 4px 8px;
  margin-bottom: 4px;
}

.record-status-chip {
  letter-spacing: 0;
}

/* 实心徽标前景色：暗色主题下 Vuetify 会把 on-success 算成深色，绿底黑字看不清。
   这里按底色反算——深底白字、亮底（琥珀）深字。 */
/* 底色也自己定：主题里的 success 是亮绿（#4CAF50），白字对比只有 2.8:1，
   黑字虽够但用户反馈「暗色主题下看不清」，所以直接用深一档的实心色 + 白字，
   与海报角标保持同一套色板，不受主题明暗影响。 */
.solid-chip-success {
  background: #059669 !important;
  color: #ffffff !important;
}

.solid-chip-error {
  background: #dc2626 !important;
  color: #ffffff !important;
}

.solid-chip-info {
  background: #2563eb !important;
  color: #ffffff !important;
}

.solid-chip-primary {
  background: #7c3aed !important;
  color: #ffffff !important;
}

.solid-chip-secondary {
  background: #475569 !important;
  color: #ffffff !important;
}

.solid-chip-active {
  background: #0891b2 !important;
  color: #ffffff !important;
}

.solid-chip-warning {
  background: #f59e0b !important;
  color: #1f2937 !important;
}

/* 选中单集的快捷卡片：文件名单行省略，按钮不换行，整块不横向溢出 */
.selected-ep-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  min-width: 0;
}

.selected-ep-main {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: 1 1 200px;
  min-width: 0;
}

/* 状态徽标不许被文件名挤扁（挤扁会让文字换行溢出卡片） */
.selected-ep-main > .v-chip {
  flex: 0 0 auto;
  white-space: nowrap;
}

.selected-ep-actions {
  display: flex;
  align-items: center;
  gap: 4px;
  flex: 0 0 auto;
}

.ep-file-name {
  display: block;
  min-width: 0;
  flex: 1 1 auto;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.record-ep {
  color: rgb(var(--v-theme-primary));
  font-size: 0.75rem;
  font-weight: 700;
}

.record-time,
.record-size {
  color: rgba(var(--v-theme-on-surface), 0.5);
  font-size: 0.7rem;
  font-variant-numeric: tabular-nums;
}

.record-size {
  margin-left: auto;
}

.record-filename {
  display: -webkit-box;
  overflow: hidden;
  color: rgba(var(--v-theme-on-surface), 0.86);
  font-weight: 500;
  line-height: 1.35;
  word-break: break-all;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}

.record-meta {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 3px 10px;
  margin-top: 5px;
  color: rgba(var(--v-theme-on-surface), 0.55);
  font-size: 0.7rem;
}

.record-meta-item {
  display: inline-flex;
  align-items: center;
  min-width: 0;
}

.record-meta-link {
  color: rgb(var(--v-theme-primary));
  font-size: 0.7rem;
  text-decoration: none;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.record-meta-link:hover {
  text-decoration: underline;
}

.record-reason {
  display: flex;
  align-items: flex-start;
  gap: 2px;
  margin-top: 6px;
  padding: 5px 7px;
  border-radius: 6px;
  font-size: 0.72rem;
  line-height: 1.4;
  word-break: break-word;
}

.record-reason.is-success {
  color: rgb(var(--v-theme-success));
  background: rgba(var(--v-theme-success), 0.09);
}

.record-reason.is-warning {
  color: rgb(var(--v-theme-warning));
  background: rgba(var(--v-theme-warning), 0.09);
}

.record-reason.is-error {
  color: rgb(var(--v-theme-error));
  background: rgba(var(--v-theme-error), 0.09);
}

.record-actions {
  flex: 0 0 auto;
}

.max-w-300 {
  max-width: 300px;
}

.min-w-0 {
  min-width: 0;
}

@keyframes pulse-border {
  0%, 100% { border-color: rgba(var(--v-theme-warning), 0.4); }
  50% { border-color: rgba(var(--v-theme-warning), 0.9); }
}
</style>
