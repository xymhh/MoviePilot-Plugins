<template>
  <!-- v1.31.0 重复扫描：同一集多份 .strm 的发现、核对与删除。
       扫描只读本地 .strm 文件（零网盘调用）；核对大小按 115 列表取视频
       实际大小；删除人工确认、逐项核对后才删（视频 + STRM + 同名 NFO）。 -->
  <div class="duplicates-queue">
    <v-card class="cs-card-clean mb-4">
      <v-card-title class="d-flex align-center flex-wrap ga-2">
        <span>重复扫描</span>
        <v-chip v-if="scanned" size="small" variant="tonal">{{ groups.length }} 组重复</v-chip>
        <v-chip v-if="enriching" size="small" color="info" variant="tonal">正在核对视频大小…（{{ enrichDone }}/{{ enrichTotal }} 组）</v-chip>
        <v-spacer />
        <v-btn size="small" variant="tonal" color="primary" :loading="scanning" @click="scanNow">
          开始扫描
        </v-btn>
        <v-btn
          size="small"
          variant="tonal"
          :loading="enriching"
          :disabled="!scanned || !groups.length"
          @click="enrichNow"
        >
          重新核对大小
        </v-btn>
        <v-btn
          v-if="sameVideoGroups.length"
          size="small"
          variant="tonal"
          color="error"
          :loading="batchDeleting"
          :disabled="scanning || enriching"
          @click="deleteSameVideoBatch"
        >
          批量删除同视频组（{{ sameVideoGroups.length }} 组）
        </v-btn>
      </v-card-title>
      <v-card-text class="pt-0 text-medium-emphasis">
        扫描递归走 .strm 根目录，只读本地文件，不访问网盘；三种文件名算同一集
        （没标签 / 带 {tmdb-} 标签 / 带 .mp4 后缀）。核对后按“留大”给出默认保留建议，
        删除前请确认保留哪一份。
        <span v-if="scanned">
          已扫 {{ dirsScanned }} 个目录、{{ strmCount }} 个 STRM，发现 {{ groups.length }} 组重复；
          根目录：{{ strmRoot || "—" }}。
        </span>
        <span v-if="scanned && savedAt">
          上次扫描 {{ formatSavedAt(savedAt) }} · {{ groups.length }} 组重复。
        </span>
        <span v-if="scanned && !groups.length">没有发现重复，可以放心。</span>
      </v-card-text>
    </v-card>

    <v-alert v-if="error" type="error" variant="tonal" class="mb-4" closable @click:close="error = ''">
      {{ error }}
    </v-alert>
    <v-alert v-if="message" type="success" variant="tonal" class="mb-4" closable @click:close="message = ''">
      {{ message }}
    </v-alert>

    <v-card v-for="group in groups" :key="group.key" class="cs-card-clean mb-4">
      <v-card-title class="d-flex align-center flex-wrap ga-2">
        <span>{{ group.title }}</span>
        <span v-if="group.season_episode" class="text-medium-emphasis">{{ group.season_episode }}</span>
        <v-chip size="small" :color="kindColor(group)" variant="tonal">{{ kindLabel(group) }}</v-chip>
        <v-chip size="small" variant="tonal">{{ group.items.length }} 份</v-chip>
        <v-spacer />
        <v-btn
          size="small"
          variant="tonal"
          color="error"
          :disabled="group.kind === 'unknown' || enriching || scanning || deletingKey === group.key"
          :loading="deletingKey === group.key"
          @click="deleteGroup(group)"
        >
          删除重复
        </v-btn>
      </v-card-title>
      <v-card-text class="pt-0">
        <div v-if="group.kind === 'unknown'" class="text-medium-emphasis mb-2">
          读不到 pickcode，无法确认视频身份，不能删除——请人工核对后手动处理。
        </div>
        <div v-if="group.kind === 'different-video' && group.suggested_keep === null" class="text-medium-emphasis mb-2">
          视频大小未知，无法按“留大”建议，请人工选择保留哪一份。
        </div>
        <v-radio-group
          :model-value="keepByKey[group.key]"
          density="compact"
          class="pa-0 ma-0"
          @update:model-value="(value) => onKeepChange(group, value)"
        >
          <v-list density="compact" class="pa-0">
            <v-list-item v-for="(item, index) in group.items" :key="item.file_name" class="px-0">
              <template #prepend>
                <v-radio :value="index" />
              </template>
              <v-list-item-title>
                {{ item.file_name }}
                <v-chip v-if="index === keepByKey[group.key]" size="small" color="success" variant="tonal" class="ml-2">
                  保留
                </v-chip>
              </v-list-item-title>
              <v-list-item-subtitle>
                STRM {{ formatSize(item.size) }} · pickcode {{ item.pickcode_prefix || "—" }}
                <span v-if="item.video_name"> · 视频 {{ item.video_name }}</span>
                <span v-if="item.video_size !== null && item.video_size !== undefined">
                  · 视频大小 {{ formatSize(item.video_size) }}
                </span>
                <span v-else-if="item.kind === 'redirect'"> · 视频大小核对中/查不到</span>
                <span v-else-if="item.kind === 'share'"> · 分享视频（不在自己网盘）</span>
              </v-list-item-subtitle>
            </v-list-item>
          </v-list>
        </v-radio-group>
      </v-card-text>
    </v-card>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from "vue";

const props = defineProps({
  api: { type: [Object, Function], default: () => ({}) },
  pluginId: { type: String, default: "CloudSubscribeX" },
  navKey: { type: String, default: "duplicates" },
  active: { type: Boolean, default: true },
  onClose: { type: Function, default: null },
});

const scanning = ref(false);
const enriching = ref(false);
const enrichDone = ref(0);
const enrichTotal = ref(0);
const deletingKey = ref("");
const batchDeleting = ref(false);
const error = ref("");
const message = ref("");
const groups = ref([]);
const keepByKey = ref({});
const strmRoot = ref("");
const dirsScanned = ref(0);
const strmCount = ref(0);
const unreadable = ref(0);
const savedAt = ref(0);
const scanned = ref(false);

function unwrap(result) {
  if (result && typeof result === "object" && "data" in result) return result.data;
  return result;
}

function formatSize(bytes) {
  const value = Number(bytes) || 0;
  if (value >= 1024 * 1024 * 1024) return `${(value / (1024 * 1024 * 1024)).toFixed(2)} GB`;
  if (value >= 1024 * 1024) return `${(value / (1024 * 1024)).toFixed(1)} MB`;
  if (value >= 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${value} B`;
}

function kindLabel(group) {
  if (!group) return "";
  if (group.kind === "same-video") return "同一视频";
  if (group.kind === "different-video") return "不同视频";
  return "无法确认";
}

function kindColor(group) {
  if (!group) return undefined;
  if (group.kind === "same-video") return "info";
  if (group.kind === "different-video") return "warning";
  return "error";
}

// v1.31.3：批量删除只针对同视频组——删的只是多余 STRM + 同名 NFO，
// 视频不动，最安全的一批；不同视频组要逐组确认保留哪一份，不批量。
const sameVideoGroups = computed(() =>
  groups.value.filter((group) => group.kind === "same-video"),
);

function formatSavedAt(seconds) {
  const value = Number(seconds) || 0;
  if (!value) return "—";
  return new Date(value * 1000).toLocaleString("zh-CN", { hour12: false });
}

async function scanNow() {
  scanning.value = true;
  error.value = "";
  message.value = "";
  try {
    const result = unwrap(await props.api.post(`plugin/${props.pluginId}/duplicates/scan`, {}));
    if (result && result.success === false) throw new Error(result.message || "扫描失败");
    const data = result || {};
    groups.value = data.groups || [];
    strmRoot.value = data.strm_root || "";
    dirsScanned.value = data.dirs_scanned || 0;
    strmCount.value = data.strm_count || 0;
    unreadable.value = data.unreadable || 0;
    savedAt.value = data.saved_at || 0;
    scanned.value = true;
    message.value = `扫描完成：${dirsScanned.value} 个目录、${strmCount.value} 个 STRM，${groups.value.length} 组重复`;
    if (groups.value.length) await enrichNow();
  } catch (err) {
    error.value = err && err.message ? err.message : String(err);
  } finally {
    scanning.value = false;
  }
}

// v1.31.2：扫描状态持久化——扫描结果 + 核对进度 + 保留选择落插件
// 私有库（runtime_records 命名空间 strm_duplicates），离开页面回来
// 可恢复。扫描成功后服务端已存一份初始状态；核对每批合并后前端再存
// 一次；删除后重新扫描即覆盖保存。保存失败不阻塞主流程。
function buildState() {
  return {
    groups: groups.value,
    strm_root: strmRoot.value,
    dirs_scanned: dirsScanned.value,
    strm_count: strmCount.value,
    unreadable: unreadable.value,
    keep_by_key: keepByKey.value,
  };
}

async function saveState(force = false) {
  if (!scanned.value || (!groups.value.length && !force)) return;
  try {
    const result = unwrap(
      await props.api.post(`plugin/${props.pluginId}/duplicates/save-state`, {
        state: buildState(),
      }),
    );
    if (result && result.saved_at) savedAt.value = result.saved_at;
  } catch (err) {
    // 保存失败不阻塞核对/删除主流程，下次保存再试。
  }
}

// 保留选择改动也要落库：只在用户点选时触发（恢复时直接写 keepByKey，
// 不走这里，不会一打开页面就重写 saved_at）；防抖 800ms，连续点选只存一次。
let saveTimer = null;
function onKeepChange(group, value) {
  keepByKey.value[group.key] = value;
  if (saveTimer) clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    saveState();
  }, 800);
}

async function restoreState() {
  try {
    const result = unwrap(
      await props.api.post(`plugin/${props.pluginId}/duplicates/load-state`, {}),
    );
    if (result && result.success === false) return;
    const state = (result || {}).state;
    if (!state || !Array.isArray(state.groups) || !state.groups.length) return;
    groups.value = state.groups;
    strmRoot.value = state.strm_root || "";
    dirsScanned.value = state.dirs_scanned || 0;
    strmCount.value = state.strm_count || 0;
    unreadable.value = state.unreadable || 0;
    savedAt.value = state.saved_at || 0;
    keepByKey.value = { ...(state.keep_by_key || {}) };
    for (const group of groups.value) {
      if (keepByKey.value[group.key] === undefined) {
        keepByKey.value[group.key] = group.suggested_keep ?? 0;
      }
    }
    scanned.value = true;
  } catch (err) {
    // 读不到保存的状态就当没有，不阻塞页面。
  }
}

onMounted(restoreState);

// v1.31.1：核对改分片——全库 93 组一次 POST 会在反代 120 秒读超时里
// 失败；每批 10 组顺序请求（不并行，避免 115 限流与超时叠加），
// 单批失败重试一次，仍失败则记下该批继续下一批，不中断整轮。
const ENRICH_BATCH_SIZE = 10;

async function enrichBatch(batch) {
  const result = unwrap(
    await props.api.post(`plugin/${props.pluginId}/duplicates/enrich`, { groups: batch }),
  );
  if (result && result.success === false) throw new Error(result.message || "核对失败");
  return (result || {}).groups || [];
}

async function enrichNow() {
  if (!groups.value.length) return;
  // 只核对尚未核对的组；全部已核对时（显式点“重新核对大小”）全量重核。
  const pending = groups.value.filter((group) => group.sizes_pending);
  const targets = pending.length ? pending : groups.value.slice();
  enriching.value = true;
  error.value = "";
  enrichDone.value = 0;
  enrichTotal.value = targets.length;
  const failedKeys = [];
  try {
    const byKey = {};
    for (const group of groups.value) byKey[group.key] = group;
    let done = 0;
    for (let start = 0; start < targets.length; start += ENRICH_BATCH_SIZE) {
      const batch = targets.slice(start, start + ENRICH_BATCH_SIZE);
      let enriched = null;
      for (let attempt = 0; attempt < 2 && enriched === null; attempt += 1) {
        try {
          enriched = await enrichBatch(batch);
        } catch (err) {
          enriched = null;
        }
      }
      if (enriched === null) {
        failedKeys.push(...batch.map((group) => group.key));
        continue;
      }
      for (const group of enriched) {
        byKey[group.key] = group;
        if (keepByKey.value[group.key] === undefined) {
          keepByKey.value[group.key] = group.suggested_keep ?? 0;
        }
      }
      // 按原顺序合并回 groups（只替换核对过的组，失败的组保留原样），
      // 每批合并后保存一次：离开页面回来可从这批继续核对。
      groups.value = groups.value.map((group) => byKey[group.key] || group);
      done += enriched.length;
      enrichDone.value = done;
      await saveState();
    }
    if (failedKeys.length) {
      error.value = `核对完成，${failedKeys.length} 组失败（大小未知），可点“重新核对大小”重试`;
    }
  } finally {
    enriching.value = false;
    enrichDone.value = 0;
    enrichTotal.value = 0;
  }
}

async function deleteGroup(group) {
  error.value = "";
  message.value = "";
  const keepIndex = keepByKey.value[group.key] ?? group.suggested_keep ?? 0;
  const keepItem = group.items[keepIndex];
  const dropItems = group.items.filter((_, index) => index !== keepIndex);
  const lines = [
    `确定删除“${group.title}${group.season_episode ? ` ${group.season_episode}` : ""}”的重复吗？`,
    `保留：${keepItem ? keepItem.file_name : "—"}`,
    `删除 ${dropItems.length} 份：${dropItems.map((item) => item.file_name).join("；")}`,
    group.kind === "same-video"
      ? "（同一视频，只删多余 STRM + 同名 NFO，视频不动）"
      : "（不同视频：视频 + STRM + 同名 NFO 一起删；分享视频不在自己网盘，只删本地 STRM）",
    "删后不可恢复。",
  ];
  if (!window.confirm(lines.join("\n"))) return;
  deletingKey.value = group.key;
  try {
    const result = unwrap(
      await props.api.post(`plugin/${props.pluginId}/duplicates/delete`, {
        group,
        keep_index: keepIndex,
      }),
    );
    if (result && result.success === false) throw new Error(result.message || "删除失败");
    const data = result || {};
    const results = data.results || [];
    const failed = results.filter((item) => !item.success);
    const deletedCount = results.filter((item) => item.success && !item.kept).length;
    if (failed.length) {
      error.value = `删除 ${deletedCount} 份，${failed.length} 份失败：${failed
        .map((item) => `${item.file_name}（${item.message}）`)
        .join("；")}`;
    } else {
      message.value = `已删除 ${deletedCount} 份重复`;
    }
    await scanNow();
  } catch (err) {
    error.value = err && err.message ? err.message : String(err);
  } finally {
    deletingKey.value = "";
  }
}

// v1.31.3：批量删除全部同视频组——一次请求发给批量接口，后端逐组
// 复用单组删除校验（重读 STRM 核对 pickcode、find_file_strict、
// playback 核对），单组失败不中断整批。成功删掉的组直接从列表里
// 移除并保存状态（不重扫整库）；失败的组留在列表里，原因写进提示。
async function deleteSameVideoBatch() {
  const targets = sameVideoGroups.value;
  if (!targets.length) return;
  error.value = "";
  message.value = "";
  const lines = [
    `确定批量删除 ${targets.length} 组同视频重复吗？`,
    "只删多余 STRM + 同名 NFO，视频不动；保留选择按每组当前选中的那一份。",
    "删后不可恢复。",
  ];
  if (!window.confirm(lines.join("\n"))) return;
  batchDeleting.value = true;
  try {
    const items = targets.map((group) => ({
      group,
      keep_index: keepByKey.value[group.key] ?? group.suggested_keep ?? 0,
    }));
    const result = unwrap(
      await props.api.post(`plugin/${props.pluginId}/duplicates/delete-batch`, { items }),
    );
    const data = result || {};
    const results = data.results || [];
    const deletedKeys = results.filter((item) => item.success).map((item) => item.key);
    const failed = results.filter((item) => !item.success);
    if (deletedKeys.length) {
      groups.value = groups.value.filter((group) => !deletedKeys.includes(group.key));
      for (const key of deletedKeys) delete keepByKey.value[key];
      // 组删空了也强制保存一次空状态，否则 saveState 的非空守卫会让
      // 下次打开页面恢复出已删掉的旧组。
      await saveState(!groups.value.length);
    }
    if (failed.length) {
      error.value = `批量删除完成：删 ${deletedKeys.length} 组，${failed.length} 组失败：${failed
        .map((item) => `${item.title || item.key}（${item.message}）`)
        .join("；")}`;
    } else {
      message.value = `批量删除完成：删 ${deletedKeys.length} 组同视频重复`;
    }
  } catch (err) {
    error.value = err && err.message ? err.message : String(err);
  } finally {
    batchDeleting.value = false;
  }
}
</script>
