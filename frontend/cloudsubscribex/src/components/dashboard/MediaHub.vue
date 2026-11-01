<template>
  <div class="media-hub-root">
    <!-- 顶部工具栏：快速搜索 + 状态筛选 -->
    <div class="hub-toolbar d-flex flex-wrap align-center justify-space-between ga-2 mb-3">
      <div class="hub-filter-chips d-flex align-center ga-1 flex-wrap">
        <v-chip-group v-model="filterStatus" mandatory selected-class="text-primary font-weight-bold">
          <v-chip value="all" size="small" variant="tonal">全部 ({{ filteredGroups.length }})</v-chip>
          <v-chip value="complete" size="small" variant="tonal" color="success">全部成功</v-chip>
          <v-chip value="has_issue" size="small" variant="tonal" color="warning">包含未转/跳过</v-chip>
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
          title="刷新媒体列表"
          :loading="loading"
          @click="$emit('refresh')" />
      </div>
    </div>

    <!-- 加载中状态 -->
    <div v-if="loading && (!items || !items.length)" class="d-flex flex-column align-center justify-center py-12">
      <v-progress-circular indeterminate color="primary" size="40" />
      <span class="text-caption text-medium-emphasis mt-2">正在加载追更工作台...</span>
    </div>

    <!-- 空数据提示 -->
    <div v-else-if="!displayedGroups.length" class="empty-state text-center py-12">
      <v-icon icon="mdi-filmstrip-off" size="56" color="medium-emphasis" class="mb-2" />
      <div class="text-body-1 font-weight-medium">暂无匹配的追更条目</div>
      <div class="text-caption text-medium-emphasis mt-1">可以通过右上角「手动添加」或等待自动同步搜源</div>
    </div>

    <!-- 媒体卡片网格 -->
    <div v-else class="media-grid">
      <v-card
        v-for="group in displayedGroups"
        :key="group.group_key || group.title"
        class="media-card"
        variant="outlined"
        hover
        @click="openGroupDrawer(group)">
        <div class="card-inner">
          <!-- 海报区 -->
          <div class="card-poster">
            <v-img
              v-if="group.poster_path || group.backdrop_path"
              :src="group.poster_path || group.backdrop_path"
              aspect-ratio="2/3"
              cover
              class="poster-img rounded">
              <template #placeholder>
                <div class="d-flex align-center justify-center fill-height bg-surface-variant">
                  <v-icon icon="mdi-filmstrip" color="medium-emphasis" />
                </div>
              </template>
            </v-img>
            <div v-else class="poster-placeholder rounded">
              <v-icon icon="mdi-filmstrip" size="28" color="medium-emphasis" />
            </div>
            <v-chip size="x-small" color="primary" variant="flat" class="poster-type-badge">
              {{ group.type || "剧集" }}
            </v-chip>
          </div>

          <!-- 内容区 -->
          <div class="card-content">
            <div class="card-header">
              <h3 class="card-title text-truncate" :title="group.title">
                {{ group.title }}
              </h3>
              <span v-if="group.year" class="card-year text-caption text-medium-emphasis">
                ({{ group.year }})
              </span>
            </div>

            <!-- 进度条与集数 -->
            <div class="progress-section my-2">
              <div class="d-flex align-center justify-space-between text-caption mb-1">
                <span class="font-weight-medium">
                  {{ getStatusText(group) }}
                </span>
                <span class="text-medium-emphasis">
                  {{ group.records ? group.records.length : 0 }} 集
                </span>
              </div>
              <v-progress-linear
                :model-value="calcProgress(group)"
                :color="getProgressColor(group)"
                height="6"
                rounded />
            </div>

            <!-- 元数据胶囊 -->
            <div class="card-chips d-flex align-center ga-1 flex-wrap">
              <v-chip
                v-for="rtype in (group.resource_types || []).slice(0, 2)"
                :key="rtype"
                size="x-small"
                variant="tonal"
                :color="resourceTypeColor(rtype)">
                {{ resourceTypeLabel(rtype) }}
              </v-chip>
              <v-chip v-if="group.total_size" size="x-small" variant="tonal" color="secondary">
                {{ formatSize(group.total_size) }}
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
                prepend-icon="mdi-card-text-outline"
                @click.stop="openGroupDrawer(group)">
                明细
              </v-btn>
            </div>
          </div>
        </div>
      </v-card>
    </div>

    <!-- 单剧集明细抽屉 -->
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

const displayedGroups = computed(() => {
  let list = filteredGroups.value

  // 状态筛选
  if (filterStatus.value === "complete") {
    list = list.filter((g) => {
      const recs = g.records || []
      return recs.length > 0 && recs.every((r) => !r.status || r.status === "成功" || r.status === "完成")
    })
  } else if (filterStatus.value === "has_issue") {
    list = list.filter((g) => {
      const recs = g.records || []
      return recs.some((r) => r.status && r.status !== "成功" && r.status !== "完成")
    })
  }

  // 片名模糊搜索
  const kw = searchKeyword.value.trim().toLowerCase()
  if (kw) {
    list = list.filter((g) => {
      const title = String(g.title || "").toLowerCase()
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

function calcProgress(group) {
  const recs = group.records || []
  if (!recs.length) return 0
  const success = recs.filter((r) => !r.status || r.status === "成功" || r.status === "完成").length
  return Math.round((success / recs.length) * 100)
}

function getProgressColor(group) {
  const p = calcProgress(group)
  if (p === 100) return "success"
  const hasError = (group.records || []).some((r) => r.status && r.status.includes("失败"))
  return hasError ? "error" : "primary"
}

function getStatusText(group) {
  const recs = group.records || []
  const success = recs.filter((r) => !r.status || r.status === "成功" || r.status === "完成").length
  if (success === recs.length && recs.length > 0) return "已转存全部"
  return `已转 ${success} / ${recs.length}`
}

function getLastUpdateTime(group) {
  const recs = group.records || []
  if (!recs.length) return ""
  return recs[0]?.time ? `更新于 ${recs[0].time}` : ""
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

function resourceTypeColor(type) {
  const map = {
    "115": "#2670e8",
    quark: "#15b886",
    alipan: "#ff6a00",
    tianyi: "#e60012",
    p123: "#6366f1",
  }
  return map[String(type).toLowerCase()] || "primary"
}

function resourceTypeLabel(type) {
  const map = {
    "115": "115",
    quark: "夸克",
    alipan: "阿里",
    tianyi: "天翼",
    p123: "123",
  }
  return map[String(type).toLowerCase()] || type
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
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
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
  flex: 0 0 80px;
}

.poster-img {
  width: 80px;
  height: 116px;
}

.poster-placeholder {
  width: 80px;
  height: 116px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(var(--v-theme-surface-variant), 0.5);
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
  display: flex;
  align-items: baseline;
  gap: 6px;
}

.card-title {
  margin: 0;
  font-size: 0.95rem;
  font-weight: 600;
  line-height: 1.3;
}

.card-year {
  flex-shrink: 0;
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
