<template>
    <v-dialog :model-value="modelValue" max-width="640" class="source-test-dialog" @update:model-value="emit('update:modelValue', $event)">
      <v-card class="source-test-card" :class="{ 'source-test-card--results': tmdbSearched || testSubmitted }">
        <v-card-title class="source-test-header d-flex align-center ga-2">
          <v-icon icon="mdi-flask-outline" color="primary" />
          {{ sourceNames[sourceTest.source] || "搜索渠道" }}测试
          <v-spacer />
          <v-btn icon="mdi-close" size="small" variant="text" title="关闭" @click="emit('update:modelValue', false)" />
        </v-card-title>
        <v-form class="source-test-form" @submit.prevent="emit('search-tmdb')">
          <v-card-text class="source-test-body">
            <v-row dense class="source-test-fields">
              <v-col cols="12" sm="8">
                <v-text-field
                  v-model="sourceTest.title"
                  label="媒体名称"
                  maxlength="100"
                  autofocus
                  clearable
                  hide-details />
              </v-col>
              <v-col cols="12" sm="4">
                <v-text-field
                  v-model="sourceTest.season"
                  label="电视剧季号"
                  type="number"
                  min="1"
                  max="999"
                  hide-details />
              </v-col>
            </v-row>
            <v-alert v-if="testError" type="error" variant="tonal" density="compact" class="source-test-error mt-3">
              {{ testError }}
              <span v-if="testElapsed != null">（耗时 {{ formatElapsed(testElapsed) }}）</span>
            </v-alert>
            <div v-if="tmdbSearched && !testSubmitted" class="source-test-tmdb mt-3">
              <div class="text-caption text-medium-emphasis mb-2">
                选择平台媒体条目后将一次读取完整媒体 ID，并立即测试
                {{ sourceNames[sourceTest.source] }}
              </div>
              <div v-if="testingSource" class="source-test-loading">
                <v-progress-circular indeterminate color="primary" size="42" width="4" />
                <div class="text-body-2 mt-3">正在读取 {{ sourceNames[sourceTest.source] }} 资源</div>
                <div class="text-caption text-medium-emphasis mt-1">仅执行只读搜索，不会解锁、转存或处理文件</div>
              </div>
              <div v-else class="source-test-tmdb-scroll">
                <v-list v-if="tmdbCandidates.length" density="compact" lines="two">
                  <v-list-item
                    v-for="item in tmdbCandidates"
                    :key="`${item.media_type}-${item.tmdb_id}`"
                    :title="item.title"
                    :subtitle="tmdbCandidateSubtitle(item)"
                    :disabled="Boolean(testingSource)"
                    rounded="lg"
                    class="source-test-tmdb-item mb-1"
                    @click="emit('test-source', item)">
                    <template #prepend>
                      <v-avatar rounded="lg" size="48" color="surface-variant">
                        <v-img v-if="item.poster" :src="item.poster" cover />
                        <v-icon
                          v-else
                          :icon="item.media_type === 'movie' ? 'mdi-movie-open-outline' : 'mdi-television-classic'" />
                      </v-avatar>
                    </template>
                    <template #append>
                      <v-progress-circular
                        v-if="selectedTmdbId === item.tmdb_id"
                        indeterminate
                        color="primary"
                        size="20"
                        width="2" />
                      <v-chip v-else size="x-small" variant="tonal" color="primary">TMDB {{ item.tmdb_id }}</v-chip>
                    </template>
                  </v-list-item>
                </v-list>
                <v-alert v-else type="info" variant="tonal" density="compact">平台未找到匹配媒体条目</v-alert>
              </div>
            </div>
            <div v-if="testSubmitted" class="source-test-result">
              <div class="source-test-summary">
                <div class="source-test-summary__line">
                  <span>
                    本次获取
                    <strong>{{ testResult.count || 0 }}</strong>
                    个搜索结果
                  </span>
                  <span>
                    当前展示
                    <strong>{{ testResult.displayed_count ?? (testResult.items || []).length }}</strong>
                    个
                  </span>
                  <span v-if="testResult.elapsed_seconds != null">
                    耗时
                    <strong>{{ formatElapsed(testResult.elapsed_seconds) }}</strong>
                  </span>
                </div>
              </div>
              <div class="source-test-notice text-medium-emphasis mb-3">
                搜索测试固定最多展示
                {{ testResult.display_limit }}
                个候选，不受正式搜索候选上限配置影响
              </div>
              <v-tabs
                v-if="testResourceTypes.length > 1"
                :model-value="selectedTestResourceType"
                @update:model-value="emit('update:selectedTestResourceType', $event)"
                color="primary"
                density="compact"
                show-arrows
                class="source-test-tabs source-test-resource-tabs">
                <v-tab
                  v-for="resourceType in testResourceTypes"
                  :key="resourceType.value"
                  :value="resourceType.value">
                  {{ resourceType.title }}
                  <v-badge inline :content="resourceType.count" color="primary" class="ml-2" />
                </v-tab>
              </v-tabs>
              <v-tabs
                v-if="testFansubs.length > 1"
                :model-value="selectedTestFansub"
                @update:model-value="emit('update:selectedTestFansub', $event)"
                color="secondary"
                density="compact"
                show-arrows
                class="source-test-tabs source-test-fansub-tabs mt-1">
                <v-tab value="">
                  全部字幕组
                  <v-badge inline :content="testResult.items?.length || 0" color="secondary" class="ml-2" />
                </v-tab>
                <v-tab
                  v-for="fansub in testFansubs"
                  :key="fansub.value"
                  :value="fansub.value">
                  {{ fansub.title }}
                  <v-badge inline :content="fansub.count" color="secondary" class="ml-2" />
                </v-tab>
              </v-tabs>
              <div class="source-test-result-scroll">
                <v-list v-if="filteredTestItems.length" density="compact" class="source-test-result-list">
                  <v-list-item
                    v-for="(item, index) in filteredTestItems"
                    :key="`${item.title}-${index}`"
                    class="source-test-result-item">
                    <div class="source-test-item-content">
                      <div class="source-test-item-title">{{ item.title }}</div>
                      <div class="source-test-item-meta">
                        <v-chip
                          size="x-small"
                          variant="tonal"
                          color="primary"
                          :href="item.source_url || undefined"
                          :target="item.source_url ? '_blank' : undefined"
                          :rel="item.source_url ? 'noopener noreferrer' : undefined"
                          :title="item.source_url ? '打开来源资源页' : undefined">
                          {{ item.source_name || testResult.source_name || "未知来源" }}
                          <v-icon v-if="item.source_url" icon="mdi-open-in-new" size="12" class="ml-1" />
                        </v-chip>
                        <v-chip size="x-small" variant="tonal">
                          {{ item.resource_type_name || item.resource_type || "未知类型" }}
                        </v-chip>
                        <v-chip
                          v-if="item.fansub"
                          size="x-small"
                          variant="tonal"
                          color="secondary"
                          class="font-weight-medium">
                          {{ item.fansub }}
                        </v-chip>
                        <v-chip
                          v-if="testItemStatus(item)"
                          size="x-small"
                          variant="tonal"
                          :color="testItemStatus(item).color">
                          {{ testItemStatus(item).label }}
                        </v-chip>
                        <span class="text-medium-emphasis">
                          {{ item.size || "大小未知" }}
                        </span>
                        <v-chip
                          v-for="tag in item.tags || []"
                          :key="`${item.title}-${tag}`"
                          size="x-small"
                          variant="outlined">
                          {{ tag }}
                        </v-chip>
                        <div class="source-test-item-actions">
                          <v-btn
                            v-if="canPreviewResource(item)"
                            icon="mdi-eye-outline"
                            size="x-small"
                            variant="text"
                            :title="'预览资源内容'"
                            :loading="previewingUrl === previewResourceKey(item)"
                            :disabled="Boolean(previewingUrl)"
                            @click="emit('preview-resource', item)" />
                          <v-btn
                            v-if="item.url"
                            icon="mdi-content-copy"
                            size="x-small"
                            variant="text"
                            title="复制资源链接"
                            @click="emit('copy-text', item.url, '资源链接')" />
                          <v-btn
                            v-if="item.need_unlock"
                            icon="mdi-lock-open-outline"
                            size="x-small"
                            variant="text"
                            color="warning"
                            :title="Number(item.unlock_points || 0) > 0 ? `确认消耗 ${Number(item.unlock_points || 0)} 积分解锁` : '获取免费资源链接'"
                            @click="emit('confirm-unlock', item)" />
                        </div>
                      </div>
                    </div>
                  </v-list-item>
                </v-list>
                <v-alert v-else type="info" variant="tonal" density="compact">当前渠道未找到候选资源</v-alert>
              </div>
            </div>
          </v-card-text>
          <v-card-actions class="source-test-actions">
            <v-spacer />
            <v-btn
              type="submit"
              color="primary"
              variant="flat"
              prepend-icon="mdi-magnify"
              :loading="searchingTmdb"
              :disabled="Boolean(testingSource) || searchingTmdb || !String(sourceTest.title || '').trim()">
              搜索资源
            </v-btn>
          </v-card-actions>
        </v-form>
      </v-card>
    </v-dialog>
</template>

<script setup>
import { formatElapsed, tmdbCandidateSubtitle, testItemStatus, canPreviewResource, previewResourceKey } from "../../utils/sourceTestHelpers.js"

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  sourceTest: { type: Object, default: () => ({}) },
  sourceNames: { type: Object, default: () => ({}) },
  searchingTmdb: { type: Boolean, default: false },
  tmdbSearched: { type: Boolean, default: false },
  tmdbCandidates: { type: Array, default: () => [] },
  selectedTmdbId: { type: Number, default: 0 },
  testingSource: { type: String, default: "" },
  testResult: { type: Object, default: () => ({}) },
  testSubmitted: { type: Boolean, default: false },
  testError: { type: String, default: "" },
  testElapsed: { type: [Number, String], default: null },
  testResourceTypes: { type: Array, default: () => [] },
  testFansubs: { type: Array, default: () => [] },
  selectedTestResourceType: { type: String, default: "" },
  selectedTestFansub: { type: String, default: "" },
  filteredTestItems: { type: Array, default: () => [] },
  previewingUrl: { type: String, default: "" },
})
const emit = defineEmits(["update:modelValue", "search-tmdb", "test-source", "update:selectedTestResourceType", "update:selectedTestFansub", "preview-resource", "copy-text", "confirm-unlock"])
</script>

<style scoped>
.source-test-card {
  max-height: min(600px, calc(100dvh - 48px));
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.source-test-card--results {
  height: min(600px, calc(100dvh - 48px));
}

.source-test-header,
.source-test-actions {
  flex: 0 0 auto;
}

.source-test-form {
  min-height: 0;
  flex: 1 1 auto;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.source-test-body {
  min-height: 0;
  flex: 1 1 auto;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.source-test-fields {
  flex: 0 0 auto;
}

.source-test-error {
  flex: 0 0 auto;
}

.source-test-tmdb {
  min-height: 0;
  flex: 1 1 auto;
  display: flex;
  flex-direction: column;
}

.source-test-tmdb-scroll {
  min-height: 0;
  flex: 1 1 auto;
  overflow-y: auto;
  overscroll-behavior: contain;
}

.source-test-loading {
  min-height: 220px;
  flex: 1 1 auto;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-direction: column;
  text-align: center;
}

.source-test-tmdb-item {
  border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
}

.source-test-result {
  min-height: 0;
  flex: 1 1 auto;
  display: flex;
  flex-direction: column;
  margin-top: 8px;
}

.source-test-summary {
  flex: 0 0 auto;
  padding: 10px 12px;
  margin-bottom: 8px;
  border-left: 3px solid rgb(var(--v-theme-primary));
  background: rgba(var(--v-theme-primary), 0.07);
}

.source-test-summary__line {
  display: flex;
  align-items: baseline;
  flex-wrap: nowrap;
  gap: 14px;
  overflow-x: auto;
  white-space: nowrap;
  color: rgba(var(--v-theme-on-surface), 0.86);
  font-size: 0.8125rem;
  line-height: 1.45;
  scrollbar-width: none;
}

.source-test-summary__line::-webkit-scrollbar {
  display: none;
}

.source-test-summary__line strong {
  color: rgb(var(--v-theme-primary));
  font-size: 0.9375rem;
}

.source-test-notice {
  font-size: 0.81rem;
  line-height: 1.4;
  margin-bottom: 2px !important;
}

.source-test-result-scroll {
  min-height: 0;
  flex: 1 1 auto;
  overflow-y: auto;
  overscroll-behavior: contain;
}

.source-test-tabs {
  flex: 0 0 auto;
  min-height: 38px;
  height: 38px;
  margin-top: 0;
  margin-bottom: 4px;
  border-bottom: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
}

.source-test-resource-tabs :deep(.v-slide-group__content) {
  min-height: 38px;
  height: 38px;
  align-items: flex-end;
}

.source-test-resource-tabs :deep(.v-tab) {
  height: 38px;
  min-height: 38px;
  min-width: 0;
  padding-inline: 12px;
}

.source-test-tabs :deep(.v-btn__content) {
  text-transform: none;
}

.source-test-result-list {
  padding: 0;
}

.source-test-result-item {
  min-height: 0 !important;
  height: auto !important;
  padding-top: 6px;
  padding-bottom: 6px;
}

.source-test-result-item :deep(.v-list-item__content) {
  align-self: stretch;
  min-width: 0;
}

.source-test-item-content {
  width: 100%;
  min-width: 0;
  position: relative;
  padding-right: 82px;
}

.source-test-item-title {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  line-height: 1.45;
}

.source-test-item-meta {
  display: flex;
  min-width: 0;
  align-items: center;
  flex-wrap: nowrap;
  gap: 6px;
  overflow: hidden;
  padding-top: 4px;
  padding-bottom: 2px;
  line-height: 1.4;
}

.source-test-item-meta > :deep(.v-chip),
.source-test-item-meta > span {
  flex: 0 0 auto;
}

.source-test-item-actions {
  position: absolute;
  top: 22px;
  right: 0;
  display: flex;
  align-items: center;
  gap: 2px;
}

</style>
