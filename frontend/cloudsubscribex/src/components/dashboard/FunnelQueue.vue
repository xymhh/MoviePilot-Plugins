<template>
  <!-- v1.29.7 阶段 1：整理队列页——队列列表（待处理/处理中/已挂起三组）+ 手动触发 + 停止。
       轮次历史与挂起认领是阶段 4 的事，这里不做。 -->
  <div class="funnel-queue">
    <v-card class="cs-card-clean mb-4">
      <v-card-title class="d-flex align-center flex-wrap ga-2">
        <span>整理队列</span>
        <v-chip v-if="workerRunning" size="small" color="success" variant="tonal"> worker 运行中 </v-chip>
        <v-chip v-else size="small" variant="tonal"> worker 未运行 </v-chip>
        <v-chip v-if="!phase1Enabled" size="small" color="warning" variant="tonal">
          阶段 1 未启用（手动提交走旧流水线）
        </v-chip>
        <v-chip v-else-if="!phase2Enabled" size="small" color="warning" variant="tonal">
          阶段 2 未启用（TG 走旧流水线，直出 strm 路仍在）
        </v-chip>
        <v-chip v-else-if="!phase3Enabled" size="small" color="warning" variant="tonal">
          阶段 3 未启用（订阅与洗版走旧流水线）
        </v-chip>
        <v-chip v-else size="small" color="success" variant="tonal"> 阶段 3 已启用（订阅与洗版已进漏斗） </v-chip>
        <v-spacer />
        <v-btn size="small" variant="tonal" :loading="running" @click="runNow"> 立即整理一轮 </v-btn>
        <v-btn size="small" variant="tonal" color="error" :loading="stopping" @click="stopQueue"> 停止 </v-btn>
        <v-btn size="small" variant="text" icon="mdi-refresh" :loading="loading" @click="load" />
      </v-card-title>
      <v-card-text class="pt-0 text-medium-emphasis">
        手动提交进入整理队列后由漏斗逐行整理：整理前改名 → 落盘前实查 → MP 整理 → 终态提交。
        <span v-if="phase1Enabled && phase2Enabled">TG 自动转存与频道手动转存也进入整理队列（直出 strm 路已关闭）。</span>
        <span v-if="phase1Enabled && phase2Enabled && phase3Enabled">订阅轮次与洗版替换也进入整理队列。</span>
        目标目录：{{ targetDir || "—" }}；待处理 {{ counts.queued || 0 }} 行，
        处理中 {{ counts.processing || 0 }} 行，已挂起 {{ counts.held || 0 }} 行。
      </v-card-text>
    </v-card>

    <v-alert v-if="error" type="error" variant="tonal" class="mb-4" closable @click:close="error = ''">
      {{ error }}
    </v-alert>
    <v-alert v-if="message" type="success" variant="tonal" class="mb-4" closable @click:close="message = ''">
      {{ message }}
    </v-alert>

    <v-card v-for="group in groups" :key="group.key" class="cs-card-clean mb-4">
      <v-card-title class="d-flex align-center">
        <span>{{ group.title }}</span>
        <v-chip size="small" variant="tonal" class="ml-2">{{ group.rows.length }}</v-chip>
      </v-card-title>
      <v-card-text class="pt-0">
        <div v-if="!group.rows.length" class="text-medium-emphasis">暂无</div>
        <v-list v-else density="compact" class="pa-0">
          <v-list-item v-for="row in group.rows" :key="row.id" class="px-0">
            <template #prepend>
              <v-chip size="small" :color="row.is_upgrade ? 'warning' : 'info'" variant="tonal">
                {{ sourceLabel(row) }}
              </v-chip>
            </template>
            <v-list-item-title class="funnel-row-title">
              {{ row.title }}
              <span v-if="row.season_episode" class="text-medium-emphasis">{{ row.season_episode }}</span>
            </v-list-item-title>
            <v-list-item-subtitle>
              {{ row.file_name }}
              <span v-if="row.attempts"> · 已试 {{ row.attempts }} 次</span>
              <span v-if="row.next_retry_at"> · 下次重试 {{ formatTime(row.next_retry_at) }}</span>
              <span v-if="row.reason"> · {{ row.reason }}</span>
            </v-list-item-subtitle>
            <template #append>
              <v-btn
                v-if="group.key === 'held'"
                size="small"
                variant="tonal"
                class="mr-2"
                @click="requeueRow(row)"
              >
                放回队列
              </v-btn>
              <v-btn size="small" variant="text" color="error" @click="deleteRow(row)"> 删除 </v-btn>
            </template>
          </v-list-item>
        </v-list>
      </v-card-text>
    </v-card>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from "vue";

const props = defineProps({
  api: { type: Object, required: true },
  active: { type: Boolean, default: false },
  pluginId: { type: String, default: "CloudSubscribeX" },
});

const loading = ref(false);
const running = ref(false);
const stopping = ref(false);
const error = ref("");
const message = ref("");
const counts = ref({});
const phase1Enabled = ref(false);
const phase2Enabled = ref(false);
const phase3Enabled = ref(false);
const targetDir = ref("");
const workerRunning = ref(false);
const queued = ref([]);
const processing = ref([]);
const held = ref([]);
const groups = computed(() => [
  { key: "queued", title: "待处理", rows: queued.value },
  { key: "processing", title: "处理中", rows: processing.value },
  { key: "held", title: "已挂起（待人工复核）", rows: held.value },
]);

let timer = null;

function unwrap(result) {
  if (result && typeof result === "object" && "data" in result) return result.data;
  return result;
}

function formatTime(epochSeconds) {
  if (!epochSeconds) return "立即";
  try {
    return new Date(epochSeconds * 1000).toLocaleString();
  } catch {
    return String(epochSeconds);
  }
}

// v1.29.8 阶段 2：行来源标签——TG 行显示频道名（source 即 Candidate.via()，
// 如 tg:@QukanMovie/12281 或 push），洗版/手动与阶段 1 一致。
// v1.29.9 阶段 3：订阅轮次行 source 为 subscribe:<id>，显示“订阅”。
function sourceLabel(row) {
  if (!row) return "手动";
  if (row.is_upgrade || row.source === "upgrade") return "洗版";
  const source = String(row.source || "");
  if (source === "push") return "TG推送";
  if (source.startsWith("tg:")) {
    const channel = source.slice(3).split("/")[0];
    return channel || "TG";
  }
  if (source.startsWith("subscribe:")) return "订阅";
  return "手动";
}

async function load() {
  loading.value = true;
  error.value = "";
  try {
    const rowsResult = unwrap(await props.api.get(`plugin/${props.pluginId}/funnel/rows`));
    if (rowsResult && rowsResult.success === false) throw new Error(rowsResult.message || "读取失败");
    const data = rowsResult || {};
    counts.value = data.counts || {};
    phase1Enabled.value = !!data.phase1_enabled;
    phase2Enabled.value = !!data.phase2_enabled;
    phase3Enabled.value = !!data.phase3_enabled;
    targetDir.value = data.target_dir || "";
    queued.value = (data.groups || {}).queued || [];
    processing.value = (data.groups || {}).processing || [];
    held.value = (data.groups || {}).held || [];
    const statusResult = unwrap(await props.api.get(`plugin/${props.pluginId}/funnel/status`));
    workerRunning.value = !!(statusResult && statusResult.worker_running);
  } catch (err) {
    error.value = err && err.message ? err.message : String(err);
  } finally {
    loading.value = false;
  }
}

async function runNow() {
  running.value = true;
  error.value = "";
  message.value = "";
  try {
    const result = unwrap(await props.api.post(`plugin/${props.pluginId}/funnel/run`, {}));
    if (result && result.success === false) throw new Error(result.message || "触发失败");
    message.value = (result && result.message) || "已触发整理一轮";
    await load();
  } catch (err) {
    error.value = err && err.message ? err.message : String(err);
  } finally {
    running.value = false;
  }
}

async function stopQueue() {
  stopping.value = true;
  error.value = "";
  message.value = "";
  try {
    const result = unwrap(await props.api.post(`plugin/${props.pluginId}/funnel/stop`, {}));
    if (result && result.success === false) throw new Error(result.message || "停止失败");
    message.value = (result && result.message) || "已请求停止";
    await load();
  } catch (err) {
    error.value = err && err.message ? err.message : String(err);
  } finally {
    stopping.value = false;
  }
}

async function requeueRow(row) {
  error.value = "";
  message.value = "";
  try {
    const result = unwrap(
      await props.api.post(`plugin/${props.pluginId}/funnel/requeue`, { ids: [row.id] }),
    );
    if (result && result.success === false) throw new Error(result.message || "放回失败");
    message.value = (result && result.message) || "已放回队列";
    await load();
  } catch (err) {
    error.value = err && err.message ? err.message : String(err);
  }
}

async function deleteRow(row) {
  error.value = "";
  message.value = "";
  try {
    const result = unwrap(
      await props.api.post(`plugin/${props.pluginId}/funnel/delete`, { ids: [row.id] }),
    );
    if (result && result.success === false) throw new Error(result.message || "删除失败");
    message.value = (result && result.message) || "已删除";
    await load();
  } catch (err) {
    error.value = err && err.message ? err.message : String(err);
  }
}

onMounted(() => {
  load();
  timer = setInterval(load, 20000);
});

onUnmounted(() => {
  if (timer) clearInterval(timer);
});
</script>

<style scoped>
.funnel-queue {
  max-width: 960px;
  margin: 0 auto;
}
.funnel-row-title {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
