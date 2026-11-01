<template>
  <div class="media-hub-root">
    <!-- 顶部工具栏：快速搜索 + 订阅状态筛选 -->
    <div class="hub-toolbar d-flex flex-wrap align-center justify-space-between ga-2 mb-3">
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

    <!-- 订阅进度卡片网格 -->
    <div v-else class="media-grid">
      <v-card
        v-for="group in displayedGroups"
        :key="group.subscribe_id || group.group_key || group.title"
        class="media-card"
        variant="outlined"
        hover
        @click="openGroupDrawer(group)">
        <div class="card-inner">
          <!-- 海报区 -->
          <div class="card-poster">
            <img
              v-if="getCoverUrl(group) && !imgErrors[group.subscribe_id || group.title]"
              :src="getCoverUrl(group)"
              class="poster-img rounded"
              alt=""
              loading="lazy"
              @error="onImgError(group)" />
            <div v-else class="poster-badge-art rounded">
              <span class="art-letter">{{ getInitial(group.title || group.name) }}</span>
            </div>
            <v-chip size="x-small" color="primary" variant="flat" class="poster-type-badge">
              {{ group.type || (group.season ? `S${String(group.season).padStart(2, '0')}` : "订阅") }}
            </v-chip>
          </div>

          <!-- 内容区 -->
          <div class="card-content">
            <div class="card-header d-flex align-center justify-space-between min-w-0">
              <div class="d-flex align-baseline ga-1 min-w-0">
                <h3 class="card-title text-truncate" :title="group.title || group.name">
                  {{ group.title || group.name }}
                </h3>
                <span v-if="group.year" class="card-year text-caption text-medium-emphasis">
                  ({{ group.year }})
                </span>
              </div>
              <v-chip v-if="group.season" size="x-small" color="primary" variant="tonal" class="font-weight-bold ml-1 flex-shrink-0">
                第 {{ group.season }} 季
              </v-chip>
            </div>

            <!-- 进度统计与缺失提示 -->
            <div class="progress-section my-2">
              <div class="d-flex align-center justify-space-between text-caption mb-1">
                <span class="font-weight-medium">
                  {{ getStatusText(group) }}
                </span>
                <span class="text-caption font-weight-bold" :class="hasMissing(group) ? 'text-warning' : 'text-success'">
                  {{ getMissingLabel(group) }}
                </span>
              </div>
              <!-- 视觉进度条 -->
              <div class="custom-progress-bar rounded">
                <div
                  class="progress-fill progress-fill-success"
                  :style="{ width: `${calcLibraryPct(group)}%` }"
                  :title="`已入库: ${group.library_count || (group.library_episodes || []).length} 集`" />
                <div
                  v-if="calcTransferringPct(group) > 0"
                  class="progress-fill progress-fill-warning"
                  :style="{ width: `${calcTransferringPct(group)}%` }"
                  :title="`正在转存中: ${(group.transferring_episodes || []).length} 集`" />
              </div>
            </div>

            <!-- 双引擎来源标签与状态 -->
            <div class="card-chips d-flex align-center ga-1 flex-wrap">
              <v-chip size="x-small" variant="tonal" color="info" prepend-icon="mdi-radar">
                频道雷达+搜源
              </v-chip>
              <v-chip
                v-if="hasMissing(group) && group.missing_episodes && group.missing_episodes.length"
                size="x-small"
                variant="tonal"
                color="warning"
                class="font-weight-medium">
                缺 {{ formatMissingRange(group.missing_episodes) }}
              </v-chip>
              <v-chip v-else-if="!hasMissing(group)" size="x-small" variant="tonal" color="success">
                全集就绪
              </v-chip>
            </div>

            <!-- 底部状态说明与操作 -->
            <div class="card-footer mt-auto pt-2 d-flex align-center justify-space-between">
              <div class="last-time text-caption text-medium-emphasis text-truncate">
                {{ getLastUpdateTime(group) }}
              </div>
              <v-btn
                size="x-small"
                variant="tonal"
                color="primary"
                prepend-icon="mdi-format-list-numbered"
                @click.stop="openGroupDrawer(group)">
                分集矩阵
              </v-btn>
            </div>
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

const filteredGroups = computed(() => {
  return props.items || []
})

const missingCountTotal = computed(() => {
  return (props.items || []).filter((g) => hasMissing(g)).length
})

const displayedGroups = computed(() => {
  let list = filteredGroups.value

  // 状态筛选
  if (filterStatus.value === "complete") {
    list = list.filter((g) => !hasMissing(g))
  } else if (filterStatus.value === "missing") {
    list = list.filter((g) => hasMissing(g))
  }

  // 片名模糊搜索
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

function hasMissing(group) {
  if (group.missing_count !== undefined) {
    return Number(group.missing_count) > 0
  }
  const recs = group.records || []
  return recs.some((r) => r.status && r.status !== "成功" && r.status !== "完成")
}

function getMissingLabel(group) {
  if (group.total_episodes !== undefined) {
    const missing = Number(group.missing_count || 0)
    return missing > 0 ? `待补齐 ${missing} 集` : "已齐"
  }
  return hasMissing(group) ? "有待补/跳过集" : "已全集转存"
}

function getStatusText(group) {
  if (group.total_episodes !== undefined && group.total_episodes > 0) {
    const libCount = group.library_count !== undefined ? group.library_count : (group.library_episodes || []).length
    return `已入库 ${libCount} / 全 ${group.total_episodes} 集`
  }
  const recs = group.records || []
  const success = recs.filter((r) => !r.status || r.status === "成功" || r.status === "完成").length
  return `已转存 ${success} 集`
}

function calcLibraryPct(group) {
  const total = Number(group.total_episodes || 0)
  if (total > 0) {
    const libCount = Number(group.library_count !== undefined ? group.library_count : (group.library_episodes || []).length)
    return Math.min(100, Math.round((libCount / total) * 100))
  }
  const recs = group.records || []
  if (!recs.length) return 0
  const success = recs.filter((r) => !r.status || r.status === "成功" || r.status === "完成").length
  return Math.round((success / recs.length) * 100)
}

function calcTransferringPct(group) {
  const total = Number(group.total_episodes || 0)
  if (total > 0 && group.transferring_episodes && group.transferring_episodes.length) {
    return Math.min(100, Math.round((group.transferring_episodes.length / total) * 100))
  }
  return 0
}

function formatMissingRange(episodes) {
  if (!episodes || !episodes.length) return ""
  // 缺集往往不连续（如 93-96 与 98-110），必须按连续段拆开，不能首尾一拉了之
  const nums = [...new Set(episodes.map(Number).filter((n) => Number.isFinite(n) && n > 0))].sort(
    (a, b) => a - b,
  )
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

function getLastUpdateTime(group) {
  if (group.last_updated_at) return `更新于 ${group.last_updated_at}`
  const recs = group.records || []
  if (!recs.length) return ""
  return recs[0]?.time ? `更新于 ${recs[0].time}` : ""
}

const imgErrors = ref({})

function onImgError(group) {
  const key = group.subscribe_id || group.title || group.name
  if (key) imgErrors.value[key] = true
}

function getCoverUrl(group) {
  if (!group) return ""
  const raw = (
    group.poster_url ||
    group.poster ||
    group.image ||
    group.poster_path ||
    group.backdrop_path ||
    (group.records && group.records[0] && (group.records[0].poster_url || group.records[0].image || group.records[0].poster || group.records[0].poster_path)) ||
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
}

function getInitial(title) {
  const clean = String(title || "").trim().replace(/^[^\w\u4e00-\u9fa5]+/, "")
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

.media-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(330px, 1fr));
  gap: 16px;
  overflow-y: auto;
}

.media-card {
  border-radius: 10px;
  border-color: rgba(var(--v-theme-on-surface), 0.08);
  transition: transform 0.2s, box-shadow 0.2s;
  cursor: pointer;
}

.media-card:hover {
  transform: translateY(-2px);
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.08);
}

.card-inner {
  display: flex;
  padding: 12px;
  gap: 12px;
  height: 100%;
}

.card-poster {
  position: relative;
  width: 80px;
  min-width: 80px;
  max-width: 80px;
  height: 116px;
  min-height: 116px;
  flex: 0 0 80px;
  border-radius: 6px;
  overflow: hidden;
  background: rgba(var(--v-theme-surface-variant), 0.3);
}

.poster-img {
  width: 80px;
  height: 116px;
  border-radius: 6px;
  display: block;
  object-fit: cover;
}

.poster-img :deep(.v-img__img),
.poster-img :deep(img) {
  object-fit: cover !important;
  width: 100% !important;
  height: 100% !important;
}

.poster-badge-art {
  width: 80px;
  height: 116px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, rgba(var(--v-theme-primary), 0.25), rgba(var(--v-theme-surface-variant), 0.8));
  border: 1px solid rgba(var(--v-theme-primary), 0.2);
}

.art-letter {
  font-size: 1.8rem;
  font-weight: 700;
  color: rgb(var(--v-theme-primary));
  opacity: 0.9;
}

.poster-type-badge {
  position: absolute;
  top: 4px;
  left: 4px;
  font-size: 0.65rem;
  padding-inline: 4px;
  height: 18px;
}

.card-content {
  flex: 1 1 auto;
  min-width: 0;
  display: flex;
  flex-direction: column;
}

.card-header {
  min-width: 0;
}

.card-title {
  margin: 0;
  font-size: 0.96rem;
  font-weight: 600;
  line-height: 1.3;
}

.card-year {
  flex-shrink: 0;
}

/* 自定义三色进度条 */
.custom-progress-bar {
  position: relative;
  display: flex;
  height: 6px;
  background: rgba(var(--v-theme-on-surface), 0.12);
  overflow: hidden;
}

.progress-fill {
  height: 100%;
  transition: width 0.3s ease;
}

.progress-fill-success {
  background: rgb(var(--v-theme-success));
}

.progress-fill-warning {
  background: rgb(var(--v-theme-warning));
}

.min-w-0 {
  min-width: 0;
}

@media (max-width: 600px) {
  .media-grid {
    grid-template-columns: 1fr;
    gap: 10px;
  }
  .hub-search-input {
    max-width: 100%;
    flex: 1 1 auto;
  }
}
</style>
