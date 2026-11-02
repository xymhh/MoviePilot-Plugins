<template>
  <v-dialog
    :model-value="modelValue"
    max-width="720"
    scrollable
    content-class="doctor-dialog-overlay"
    @update:model-value="emit('update:modelValue', $event)">
    <v-card class="doctor-dialog">
      <v-card-title class="doctor-header d-flex align-center ga-2 px-4 py-3">
        <v-icon :icon="overallIcon" :color="overallColor" size="small" />
        <span class="doctor-title">插件体检</span>
        <v-chip v-if="checkedAt" size="x-small" variant="tonal" :color="overallColor">
          {{ overallText }}
        </v-chip>
        <v-spacer />
        <span v-if="checkedAt" class="doctor-meta">{{ checkedAt }} · {{ elapsedMs }} ms</span>
      </v-card-title>

      <v-card-text class="doctor-body">
        <div v-if="loading" class="doctor-loading">
          <v-progress-circular indeterminate size="20" width="2" />
          <span>正在逐项检查…</span>
        </div>
        <template v-else>
          <div v-if="error" class="doctor-error">{{ error }}</div>
          <div v-for="item in items" :key="item.key" :class="['doctor-row', `is-${item.status}`]">
            <span :class="['doctor-dot', `is-${item.status}`]" />
            <div class="doctor-row-body">
              <div class="doctor-row-head">
                <span class="doctor-name">{{ item.name }}</span>
                <span :class="['doctor-status', `is-${item.status}`]">{{ statusText(item.status) }}</span>
                <span class="doctor-ms">{{ item.ms }}ms</span>
              </div>
              <div class="doctor-detail">{{ item.detail }}</div>
              <div v-if="item.hint" class="doctor-hint">
                <v-icon icon="mdi-lightbulb-on-outline" size="12" />
                <span>{{ item.hint }}</span>
              </div>
            </div>
          </div>
        </template>
      </v-card-text>

      <v-card-actions class="doctor-actions px-4 py-2">
        <span class="doctor-legend">
          <i class="doctor-dot is-ok" />正常
          <i class="doctor-dot is-warn" />注意
          <i class="doctor-dot is-error" />故障
          <i class="doctor-dot is-skip" />跳过
        </span>
        <v-spacer />
        <v-btn variant="text" size="small" :loading="loading" @click="run">重新体检</v-btn>
        <v-btn variant="tonal" size="small" color="primary" @click="emit('update:modelValue', false)">关闭</v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>
</template>

<script setup>
import {computed, ref, watch} from "vue";

const props = defineProps({
  modelValue: Boolean,
  api: {type: [Object, Function], required: true},
});
const emit = defineEmits(["update:modelValue"]);
const pluginId = "CloudSubscribeX";

const loading = ref(false);
const error = ref("");
const items = ref([]);
const checkedAt = ref("");
const elapsedMs = ref(0);
const overall = ref("ok");

const STATUS_TEXT = {ok: "正常", warn: "注意", error: "故障", skip: "跳过"};
const statusText = (status) => STATUS_TEXT[status] || status;

const overallColor = computed(() => ({ok: "success", warn: "warning", error: "error"}[overall.value] || "success"));
const overallIcon = computed(() => ({
  ok: "mdi-check-decagram-outline",
  warn: "mdi-alert-outline",
  error: "mdi-close-octagon-outline",
}[overall.value] || "mdi-check-decagram-outline"));
const overallText = computed(() => ({ok: "全部正常", warn: "有需要注意的项", error: "有故障项"}[overall.value] || ""));

async function run() {
  loading.value = true;
  error.value = "";
  try {
    const result = await props.api.get(`plugin/${pluginId}/doctor`);
    if (result?.success === false) {
      error.value = result.message || "体检接口返回失败";
      items.value = [];
      return;
    }
    const data = result?.data || {};
    items.value = Array.isArray(data.items) ? data.items : [];
    checkedAt.value = data.checked_at || "";
    elapsedMs.value = data.elapsed_ms || 0;
    overall.value = data.overall || "ok";
  } catch (err) {
    error.value = `体检请求失败：${err?.message || err}`;
    items.value = [];
  } finally {
    loading.value = false;
  }
}

watch(() => props.modelValue, (open) => {
  if (open && !items.value.length && !loading.value) run();
});
</script>

<style scoped>
.doctor-header { border-bottom: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); }
.doctor-title { font-size: 15px; font-weight: 600; }
.doctor-meta { font-size: 11px; opacity: 0.6; }
.doctor-body { padding: 8px 0 !important; }
.doctor-loading { display: flex; align-items: center; gap: 10px; padding: 24px; font-size: 13px; opacity: 0.75; }
.doctor-error { margin: 8px 16px; padding: 8px 10px; border-radius: 6px; background: rgba(var(--v-theme-error), 0.1); color: rgb(var(--v-theme-error)); font-size: 12px; }
.doctor-row { display: flex; gap: 10px; padding: 9px 16px; align-items: flex-start; }
.doctor-row + .doctor-row { border-top: 1px solid rgba(var(--v-border-color), 0.5); }
.doctor-row-body { flex: 1; min-width: 0; }
.doctor-row-head { display: flex; align-items: center; gap: 8px; }
.doctor-name { font-size: 13px; font-weight: 600; }
.doctor-status { font-size: 11px; padding: 0 6px; border-radius: 4px; line-height: 17px; }
.doctor-ms { margin-left: auto; font-size: 10px; opacity: 0.45; font-variant-numeric: tabular-nums; }
.doctor-detail { font-size: 12px; opacity: 0.78; margin-top: 2px; word-break: break-all; }
.doctor-hint { display: flex; align-items: flex-start; gap: 4px; margin-top: 3px; font-size: 11px; color: rgb(var(--v-theme-warning)); }
.doctor-dot { width: 8px; height: 8px; border-radius: 50%; margin-top: 6px; flex: 0 0 auto; display: inline-block; }
.doctor-dot.is-ok, .doctor-status.is-ok { background: rgb(var(--v-theme-success)); }
.doctor-status.is-ok { color: #fff; }
.doctor-dot.is-warn, .doctor-status.is-warn { background: rgb(var(--v-theme-warning)); }
.doctor-status.is-warn { color: #000; }
.doctor-dot.is-error, .doctor-status.is-error { background: rgb(var(--v-theme-error)); }
.doctor-status.is-error { color: #fff; }
.doctor-dot.is-skip, .doctor-status.is-skip { background: rgba(var(--v-border-color), 0.9); }
.doctor-status.is-skip { color: rgba(var(--v-theme-on-surface), 0.7); }
.doctor-actions { border-top: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); }
.doctor-legend { display: flex; align-items: center; gap: 6px; font-size: 11px; opacity: 0.6; }
.doctor-legend .doctor-dot { margin-top: 0; margin-right: 2px; }
</style>
