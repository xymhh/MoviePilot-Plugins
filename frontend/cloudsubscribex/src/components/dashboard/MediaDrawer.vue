<template>
  <v-dialog
    v-model="visible"
    max-width="760"
    scrollable
    class="media-detail-dialog">
    <v-card v-if="group" class="rounded-lg elevation-8 overflow-hidden">
      <!-- 头部：海报 + 基础元信息 + 关闭按钮 -->
      <v-card-title class="drawer-header pa-4 bg-surface-variant d-flex align-start ga-3">
        <div class="drawer-cover">
          <v-img
            v-if="coverUrl"
            :src="coverUrl"
            aspect-ratio="2/3"
            cover
            class="rounded elevation-2 cover-img">
            <template #placeholder>
              <div class="d-flex align-center justify-center fill-height bg-surface">
                <v-icon icon="mdi-filmstrip" color="medium-emphasis" />
              </div>
            </template>
            <template #error>
              <div class="cover-badge-art rounded elevation-2">
                <span class="art-letter">{{ getInitial(group.title) }}</span>
              </div>
            </template>
          </v-img>
          <div v-else class="cover-badge-art rounded elevation-2">
            <span class="art-letter">{{ getInitial(group.title) }}</span>
          </div>
        </div>

        <div class="drawer-title-box flex-grow-1 min-w-0">
          <div class="d-flex align-center justify-space-between">
            <h2 class="text-h6 font-weight-bold drawer-title text-truncate" :title="group.title">
              {{ group.title }}
              <span v-if="group.year" class="text-medium-emphasis text-body-1">({{ group.year }})</span>
            </h2>
            <v-btn icon="mdi-close" variant="text" size="small" @click="visible = false" />
          </div>

          <div class="drawer-meta-tags mt-2 d-flex flex-wrap align-center ga-1">
            <v-chip size="x-small" color="primary" variant="tonal">
              {{ group.type || "电视剧" }}
            </v-chip>
            <v-chip size="x-small" variant="tonal" color="info">
              共 {{ group.records ? group.records.length : 0 }} 集
            </v-chip>
            <v-chip v-if="group.total_size" size="x-small" variant="tonal" color="secondary">
              {{ formatSize(group.total_size) }}
            </v-chip>
            <v-chip
              v-for="rtype in group.resource_types || []"
              :key="rtype"
              size="x-small"
              variant="flat"
              :color="resourceTypeColor(rtype)">
              {{ resourceTypeLabel(rtype) }}
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

      <!-- 明细列表 (可滚动) -->
      <v-card-text class="drawer-body pa-3" style="max-height: 60vh;">
        <div class="d-flex align-center justify-space-between px-1 mb-2">
          <span class="text-subtitle-2 font-weight-bold">集数转存明细</span>
          <span class="text-caption text-medium-emphasis">
            已成功 {{ successCount }} / {{ (group.records || []).length }}
          </span>
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
                  <span v-if="rec.source_type">来源: {{ rec.source_type }}</span>
                  <span v-if="rec.resource_type">网盘: {{ resourceTypeLabel(rec.resource_type) }}</span>
                </div>
                <div v-if="rec.reason" class="record-reason text-caption text-warning mt-1">
                  ⚠️ 原因: {{ rec.reason }}
                </div>
              </div>

              <!-- 单条操作按钮 -->
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
      </v-card-text>

      <v-divider />
      <v-card-actions class="pa-3 bg-surface-variant d-flex justify-end">
        <v-btn variant="text" size="small" @click="visible = false">关闭</v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>
</template>

<script setup>
import { computed } from "vue"

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  group: { type: Object, default: null },
  retrying: { type: Boolean, default: false },
  retryingKey: { type: String, default: "" },
})

const emit = defineEmits(["update:modelValue", "retry", "retry-failed", "delete"])

const visible = computed({
  get: () => props.modelValue,
  set: (val) => emit("update:modelValue", val),
})

const coverUrl = computed(() => {
  if (!props.group) return ""
  return (
    props.group.poster ||
    props.group.image ||
    props.group.poster_path ||
    props.group.backdrop_path ||
    props.group.poster_url ||
    (props.group.records && props.group.records[0] && (props.group.records[0].image || props.group.records[0].poster)) ||
    ""
  )
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

const hasFailedRecords = computed(() => {
  return (props.group?.records || []).some((r) => r.status && r.status !== "成功" && r.status !== "完成")
})

const successCount = computed(() => {
  return (props.group?.records || []).filter((r) => !r.status || r.status === "成功" || r.status === "完成").length
})

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
  flex: 0 0 72px;
}

.cover-img {
  width: 72px;
  height: 108px;
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

.record-item {
  border-color: rgba(var(--v-theme-on-surface), 0.12);
  background: rgba(var(--v-theme-surface-variant), 0.25);
  border-radius: 8px;
}

.record-filename {
  font-weight: 500;
  word-break: break-all;
}

.min-w-0 {
  min-width: 0;
}
</style>
