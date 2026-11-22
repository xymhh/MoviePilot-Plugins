<template>
  <v-dialog
    :model-value="modelValue"
    :fullscreen="isMobile"
    max-width="680"
    class="manual-resource-dialog"
    @update:model-value="close">
    <v-card rounded="lg" class="manual-resource-card">
      <v-card-title class="text-subtitle-1 d-flex align-center ga-2">
        <v-icon icon="mdi-link-variant-plus" color="primary" size="small" />
        手动添加
      </v-card-title>
      <v-card-text>
        <v-alert v-if="error" type="error" variant="tonal" density="compact" class="mb-3">{{ error }}</v-alert>
        <v-alert v-else-if="resolvedLabel" type="info" variant="tonal" density="compact" class="mb-3">{{ resolvedLabel }}</v-alert>

        <div v-if="manualNeeded" class="manual-media-box mb-3">
          <v-divider class="mb-3" />
          <div class="text-subtitle-2 mb-2">手动指定影片</div>
          <v-list v-if="manualCandidates.length" density="compact" class="manual-candidate-list mb-2">
            <v-list-item
              v-for="item in manualCandidates"
              :key="`${item.media_type}-${item.tmdb_id}`"
              :title="`${item.title} · ${item.media_type === 'movie' ? '电影' : '电视剧'}${item.year ? ` · ${item.year}` : ''}`"
              :subtitle="`TMDB ${item.tmdb_id}`"
              @click="selectManualCandidate(item)" />
          </v-list>
          <v-text-field
            v-model="manualTitle"
            label="片名"
            placeholder="例如：斗破苍穹剧场版 双帝之战"
            variant="outlined"
            density="compact"
            hide-details="auto"
            class="mb-2"
            :disabled="submitting || manualResolving" />
          <div class="d-flex ga-2">
            <v-text-field
              v-model="manualTmdbId"
              label="TMDB ID（可选）"
              placeholder="例如：1599184"
              inputmode="numeric"
              variant="outlined"
              density="compact"
              hide-details="auto"
              class="flex-grow-1"
              :disabled="submitting || manualResolving" />
            <v-select
              v-model="manualMediaType"
              :items="manualMediaTypeItems"
              label="类型"
              variant="outlined"
              density="compact"
              hide-details="auto"
              clearable
              style="max-width: 130px"
              :disabled="submitting || manualResolving" />
          </div>
          <v-btn
            color="primary"
            variant="tonal"
            size="small"
            class="mt-2"
            :loading="manualResolving"
            :disabled="submitting || (!manualTitle.trim() && !manualTmdbId.trim())"
            @click="resolveWithManualMedia">
            重新识别
          </v-btn>
        </div>

        <v-select
          v-if="resolvedMedia?.media_type === 'tv' && availableSeasons.length"
          v-model="seasons"
          :items="availableSeasons"
          multiple
          chips
          closable-chips
          :disabled="submitting || !availableSeasons.length"
          label="季"
          placeholder="留空默认全部季"
          variant="outlined"
          density="compact"
          hide-details="auto"
          class="mb-3"
          @update:model-value="updateSeasons" />

        <v-textarea
          :model-value="resourceLinks"
          :label="selectedCloudResource ? '网盘路径' : '资源链接'"
          placeholder="每行一个115分享、ED2K或Magnet链接"
          :hint="
            selectedCloudResource && !selectedCloudResourceAllowed
              ? '当前媒体类型未启用跨盘转存，请重新选择目标网盘路径。'
              : selectedCloudResource
                ? '目标盘路径直接整理；其他网盘路径通过跨盘转存后整理。'
                : '支持单个或多个资源包，单次最多50条；也可从右侧选择网盘路径。粘贴链接后自动识别媒体。'
          "
          persistent-hint
          auto-grow
          rows="5"
          variant="outlined"
          density="compact"
          :class="{ 'cloud-resource-input': selectedCloudResource }"
          :readonly="Boolean(selectedCloudResource)"
          :disabled="submitting"
          @update:model-value="updateResourceInput">
          <template #append-inner>
            <div class="resource-path-actions">
              <v-btn
                icon="mdi-folder-open"
                variant="text"
                size="small"
                title="选择网盘路径"
                :disabled="submitting || !selectableCloudDrives.length"
                @click.stop="cloudDirectoryVisible = true" />
              <v-btn
                v-if="selectedCloudResource"
                icon="mdi-delete-outline"
                variant="text"
                size="small"
                color="error"
                title="删除网盘路径"
                :disabled="submitting"
                @click.stop="clearCloudResource" />
            </div>
          </template>
        </v-textarea>
        <v-checkbox
          v-model="manualUpgrade"
          label="将手动资源作为洗版候选"
          density="compact"
          hide-details
          class="mt-2"
          :disabled="submitting" />
        <v-checkbox
          v-model="skipHistory"
          label="不记录到历史列表"
          density="compact"
          hide-details
          :disabled="submitting" />
      </v-card-text>
      <v-card-actions class="px-4 pb-3">
        <v-spacer />
        <v-btn variant="text" :disabled="submitting" @click="close(false)">取消</v-btn>
        <v-btn
          color="primary"
          prepend-icon="mdi-play"
          :loading="submitting"
          :disabled="!canSubmit"
          @click="submit">
          开始处理
        </v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>
  <DirectoryDialog
    v-model="cloudDirectoryVisible"
    :api="api"
    :plugin-id="pluginId"
    :provider="selectedCloudResource?.provider || targetCloudDrive"
    :target-provider="targetCloudDrive"
    :providers="selectableCloudDrives"
    :initial-path="selectedCloudResource?.path || '/'"
    title="选择待整理的网盘路径"
    :allow-create="false"
    @select="selectCloudPath" />
</template>

<script setup>
import {computed, ref, watch} from "vue";
import {useDisplay} from "vuetify";
import DirectoryDialog from "./DirectoryDialog.vue";

// v1.23.0：只留“粘贴分享链接 → sync/manual/resolve → 提交”；TMDB 搜索、
// 订阅选择、媒体洗版（media/servers + media/content 浏览）已删，洗版入口
// 本就无人调用（PluginWorkbench 只以 links 模式打开本弹窗）。
// 零/多候选时允许用户在弹窗内手动指定影片或点选候选（不另开搜索页）。
const props = defineProps({
  modelValue: Boolean,
  api: { type: [Object, Function], required: true },
  pluginId: { type: String, default: "CloudSubscribeX" },
})
const emit = defineEmits(["update:modelValue", "started"])
const display = useDisplay()
const isMobile = computed(() => display.xs.value)
const resourceLinks = ref("")
const cloudDrives = ref([]);
const targetCloudDrive = ref("");
const crossTransferMediaTypes = ref([]);
const selectedCloudResource = ref(null);
const cloudDirectoryVisible = ref(false);
const manualUpgrade = ref(false)
const skipHistory = ref(false);
// resolve 识别出的媒体（内部状态）：
// {subscribe_id, tmdb_id, media_type, title, year}，无匹配时为 null。
// resolve 返回零/多候选时无法自动定，改由下面的手动指定区让用户定
// （填片名/TMDB ID 重新识别，或直接点选候选）。
const resolvedMedia = ref(null)
const seasons = ref([]);
const availableSeasons = ref([]);
// 手动指定影片：resolve 零/多候选时出现；填好后重新调 resolve。
const manualNeeded = ref(false)
const manualCandidates = ref([])
const manualTitle = ref("")
const manualTmdbId = ref("")
const manualMediaType = ref("")
const manualResolving = ref(false)
const manualMediaTypeItems = [
  { title: "电影", value: "movie" },
  { title: "电视剧", value: "tv" },
]
const submitting = ref(false)
const error = ref("")
let cloudMediaResolveRevision = 0;
let resourceResolveTimer = null;
let resourceResolveRevision = 0;

const resolvedLabel = computed(() => {
  const media = resolvedMedia.value
  if (!media) return ""
  const type = media.media_type === "movie" ? "电影" : media.media_type === "tv" ? "电视剧" : ""
  const title = String(media.title || "").trim()
  const suffix = type ? ` · ${type}` : ""
  if (media.subscribe_id) return `已定位订阅：${title}${suffix}`
  return `已识别：${title}${suffix}`
})

const selectableCloudDrives = computed(() => {
  const mediaType = resolvedMedia.value?.media_type || "";
  if (!mediaType) return cloudDrives.value;
  return cloudDrives.value.filter((item) => item.mode !== "cross" || crossTransferMediaTypes.value.includes(mediaType));
})

const selectedCloudResourceAllowed = computed(
  () =>
    !selectedCloudResource.value ||
    selectableCloudDrives.value.some((item) => item.value === selectedCloudResource.value.provider),
)

const canSubmit = computed(() => {
  if (submitting.value) return false
  if (!resourceLinks.value.trim()) return false
  if (!selectedCloudResourceAllowed.value) return false;
  // 提交必须带已识别的媒体（订阅或 TMDB 单候选）；多候选/零候选时
  // resolve 已报错，resolvedMedia 为 null，提交保持禁用。
  return Boolean(resolvedMedia.value?.subscribe_id || resolvedMedia.value?.tmdb_id);
})

function normalizeSeasons(values) {
  const normalized = [];
  for (const value of Array.isArray(values) ? values : [values]) {
    for (const token of String(value ?? "").split(/[,，\s]+/)) {
      const season = Number(token);
      if (Number.isInteger(season) && season > 0 && season <= 999 && !normalized.includes(season)) {
        normalized.push(season);
      }
    }
  }
  return normalized.sort((left, right) => left - right);
}

function updateSeasons(value) {
  seasons.value = normalizeSeasons(value);
}

function unwrap(result) {
  return result?.data && typeof result.data === "object" && "success" in result.data ? result.data : result || {}
}

function cloudResourceLabel(resource) {
  const provider = cloudDrives.value.find((item) => item.value === resource?.provider);
  return `网盘路径：[${provider?.title || resource?.provider || "目标网盘"}] ${resource?.path || "/"}`;
}

function updateResourceInput(value) {
  const text = String(value || "");
  if (selectedCloudResource.value && text !== cloudResourceLabel(selectedCloudResource.value)) {
    selectedCloudResource.value = null;
  }
  resourceLinks.value = text;
  clearManualState()
  if (!selectedCloudResource.value) scheduleResourceTargetResolve(text);
}

function clearResolvedMedia() {
  resolvedMedia.value = null
  seasons.value = []
  availableSeasons.value = []
}

function clearManualState() {
  manualNeeded.value = false
  manualCandidates.value = []
  manualTitle.value = ""
  manualTmdbId.value = ""
  manualMediaType.value = ""
}

function scheduleResourceTargetResolve(text) {
  if (resourceResolveTimer) window.clearTimeout(resourceResolveTimer);
  const revision = ++resourceResolveRevision;
  if (!/(?:https?:\/\/|ed2k:\/\/|magnet:\?)/i.test(text)) {
    clearResolvedMedia()
    return;
  }
  resourceResolveTimer = window.setTimeout(() => {
    resourceResolveTimer = null;
    void resolveResourceTarget(text, revision);
  }, 450)
}

// resolve 只返回一种可用媒体：已定位的订阅，或 TMDB 唯一候选；
// 多候选/零候选无法自动定：先记下手动指定区要用的状态再报错——
// 报错文案照旧（用户还没填手动信息时看到的仍是它），手动指定区随之出现。
function applyResolveData(data) {
  const parsed = normalizeSeasons(data.seasons || []);
  availableSeasons.value = normalizeSeasons(data.available_seasons || []);
  seasons.value = parsed;
  const matchedId = Number(data.subscribe_id || 0);
  if (matchedId > 0) {
    manualNeeded.value = false
    manualCandidates.value = []
    resolvedMedia.value = {
      subscribe_id: matchedId,
      tmdb_id: null,
      media_type: String(data.media_type || ""),
      title: String(data.title || ""),
      year: "",
    }
    return;
  }
  const candidates = Array.isArray(data.candidates) ? data.candidates : [];
  if (candidates.length === 1) {
    const item = candidates[0] || {};
    manualNeeded.value = false
    manualCandidates.value = []
    resolvedMedia.value = {
      subscribe_id: null,
      tmdb_id: Number(item.tmdb_id || 0),
      media_type: String(item.media_type || data.media_type || ""),
      title: String(item.title || data.title || ""),
      year: String(item.year || ""),
    }
    return;
  }
  clearResolvedMedia()
  if (candidates.length > 1) {
    manualCandidates.value = candidates.map((item) => ({
      tmdb_id: Number(item?.tmdb_id || 0),
      media_type: String(item?.media_type || data.media_type || ""),
      title: String(item?.title || data.title || ""),
      year: String(item?.year || ""),
    }))
    manualNeeded.value = true
    throw new Error("识别到多个同名媒体，请在链接标题中补充年份或季信息后重试");
  }
  manualCandidates.value = []
  manualNeeded.value = true
  throw new Error("未能从链接识别媒体，请检查链接是否完整");
}

function selectManualCandidate(item) {
  if (!item || !Number(item.tmdb_id || 0)) return
  resolvedMedia.value = {
    subscribe_id: null,
    tmdb_id: Number(item.tmdb_id || 0),
    media_type: String(item.media_type || ""),
    title: String(item.title || ""),
    year: String(item.year || ""),
  }
  manualNeeded.value = false
  manualCandidates.value = []
  error.value = ""
}

async function resolveWithManualMedia() {
  const title = manualTitle.value.trim()
  const tmdbId = Number(manualTmdbId.value.trim())
  const hasTmdbId = Number.isInteger(tmdbId) && tmdbId > 0
  if (!title && !hasTmdbId) return
  manualResolving.value = true
  error.value = ""
  try {
    const result = unwrap(
      await props.api.post(`plugin/${props.pluginId}/sync/manual/resolve`, {
        resource_links: resourceLinks.value.split(/\r?\n/),
        title: title || undefined,
        tmdb_id: hasTmdbId ? tmdbId : undefined,
        media_type: manualMediaType.value || undefined,
      }),
    )
    if (result.success === false) throw new Error(result.message || "资源识别失败");
    applyResolveData(result.data || {});
    error.value = "";
  } catch (e) {
    clearResolvedMedia()
    error.value = e.message || "资源识别失败";
  } finally {
    manualResolving.value = false
  }
}

async function resolveResourceTarget(text, revision) {
  try {
    const result = unwrap(
      await props.api.post(`plugin/${props.pluginId}/sync/manual/resolve`, {
        resource_links: text.split(/\r?\n/),
      }),
    )
    if (revision !== resourceResolveRevision) return;
    if (result.success === false) throw new Error(result.message || "资源识别失败");
    applyResolveData(result.data || {});
    error.value = "";
  } catch (e) {
    if (revision === resourceResolveRevision) {
      clearResolvedMedia()
      error.value = e.message || "资源识别失败";
    }
  }
}

function parseCloudPathMedia(path) {
  const segments = String(path || "")
    .split("/")
    .map((value) => value.trim())
    .filter(Boolean)
  const directoryName = segments[segments.length - 1] || "";
  const marker = /\{\s*tmdb\s*id\s*[-_:]?\s*(\d+)\s*\}/i.exec(directoryName);
  if (!marker) return null;
  let title = directoryName
    .slice(0, marker.index)
    .replace(/^\s*[A-Za-z]\s+(?=\p{Script=Han})/u, "")
    .replace(/[._\-\s]+$/g, "")
    .trim()
  if (!title) return null;
  const seasonMatch = /(?:^|[\s._()[\]-])S(?:eason)?\s*0*(\d{1,3})(?=$|[\s._()[\]E{-])/i.exec(directoryName);
  const seasonNumber = seasonMatch ? Math.max(1, Number(seasonMatch[1])) : null;
  return {
    tmdbId: Number(marker[1]),
    title,
    season: seasonNumber,
    preferTv: /(?:更新|连载|\.更\s*\d+)/.test(directoryName),
  }
}

async function resolveCloudPathMedia(path, revision) {
  const hint = parseCloudPathMedia(path);
  if (!hint) return;
  clearResolvedMedia()
  error.value = "";
  try {
    const result = unwrap(
      await props.api.post(`plugin/${props.pluginId}/sync/manual/resolve`, {
        cloud_path: path,
        cloud_provider: selectedCloudResource.value?.provider || null,
        title: hint.title,
        tmdb_id: hint.tmdbId,
        media_type: hint.preferTv ? "tv" : null,
      }),
    )
    if (revision !== cloudMediaResolveRevision) return;
    if (result.success === false) throw new Error(result.message || "网盘路径媒体识别失败");
    const data = result.data || {};
    const matchedId = Number(data.subscribe_id || 0);
    if (matchedId > 0) {
      availableSeasons.value = normalizeSeasons(data.available_seasons || []);
      seasons.value = normalizeSeasons(data.seasons || []);
      resolvedMedia.value = {
        subscribe_id: matchedId,
        tmdb_id: null,
        media_type: String(data.media_type || ""),
        title: String(data.title || hint.title),
        year: "",
      }
      return;
    }
    const candidates = Array.isArray(data.candidates) ? data.candidates : [];
    const exactMatches = candidates.filter((item) => Number(item?.tmdb_id || 0) === hint.tmdbId);
    const matchedMedia =
      (hint.preferTv && exactMatches.find((item) => item.media_type === "tv")) || exactMatches[0] || null
    if (!matchedMedia) {
      throw new Error(`已识别 TMDB ID ${hint.tmdbId}，但未查询到对应媒体`);
    }
    availableSeasons.value = normalizeSeasons(data.available_seasons || []);
    seasons.value = normalizeSeasons(data.seasons || []);
    resolvedMedia.value = {
      subscribe_id: null,
      tmdb_id: Number(matchedMedia.tmdb_id || 0),
      media_type: String(matchedMedia.media_type || data.media_type || ""),
      title: String(matchedMedia.title || hint.title),
      year: String(matchedMedia.year || ""),
    }
  } catch (e) {
    if (revision === cloudMediaResolveRevision) {
      clearResolvedMedia()
      error.value = e.message || "网盘路径媒体识别失败";
    }
  }
}

function selectCloudPath(path, provider) {
  if (resourceResolveTimer) window.clearTimeout(resourceResolveTimer);
  resourceResolveRevision += 1;
  clearManualState()
  selectedCloudResource.value = {
    provider: String(provider || targetCloudDrive.value || "").trim(),
    path: String(path || "/").trim() || "/",
  }
  resourceLinks.value = cloudResourceLabel(selectedCloudResource.value);
  cloudDirectoryVisible.value = false;
  cloudMediaResolveRevision += 1;
  void resolveCloudPathMedia(selectedCloudResource.value.path, cloudMediaResolveRevision);
}

function clearCloudResource() {
  cloudMediaResolveRevision += 1;
  clearResolvedMedia()
  clearManualState()
  selectedCloudResource.value = null;
  resourceLinks.value = "";
}

async function loadOptions() {
  error.value = ""
  try {
    const result = unwrap(await props.api.get(`plugin/${props.pluginId}/ui_options?scope=subscriptions`))
    if (result.success === false) throw new Error(result.message || "加载订阅失败")
    const data = result.data?.data || result.data || result
    cloudDrives.value = Array.isArray(data.cloud_drives) ? data.cloud_drives : [];
    targetCloudDrive.value = String(data.target_cloud_drive || "");
    crossTransferMediaTypes.value = Array.isArray(data.cross_transfer_media_types)
      ? data.cross_transfer_media_types.map((value) => String(value))
      : []
  } catch (e) {
    error.value = e.message || "加载订阅失败"
  }
}

function close(value) {
  if (submitting.value) return
  emit("update:modelValue", Boolean(value))
}

async function submit() {
  if (!canSubmit.value) return
  submitting.value = true
  error.value = ""
  try {
    const media = resolvedMedia.value
    const result = unwrap(
      await props.api.post(`plugin/${props.pluginId}/sync/manual`, {
        // resolve 已定位订阅时只传 subscribe_id；否则传 TMDB 单候选媒体，
        // 后端按 media 兜底识别（见 api_vue_start_manual_sync）。
        subscribe_id: media?.subscribe_id || null,
        media: media?.subscribe_id
          ? null
          : {
            tmdb_id: media?.tmdb_id,
            media_type: media?.media_type,
            title: media?.title,
            year: media?.year || undefined,
            seasons: normalizeSeasons(seasons.value),
          },
        resource_links: selectedCloudResource.value ? [] : resourceLinks.value.split(/\r?\n/),
        cloud_path: selectedCloudResource.value?.path || null,
        cloud_provider: selectedCloudResource.value?.provider || null,
        manual_upgrade: manualUpgrade.value,
        skip_history: skipHistory.value,
      }),
    )
    if (result.success === false) throw new Error(result.message || "提交失败")
    emit("started", result.message || "任务已启动")
    resourceLinks.value = ""
    selectedCloudResource.value = null;
    clearResolvedMedia()
    clearManualState()
    manualUpgrade.value = false
    skipHistory.value = false;
    emit("update:modelValue", false)
  } catch (e) {
    error.value = e.message || "提交失败"
  } finally {
    submitting.value = false
  }
}

watch(
  () => props.modelValue,
  async (visible) => {
    if (visible) {
      cloudMediaResolveRevision += 1;
      selectedCloudResource.value = null;
      skipHistory.value = false;
      cloudDirectoryVisible.value = false;
      resourceLinks.value = ""
      clearResolvedMedia()
      clearManualState()
      await loadOptions()
    } else {
      error.value = ""
      cloudDirectoryVisible.value = false;
    }
  },
  { immediate: true },
)
</script>

<style scoped>
.manual-resource-card {
  max-height: min(760px, calc(100dvh - 32px));
}

.resource-path-actions {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.manual-media-box {
  border: 1px dashed rgba(var(--v-theme-primary), 0.4);
  border-radius: 8px;
  padding: 12px;
}

.manual-candidate-list {
  border: 1px solid rgba(var(--v-theme-on-surface), 0.12);
  border-radius: 8px;
}

.cloud-resource-input :deep(.v-field) {
  background: rgba(var(--v-theme-primary), 0.06);
}

.cloud-resource-input :deep(textarea) {
  color: rgb(var(--v-theme-primary));
  font-weight: 500;
  cursor: default;
}

@media (max-width: 600px) {
  .manual-resource-card {
    width: 100%;
    height: 100dvh;
    max-height: 100dvh;
    border-radius: 0 !important;
  }

  .manual-resource-card > :deep(.v-card-text) {
    min-height: 0;
    flex: 1 1 auto;
    overflow-y: auto;
    padding: 12px;
  }

  .manual-resource-card > :deep(.v-card-title) {
    padding: 12px;
  }

  .manual-resource-card > :deep(.v-card-actions) {
    flex: 0 0 auto;
    padding: 8px 12px 12px !important;
  }
}
</style>
