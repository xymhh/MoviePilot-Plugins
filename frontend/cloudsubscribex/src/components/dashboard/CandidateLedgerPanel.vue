<template>
  <div class="ledger-panel">
    <div class="ledger-toolbar">
      <div class="ledger-filters">
        <v-select
          v-model="days"
          :items="dayOptions"
          item-title="title"
          item-value="value"
          density="compact"
          variant="outlined"
          hide-details
          class="filter-days"
          label="时间范围" />
        <v-select
          v-model="kind"
          :items="kindOptions"
          item-title="title"
          item-value="value"
          density="compact"
          variant="outlined"
          hide-details
          class="filter-kind"
          label="原因类型" />
        <v-text-field
          v-model="keyword"
          density="compact"
          variant="outlined"
          hide-details
          clearable
          class="filter-keyword"
          placeholder="搜索片名 / 链接 / 原因"
          prepend-inner-icon="mdi-magnify"
          @keyup.enter="reload" />
        <v-btn
          color="primary"
          variant="tonal"
          size="small"
          prepend-icon="mdi-refresh"
          :loading="loading"
          @click="reload">
          刷新
        </v-btn>
      </div>
      <div class="ledger-summary text-caption">
        最近 {{ days }} 天共 <strong>{{ summary.total }}</strong> 条候选未转存记录
      </div>
    </div>

    <div class="ledger-chips">
      <v-chip
        v-for="item in kindChips"
        :key="item.kind"
        size="small"
        :color="item.color"
        variant="tonal"
        class="mr-2 mb-1">
        {{ item.label }} {{ item.count }}
      </v-chip>
      <v-chip v-if="!kindChips.length" size="small" variant="text">
        暂无记录
      </v-chip>
    </div>

    <v-alert
      v-if="duplicateTotal > 0"
      type="info"
      variant="tonal"
      density="compact"
      class="mb-2">
      <div class="dup-row">
        <span>
          同步时发现 <strong>{{ duplicateTotal }}</strong> 张重复订阅卡片（同一部剧被订阅多次），
          同步只会处理最早创建的那张。
        </span>
        <v-btn
          size="small"
          color="warning"
          variant="flat"
          :loading="pausing"
          @click="pauseDuplicates">
          暂停多余卡片
        </v-btn>
      </div>
      <div class="text-caption mt-1 dup-detail">
        <span v-for="group in duplicateGroups" :key="group.keep_id" class="dup-group">
          {{ group.name || `#${group.keep_id}` }}：保留 #{{ group.keep_id }}，多余
          {{ (group.duplicates || []).map((item) => `#${item.id}`).join("、") }}
        </span>
      </div>
    </v-alert>

    <v-alert
      v-if="errorMessage"
      type="warning"
      variant="tonal"
      density="compact"
      class="mb-2">
      {{ errorMessage }}
    </v-alert>

    <v-table density="compact" class="ledger-table">
      <thead>
        <tr>
          <th class="col-time">时间</th>
          <th class="col-kind">原因</th>
          <th class="col-provider">网盘</th>
          <th>资源标题</th>
          <th class="col-ref">链接</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="(row, index) in items" :key="`${row.ts}-${index}`">
          <td class="text-caption text-medium-emphasis">{{ formatTime(row.ts) }}</td>
          <td>
            <v-chip size="x-small" :color="kindColor(row.kind)" variant="tonal">
              {{ kindLabel(row.kind) }}
            </v-chip>
          </td>
          <td class="text-caption">{{ row.provider || "-" }}</td>
          <td class="col-title">
            <div class="title-text" :title="row.resource_title">{{ row.resource_title || "-" }}</div>
            <div v-if="row.reason" class="reason-text">{{ row.reason }}</div>
          </td>
          <td class="col-ref">
            <a
              v-if="row.reference"
              :href="row.reference"
              target="_blank"
              rel="noopener"
              class="ref-link"
              :title="row.reference">
              {{ shortRef(row.reference) }}
            </a>
            <span v-else class="text-medium-emphasis">-</span>
          </td>
        </tr>
        <tr v-if="!items.length && !loading">
          <td colspan="5" class="empty-cell text-medium-emphasis">
            最近 {{ days }} 天没有候选被跳过或被拒的记录。
          </td>
        </tr>
      </tbody>
    </v-table>

    <div class="ledger-footer text-caption">
      <span>
        缓存健康度：命中 {{ cacheStats.hit_rate ?? 0 }}% ·
        编码跳过 {{ cacheStats.encode_skip ?? 0 }} ·
        解码失败 {{ cacheStats.decode_fail ?? 0 }}
      </span>
    </div>

    <div v-if="total > pageSize" class="ledger-pager">
      <v-btn
        size="small"
        variant="text"
        :disabled="page === 1 || loading"
        prepend-icon="mdi-chevron-left"
        @click="goPage(page - 1)">
        上一页
      </v-btn>
      <span class="text-caption">{{ page }} / {{ totalPages }}</span>
      <v-btn
        size="small"
        variant="text"
        :disabled="page >= totalPages || loading"
        append-icon="mdi-chevron-right"
        @click="goPage(page + 1)">
        下一页
      </v-btn>
    </div>
  </div>
</template>

<script setup>
import {computed, onMounted, ref, watch} from "vue";

const props = defineProps({
  api: {type: Object, default: () => ({})},
  pluginId: {type: String, default: "CloudSubscribeX"},
});

const days = ref(3);
const kind = ref("");
const keyword = ref("");
const items = ref([]);
const total = ref(0);
const page = ref(1);
const pageSize = 100;
const summary = ref({total: 0, by_kind: {}});
const loading = ref(false);
const errorMessage = ref("");
const duplicateGroups = ref([]);
const duplicateTotal = ref(0);
const pausing = ref(false);
const cacheStats = ref({});

const dayOptions = [
  {title: "今天", value: 1},
  {title: "最近 3 天", value: 3},
  {title: "最近 7 天", value: 7},
  {title: "最近 14 天", value: 14},
];

const kindLabels = {
  invalid: "分享失效",
  empty: "无可用文件",
  mismatch: "内容不匹配",
};
const kindColors = {
  invalid: "error",
  empty: "warning",
  mismatch: "info",
};

const kindOptions = computed(() => [
  {title: "全部原因", value: ""},
  ...Object.entries(kindLabels).map(([value, title]) => ({title, value})),
]);

const kindChips = computed(() =>
  Object.entries(summary.value.by_kind || {})
    .sort((a, b) => b[1] - a[1])
    .map(([key, count]) => ({
      kind: key,
      count,
      label: kindLabels[key] || key,
      color: kindColors[key] || "primary",
    })),
);

const totalPages = computed(() => Math.max(1, Math.ceil(total.value / pageSize)));

function kindLabel(value) {
  return kindLabels[value] || value || "未知";
}

function kindColor(value) {
  return kindColors[value] || "primary";
}

function formatTime(text) {
  const raw = String(text || "");
  if (raw.length < 19) return raw || "-";
  return `${raw.slice(5, 10)} ${raw.slice(11, 16)}`;
}

function shortRef(text) {
  const raw = String(text || "");
  return raw.length > 36 ? `${raw.slice(0, 34)}…` : raw;
}

async function request(path, params) {
  const query = new URLSearchParams();
  Object.entries(params || {}).forEach(([key, value]) => {
    if (value !== "" && value !== undefined && value !== null) {
      query.set(key, String(value));
    }
  });
  const suffix = query.toString() ? `?${query.toString()}` : "";
  const response = await props.api.get(
    `plugin/${props.pluginId}/${path}${suffix}`,
  );
  return response?.data !== undefined ? response.data : response;
}

async function loadSummary() {
  try {
    const data = await request("ledger/summary", {days: days.value});
    summary.value = data || {total: 0, by_kind: {}};
  } catch (error) {
    summary.value = {total: 0, by_kind: {}};
  }
}

async function loadPage() {
  loading.value = true;
  errorMessage.value = "";
  try {
    const data = await request("ledger/list", {
      days: days.value,
      kind: kind.value,
      keyword: keyword.value,
      limit: pageSize,
      offset: (page.value - 1) * pageSize,
    });
    items.value = Array.isArray(data?.items) ? data.items : [];
    total.value = Number(data?.total || 0);
  } catch (error) {
    items.value = [];
    total.value = 0;
    errorMessage.value = String(error?.message || error);
  } finally {
    loading.value = false;
  }
}

async function loadDuplicates() {
  try {
    const data = await request("duplicate/list", {});
    duplicateGroups.value = Array.isArray(data?.groups) ? data.groups : [];
    duplicateTotal.value = Number(data?.duplicate_total || 0);
  } catch (error) {
    duplicateGroups.value = [];
    duplicateTotal.value = 0;
  }
}

async function loadCacheStats() {
  try {
    cacheStats.value = await request("cache/stats", {});
  } catch (error) {
    cacheStats.value = {};
  }
}

async function pauseDuplicates() {
  pausing.value = true;
  try {
    const response = await props.api.post(
      `plugin/${props.pluginId}/duplicate/pause`,
      { all: true },
    );
    const payload = response?.data !== undefined ? response.data : response;
    errorMessage.value = payload?.message || "已提交暂停请求";
    await Promise.all([loadDuplicates(), loadPage()]);
  } catch (error) {
    errorMessage.value = String(error?.message || error);
  } finally {
    pausing.value = false;
  }
}

async function reload() {
  page.value = 1;
  await Promise.all([
    loadSummary(),
    loadPage(),
    loadDuplicates(),
    loadCacheStats(),
  ]);
}

function goPage(target) {
  const next = Math.min(Math.max(1, target), totalPages.value);
  if (next === page.value) return;
  page.value = next;
  loadPage();
}

watch([days, kind], () => {
  reload();
});

onMounted(() => {
  reload();
});
</script>

<style scoped>
.ledger-panel {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 4px 2px 12px;
  min-height: 0;
}

.ledger-toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.ledger-filters {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.filter-days {
  width: 130px;
}

.filter-kind {
  width: 150px;
}

.filter-keyword {
  width: 240px;
}

.ledger-chips {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
}

.ledger-table {
  width: 100%;
}

.col-time {
  width: 84px;
  white-space: nowrap;
}

.col-kind {
  width: 104px;
}

.col-provider {
  width: 82px;
  white-space: nowrap;
}

.col-ref {
  width: 220px;
}

.col-title .title-text {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 420px;
}

.col-title .reason-text {
  font-size: 11px;
  opacity: 0.72;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 420px;
}

.ref-link {
  color: rgb(var(--v-theme-primary));
  text-decoration: none;
  font-size: 12px;
}

.ref-link:hover {
  text-decoration: underline;
}

.empty-cell {
  text-align: center;
  padding: 24px 0;
}

.dup-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

.dup-detail {
  opacity: 0.8;
}

.dup-group {
  display: inline-block;
  margin-right: 10px;
}

.ledger-footer {
  opacity: 0.7;
  padding: 2px 2px 0;
}

.ledger-pager {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
}
</style>
