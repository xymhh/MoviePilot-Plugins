<template>
  <div class="media-hub-root">
    <!-- 顶部工具栏：快速搜索 + 订阅状态筛选 -->
    <div class="hub-toolbar d-flex flex-wrap align-center justify-space-between ga-2 mb-4">
      <div class="hub-filter-chips d-flex align-center ga-1 flex-wrap">
        <v-chip-group v-model="filterStatus" mandatory selected-class="text-primary font-weight-bold">
          <v-chip value="all" size="small" variant="tonal">全部订阅 ({{ filteredGroups.length }})</v-chip>
          <v-chip value="missing" size="small" variant="tonal" color="warning">
            <v-icon icon="mdi-alert-circle-outline" size="14" class="mr-1" />
            有缺集待补 ({{ missingCountTotal }})
          </v-chip>
          <v-chip value="complete" size="small" variant="tonal" color="success">
            <v-icon icon="mdi-check-circle-outline" size="14" class="mr-1" />
            已全部入库
          </v-chip>
        </v-chip-group>
      </div>

      <div class="hub-search-box d-flex align-center ga-2">
        <v-text-field
          v-model="searchKeyword"
          density="compact"
          variant="outlined"
          placeholder="按片名快速搜索..."
          prepend-inner-icon="mdi-magnify"
          clearable
          hide-details
          class="hub-search-input" />
        <v-btn
          icon="mdi-refresh"
          variant="tonal"
          size="small"
          color="primary"
          title="刷新订阅进度"
          :loading="loading"
          @click="$emit('refresh')" />
      </div>
    </div>

    <!-- 加载中状态 -->
    <div v-if="loading && (!items || !items.length)" class="d-flex flex-column align-center justify-center py-12">
      <v-progress-circular indeterminate color="primary" size="40" />
      <span class="text-caption text-medium-emphasis mt-2">正在从 MoviePilot 同步追更列表...</span>
    </div>

    <!-- 空数据提示 -->
    <div v-else-if="!displayedGroups.length" class="empty-state text-center py-12">
      <v-icon icon="mdi-filmstrip-off" size="56" color="medium-emphasis" class="mb-2" />
      <div class="text-body-1 font-weight-medium">暂无匹配的追更订阅</div>
      <div class="text-caption text-medium-emphasis mt-1">请先在 MoviePilot 添加订阅，或通过右上角「手动添加」</div>
    </div>

    <!-- 订阅进度卡片网格：内容优先，海报主导，信息四层递减 -->
    <div v-else class="media-grid">
      <v-card
        v-for="group in displayedGroups"
        :key="group.subscribe_id || group.group_key || group.title"
        class="media-card"
        variant="flat"
        @click="openGroupDrawer(group)">
        <div class="card-inner">
          <div class="card-poster">
            <img
              v-if="getCoverUrl(group) && !imgErrors[group.subscribe_id || group.title]"
              :src="getCoverUrl(group)"
              class="poster-img"
              alt=""
              loading="lazy"
              @error="onImgError(group)" />
            <div v-else class="poster-badge-art">
              <span class="art-letter">{{ getInitial(group.title || group.name) }}</span>
            </div>
            <!-- 海报右下角状态角标 -->
            <span
              v-if="cardStatusLabel(group)"
              :class="['poster-status-badge', cardStatusTone(group)]"
              :title="cardStatusTitle(group)">
              {{ cardStatusLabel(group) }}
            </span>
          </div>

          <div class="card-body">
            <h3 class="card-title" :title="group.title || group.name">
              {{ group.title || group.name }}
            </h3>
            <p class="card-sub">{{ getSubline(group) }}</p>

            <div class="card-footer">
              <div v-if="!isMovie(group)" class="card-metrics">
                <div class="metric">
                  <span class="metric-value">{{ metricNumber(group.total_episodes) }}</span>
                  <span class="metric-label">全集</span>
                </div>
                <div class="metric">
                  <span class="metric-value is-aired">{{ metricNumber(group.aired_episodes) }}</span>
                  <span class="metric-label">已播</span>
                </div>
                <div class="metric">
                  <span class="metric-value is-success">{{ metricNumber(libraryCount(group)) }}</span>
                  <span class="metric-label">入库</span>
                </div>
              </div>

              <div class="card-progress">
                <div class="progress-head">
                  <span class="progress-count">{{ getProgressCount(group) }}</span>
                  <span :class="['progress-status', statusTone(group)]">{{ getStatusLabel(group) }}</span>
                </div>
                <div class="progress-track">
                  <div class="progress-fill" :class="statusTone(group)" :style="{ width: `${calcLibraryPct(group)}%` }" />
                </div>
              </div>

              <div class="card-time-row">
                <span class="card-time-label">最新一集</span>
                <span class="card-time" :title="getLatestAirTitle(group)">{{ getLatestAirText(group) }}</span>
              </div>
            </div>
          </div>
        </div>
      </v-card>
    </div>

    <!-- 单剧集明细抽屉 / 选集矩阵 -->
    <MediaDrawer
      v-model="drawerVisible"
      :group="selectedGroup"
      :task="selectedTask"
      :retrying-key="retryingKey"
      @retry="(rec) => $emit('retry', rec)"
      @retry-failed="(grp) => $emit('retry-failed', grp)"
      @delete="(rec) => $emit('delete', rec)" />
  </div>
</template>

<script setup>
import { ref, computed, watch } from "vue"
import MediaDrawer from "./MediaDrawer.vue"
import {
  isTaskActive,
  taskPhaseText,
  taskStatusColor,
  taskStatusLabel,
} from "../../utils/taskUtils.js"

const props = defineProps({
  items: { type: Array, default: () => [] },
  tasks: { type: Array, default: () => [] },
  isSubscribeMode: { type: Boolean, default: false },
  loading: { type: Boolean, default: false },
  retryingKey: { type: String, default: "" },
})

defineEmits(["refresh", "retry", "retry-failed", "delete"])

const filterStatus = ref("all")
const searchKeyword = ref("")
const drawerVisible = ref(false)
const selectedGroup = ref(null)

const activeTasks = computed(() => (props.tasks || []).filter(isTaskActive))

const taskIndex = computed(() => {
  const byId = new Map()
  const byTitle = new Map()
  for (const task of activeTasks.value) {
    if (task.subscribe_id !== null && task.subscribe_id !== undefined) {
      byId.set(String(task.subscribe_id), task)
    }
    const title = normalizeTitle(task.title)
    if (title) {
      byTitle.set(`${title}|${Number(task.season || 0)}`, task)
    }
  }
  return { byId, byTitle }
})

const selectedTask = computed(() => taskForGroup(selectedGroup.value))

watch(
  () => props.items,
  (items) => {
    if (!selectedGroup.value) return
    const current = selectedGroup.value
    const next = (items || []).find(
      (item) =>
        (current.subscribe_id !== undefined &&
          current.subscribe_id !== null &&
          item.subscribe_id === current.subscribe_id) ||
        (normalizeTitle(item.title || item.name) === normalizeTitle(current.title || current.name) &&
          Number(item.season || 0) === Number(current.season || 0)),
    )
    if (next) selectedGroup.value = next
  },
)

function normalizeTitle(value) {
  return String(value || "").trim().toLowerCase().replace(/\s+/g, "")
}

function taskForGroup(group) {
  if (!group) return null
  const ids = [group.subscribe_id, ...(group.duplicate_subscribe_ids || [])]
    .filter((id) => id !== null && id !== undefined && id !== "")
    .map(String)
  for (const id of ids) {
    const task = taskIndex.value.byId.get(id)
    if (task) return task
  }
  const title = normalizeTitle(group.title || group.name)
  if (!title) return null
  return taskIndex.value.byTitle.get(`${title}|${Number(group.season || 0)}`) || null
}

function cardStatusLabel(group) {
  const task = taskForGroup(group)
  if (task) return taskStatusLabel(task, { withProgress: true })
  if (props.isSubscribeMode) return "监控中"
  return getStatusLabel(group)
}

function cardStatusTone(group) {
  const task = taskForGroup(group)
  if (task) return `is-${taskStatusColor(task)}`
  if (props.isSubscribeMode) return "is-active"
  return statusTone(group)
}

function cardStatusTitle(group) {
  const task = taskForGroup(group)
  if (task) return taskPhaseText(task) || taskStatusLabel(task)
  return props.isSubscribeMode ? "订阅监控中，点击查看详情" : ""
}

const filteredGroups = computed(() => props.items || [])

const missingCountTotal = computed(() => (props.items || []).filter((g) => hasMissing(g)).length)

const displayedGroups = computed(() => {
  let list = filteredGroups.value
  if (filterStatus.value === "complete") {
    list = list.filter((g) => !hasMissing(g))
  } else if (filterStatus.value === "missing") {
    list = list.filter((g) => hasMissing(g))
  }
  const kw = searchKeyword.value.trim().toLowerCase()
  if (kw) {
    list = list.filter((g) => {
      const title = String(g.title || g.name || "").toLowerCase()
      const year = String(g.year || "")
      return title.includes(kw) || year.includes(kw)
    })
  }
  return list
})

function openGroupDrawer(group) {
  selectedGroup.value = group
  drawerVisible.value = true
}

function isMovie(group) {
  if (group.media_type) return group.media_type === "movie"
  return !group.season && !Number(group.total_episodes || 0)
}

function movieLanded(group) {
  if (Number(group.library_count || 0) > 0) return true
  return (group.records || []).some((r) => !r.status || r.status === "成功" || r.status === "完成")
}

function hasMissing(group) {
  if (isMovie(group)) return !movieLanded(group)
  if (group.missing_count !== undefined) return Number(group.missing_count) > 0
  const recs = group.records || []
  return recs.some((r) => r.status && r.status !== "成功" && r.status !== "完成")
}

function libraryCount(group) {
  if (group.library_count !== undefined) return Number(group.library_count)
  return (group.library_episodes || []).length
}

/** 副信息：年份 · 季/电影，一行说清「这是什么」 */
function getSubline(group) {
  const parts = []
  if (group.year) parts.push(String(group.year))
  if (isMovie(group)) {
    parts.push("电影")
  } else if (group.season) {
    parts.push(`第 ${group.season} 季`)
  } else {
    parts.push("剧集")
  }
  return parts.join(" · ")
}

/** 进度数字：电影不显示（没有集的概念） */
function getProgressCount(group) {
  if (isMovie(group)) return ""
  const total = Number(group.total_episodes || 0)
  if (total > 0) return `${libraryCount(group)} / ${total} 集`
  const recs = group.records || []
  const success = recs.filter((r) => !r.status || r.status === "成功" || r.status === "完成").length
  return recs.length ? `${success} / ${recs.length} 集` : ""
}

/** 状态：一句话，不重复「未入库」 */
function getStatusLabel(group) {
  if (isMovie(group)) return movieLanded(group) ? "已入库" : "待入库"
  if (Number(group.missing_count || 0) > 0) {
    const ranges = formatMissingRange(group.missing_episodes)
    return ranges ? `缺 ${ranges}` : "待补齐"
  }
  if (group.total_episodes !== undefined) return "已齐"
  return hasMissing(group) ? "有待补" : "已全集"
}

function statusTone(group) {
  if (isMovie(group)) return movieLanded(group) ? "is-success" : "is-warning"
  return Number(group.missing_count || 0) > 0 || hasMissing(group) ? "is-warning" : "is-success"
}

function calcLibraryPct(group) {
  if (isMovie(group)) return movieLanded(group) ? 100 : 0
  const total = Number(group.total_episodes || 0)
  if (total > 0) return Math.min(100, Math.round((libraryCount(group) / total) * 100))
  const recs = group.records || []
  if (!recs.length) return 0
  const success = recs.filter((r) => !r.status || r.status === "成功" || r.status === "完成").length
  return Math.round((success / recs.length) * 100)
}

function formatMissingRange(episodes) {
  if (!episodes || !episodes.length) return ""
  const nums = [...new Set(episodes.map(Number).filter((n) => Number.isFinite(n) && n > 0))].sort((a, b) => a - b)
  if (!nums.length) return ""

  const ranges = []
  let start = nums[0]
  let prev = nums[0]
  for (let i = 1; i < nums.length; i += 1) {
    const n = nums[i]
    if (n === prev + 1) {
      prev = n
      continue
    }
    ranges.push(start === prev ? `E${start}` : `E${start}-E${prev}`)
    start = n
    prev = n
  }
  ranges.push(start === prev ? `E${start}` : `E${start}-E${prev}`)

  if (ranges.length <= 3) return ranges.join(",")
  return `${ranges.slice(0, 3).join(",")} 等 ${ranges.length} 段`
}

function metricNumber(value) {
  if (value === null || value === undefined || value === "") return "—"
  const number = Number(value)
  return Number.isFinite(number) && number >= 0 ? String(number) : "—"
}

function formatAirDate(value) {
  const raw = String(value || "").trim()
  if (!raw) return ""
  const match = raw.match(/^(\d{4})-(\d{2})-(\d{2})/)
  return match ? `${match[1]}-${match[2]}-${match[3]}` : raw
}

function getLatestAirText(group) {
  const airDate = formatAirDate(group.latest_air_date)
  if (!airDate) return "暂无开播信息"
  const episode = Number(group.latest_air_episode || 0)
  const season = Number(group.season || 0)
  if (episode <= 0) return `${airDate} 开播`
  const prefix = season > 0
    ? `S${String(season).padStart(2, "0")}E${String(episode).padStart(2, "0")}`
    : `E${episode}`
  return `${prefix} · ${airDate} 开播`
}

function getLatestAirTitle(group) {
  const airDate = formatAirDate(group.latest_air_date)
  return airDate ? `最新一集开播日期：${airDate}` : "暂无开播信息"
}

const imgErrors = ref({})

function onImgError(group) {
  const key = group.subscribe_id || group.title || group.name
  if (key) imgErrors.value[key] = true
}

function getCoverUrl(group) {
  if (!group) return ""
  const raw =
    group.poster_url ||
    group.poster ||
    group.image ||
    group.poster_path ||
    group.backdrop_path ||
    (group.records &&
      group.records[0] &&
      (group.records[0].poster_url ||
        group.records[0].image ||
        group.records[0].poster ||
        group.records[0].poster_path)) ||
    ""
  if (!raw) return ""
  const str = String(raw).trim()
  if (str.startsWith("data:") || str.startsWith("blob:")) return str
  if (str.startsWith("/api/v1/system/cache/image") || str.startsWith("api/v1/plugin/")) return str
  let target = str
  if (target.startsWith("/") && !target.startsWith("//")) {
    target = `https://image.tmdb.org/t/p/w500${target}`
  }
  return `/api/v1/system/cache/image?url=${encodeURIComponent(target)}`
}

function getInitial(title) {
  const clean = String(title || "")
    .trim()
    .replace(/^[^\w\u4e00-\u9fa5]+/, "")
  return clean ? clean.slice(0, 1) : "片"
}
</script>

<style scoped>
.media-hub-root {
  display: flex;
  flex-direction: column;
  min-height: 100%;
}

.hub-search-box {
  flex: 0 0 auto;
}

.hub-search-input {
  width: 220px;
}

/* 内容优先：更大的卡片、更松的间距 */
.media-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(360px, 1fr));
  gap: 20px;
  overflow-y: auto;
}

.media-card {
  border-radius: 12px;
  background: rgba(var(--v-theme-surface), 0.6) !important;
  border: 1px solid var(--cs-border-subtle, rgba(var(--v-border-color), 0.12));
  transition:
    border-color 0.2s ease,
    transform 0.2s ease,
    background-color 0.2s ease;
  cursor: pointer;
}

.media-card:hover {
  border-color: var(--cs-border-active, rgba(var(--v-theme-primary), 0.4));
  background: rgba(var(--v-theme-surface-variant), 0.25) !important;
  transform: translateY(-1px);
}

.card-inner {
  display: flex;
  gap: 16px;
  padding: 16px;
  height: 100%;
}

/* 海报主导：80×116 → 112×162 */
.card-poster {
  position: relative;
  flex: 0 0 112px;
  width: 112px;
  height: 162px;
  border-radius: 8px;
  overflow: hidden;
  background: rgba(var(--v-theme-surface-variant), 0.3);
}

.poster-status-badge {
  position: absolute;
  right: 4px;
  bottom: 4px;
  padding: 1px 5px;
  font-size: 10px;
  font-weight: 600;
  border-radius: 4px;
  backdrop-filter: blur(8px);
  background: rgba(0, 0, 0, 0.65);
  color: #fff;
  letter-spacing: -0.2px;
}

.poster-status-badge.is-success {
  color: #10b981;
  border: 1px solid rgba(16, 185, 129, 0.4);
}

.poster-status-badge.is-warning {
  color: #f59e0b;
  border: 1px solid rgba(245, 158, 11, 0.4);
}

.poster-status-badge.is-info {
  color: #38bdf8;
  border: 1px solid rgba(56, 189, 248, 0.4);
}

.poster-status-badge.is-primary {
  color: #a78bfa;
  border: 1px solid rgba(167, 139, 250, 0.4);
}

.poster-status-badge.is-error {
  color: #f87171;
  border: 1px solid rgba(248, 113, 113, 0.4);
}

.poster-status-badge.is-secondary {
  color: #cbd5e1;
  border: 1px solid rgba(203, 213, 225, 0.35);
}

.poster-status-badge.is-active {
  color: #22d3ee;
  border: 1px solid rgba(34, 211, 238, 0.45);
}

.poster-img {
  display: block;
  width: 112px;
  height: 162px;
  object-fit: cover;
}

.poster-badge-art {
  display: flex;
  width: 112px;
  height: 162px;
  align-items: center;
  justify-content: center;
  background: rgba(var(--v-theme-surface-variant), 0.35);
}

.art-letter {
  font-size: 2rem;
  font-weight: 700;
  color: rgba(var(--v-theme-on-surface), 0.35);
}

.card-body {
  display: flex;
  flex: 1 1 auto;
  flex-direction: column;
  min-width: 0;
  padding: 2px 0;
}

.card-title {
  margin: 0;
  color: rgb(var(--v-theme-on-surface));
  font-size: 1rem;
  font-weight: 600;
  line-height: 1.35;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.card-sub {
  margin: 4px 0 0;
  color: rgba(var(--v-theme-on-surface), 0.55);
  font-size: 0.75rem;
  line-height: 1.3;
}

.card-progress {
  margin-top: 10px;
}

.progress-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 6px;
}

.progress-count {
  color: rgb(var(--v-theme-on-surface));
  font-size: 0.8125rem;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}

.progress-status {
  font-size: 0.75rem;
  font-weight: 500;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.progress-status.is-success {
  color: rgb(var(--v-theme-success));
}

.progress-status.is-warning {
  color: rgb(var(--v-theme-warning));
}

.progress-status.is-info {
  color: rgb(var(--v-theme-info));
}

.progress-status.is-primary {
  color: rgb(var(--v-theme-primary));
}

.progress-status.is-error {
  color: rgb(var(--v-theme-error));
}

.progress-status.is-active {
  color: rgb(var(--v-theme-info));
}

.progress-status.is-secondary {
  color: rgba(var(--v-theme-on-surface), 0.6);
}

.progress-track {
  height: 3px;
  border-radius: 999px;
  background: rgba(var(--v-theme-on-surface), 0.1);
  overflow: hidden;
}

.progress-fill {
  height: 100%;
  border-radius: 999px;
  transition: width 0.3s ease;
}

.progress-fill.is-success {
  background: rgb(var(--v-theme-success));
}

.progress-fill.is-warning {
  background: rgb(var(--v-theme-warning));
}

.progress-fill.is-info,
.progress-fill.is-active {
  background: rgb(var(--v-theme-info));
}

.progress-fill.is-primary {
  background: rgb(var(--v-theme-primary));
}

.progress-fill.is-error {
  background: rgb(var(--v-theme-error));
}

.progress-fill.is-secondary {
  background: rgba(var(--v-theme-on-surface), 0.4);
}

.card-footer {
  margin-top: auto;
}

.card-metrics {
  display: flex;
  gap: 6px;
}

.metric {
  flex: 1 1 0;
  min-width: 0;
  padding: 5px 4px 4px;
  border-radius: 7px;
  background: rgba(var(--v-theme-on-surface), 0.045);
  text-align: center;
}

.metric-value {
  display: block;
  color: rgb(var(--v-theme-on-surface));
  font-size: 0.95rem;
  font-weight: 700;
  line-height: 1.1;
  font-variant-numeric: tabular-nums;
}

.metric-value.is-aired {
  color: rgb(var(--v-theme-info));
}

.metric-value.is-success {
  color: rgb(var(--v-theme-success));
}

.metric-label {
  display: block;
  margin-top: 2px;
  color: rgba(var(--v-theme-on-surface), 0.5);
  font-size: 0.625rem;
  line-height: 1;
}

.card-time-row {
  display: flex;
  align-items: baseline;
  gap: 6px;
  margin-top: 8px;
  min-width: 0;
}

.card-time-label {
  flex: 0 0 auto;
  color: rgba(var(--v-theme-on-surface), 0.42);
  font-size: 0.6875rem;
  line-height: 1.2;
}

.card-time {
  min-width: 0;
  color: rgba(var(--v-theme-on-surface), 0.55);
  font-size: 0.6875rem;
  line-height: 1.2;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

@media (max-width: 640px) {
  .hub-toolbar {
    flex-direction: column;
    align-items: stretch !important;
    gap: 8px;
  }

  .hub-filter-chips {
    overflow-x: auto;
    scrollbar-width: none;
    -webkit-overflow-scrolling: touch;
    flex-wrap: nowrap !important;
  }

  .hub-filter-chips::-webkit-scrollbar {
    display: none;
  }

  .hub-search-box {
    width: 100%;
  }

  .hub-search-input {
    width: 100% !important;
    max-width: 100%;
    flex: 1 1 auto;
  }

  .media-grid {
    grid-template-columns: 1fr;
    gap: 10px;
  }

  .card-inner {
    gap: 12px;
    padding: 10px;
  }

  .card-poster,
  .poster-img,
  .poster-badge-art {
    flex-basis: 80px;
    width: 80px;
    height: 116px;
  }

  .card-title {
    font-size: 0.875rem !important;
    line-height: 1.35 !important;
  }
}
</style>
