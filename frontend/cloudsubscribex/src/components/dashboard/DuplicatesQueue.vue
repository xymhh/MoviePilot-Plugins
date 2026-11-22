<template>
  <!-- v1.31.0 重复扫描：同一集多份 .strm 的发现、核对与删除。
       扫描只读本地 .strm 文件（零网盘调用）；核对大小按 115 列表取视频
       实际大小；删除人工确认、逐项核对后才删（视频 + STRM + 同名 NFO）。 -->
  <div class="duplicates-queue">
    <v-card class="cs-card-clean mb-4">
      <v-card-title class="d-flex align-center flex-wrap ga-2">
        <span>重复扫描</span>
        <v-chip v-if="scanned" size="small" variant="tonal">{{ groups.length }} 组重复</v-chip>
        <v-chip v-if="enriching" size="small" color="info" variant="tonal">正在核对视频大小…</v-chip>
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
      </v-card-title>
      <v-card-text class="pt-0 text-medium-emphasis">
        扫描递归走 .strm 根目录，只读本地文件，不访问网盘；三种文件名算同一集
        （没标签 / 带 {tmdb-} 标签 / 带 .mp4 后缀）。核对后按“留大”给出默认保留建议，
        删除前请确认保留哪一份。
        <span v-if="scanned">
          已扫 {{ dirsScanned }} 个目录、{{ strmCount }} 个 STRM，发现 {{ groups.length }} 组重复；
          根目录：{{ strmRoot || "—" }}。
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
          @update:model-value="(value) => (keepByKey[group.key] = value)"
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
import { ref } from "vue";

const props = defineProps({
  api: { type: [Object, Function], default: () => ({}) },
  pluginId: { type: String, default: "CloudSubscribeX" },
  navKey: { type: String, default: "duplicates" },
  active: { type: Boolean, default: true },
  onClose: { type: Function, default: null },
});

const scanning = ref(false);
const enriching = ref(false);
const deletingKey = ref("");
const error = ref("");
const message = ref("");
const groups = ref([]);
const keepByKey = ref({});
const strmRoot = ref("");
const dirsScanned = ref(0);
const strmCount = ref(0);
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
    scanned.value = true;
    message.value = `扫描完成：${dirsScanned.value} 个目录、${strmCount.value} 个 STRM，${groups.value.length} 组重复`;
    if (groups.value.length) await enrichNow();
  } catch (err) {
    error.value = err && err.message ? err.message : String(err);
  } finally {
    scanning.value = false;
  }
}

async function enrichNow() {
  if (!groups.value.length) return;
  enriching.value = true;
  error.value = "";
  try {
    const result = unwrap(
      await props.api.post(`plugin/${props.pluginId}/duplicates/enrich`, { groups: groups.value }),
    );
    if (result && result.success === false) throw new Error(result.message || "核对失败");
    const data = result || {};
    groups.value = data.groups || groups.value;
    for (const group of groups.value) {
      if (keepByKey.value[group.key] === undefined) {
        keepByKey.value[group.key] = group.suggested_keep ?? 0;
      }
    }
  } catch (err) {
    error.value = err && err.message ? err.message : String(err);
  } finally {
    enriching.value = false;
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
</script>
