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
          <v-img
            v-if="coverUrl"
            :src="coverUrl"
            :width="72"
            :height="108"
            aspect-ratio="2/3"
            cover
            referrerpolicy="no-referrer"
            class="rounded elevation-2 cover-img">
            <template #placeholder>
              <div class="d-flex align-center justify-center fill-height bg-surface">
                <v-icon icon="mdi-filmstrip" color="medium-emphasis" />
              </div>
            </template>
            <template #error>
              <div class="cover-badge-art rounded elevation-2">
                <span class="art-letter">{{ getInitial(group.title || group.name) }}</span>
              </div>
            </template>
          </v-img>
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
              {{ group.type || (group.season ? `S${String(group.season).padStart(2, '0')}` : "电视剧") }}
            </v-chip>
            <v-chip v-if="group.season" size="x-small" color="info" variant="tonal">
              第 {{ group.season }} 季
            </v-chip>
            <v-chip size="x-small" variant="tonal" color="success">
              已入库 {{ libraryCount }} 集
            </v-chip>
            <v-chip v-if="missingCount > 0" size="x-small" variant="tonal" color="warning">
              待补齐 {{ missingCount }} 集
            </v-chip>
            <v-chip v-if="totalCount > 0" size="x-small" variant="tonal" color="secondary">
              全剧共 {{ totalCount }} 集
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
        <!-- 1. 分集选集方块矩阵 (B站/爱奇艺风格) -->
        <div v-if="episodesList.length" class="episode-matrix-section mb-4">
          <div class="d-flex align-center justify-space-between mb-2">
            <div class="d-flex align-center ga-2">
              <span class="text-subtitle-2 font-weight-bold">选集追更全景矩阵</span>
              <span class="text-caption text-medium-emphasis">（点击单集查看详情与状态）</span>
            </div>
            <!-- 状态图例 -->
            <div class="matrix-legend d-flex align-center ga-2 text-caption">
              <span class="legend-item"><span class="legend-dot bg-success"></span>已入库</span>
              <span class="legend-item"><span class="legend-dot bg-warning"></span>转存中</span>
              <span class="legend-item"><span class="legend-dot bg-surface-variant border"></span>待补齐</span>
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
              <div class="d-flex align-center justify-space-between">
                <div class="d-flex align-center ga-2">
                  <v-chip size="small" :color="selectedEpData.statusColor" variant="tonal" class="font-weight-bold">
                    第 {{ selectedEpData.number }} 集 · {{ selectedEpData.statusText }}
                  </v-chip>
                  <span v-if="selectedEpData.matchedRecord?.file_name" class="text-caption text-truncate max-w-300" :title="selectedEpData.matchedRecord.file_name">
                    {{ selectedEpData.matchedRecord.file_name }}
                  </span>
                </div>
                <div class="d-flex align-center ga-1">
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

        <!-- 2. 转存流水记录（如存在） -->
        <div v-if="(group.records || []).length" class="records-log-section mt-4">
          <div class="d-flex align-center justify-space-between mb-2">
            <span class="text-subtitle-2 font-weight-bold">转存流水与跳过原因 ({{ (group.records || []).length }})</span>
          </div>

          <div class="record-list">
            <v-card
              v-for="(rec, idx) in group.records || []"
              :key="recordKey(rec, idx)"
              variant="outlined"
              class="record-item mb-2 pa-3">
              <div class="d-flex align-start justify-space-between ga-2">
                <div class="record-main min-w-0">
                  <div class="d-flex align-center ga-2 mb-1">
                    <v-chip
                      size="x-small"
                      :color="recordStatusColor(rec)"
                      variant="tonal"
                      class="font-weight-medium">
                      {{ rec.status || "已转存" }}
                    </v-chip>
                    <span v-if="rec.season || rec.episode" class="text-caption font-weight-bold text-primary">
                      {{ formatSeasonEpisode(rec) }}
                    </span>
                    <span class="text-caption text-medium-emphasis">
                      {{ rec.time || "-" }}
                    </span>
                  </div>
                  <div class="record-filename text-body-2 text-truncate" :title="rec.file_name">
                    {{ rec.file_name || "未知文件" }}
                  </div>
                  <div class="record-meta mt-1 d-flex align-center ga-2 text-caption text-medium-emphasis">
                    <span v-if="rec.size">{{ formatSize(rec.size) }}</span>
                    <span v-if="rec.source_type || rec.source">来源: {{ rec.source_type || rec.source }}</span>
                    <span v-if="rec.resource_type">网盘: {{ resourceTypeLabel(rec.resource_type) }}</span>
                  </div>
                  <div v-if="rec.reason" class="record-reason text-caption text-warning mt-1">
                    ⚠️ 原因: {{ rec.reason }}
                  </div>
                </div>

                <div class="record-actions d-flex align-center ga-1">
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
          <div>该订阅当前没有分集与转存记录</div>
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
import { ref, computed } from "vue"

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  group: { type: Object, default: null },
  retrying: { type: Boolean, default: false },
  retryingKey: { type: String, default: "" },
})

const emit = defineEmits(["update:modelValue", "retry", "retry-failed", "delete"])

const selectedEpisode = ref(null)

const visible = computed({
  get: () => props.modelValue,
  set: (val) => {
    emit("update:modelValue", val)
    if (!val) selectedEpisode.value = null
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
  if (str.startsWith("/api/") || str.startsWith("data:") || str.startsWith("blob:")) {
    return str
  }
  if (str.startsWith("/") && !str.startsWith("//")) {
    const full = `https://image.tmdb.org/t/p/w500${str}`
    return `/api/v1/system/img/1?cache=1&imgurl=${encodeURIComponent(full)}`
  }
  if (str.includes("image.tmdb.org") || str.includes("themoviedb.org")) {
    return `/api/v1/system/img/1?cache=1&imgurl=${encodeURIComponent(str)}`
  }
  return str
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

  // 如果没有显式 total_episodes，根据 records 提取集号
  const maxEp = Math.max(total, ...recs.map((r) => Number(r.episode || 0)))
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
    } else {
      // 检查 records 中是否有该集的记录
      const rec = recs.find((r) => Number(r.episode) === i)
      if (rec) {
        if (!rec.status || rec.status === "成功" || rec.status === "完成") {
          status = "success"
          statusText = "已入库"
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

function recordStatusColor(rec) {
  const status = rec.status
  if (!status || status === "成功" || status === "完成") return "success"
  if (status.includes("跳过") || status.includes("未命中")) return "warning"
  return "error"
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

.record-item {
  border-color: rgba(var(--v-theme-on-surface), 0.12);
  background: rgba(var(--v-theme-surface-variant), 0.25);
  border-radius: 8px;
}

.record-filename {
  font-weight: 500;
  word-break: break-all;
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
