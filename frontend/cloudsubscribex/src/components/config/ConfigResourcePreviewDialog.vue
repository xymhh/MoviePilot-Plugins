<template>
    <v-dialog :model-value="modelValue" max-width="760" scrollable class="resource-preview-dialog" @update:model-value="emit('update:modelValue', $event)">
      <v-card class="source-preview-card">
        <v-card-title class="source-preview-header d-flex align-center justify-space-between py-3 px-4">
          <div class="d-flex align-center ga-2 min-w-0">
            <v-icon icon="mdi-folder-open-outline" color="primary" />
            <div class="min-w-0">
              <div class="text-subtitle-1 font-weight-bold">资源内容预览</div>
              <div
                class="text-caption text-medium-emphasis text-truncate"
                :title="previewMeta.display_name || previewMeta.share_url">
                {{ previewMeta.display_name || previewMeta.share_url || "文件树解析" }}
              </div>
            </div>
          </div>
          <div class="d-flex align-center ga-1">
            <v-btn
              v-if="previewMeta.share_url"
              icon="mdi-content-copy"
              size="small"
              variant="text"
              title="复制网盘链接"
              @click="emit('copy-text', previewMeta.share_url, '网盘链接')" />
            <v-btn icon="mdi-close" size="small" variant="text" title="关闭" @click="emit('update:modelValue', false)" />
          </div>
        </v-card-title>
        <v-card-text class="source-preview-body">
          <div
            v-if="
              previewMeta.display_name ||
              previewMeta.info_hash ||
              previewMeta.size ||
              previewMeta.provider_name ||
              previewMeta.share_url
            "
            class="source-preview-meta text-caption text-medium-emphasis mb-3">
            <span v-if="previewMeta.provider_name">
              网盘
              <strong>{{ previewMeta.provider_name }}</strong>
            </span>
            <span v-if="previewMeta.resource_type_name">
              类型
              <strong>{{ previewMeta.resource_type_name }}</strong>
            </span>
            <span v-if="previewMeta.size">
              总大小
              <strong>{{ formatPreviewSize(previewMeta.size) }}</strong>
            </span>
            <span>
              当前层
              <strong class="text-primary">{{ previewItems.length }}</strong>
              项
            </span>
          </div>
          <div v-if="previewBreadcrumbs.length > 1" class="source-preview-breadcrumbs mb-3">
            <template v-for="(breadcrumb, index) in previewBreadcrumbs" :key="`${breadcrumb.id}-${index}`">
              <v-icon v-if="index" icon="mdi-chevron-right" size="small" />
              <v-btn
                size="small"
                variant="text"
                :disabled="previewLoading || index === previewBreadcrumbs.length - 1"
                @click="emit('open-breadcrumb', index)">
                {{ breadcrumb.name }}
              </v-btn>
            </template>
          </div>
          <div v-if="previewLoading" class="source-preview-loading">
            <v-progress-circular indeterminate color="primary" size="44" width="4" />
          </div>
          <v-alert v-if="previewError" type="error" variant="tonal" density="compact" class="my-3">
            {{ previewError }}
          </v-alert>
          <div v-else-if="!previewLoading && previewItems.length" class="source-preview-list-scroll">
            <v-list density="compact" lines="one" class="source-preview-list">
              <template v-for="(file, index) in previewItems" :key="`${file.name}-${index}`">
                <v-list-item
                  class="source-preview-file"
                  :class="{ 'source-preview-file--directory': file.can_enter }"
                  @click="file.can_enter && emit('open-folder', file)">
                  <template #prepend>
                    <v-icon :icon="previewFileIcon(file)" />
                  </template>
                  <v-list-item-title class="source-preview-file-name" :title="file.name">
                    <span v-if="file.is_dir" class="source-preview-file-stem">{{ file.name }}</span>
                    <template v-else>
                      <span class="source-preview-file-stem">{{ previewFileStem(file.name) }}</span>
                      <span class="source-preview-file-extension">{{ previewFileExtension(file.name) }}</span>
                    </template>
                  </v-list-item-title>
                  <template #append>
                    <span
                      v-if="formatPreviewSize(file.size)"
                      class="source-preview-file-size text-caption text-medium-emphasis">
                      {{ formatPreviewSize(file.size) }}
                    </span>
                    <v-icon v-if="file.can_enter" icon="mdi-chevron-right" size="small" class="ml-2" />
                  </template>
                </v-list-item>
                <v-divider v-if="index < previewItems.length - 1" />
              </template>
            </v-list>
          </div>
          <v-alert v-else-if="!previewLoading && !previewError" type="info" variant="tonal" density="compact">
            当前目录为空
          </v-alert>
        </v-card-text>
      </v-card>
    </v-dialog>
</template>

<script setup>
const props = defineProps({
  modelValue: { type: Boolean, default: false },
  previewMeta: { type: Object, default: () => ({}) },
  previewItems: { type: Array, default: () => [] },
  previewBreadcrumbs: { type: Array, default: () => [] },
  previewLoading: { type: Boolean, default: false },
  previewError: { type: String, default: "" },
})
const emit = defineEmits(["update:modelValue", "copy-text", "open-folder", "open-breadcrumb"])

// 以下四个纯函数原在 Config.vue，只供本弹窗模板使用，随弹窗一起搬过来。
function formatPreviewSize(value) {
  const size = Number(value || 0)
  if (!Number.isFinite(size) || size <= 0) return ""
  const units = ["B", "KB", "MB", "GB", "TB"]
  const index = Math.min(Math.floor(Math.log(size) / Math.log(1024)), units.length - 1)
  return `${(size / 1024 ** index).toFixed(index ? 1 : 0)} ${units[index]}`
}

function previewFileIcon(file) {
  if (file?.is_dir) return "mdi-folder-outline"
  const name = String(file?.name || "").toLowerCase()
  if (/\.(mkv|mp4|avi|ts|m2ts|mov|wmv|webm)$/.test(name)) return "mdi-filmstrip"
  if (/\.(srt|ass|ssa|sup|vtt)$/.test(name)) return "mdi-subtitles-outline"
  if (/\.(zip|rar|7z|tar|gz)$/.test(name)) return "mdi-archive-outline"
  return "mdi-file-outline"
}

function previewFileExtension(value) {
  const name = String(value || "未命名文件")
  const extensionMatch = name.match(/(\.[^./\\\s]{1,12})$/)
  return extensionMatch ? extensionMatch[1] : ""
}

function previewFileStem(value) {
  const name = String(value || "未命名文件")
  const extension = previewFileExtension(name)
  const slash = Math.max(name.lastIndexOf("/"), name.lastIndexOf("\\"))
  const basename = slash >= 0 ? name.slice(slash + 1) : name
  return extension ? basename.slice(0, -extension.length) : basename
}
</script>

<style scoped>
.source-preview-card {
  max-height: min(78vh, 720px);
  border-radius: 10px;
}

.source-preview-header {
  min-height: 58px;
  background: rgb(var(--v-theme-surface));
}

.source-preview-body {
  display: flex;
  min-height: 0;
  flex-direction: column;
  overflow: hidden;
}

.source-preview-loading {
  min-height: 120px;
  display: flex;
  align-items: center;
  justify-content: center;
}

.source-preview-list-scroll {
  min-height: 0;
  max-height: min(58vh, 520px);
  overflow-y: auto;
}

.source-preview-list {
  font-size: 0.86rem;
}

.source-preview-list :deep(.v-list-item-title) {
  font-size: 0.86rem;
  line-height: 1.35rem;
}

.source-preview-meta {
  display: flex;
  gap: 8px 18px;
  padding: 9px 12px;
  border: 1px solid rgba(var(--v-border-color), 0.12);
  border-radius: 6px;
  background: rgba(var(--v-theme-surface-variant), 0.22);
  overflow-wrap: anywhere;
  line-height: 1.55;
}

.source-preview-meta > span {
  margin-right: 0;
  white-space: nowrap;
}

.source-preview-breadcrumbs {
  display: flex;
  min-height: 32px;
  align-items: center;
  overflow-x: auto;
  padding-bottom: 6px;
  white-space: nowrap;
}

.source-preview-file--directory {
  cursor: pointer;
}

.source-preview-file--directory:hover {
  background: rgba(var(--v-theme-primary), 0.06);
}

.source-preview-file :deep(.v-list-item__prepend) {
  width: 36px;
  min-width: 36px;
}

.source-preview-file :deep(.v-list-item__content) {
  min-width: 0;
  overflow: hidden;
}

.source-preview-file-name {
  display: flex;
  flex-direction: row;
  justify-content: flex-start;
  min-width: 0;
  overflow: hidden;
  direction: ltr;
  text-align: left;
  white-space: nowrap;
}

.source-preview-file-stem {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  direction: ltr;
  text-align: left;
}

.source-preview-file-extension {
  flex: 0 0 auto;
}

.source-preview-file-size {
  width: 76px;
  flex: 0 0 76px;
  text-align: right;
  white-space: nowrap;
}

</style>
