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
      <span class="text-caption text-medium-emphasis mt-2">正在从 MoviePilot 同步追更进度大屏...</span>
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
          </div>

          <div class="card-body">
            <h3 class="card-title" :title="group.title || group.name">
              {{ group.title || group.name }}
            </h3>
            <p class="card-sub">{{ getSubline(group) }}</p>

            <div class="card-progress">
              <div class="progress-head">
                <span class="progress-count">{{ getProgressCount(group) }}</span>
                <span :class="['progress-status', statusTone(group)]">{{ getStatusLabel(group) }}</span>
              </div>
              <div class="progress-track">
                <div class="progress-fill" :class="statusTone(group)" :style="{ width: `${calcLibraryPct(group)}%` }" />
              </div>
            </div>

            <span class="card-time" :title="getFullTime(group)">{{ getRelativeTime(group) }}</span>
          </div>
        </div>
      </v-card>
    </div>

    <!-- 单剧集明细抽屉 / 选集矩阵 -->
    <MediaDrawer
      v-model="drawerVisible"
      :group="selectedGroup"
      :retrying-key="retryingKey"
      @retry="(rec) => $emit('retry', rec)"
      @retry-failed="(grp) => $emit('retry-failed', grp)"
      @delete="(rec) => $emit('delete', rec)" />
  </div>
</template>

<script setup>
import { ref, computed } from "vue"
import MediaDrawer from "./MediaDrawer.vue"

const props = defineProps({
  items: { type: Array, default: () => [] },
  isSubscribeMode: { type: Boolean, default: false },
  loading: { type: Boolean, default: false },
  retryingKey: { type: String, default: "" },
})

defineEmits(["refresh", "retry", "retry-failed", "delete"])

const filterStatus = ref("all")
const searchKeyword = ref("")
const drawerVisible = ref(false)
const selectedGroup = ref(null)

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

function rawLastTime(group) {
  if (group.last_updated_at) return String(group.last_updated_at)
  const recs = group.records || []
  return recs[0]?.time ? String(recs[0].time) : ""
}

function parseTime(value) {
  if (!value) return null
  const normalized = value.replace(" ", "T")
  const date = new Date(normalized)
  return Number.isNaN(date.getTime()) ? null : date
}

/** 相对时间：今天 11:15 / 昨天 / 3 天前 / 2026-10-28 */
function getRelativeTime(group) {
  const raw = rawLastTime(group)
  if (!raw) return ""
  const date = parseTime(raw)
  if (!date) return ""
  const now = new Date()
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  const diffDays = Math.floor((startOfToday - new Date(date.getFullYear(), date.getMonth(), date.getDate())) / 86400000)
  if (diffDays === 0) {
    const hh = String(date.getHours()).padStart(2, "0")
    const mm = String(date.getMinutes()).padStart(2, "0")
    return `今天 ${hh}:${mm}`
  }
  if (diffDays === 1) return "昨天"
  if (diffDays > 1 && diffDays < 30) return `${diffDays} 天前`
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`
}

function getFullTime(group) {
  return rawLastTime(group)
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
  background: rgb(var(--v-theme-surface));
  border: 1px solid rgba(var(--v-theme-on-surface), 0.07);
  transition:
    border-color 0.2s ease,
    transform 0.2s ease;
  cursor: pointer;
}

.media-card:hover {
  border-color: rgba(var(--v-theme-on-surface), 0.18);
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
  margin-top: auto;
  padding-top: 12px;
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

.card-time {
  margin-top: 8px;
  color: rgba(var(--v-theme-on-surface), 0.4);
  font-size: 0.6875rem;
  line-height: 1.2;
}

@media (max-width: 600px) {
  .media-grid {
    grid-template-columns: 1fr;
    gap: 12px;
  }

  .hub-search-input {
    max-width: 100%;
    flex: 1 1 auto;
  }

  .card-inner {
    gap: 12px;
    padding: 12px;
  }

  .card-poster,
  .poster-img,
  .poster-badge-art {
    flex-basis: 92px;
    width: 92px;
    height: 132px;
  }
}
</style>
