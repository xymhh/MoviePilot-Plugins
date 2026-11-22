<template>
  <!-- v1.29.7 阶段 1：整理队列页——队列列表（待处理/处理中/已挂起三组）+ 手动触发 + 停止。
       v1.30.0 阶段 4：轮次历史、挂起认领、删孤儿文件、扫描对账 dry-run。 -->
  <div class="funnel-queue">
    <v-card class="cs-card-clean mb-4">
      <v-card-title class="d-flex align-center flex-wrap ga-2">
        <span>整理队列</span>
        <v-chip v-if="workerRunning" size="small" color="success" variant="tonal"> worker 运行中 </v-chip>
        <v-chip v-else size="small" variant="tonal"> worker 未运行 </v-chip>
        <v-chip v-if="!funnelEnabled" size="small" color="error" variant="tonal">
          总闸已关（全部回旧流水线）
        </v-chip>
        <v-chip v-else-if="!phase1Enabled" size="small" color="warning" variant="tonal">
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
        <v-btn size="small" variant="tonal" :loading="scanning" @click="scanNow"> 扫描对账 </v-btn>
        <v-btn size="small" variant="tonal" color="error" :loading="stopping" @click="stopQueue"> 停止 </v-btn>
        <v-btn size="small" variant="text" icon="mdi-refresh" :loading="loading" @click="load" />
      </v-card-title>
      <v-card-text class="pt-0 text-medium-emphasis">
        手动提交进入整理队列后由漏斗逐行整理：整理前改名 → 落盘前实查 → MP 整理 → 终态提交。
        <span v-if="phase1Enabled && phase2Enabled">TG 自动转存与频道手动转存也进入整理队列（直出 strm 路已关闭）。</span>
        <span v-if="phase1Enabled && phase2Enabled && phase3Enabled">订阅轮次与洗版替换也进入整理队列。</span>
        目标目录：{{ targetDir || "—" }}；待处理 {{ counts.queued || 0 }} 行，
        处理中 {{ counts.processing || 0 }} 行，已挂起 {{ counts.held || 0 }} 行。
        <span v-if="funnelScanDryRun">每天 04:00 扫描对账为 dry-run：只分类记日志，不入队、不删文件。</span>
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
              <v-btn
                v-if="group.key === 'held'"
                size="small"
                variant="tonal"
                class="mr-2"
                @click="openClaim(row)"
              >
                认领
              </v-btn>
              <v-btn
                v-if="row.has_old_version && (group.key === 'held' || row.status === 'failed')"
                size="small"
                variant="tonal"
                color="warning"
                class="mr-2"
                @click="deleteOldVersion(row)"
              >
                删旧版
              </v-btn>
              <v-btn
                v-if="row.has_orphan && group.key === 'held'"
                size="small"
                variant="tonal"
                color="warning"
                class="mr-2"
                @click="deleteOrphan(row)"
              >
                删孤儿
              </v-btn>
              <v-btn size="small" variant="text" color="error" @click="deleteRow(row)"> 删除 </v-btn>
            </template>
          </v-list-item>
        </v-list>
      </v-card-text>
    </v-card>

    <v-card class="cs-card-clean mb-4">
      <v-card-title class="d-flex align-center">
        <span>轮次历史</span>
        <v-chip size="small" variant="tonal" class="ml-2">{{ rounds.length }}</v-chip>
      </v-card-title>
      <v-card-text class="pt-0">
        <div v-if="!rounds.length" class="text-medium-emphasis">暂无（最近 20 轮，worker 与扫描轮次）</div>
        <v-list v-else density="compact" class="pa-0">
          <v-list-item v-for="round in rounds" :key="round.started_at + '-' + round.kind" class="px-0">
            <v-list-item-title>
              {{ round.kind === "scan" ? "扫描对账" : "整理轮次" }}
              <span class="text-medium-emphasis"> · {{ formatTime(round.finished_at || round.started_at) }}</span>
            </v-list-item-title>
            <v-list-item-subtitle>{{ roundSummary(round) }}</v-list-item-subtitle>
          </v-list-item>
        </v-list>
      </v-card-text>
    </v-card>

    <v-dialog v-model="claimDialog" max-width="480">
      <v-card>
        <v-card-title>认领挂起行</v-card-title>
        <v-card-text>
          <div class="text-medium-emphasis mb-3">{{ claimRow ? claimRow.file_name : "" }}</div>
          <v-text-field v-model="claimTmdbId" label="TMDB ID" type="number" density="compact" />
          <v-select
            v-model="claimMediaType"
            label="类型"
            :items="[
              { title: '剧集', value: 'tv' },
              { title: '电影', value: 'movie' },
            ]"
            density="compact"
          />
          <v-text-field
            v-if="claimMediaType === 'tv'"
            v-model="claimSeason"
            label="季号"
            type="number"
            density="compact"
          />
          <v-text-field
            v-if="claimMediaType === 'tv'"
            v-model="claimEpisode"
            label="集号"
            type="number"
            density="compact"
          />
          <div class="text-medium-emphasis">按 TMDB ID 直查媒体，查到后改写行身份并重新入队。</div>
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="claimDialog = false">取消</v-btn>
          <v-btn color="primary" :loading="claiming" @click="submitClaim">认领并重新入队</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>
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
const scanning = ref(false);
const error = ref("");
const message = ref("");
const counts = ref({});
const funnelEnabled = ref(true);
const funnelScanDryRun = ref(true);
const phase1Enabled = ref(false);
const phase2Enabled = ref(false);
const phase3Enabled = ref(false);
const targetDir = ref("");
const workerRunning = ref(false);
const queued = ref([]);
const processing = ref([]);
const held = ref([]);
const rounds = ref([]);
const claimDialog = ref(false);
const claimRow = ref(null);
const claimTmdbId = ref("");
const claimMediaType = ref("tv");
const claimSeason = ref(1);
const claimEpisode = ref("");
const claiming = ref(false);
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
    funnelEnabled.value = data.funnel_enabled !== false;
    funnelScanDryRun.value = !!data.funnel_scan_dry_run;
    phase1Enabled.value = !!data.phase1_enabled;
    phase2Enabled.value = !!data.phase2_enabled;
    phase3Enabled.value = !!data.phase3_enabled;
    targetDir.value = data.target_dir || "";
    queued.value = (data.groups || {}).queued || [];
    processing.value = (data.groups || {}).processing || [];
    held.value = (data.groups || {}).held || [];
    const statusResult = unwrap(await props.api.get(`plugin/${props.pluginId}/funnel/status`));
    workerRunning.value = !!(statusResult && statusResult.worker_running);
    const roundsResult = unwrap(await props.api.get(`plugin/${props.pluginId}/funnel/rounds`));
    rounds.value = (roundsResult && roundsResult.rounds) || [];
  } catch (err) {
    error.value = err && err.message ? err.message : String(err);
  } finally {
    loading.value = false;
  }
}

function roundSummary(round) {
  if (!round) return "";
  if (round.kind === "scan") {
    return (
      `扫描 ${round.scanned || 0} 包，可认领 ${round.recognizable || 0}，` +
      `待人工认领 ${round.unrecognizable || 0}，已入库跳过 ${round.already_in_library || 0}` +
      (round.dry_run === false ? "（非 dry-run）" : "（dry-run，未入队）")
    );
  }
  return (
    `处理 ${round.processed || 0} 行，完成 ${round.done || 0} 行，` +
    `失败 ${round.failed || 0} 行，挂起 ${round.held || 0} 行，跳过 ${round.skipped || 0} 行`
  );
}

async function scanNow() {
  scanning.value = true;
  error.value = "";
  message.value = "";
  try {
    const result = unwrap(await props.api.post(`plugin/${props.pluginId}/funnel/scan`, {}));
    if (result && result.success === false) throw new Error(result.message || "扫描失败");
    message.value = (result && result.message) || "扫描对账完成";
    await load();
  } catch (err) {
    error.value = err && err.message ? err.message : String(err);
  } finally {
    scanning.value = false;
  }
}

function openClaim(row) {
  claimRow.value = row;
  claimTmdbId.value = row.tmdb_id || "";
  claimMediaType.value = row.media_type === "movie" ? "movie" : "tv";
  claimSeason.value = row.season || 1;
  claimEpisode.value = row.episode || "";
  claimDialog.value = true;
}

async function submitClaim() {
  if (!claimRow.value) return;
  claiming.value = true;
  error.value = "";
  message.value = "";
  try {
    const payload = {
      id: claimRow.value.id,
      tmdb_id: Number(claimTmdbId.value),
      media_type: claimMediaType.value,
    };
    if (claimMediaType.value === "tv") {
      payload.season = Number(claimSeason.value) || 1;
      payload.episode = Number(claimEpisode.value) || 0;
    }
    const result = unwrap(await props.api.post(`plugin/${props.pluginId}/funnel/claim`, payload));
    if (result && result.success === false) throw new Error(result.message || "认领失败");
    message.value = (result && result.message) || "已认领并重新入队";
    claimDialog.value = false;
    await load();
  } catch (err) {
    error.value = err && err.message ? err.message : String(err);
  } finally {
    claiming.value = false;
  }
}

async function deleteOrphan(row) {
  error.value = "";
  message.value = "";
  const location = row.orphan_location || row.file_name;
  if (!window.confirm(`确定删除孤儿文件吗？\n${location}\n（.mkv 连带同名 .strm 一起删，删后不可恢复）`)) return;
  try {
    const result = unwrap(
      await props.api.post(`plugin/${props.pluginId}/funnel/delete-orphan`, { ids: [row.id] }),
    );
    if (result && result.success === false) throw new Error(result.message || "删孤儿失败");
    message.value = (result && result.message) || "已删孤儿文件";
    await load();
  } catch (err) {
    error.value = err && err.message ? err.message : String(err);
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

async function deleteOldVersion(row) {
  error.value = "";
  message.value = "";
  const count = row.old_version_count || 1;
  if (!window.confirm(`确定删除该行的 ${count} 个旧版文件吗？新版不受影响，删后不可恢复。`)) return;
  try {
    const result = unwrap(
      await props.api.post(`plugin/${props.pluginId}/funnel/delete-old-version`, { ids: [row.id] }),
    );
    if (result && result.success === false) throw new Error(result.message || "删旧版失败");
    message.value = (result && result.message) || "已删旧版";
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
