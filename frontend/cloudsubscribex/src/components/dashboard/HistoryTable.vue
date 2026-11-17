<template>
  <!-- v1.13.3 瘦页：失败记录扁平表——只列重试索引里的失败与跨盘成功待修复行，
       不再按媒体分组、不再展开子记录、不再有洗版/补发通知/Emby 播放入口。 -->
  <div class="history-table-root">
    <div class="history-toolbar">
      <div class="history-toolbar-head">
        <v-text-field
          v-if="!isMobile"
          v-model="keyword"
          class="history-search"
          placeholder="搜索标题或文件名"
          prepend-inner-icon="mdi-magnify"
          clearable
          density="compact"
          variant="outlined"
          hide-details
          @keyup.enter="submitSearch"
          @click:prepend-inner="submitSearch"
          @click:clear="clearSearch" />
        <div v-else class="history-search-trigger">
          <v-menu v-model="searchVisible" :close-on-content-click="false" location="bottom start" offset="6">
            <template #activator="{ props: menuProps }">
              <v-btn
                v-bind="menuProps"
                icon="mdi-magnify"
                :color="keyword ? 'primary' : undefined"
                variant="text"
                size="small"
                title="搜索失败记录" />
            </template>
            <v-card class="history-search-menu" elevation="8">
              <v-card-text class="pa-2">
                <v-text-field
                  v-model="keyword"
                  placeholder="搜索标题或文件名"
                  prepend-inner-icon="mdi-magnify"
                  clearable
                  autofocus
                  density="compact"
                  variant="outlined"
                  hide-details
                  @keyup.enter="submitSearch"
                  @click:prepend-inner="submitSearch"
                  @click:clear="clearSearch" />
              </v-card-text>
            </v-card>
          </v-menu>
        </div>
        <v-chip
          v-if="statusFilter"
          size="small"
          color="primary"
          variant="tonal"
          closable
          :title="`当前只看「${statusFilterLabel}」`"
          @click:close="clearStatusFilter">
          {{ statusFilterLabel }}
        </v-chip>
        <v-spacer />
        <v-btn
          class="retry-batch-button"
          color="primary"
          variant="text"
          size="small"
          :disabled="!retryableFailedCount"
          :title="retryableFailedCount ? `重试全部失败（${retryableFailedCount} 条）` : '没有可重试的失败记录'"
          @click.stop="retryAllFailed">
          <v-icon icon="mdi-replay" class="history-action-icon" />
          <span class="history-action-label">重试全部失败</span>
        </v-btn>
        <v-btn
          v-if="selectedRecords.length"
          class="retry-batch-button"
          color="primary"
          variant="text"
          size="small"
          :disabled="!retryableSelectedCount"
          :title="retryableSelectedCount ? `重试选中（${retryableSelectedCount} 条）` : '所选记录没有可重试项'"
          @click.stop="retrySelectedRecords">
          <v-icon icon="mdi-replay" class="history-action-icon" />
          <span class="history-action-label">重试选中</span>
        </v-btn>
        <v-btn
          v-if="selectedRecords.length"
          class="mark-success-button"
          color="warning"
          variant="text"
          size="small"
          :disabled="!manualSuccessSelectedCount"
          :title="manualSuccessSelectedCount ? `标记选中为成功（${manualSuccessSelectedCount} 条）` : '所选记录没有待修复或待确认项'"
          @click.stop="markSelectedSuccess">
          <v-icon icon="mdi-check-decagram-outline" class="history-action-icon" />
          <span class="history-action-label">标记成功</span>
        </v-btn>
        <v-btn
          class="delete-selected-button"
          color="error"
          variant="text"
          size="small"
          :disabled="!deletableSelectedRecords.length"
          :loading="deletingKey === 'batch'"
          @click.stop="deleteSelected">
          <v-icon icon="mdi-delete-outline" class="history-action-icon" />
          <span class="history-action-label">删除所选</span>
        </v-btn>
        <div class="history-actions">
          <v-btn variant="text" size="small" :loading="loading" title="刷新失败记录" @click.stop="emit('refresh')">
            <v-icon icon="mdi-refresh" class="history-action-icon" />
            <span class="history-action-label">刷新</span>
          </v-btn>
          <v-btn color="error" variant="text" size="small" title="清空失败记录" @click.stop="emit('clear')">
            <v-icon icon="mdi-delete-sweep" class="history-action-icon" />
            <span class="history-action-label">清空失败</span>
          </v-btn>
        </div>
      </div>
      <v-divider />
    </div>

    <div class="history-content">
      <div
        v-if="loading"
        :class="[
          'history-loading-mask',
          {
            'history-loading-mask--empty': !failureRecords.length,
            'history-loading-mask--mobile': isMobile,
            'history-loading-mask--mobile-pagination': isMobile && totalPages > 1,
          },
        ]">
        <div class="history-loading-state">
          <v-progress-circular indeterminate color="primary" size="42" width="4" />
          <span class="text-body-2">正在加载失败记录...</span>
        </div>
      </div>

      <v-data-table-server
        v-if="!isMobile"
        :headers="headers"
        :items="failureRecords"
        :items-length="total"
        :page="page"
        v-model="selectedRecordKeys"
        item-value="record_key"
        item-selectable="selectable"
        select-strategy="all"
        show-select
        density="compact"
        hover
        fixed-header
        :items-per-page="pageSize"
        :items-per-page-options="pageSizes"
        items-per-page-text="每页"
        page-text="第 {0}-{1} 条，共 {2} 条"
        no-data-text="暂无失败记录"
        loading-text="正在加载失败记录..."
        class="history-table"
        @update:page="changePage"
        @update:items-per-page="changePageSize">
        <template #item.media="{ item }">
          <div class="media-cell">
            <span class="media-title font-weight-medium" :title="displayTitle(item)">
              {{ displayTitle(item) }}
              <span v-if="item.year">({{ item.year }})</span>
            </span>
            <v-btn
              v-if="mediaDetailLink(item)"
              icon="mdi-open-in-new"
              variant="text"
              size="x-small"
              color="primary"
              title="查看媒体详情"
              class="media-link"
              @click.stop="openMediaDetail(item)" />
          </div>
          <div class="media-meta text-caption text-medium-emphasis" :title="item.file_name || ''">
            {{ item.file_name || "-" }}
          </div>
        </template>

        <template #item.resource_type="{ item }">
          <v-chip size="x-small" variant="tonal" :color="resourceTypeColor(resourceType(item))">
            {{ resourceTypeLabel(resourceType(item)) }}
          </v-chip>
        </template>

        <template #item.source="{ item }">
          <a
            v-if="item.source_link"
            :href="item.source_link"
            target="_blank"
            rel="noopener noreferrer"
            class="text-decoration-none"
            title="打开来源详情页"
            @click.stop>
            <v-chip
              size="x-small"
              variant="tonal"
              :color="sourceColor(item.source)"
              :prepend-icon="sourceIcon(item.source)"
              class="cursor-pointer">
              {{ sourceLabel(item.source) }}
              <v-icon icon="mdi-open-in-new" size="10" class="ml-1" />
            </v-chip>
          </a>
          <v-chip
            v-else
            size="x-small"
            variant="tonal"
            :color="sourceColor(item.source)"
            :prepend-icon="sourceIcon(item.source)">
            {{ sourceLabel(item.source) }}
          </v-chip>
        </template>

        <template #item.status="{ item }">
          <v-chip :color="statusColor(item)" size="x-small" variant="tonal">
            {{ statusLabel(item) }}
            <v-tooltip
              v-if="item.failure_reason || item.failure_label"
              activator="parent"
              location="top"
              max-width="380"
            >
              {{ item.failure_reason || item.failure_label }}
            </v-tooltip>
          </v-chip>
          <div v-if="item.stage_label" class="text-caption text-medium-emphasis mt-1">
            {{ item.stage_label }}
          </div>
          <div v-if="item.is_cross_transfer" class="mt-1">
            <v-chip size="x-small" color="info" variant="tonal">
              跨盘
              <v-tooltip v-if="item.cross_transfer_title" activator="parent" location="top">
                {{ item.cross_transfer_title }}
              </v-tooltip>
            </v-chip>
          </div>
        </template>

        <template #item.failure_reason="{ item }">
          <span
            v-if="recordFailureReason(item)"
            class="failure-reason-cell"
            :title="item.failure_reason || item.failure_label || ''">
            {{ recordFailureReason(item) }}
          </span>
          <span v-else class="text-medium-emphasis">-</span>
        </template>

        <template #item.actions="{ item }">
          <div class="action-buttons">
            <v-btn
              icon="mdi-reload"
              color="primary"
              variant="text"
              size="x-small"
              :disabled="!canRetryRecord(item)"
              :loading="retryingKey === historyRetryKey(item)"
              :title="retryTitle(item)"
              @click.stop="emit('retry', item)" />
            <v-btn
              icon="mdi-delete-outline"
              color="error"
              variant="text"
              size="x-small"
              :disabled="!canDeleteRecord(item)"
              :loading="deletingKey === historyRetryKey(item)"
              :title="deleteTitle(item)"
              @click.stop="emit('delete', item)" />
          </div>
        </template>
      </v-data-table-server>

      <div v-else class="history-mobile-list">
        <div class="history-mobile-scroll">
          <div v-if="!loading && !failureRecords.length" class="history-mobile-empty text-body-2 text-medium-emphasis">
            暂无失败记录
          </div>
          <div v-else class="history-mobile-panels">
            <v-card
              v-for="record in failureRecords"
              :key="recordKey(record)"
              :class="['history-mobile-panel', { 'history-group-selected': isRecordSelected(record) }]"
              variant="flat">
              <div class="history-mobile-record-head">
                <v-checkbox-btn
                  :model-value="isRecordSelected(record)"
                  :disabled="!record.selectable"
                  density="compact"
                  color="primary"
                  :aria-label="`选择 ${displayTitle(record)}`"
                  @click.stop
                  @update:model-value="selectRecord(record, $event)" />
                <div class="history-mobile-summary">
                  <div class="history-mobile-media-line">
                    <span class="history-mobile-media-title font-weight-medium">
                      {{ displayTitle(record) }}
                      <span v-if="record.year" class="text-medium-emphasis">({{ record.year }})</span>
                    </span>
                    <v-chip :color="statusColor(record)" size="x-small" variant="tonal">
                      {{ statusLabel(record) }}
                    </v-chip>
                    <v-spacer />
                    <div class="action-buttons history-mobile-actions">
                      <v-btn
                        icon="mdi-reload"
                        color="primary"
                        variant="text"
                        size="x-small"
                        :disabled="!canRetryRecord(record)"
                        :loading="retryingKey === historyRetryKey(record)"
                        :title="retryTitle(record)"
                        @click.stop="emit('retry', record)" />
                      <v-btn
                        icon="mdi-delete-outline"
                        color="error"
                        variant="text"
                        size="x-small"
                        :disabled="!canDeleteRecord(record)"
                        :loading="deletingKey === historyRetryKey(record)"
                        :title="deleteTitle(record)"
                        @click.stop="emit('delete', record)" />
                    </div>
                  </div>
                  <div v-if="recordFailureReason(record)" class="history-mobile-failure text-caption text-error">
                    失败原因：{{ recordFailureReason(record) }}
                  </div>
                  <div class="history-mobile-record-footer">
                    <div class="history-mobile-record-meta text-caption text-medium-emphasis">
                      <span>{{ resourceTypeLabel(resourceType(record)) }}</span>
                      <span>{{ sourceLabel(record.source) }}</span>
                      <span>{{ formatSize(record.file_size) }}</span>
                      <span>{{ record.time || "-" }}</span>
                    </div>
                  </div>
                  <div
                    v-if="record.file_name"
                    class="history-mobile-file text-caption text-medium-emphasis"
                    :title="record.file_name">
                    {{ record.file_name }}
                  </div>
                </div>
              </div>
            </v-card>
          </div>
        </div>
        <v-pagination
          v-if="totalPages > 1"
          :model-value="page"
          :length="totalPages"
          :total-visible="5"
          density="compact"
          class="history-mobile-pagination"
          @update:model-value="changePage" />
      </div>
    </div>
  </div>
</template>

<script setup>
import {computed, ref, watch} from "vue";
import {useDisplay} from "vuetify";
import {
  getResourceTypeName,
  getSourceColor,
  getSourceIcon,
  getSourceName,
  getTypeColor,
} from "../../composables/resourceUtils";
import {
  statusColor as statusColorOf,
  statusText,
} from "../../utils/status.js";

const props = defineProps({
  api: {type: Object, default: () => ({})},
  items: {type: Array, default: () => []},
  page: {type: Number, default: 1},
  pageSize: {type: Number, default: 10},
  total: {type: Number, default: 0},
  totalPages: {type: Number, default: 1},
  loading: Boolean,
  retryingKey: {type: String, default: ""},
  deletingKey: {type: String, default: ""},
  // 顶部统计卡片下发的行状态过滤："" 全部 / "失败" / "成功"（待修复）/ "处理中"
  statusFilter: {type: String, default: ""},
})
const emit = defineEmits([
  "refresh",
  "clear",
  "retry",
  "retry-failed",
  "retry-selected",
  "mark-success",
  "delete",
  "delete-records",
  "selection-change",
  "query-change",
  "open-media",
])
const display = useDisplay();
const isMobile = computed(() => display.smAndDown.value || (typeof window !== "undefined" && window.innerWidth <= 768));
const keyword = ref("");
const appliedKeyword = ref("");
const selectedRecordKeys = ref([]);
const searchVisible = ref(false);
let lastQuerySignature = "";

const pageSizes = [
  {value: 10, title: "10"},
  {value: 20, title: "20"},
  {value: 50, title: "50"},
]
const headers = [
  {title: "媒体", key: "media", sortable: false, width: "28%"},
  {title: "类型", key: "resource_type", sortable: false, width: "8%"},
  {title: "来源", key: "source", sortable: false, width: "10%"},
  {title: "大小", key: "file_size", sortable: false, width: "9%"},
  {title: "状态", key: "status", sortable: false, width: "10%"},
  {title: "失败原因", key: "failure_reason", sortable: false, width: "18%"},
  {title: "时间", key: "time", width: 160},
  {title: "", key: "actions", sortable: false, width: 88},
]

// 跨盘成功待修复行 status 是“成功”，展示成“待修复”避免和真成功混淆。
function isRepair(record) {
  return String(record?.status || "") === "成功" && record?.landed === false;
}

function statusLabel(record) {
  if (isRepair(record)) return "待修复";
  return statusText(record?.status);
}

function statusColor(record) {
  if (isRepair(record)) return "warning";
  // 秒传未命中不是真失败：单独琥珀色，避免和红色失败混在一起。
  if (record?.status === "失败" && record?.failure_kind === "checksum_miss") return "orange-lighten-1";
  return statusColorOf(record?.status);
}

const statusFilterLabel = computed(() =>
  props.statusFilter === "成功" ? "待修复" : (props.statusFilter || ""),
);

const failureRecords = computed(() => {
  const list = Array.isArray(props.items) ? props.items : [];
  return list.map((record) => ({
    ...record,
    record_key: recordKey(record),
    selectable: true,
  }));
});
const recordKeys = computed(() => new Set(failureRecords.value.map((record) => record.record_key)));
const selectedRecords = computed(() => {
  const keys = new Set(selectedRecordKeys.value);
  return failureRecords.value.filter((record) => keys.has(record.record_key));
});
const deletableSelectedRecords = computed(() =>
  selectedRecords.value.filter((record) => canDeleteRecord(record)),
);
const retryableFailedCount = computed(() =>
  failureRecords.value.filter(
    (record) => record?.status === "失败" && Boolean(record?.can_retry),
  ).length,
);
const retryableSelectedCount = computed(() =>
  selectedRecords.value.filter((record) => Boolean(record?.can_retry)).length,
);
// 可手动标记成功的来源：待修复行（跨盘成功 landed=False）与待确认/处理中/下载中。
const MANUAL_SUCCESS_STATUSES = ["待确认", "处理中", "下载中"]
const manualSuccessSelectedCount = computed(
  () =>
    selectedRecords.value.filter((record) =>
      isRepair(record) || MANUAL_SUCCESS_STATUSES.includes(String(record?.status || "")),
    ).length,
)

function emitQueryChange(overrides = {}) {
  const query = {
    page: props.page,
    pageSize: props.pageSize,
    keyword: appliedKeyword.value,
    status: props.statusFilter,
    ...overrides,
  }
  const signature = JSON.stringify(query);
  if (signature === lastQuerySignature) return;
  lastQuerySignature = signature;
  selectedRecordKeys.value = [];
  emit("query-change", query);
}

function changePage(value) {
  emitQueryChange({page: Math.max(1, Number(value) || 1)});
}

function changePageSize(value) {
  emitQueryChange({
    page: 1,
    pageSize: Math.min(50, Math.max(1, Number(value) || 10)),
  })
}

function submitSearch() {
  const nextKeyword = String(keyword.value || "").trim();
  appliedKeyword.value = nextKeyword;
  searchVisible.value = false;
  emitQueryChange({page: 1, keyword: nextKeyword});
}

function clearSearch() {
  keyword.value = "";
  appliedKeyword.value = "";
  emitQueryChange({page: 1, keyword: ""});
}

function clearStatusFilter() {
  emitQueryChange({page: 1, status: ""});
}

watch(
  () => props.page,
  () => {
    selectedRecordKeys.value = [];
  },
)

watch(recordKeys, (keys) => {
  selectedRecordKeys.value = selectedRecordKeys.value.filter((key) => keys.has(key));
})

watch(
  selectedRecords,
  (records) => {
    emit("selection-change", {
      groupCount: records.length,
      subscribeIds: uniqueOptions(
        records.map((record) => Number(record.subscribe_id || 0)).filter((value) => value > 0),
      ),
      targets: records.map((record) => ({
        tmdb_id: Number(record.tmdb_id || 0),
        media_type: record.type || "",
        title: record.title || "",
        year: record.year || "",
        seasons: record.season ? [Number(record.season)] : [],
        season_episodes: historySeasonEpisodes(records),
      })),
    });
  },
  {immediate: true},
)

function historySeasonEpisodes(records) {
  const episodes = {};
  for (const record of records || []) {
    const season = Number(record?.season || 0);
    const episode = Number(record?.episode || 0);
    if (season <= 0 || episode <= 0) continue;
    const key = String(season);
    if (!episodes[key]) episodes[key] = [];
    episodes[key].push(episode);
  }
  return Object.fromEntries(
    Object.entries(episodes).map(([season, values]) => [season, uniqueOptions(values).sort((a, b) => a - b)]),
  )
}

function uniqueOptions(values) {
  return [...new Set(values.filter(Boolean))];
}

function normalizeSource(value) {
  const normalized =
    String(value || "unknown")
      .trim()
      .toLowerCase() || "unknown"
  return ["manual", "手动添加", "手动资源"].includes(normalized) ? "manual" : normalized;
}

function sourceLabel(value) {
  return getSourceName(normalizeSource(value));
}

function sourceColor(value) {
  return getSourceColor(normalizeSource(value));
}

function sourceIcon(value) {
  return getSourceIcon(normalizeSource(value));
}

function resourceType(record) {
  if (typeof record === "string") return record.trim().toLowerCase();
  const configured = String(record?.resource_type || "")
    .trim()
    .toLowerCase()
  return configured || "unknown";
}

function resourceTypeLabel(value) {
  return getResourceTypeName(resourceType(value));
}

function resourceTypeColor(value) {
  return getTypeColor(resourceType(value));
}

function displayTitle(record) {
  const title = String(record?.title || record?.file_name || "未知").trim();
  const season = Number(record?.season || 0);
  const episode = Number(record?.episode || 0);
  if (season > 0 && episode > 0) {
    return `${title} S${String(season).padStart(2, "0")}E${String(episode).padStart(2, "0")}`;
  }
  return title;
}

function mediaDetailLink(item) {
  if (!item?.tmdb_id) return "";
  return [
    `#/media?mediaid=tmdb:${encodeURIComponent(String(item.tmdb_id))}`,
    `title=${encodeURIComponent(item.title || "")}`,
    `year=${encodeURIComponent(item.year || "")}`,
    `type=${encodeURIComponent(item.type || "")}`,
  ].join("&")
}

function openMediaDetail(item) {
  const link = mediaDetailLink(item);
  if (link) emit("open-media", link);
}

function recordFailureReason(record) {
  if (!record || record.status !== "失败") return "";
  const label = String(record.failure_label || "").trim();
  if (label) return label;
  const raw = String(record.failure_reason || "").trim();
  if (!raw) return "未记录原因";
  return raw.length > 16 ? `${raw.slice(0, 16)}…` : raw;
}

function formatSize(bytes) {
  let value = Number(bytes || 0);
  if (!value) return "-";
  for (const unit of ["B", "KB", "MB", "GB", "TB"]) {
    if (value < 1024 || unit === "TB") return `${value.toFixed(value >= 100 ? 0 : 1)} ${unit}`;
    value /= 1024;
  }
  return "-";
}

function recordKey(record, index = 0) {
  return [record?.time, record?.share_url, record?.file_name, index].join("|");
}

function historyRetryKey(record) {
  return [record?.time, record?.share_url, record?.file_name].join("|");
}

function isRecordSelected(record) {
  return selectedRecordKeys.value.includes(record.record_key);
}

function selectRecord(record, selected) {
  if (!record?.selectable) return;
  const keys = new Set(selectedRecordKeys.value);
  if (selected) keys.add(record.record_key);
  else keys.delete(record.record_key);
  selectedRecordKeys.value = [...keys];
}

function deleteSelected() {
  if (!deletableSelectedRecords.value.length) return;
  emit("delete-records", {
    recordCount: deletableSelectedRecords.value.length,
    records: deletableSelectedRecords.value,
  })
}

function retryAllFailed() {
  const records = failureRecords.value.filter(
    (record) => record?.status === "失败" && Boolean(record?.can_retry),
  );
  if (!records.length) return;
  emit("retry-failed", { records });
}

function retrySelectedRecords() {
  const records = selectedRecords.value.filter((record) => Boolean(record?.can_retry));
  if (!records.length) return;
  emit("retry-selected", {
    recordCount: selectedRecords.value.length,
    records,
  });
}

function markSelectedSuccess() {
  const records = selectedRecords.value.filter((record) =>
    isRepair(record) || MANUAL_SUCCESS_STATUSES.includes(String(record?.status || "")),
  );
  if (!records.length) return;
  emit("mark-success", { records });
}

function canRetryRecord(record) {
  return Boolean(record?.can_retry);
}

function retryTitle(record) {
  return record?.retry_title || "当前记录无需重试";
}

function canDeleteRecord(record) {
  // 处理中行正在重试，删了会丢重试结果；等它落定再删。
  return String(record?.status || "") !== "处理中";
}

function deleteTitle(record) {
  if (!canDeleteRecord(record)) return "任务仍在处理，完成后才能删除";
  return "删除此条失败记录（网盘文件与 STRM 保留）";
}
</script>

<style scoped>
.history-table-root {
  display: flex;
  flex: 1 1 auto;
  min-width: 0;
  min-height: 0;
  flex-direction: column;
  overflow: hidden;
  background: rgb(var(--v-theme-surface));
}

.history-content {
  position: relative;
  display: flex;
  flex: 1 1 auto;
  min-width: 0;
  min-height: 0;
  flex-direction: column;
  overflow: hidden;
}

.history-loading-mask {
  position: absolute;
  z-index: 5;
  top: 48px;
  right: 0;
  bottom: 56px;
  left: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(var(--v-theme-surface), 0.76);
  backdrop-filter: blur(1px);
}

.history-loading-mask--empty,
.history-loading-mask--mobile {
  top: 0;
  bottom: 0;
}

.history-loading-mask--mobile-pagination {
  bottom: 48px;
}

.history-loading-state {
  display: flex;
  align-items: center;
  flex-direction: column;
  gap: 12px;
  color: rgb(var(--v-theme-primary));
}

.history-toolbar {
  flex: 0 0 auto;
  background: rgb(var(--v-theme-surface));
}

.history-toolbar-head {
  display: flex;
  align-items: center;
  min-width: 0;
  gap: 4px;
  padding: 6px 8px;
}

.history-search {
  width: min(260px, 28vw);
  min-width: 180px;
  flex: 0 1 260px;
}

.history-search-trigger {
  display: flex;
  align-items: center;
}

.history-search-menu {
  width: min(340px, calc(100vw - 24px));
}

.history-actions {
  display: flex;
  align-items: center;
  flex: 0 0 auto;
  gap: 2px;
}

.history-actions :deep(.v-btn) {
  white-space: nowrap;
}

.history-action-icon {
  margin-right: 6px;
}

.history-toolbar :deep(.v-field) {
  font-size: 0.85rem;
}

.history-table {
  flex: 1 1 auto;
  min-height: 0;
}

.history-table :deep(.v-data-table__th) {
  font-weight: 600;
}

.media-cell {
  min-width: 0;
}

.media-title {
  display: inline-block;
  overflow: hidden;
  max-width: 100%;
  text-overflow: ellipsis;
  vertical-align: middle;
  white-space: nowrap;
}

.media-link {
  vertical-align: middle;
}

.media-meta {
  overflow: hidden;
  max-width: 100%;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.failure-reason-cell {
  display: inline-block;
  overflow: hidden;
  max-width: 100%;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.action-buttons {
  display: flex;
  align-items: center;
  gap: 2px;
}

.history-mobile-list {
  display: flex;
  flex: 1 1 auto;
  min-height: 0;
  flex-direction: column;
  overflow: hidden;
}

.history-mobile-scroll {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
}

.history-mobile-empty {
  padding: 32px 16px;
  text-align: center;
}

.history-mobile-panels {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 8px;
}

.history-mobile-panel {
  border: 1px solid rgba(var(--v-theme-on-surface), 0.12);
  border-radius: 8px;
  padding: 8px 10px;
}

.history-group-selected {
  border-color: rgb(var(--v-theme-primary));
  background: rgba(var(--v-theme-primary), 0.06);
}

.history-mobile-record-head {
  display: flex;
  align-items: flex-start;
  gap: 4px;
}

.history-mobile-summary {
  display: flex;
  min-width: 0;
  flex: 1 1 auto;
  flex-direction: column;
  gap: 4px;
}

.history-mobile-media-line {
  display: flex;
  align-items: center;
  min-width: 0;
  gap: 6px;
}

.history-mobile-media-title {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.history-mobile-actions {
  flex: 0 0 auto;
}

.history-mobile-failure {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.history-mobile-record-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.history-mobile-record-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.history-mobile-file {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.history-mobile-pagination {
  flex: 0 0 auto;
  padding: 4px 0 8px;
}
</style>
