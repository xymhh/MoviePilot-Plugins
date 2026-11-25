<template>
  <v-dialog v-model="model" max-width="780" scrollable class="resource-preview-dialog">
    <v-card class="source-preview-card">
      <v-card-title class="source-preview-header d-flex align-center justify-space-between py-2 px-3">
        <div class="source-preview-heading d-flex align-center ga-2 min-w-0">
          <v-avatar color="primary" variant="tonal" size="26" class="rounded-lg">
            <v-icon icon="mdi-folder-open-outline" size="15" />
          </v-avatar>
          <span class="text-subtitle-2 font-weight-bold flex-none">资源内容预览</span>
          <span class="text-caption text-medium-emphasis text-truncate" :title="meta.share_url || meta.display_name">
            {{ meta.display_name || meta.share_url || "文件树解析" }}
          </span>
        </div>
        <div class="d-flex align-center ga-1">
          <v-btn
            v-if="shareUrl"
            icon="mdi-content-copy"
            size="small"
            variant="text"
            title="复制网盘/磁力链接"
            @click="$emit('copy', shareUrl)" />
          <v-btn icon="mdi-close" size="small" variant="text" title="关闭" @click="model = false" />
        </div>
      </v-card-title>

      <v-divider />
      <v-card-text class="source-preview-body pa-3 pt-1">
        <v-alert v-if="isLocked" type="warning" variant="tonal" density="compact" class="mb-3 text-caption">
          当前内容尚未解锁，仅供查看文件列表。未解锁内容不支持按文件单独转存，请使用积分整包解锁后再入库。
        </v-alert>

        <div class="source-preview-toolbar mb-2">
          <div class="source-preview-meta d-flex align-center ga-2 flex-wrap">
            <v-chip v-if="meta.provider_name" size="small" variant="tonal" color="primary">
              <v-icon start icon="mdi-cloud-outline" size="13" />
              {{ meta.provider_name }}
            </v-chip>
            <!-- 网盘名与资源类型名常常相同，重复时不重复展示 -->
            <v-chip
              v-if="meta.resource_type_name && meta.resource_type_name !== meta.provider_name"
              size="small"
              variant="tonal"
              color="secondary">
              <v-icon start icon="mdi-tag-outline" size="13" />
              {{ meta.resource_type_name }}
            </v-chip>
            <v-chip v-if="meta.size" size="small" variant="tonal" color="info">
              <v-icon start icon="mdi-harddisk" size="13" />
              {{ formatBytes(meta.size) }}
            </v-chip>
            <v-chip size="small" variant="tonal">共 {{ items.length }} 个项目</v-chip>
            <v-chip v-if="isLocked" size="small" variant="tonal" color="warning" class="font-weight-medium">
              <v-icon start icon="mdi-lock-outline" size="13" />
              未解锁 (需 {{ meta.unlock_points || 0 }} 积分)
            </v-chip>
          </div>
          <v-text-field
            v-if="meta.searchable"
            v-model="keywordInput"
            class="source-preview-search"
            density="compact"
            variant="solo-filled"
            flat
            hide-details
            single-line
            rounded="lg"
            clearable
            prepend-inner-icon="mdi-magnify"
            placeholder="搜索文件名"
            :disabled="loading"
            @keyup.enter="submitSearch()"
            @click:clear="submitSearch('')" />
        </div>

        <div v-if="items.length && !isOffline" class="source-preview-breadcrumbs mb-2">
          <v-checkbox-btn
            v-if="canTransfer && !isLocked"
            :model-value="allSelected"
            density="compact"
            hide-details
            color="primary"
            class="preview-select-all"
            title="全选"
            @update:model-value="$emit('toggle-all', $event)" />
          <template v-for="(breadcrumb, index) in breadcrumbs" :key="`${breadcrumb.id}-${index}`">
            <span v-if="index > 0" class="breadcrumb-separator">/</span>
            <v-btn
              variant="text"
              size="x-small"
              class="breadcrumb-btn"
              :disabled="loading || index === breadcrumbs.length - 1"
              @click="$emit('open-breadcrumb', index)">
              {{ breadcrumb.name }}
            </v-btn>
          </template>
          <v-spacer v-if="canTransfer && !isLocked" />
          <!-- 常驻占位：按钮出现/隐藏都不改变本行高度与宽度 -->
          <v-btn
            v-if="canTransfer && !isLocked"
            size="x-small"
            color="primary"
            variant="flat"
            prepend-icon="mdi-check-all"
            class="preview-batch-btn font-weight-bold flex-none"
            :class="{ 'preview-batch-btn--idle': !selectedItems.length }"
            :disabled="!selectedItems.length"
            @click="$emit('batch-transfer')">
            转存 ({{ selectedItems.length }})
          </v-btn>
        </div>

        <div v-if="loading" class="pa-8 text-center">
          <v-progress-circular indeterminate color="primary" size="36" />
          <div class="text-caption text-medium-emphasis mt-2">正在解析资源文件目录，请稍候...</div>
        </div>
        <v-alert v-else-if="error" type="error" variant="tonal" density="compact" class="my-3">{{ error }}</v-alert>
        <div v-else-if="!items.length" class="text-center pa-8 text-medium-emphasis">
          <v-icon icon="mdi-folder-alert-outline" size="40" class="mb-2" />
          <div class="text-body-2">当前目录为空或未解析到文件</div>
        </div>
        <div v-else class="source-preview-list-scroll">
          <v-list density="compact" lines="one" class="pa-0">
            <template v-for="(file, index) in items" :key="`${file.name}-${index}`">
              <v-list-item
                class="source-preview-file py-1"
                :class="{ 'source-preview-file--directory': file.can_enter }">
                <template #prepend>
                  <v-checkbox-btn
                    v-if="canTransfer && !isOffline && !isLocked"
                    :model-value="isSelected(file)"
                    density="compact"
                    hide-details
                    color="primary"
                    class="mr-2"
                    @click.stop="$emit('toggle-item', file)" />
                  <v-icon
                    :icon="previewFileIcon(file)"
                    :color="file.is_dir ? 'amber' : 'primary'"
                    size="20"
                    class="mr-2"
                    @click="file.can_enter && $emit('open-folder', file)" />
                </template>
                <v-list-item-title
                  class="source-preview-file-name cursor-pointer"
                  :title="file.name"
                  @click="file.can_enter && $emit('open-folder', file)">
                  <span v-if="file.is_dir" class="font-weight-medium">{{ file.name }}</span>
                  <span v-else>{{ previewFileStem(file.name) }}</span>
                  <span v-if="!file.is_dir" class="text-medium-emphasis">{{ previewFileExtension(file.name) }}</span>
                </v-list-item-title>
                <template #append>
                  <span v-if="file.size" class="text-caption text-medium-emphasis mr-2">
                    {{ formatBytes(file.size) }}
                  </span>
                  <v-btn
                    v-if="canTransfer && !file.is_dir && !isOffline && !isLocked"
                    size="small"
                    color="primary"
                    variant="tonal"
                    class="preview-item-transfer-btn font-weight-medium"
                    prepend-icon="mdi-download"
                    title="仅转存此单个内容并自动整理"
                    @click.stop="$emit('single-transfer', file)">
                    转存
                  </v-btn>
                  <v-icon
                    v-if="file.can_enter"
                    icon="mdi-chevron-right"
                    size="18"
                    color="medium-emphasis"
                    class="ml-1 cursor-pointer"
                    @click.stop="$emit('open-folder', file)" />
                </template>
              </v-list-item>
              <v-divider v-if="index < items.length - 1" />
            </template>
          </v-list>
        </div>
      </v-card-text>

      <v-divider />
      <v-card-actions class="pa-3 justify-space-between">
        <span v-if="isLocked" class="text-caption text-warning">
          未解锁内容不支持按文件部分转存，可点击右侧「解锁并转存」
        </span>
        <span v-else-if="isOffline" class="text-caption text-medium-emphasis">
          磁力任务将作为离线下载整体转存，完成后自动识别并整理正片
        </span>
        <span v-else-if="canTransfer" class="text-caption text-medium-emphasis">
          支持下钻浏览各层级子文件夹，可按需勾选转存
        </span>
        <span v-else class="text-caption text-medium-emphasis">当前为只读预览，仅用于查看文件列表</span>
        <div class="d-flex ga-2">
          <v-btn variant="tonal" size="small" @click="model = false">关闭</v-btn>
          <v-btn
            v-if="canTransfer"
            :color="isLocked ? 'warning' : 'primary'"
            variant="flat"
            size="small"
            :prepend-icon="isLocked ? 'mdi-lock-open-outline' : 'mdi-download'"
            @click="$emit('quick-transfer')">
            {{ isLocked ? `解锁并转存 (${meta.unlock_points || 0} 积分)` : "一键转存" }}
          </v-btn>
        </div>
      </v-card-actions>
    </v-card>
  </v-dialog>
</template>

<script setup>
import { computed, ref, watch } from "vue"
import {
  formatBytes,
  isOfflineResourceType,
  previewFileExtension,
  previewFileIcon,
  previewFileStem,
} from "../../composables/resourceUtils"

const props = defineProps({
  modelValue: Boolean,
  loading: Boolean,
  error: { type: String, default: "" },
  items: { type: Array, default: () => [] },
  selectedItems: { type: Array, default: () => [] },
  meta: { type: Object, default: () => ({}) },
  breadcrumbs: { type: Array, default: () => [] },
  allSelected: Boolean,
  shareUrl: { type: String, default: "" },
  canTransfer: Boolean,
  isSelected: { type: Function, default: () => () => false },
})
const emit = defineEmits([
  "update:modelValue",
  "copy",
  "search",
  "open-folder",
  "open-breadcrumb",
  "toggle-all",
  "toggle-item",
  "single-transfer",
  "batch-transfer",
  "quick-transfer",
])
const model = computed({ get: () => props.modelValue, set: (value) => emit("update:modelValue", value) })
// 搜索框只承载用户自己输入/搜索的关键词，渠道内部的定位词不回填
const keywordInput = ref("")

// 每次打开弹窗都从空关键词开始，避免上一次搜索的残留
watch(
  () => props.modelValue,
  (visible) => {
    if (visible) keywordInput.value = ""
  },
)

function submitSearch(explicit) {
  const value = typeof explicit === "string" ? explicit : keywordInput.value
  emit("search", String(value || "").trim())
}

const isLocked = computed(() => {
  const meta = props.meta || {}
  return Boolean(meta.need_unlock && !meta.is_unlocked && !props.shareUrl)
})

const isOffline = computed(() => {
  const type = String(props.meta?.resource_type || "").toLowerCase()
  const share = String(props.meta?.share_url || props.shareUrl || "").toLowerCase()
  return (
    isOfflineResourceType(type) ||
    type === "magnet" ||
    type === "ed2k" ||
    share.startsWith("magnet:") ||
    share.startsWith("ed2k://")
  )
})
</script>

<style scoped>
.source-preview-card {
  max-height: min(80vh, 740px);
  border-radius: 8px;
  overflow: hidden;
}

.source-preview-header {
  min-height: 44px;
}

.source-preview-header .v-avatar {
  flex: none;
}

.source-preview-heading {
  flex: 1 1 auto;
}

.source-preview-meta {
  padding: 2px 0;
}

.source-preview-meta :deep(.v-chip) {
  height: 24px;
  font-size: 0.75rem;
}

.source-preview-toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.source-preview-toolbar .source-preview-meta {
  flex: 1 1 auto;
  min-width: 0;
}

.source-preview-search {
  flex: 1 1 200px;
  min-width: 180px;
  max-width: 300px;
}

.source-preview-search :deep(.v-field) {
  min-height: 32px;
}

.source-preview-search :deep(.v-field__input) {
  padding-top: 2px;
  padding-bottom: 2px;
  font-size: 0.8125rem;
  text-overflow: ellipsis;
}

.source-preview-breadcrumbs {
  display: flex;
  align-items: center;
  flex-wrap: nowrap;
  gap: 2px;
  padding: 4px 6px;
  min-height: 30px;
  overflow: hidden;
  border-bottom: 1px solid rgba(var(--v-border-color), 0.08);
}

.preview-select-all {
  flex: none;
  margin-inline-end: 2px;
}

.preview-batch-btn--idle {
  visibility: hidden;
  pointer-events: none;
}

.breadcrumb-separator {
  color: rgba(var(--v-theme-on-surface), 0.4);
  margin: 0 3px;
  font-size: 0.75rem;
}

.breadcrumb-btn {
  text-transform: none !important;
  padding: 0 6px !important;
  font-size: 0.75rem !important;
}

.source-preview-list-scroll {
  min-height: 0;
  max-height: min(50vh, 440px);
  overflow-y: auto;
}

.source-preview-file {
  min-height: 44px;
  border-radius: 6px;
  transition: background 0.15s ease;
}

.source-preview-file--directory {
  cursor: pointer;
}

.source-preview-file--directory:hover {
  background: rgba(var(--v-theme-primary), 0.08);
}

.source-preview-file-name {
  font-size: 0.85rem;
  line-height: 1.4;
  word-break: break-all;
}

.preview-batch-btn {
  border-radius: 6px !important;
  height: 28px !important;
  padding: 0 12px !important;
  font-size: 0.78rem !important;
  box-shadow: 0 2px 6px rgba(var(--v-theme-primary), 0.28) !important;
}

.preview-item-transfer-btn {
  border-radius: 6px !important;
  height: 26px !important;
  padding: 0 10px !important;
  font-size: 0.74rem !important;
  letter-spacing: 0;
}
</style>
