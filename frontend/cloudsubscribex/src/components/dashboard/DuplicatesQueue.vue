<template>
  <!-- v1.31.0 重复扫描：同一集多份 .strm 的发现、核对与删除。
       扫描只读本地 .strm 文件（零网盘调用）；核对大小按 115 列表取视频
       实际大小；删除人工确认、逐项核对后才删（视频 + STRM + 同名 NFO）。
       视觉重做（移动优先 390px）：sticky 汇总条 + 卡片式分组 + 底部固定
       toast——结果提示不再只写在页面顶部，滚到下方也能看到。 -->
  <div class="duplicates-queue">
    <div class="dup-summary">
      <div class="dup-summary-head">
        <div class="dup-summary-titles">
          <h1 class="dup-title">重复扫描</h1>
          <p class="dup-subtitle">
            <template v-if="scanned">
              上次扫描 {{ formatSavedAt(savedAt) }} · 已扫 {{ dirsScanned }} 个目录、{{ strmCount }} 个 STRM
            </template>
            <template v-else>扫描本地 .strm 文件，发现同一集的多余副本</template>
          </p>
        </div>
        <div v-if="scanned" class="dup-stats">
          <span class="dup-stat">
            <strong>{{ groups.length }}</strong>
            组重复
          </span>
          <span v-if="sameVideoGroups.length" class="dup-stat">
            <strong>{{ sameVideoGroups.length }}</strong>
            组同视频
          </span>
        </div>
      </div>
      <div v-if="enriching" class="dup-progress" role="status">
        <span class="dup-progress-text">正在核对视频大小…（{{ enrichDone }}/{{ enrichTotal }} 组）</span>
        <div class="dup-progress-track">
          <div
            class="dup-progress-fill"
            :style="{ width: enrichTotal ? `${Math.round((enrichDone / enrichTotal) * 100)}%` : '0%' }" />
        </div>
      </div>
      <div class="dup-actions">
        <v-btn size="small" variant="flat" color="primary" :loading="scanning" @click="scanNow()">开始扫描</v-btn>
        <v-btn
          size="small"
          variant="outlined"
          :loading="enriching"
          :disabled="!scanned || !groups.length"
          @click="enrichNow">
          重新核对大小
        </v-btn>
        <v-btn
          v-if="sameVideoGroups.length"
          size="small"
          variant="tonal"
          color="error"
          :loading="batchDeleting"
          :disabled="scanning || enriching"
          @click="deleteSameVideoBatch">
          批量删除同视频组（{{ sameVideoGroups.length }} 组）
        </v-btn>
      </div>
    </div>

    <p class="dup-intro">
      扫描递归走 .strm 根目录，只读本地文件，不访问网盘；三种文件名算同一集（没标签 / 带 {tmdb-} 标签 / 带 .mp4
      后缀）。保留规则：同一视频保留规范名那份——名字当前在网盘里的那份，删了不会再生；不同视频保留最大的那份。删除前请确认保留哪一份。
      <span v-if="scanned">根目录：{{ strmRoot || "—" }}。</span>
    </p>

    <v-alert v-if="error" type="error" variant="tonal" class="dup-alert" closable @click:close="error = ''">
      {{ error }}
    </v-alert>
    <v-alert v-if="message" type="success" variant="tonal" class="dup-alert" closable @click:close="message = ''">
      {{ message }}
    </v-alert>

    <div v-if="scanned && !groups.length" class="dup-empty">没有发现重复，可以放心。</div>

    <v-card v-for="group in groups" :key="group.key" class="cs-card-clean dup-group">
      <div class="dup-group-head">
        <div class="dup-group-titles">
          <div class="dup-group-title">{{ group.title }}</div>
          <div v-if="group.season_episode" class="dup-group-sub">{{ group.season_episode }}</div>
        </div>
        <div class="dup-group-chips">
          <v-chip size="small" :color="kindColor(group)" variant="tonal">{{ kindLabel(group) }}</v-chip>
          <v-chip size="small" variant="outlined">{{ group.items.length }} 份</v-chip>
        </div>
        <v-btn
          size="small"
          variant="outlined"
          color="error"
          :disabled="group.kind === 'unknown' || enriching || scanning || deletingKey === group.key"
          :loading="deletingKey === group.key"
          @click="deleteGroup(group)">
          删除重复
        </v-btn>
      </div>
      <div class="dup-group-body">
        <p v-if="group.kind === 'unknown'" class="dup-note">
          读不到 pickcode，无法确认视频身份，不能删除——请人工核对后手动处理。
        </p>
        <p v-if="group.kind === 'different-video' && group.suggested_keep === null" class="dup-note">
          视频大小未知，无法按“留大”建议，请人工选择保留哪一份。
        </p>
        <v-radio-group
          :model-value="keepByKey[group.key]"
          density="compact"
          class="dup-radios"
          @update:model-value="(value) => onKeepChange(group, value)">
          <div
            v-for="(item, index) in group.items"
            :key="item.file_name"
            class="dup-item"
            :class="{ 'dup-item--kept': index === keepByKey[group.key] }"
            @click="onKeepChange(group, index)">
            <v-radio :value="index" density="compact" hide-details class="dup-item-radio" />
            <div class="dup-item-main">
              <div class="dup-item-name">
                {{ item.file_name }}
                <v-chip
                  v-if="index === keepByKey[group.key]"
                  size="x-small"
                  color="success"
                  variant="tonal"
                  class="dup-keep-chip">
                  保留
                </v-chip>
              </div>
              <div v-if="item.video_name" class="dup-item-video">{{ item.video_name }}</div>
              <div class="dup-item-meta">
                STRM {{ formatSize(item.size) }} · pickcode {{ item.pickcode_prefix || "—" }}
                <span v-if="item.video_size !== null && item.video_size !== undefined">
                  · 视频 {{ formatSize(item.video_size) }}
                </span>
                <span v-else-if="item.kind === 'redirect'">· 视频大小核对中/查不到</span>
                <span v-else-if="item.kind === 'share'">· 分享视频（不在自己网盘）</span>
              </div>
            </div>
          </div>
        </v-radio-group>
      </div>
    </v-card>

    <!-- 底部固定 toast：最新一条结果提示同时写进这里；扫描/重扫只清页面
         顶部的 inline 提示，不清 toast；点 × 关闭或被下一条提示替换。 -->
    <div v-if="toast" class="dup-toast" :class="`dup-toast--${toast.kind}`" role="status" aria-live="polite">
      <span class="dup-toast-text">{{ toast.text }}</span>
      <button class="dup-toast-close" type="button" aria-label="关闭提示" @click="toast = null">×</button>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from "vue"

const props = defineProps({
  api: { type: [Object, Function], default: () => ({}) },
  pluginId: { type: String, default: "CloudSubscribeX" },
  navKey: { type: String, default: "duplicates" },
  active: { type: Boolean, default: true },
  onClose: { type: Function, default: null },
})

const scanning = ref(false)
const enriching = ref(false)
const enrichDone = ref(0)
const enrichTotal = ref(0)
const deletingKey = ref("")
const batchDeleting = ref(false)
const error = ref("")
const message = ref("")
const toast = ref(null)
const groups = ref([])
const keepByKey = ref({})
const strmRoot = ref("")
const dirsScanned = ref(0)
const strmCount = ref(0)
const unreadable = ref(0)
const savedAt = ref(0)
const scanned = ref(false)

function unwrap(result) {
  if (result && typeof result === "object" && "data" in result) return result.data
  return result
}

function formatSize(bytes) {
  const value = Number(bytes) || 0
  if (value >= 1024 * 1024 * 1024) return `${(value / (1024 * 1024 * 1024)).toFixed(2)} GB`
  if (value >= 1024 * 1024) return `${(value / (1024 * 1024)).toFixed(1)} MB`
  if (value >= 1024) return `${(value / 1024).toFixed(1)} KB`
  return `${value} B`
}

function kindLabel(group) {
  if (!group) return ""
  if (group.kind === "same-video") return "同一视频"
  if (group.kind === "different-video") return "不同视频"
  return "无法确认"
}

function kindColor(group) {
  if (!group) return undefined
  if (group.kind === "same-video") return "info"
  if (group.kind === "different-video") return "warning"
  return "error"
}

// v1.31.3：批量删除只针对同视频组——删的只是多余 STRM + 同名 NFO，
// 视频不动，最安全的一批；不同视频组要逐组确认保留哪一份，不批量。
const sameVideoGroups = computed(() => groups.value.filter((group) => group.kind === "same-video"))

function formatSavedAt(seconds) {
  const value = Number(seconds) || 0
  if (!value) return "—"
  return new Date(value * 1000).toLocaleString("zh-CN", { hour12: false })
}

// 结果提示双写：inline 提示区（页面顶部）+ 底部固定 toast。扫描/重扫/
// 删除开头的 inline 清空只清 inline，不动 toast——用户滚到下方也能
// 看到最新一条结果；toast 只在点 × 或被下一条提示替换时消失。
function showToast(kind, text) {
  toast.value = { kind, text }
}

function setMessage(text) {
  message.value = text
  showToast("success", text)
}

function setError(text) {
  error.value = text
  showToast("error", text)
}

async function scanNow(quiet = false) {
  scanning.value = true
  error.value = ""
  message.value = ""
  try {
    const result = unwrap(await props.api.post(`plugin/${props.pluginId}/duplicates/scan`, {}))
    if (result && result.success === false) throw new Error(result.message || "扫描失败")
    const data = result || {}
    groups.value = data.groups || []
    strmRoot.value = data.strm_root || ""
    dirsScanned.value = data.dirs_scanned || 0
    strmCount.value = data.strm_count || 0
    unreadable.value = data.unreadable || 0
    savedAt.value = data.saved_at || 0
    scanned.value = true
    message.value = `扫描完成：${dirsScanned.value} 个目录、${strmCount.value} 个 STRM，${groups.value.length} 组重复`
    if (!quiet) showToast("success", message.value)
    if (groups.value.length) await enrichNow()
  } catch (err) {
    setError(err && err.message ? err.message : String(err))
  } finally {
    scanning.value = false
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
  }
}

async function saveState(force = false) {
  if (!scanned.value || (!groups.value.length && !force)) return
  try {
    const result = unwrap(
      await props.api.post(`plugin/${props.pluginId}/duplicates/save-state`, {
        state: buildState(),
      }),
    )
    if (result && result.saved_at) savedAt.value = result.saved_at
  } catch (err) {
    // 保存失败不阻塞核对/删除主流程，下次保存再试。
  }
}

// 保留选择改动也要落库：只在用户点选时触发（恢复时直接写 keepByKey，
// 不走这里，不会一打开页面就重写 saved_at）；防抖 800ms，连续点选只存一次。
let saveTimer = null
function onKeepChange(group, value) {
  keepByKey.value[group.key] = value
  if (saveTimer) clearTimeout(saveTimer)
  saveTimer = setTimeout(() => {
    saveState()
  }, 800)
}

async function restoreState() {
  try {
    const result = unwrap(await props.api.post(`plugin/${props.pluginId}/duplicates/load-state`, {}))
    if (result && result.success === false) return
    const state = (result || {}).state
    if (!state || !Array.isArray(state.groups) || !state.groups.length) return
    groups.value = state.groups
    strmRoot.value = state.strm_root || ""
    dirsScanned.value = state.dirs_scanned || 0
    strmCount.value = state.strm_count || 0
    unreadable.value = state.unreadable || 0
    savedAt.value = state.saved_at || 0
    keepByKey.value = { ...(state.keep_by_key || {}) }
    for (const group of groups.value) {
      if (keepByKey.value[group.key] === undefined) {
        keepByKey.value[group.key] = group.suggested_keep ?? 0
      }
    }
    scanned.value = true
  } catch (err) {
    // 读不到保存的状态就当没有，不阻塞页面。
  }
}

onMounted(restoreState)

// v1.31.1：核对改分片——全库 93 组一次 POST 会在反代 120 秒读超时里
// 失败；每批 10 组顺序请求（不并行，避免 115 限流与超时叠加），
// 单批失败重试一次，仍失败则记下该批继续下一批，不中断整轮。
const ENRICH_BATCH_SIZE = 10

async function enrichBatch(batch) {
  const result = unwrap(await props.api.post(`plugin/${props.pluginId}/duplicates/enrich`, { groups: batch }))
  if (result && result.success === false) throw new Error(result.message || "核对失败")
  return (result || {}).groups || []
}

async function enrichNow() {
  if (!groups.value.length) return
  // 只核对尚未核对的组；全部已核对时（显式点“重新核对大小”）全量重核。
  const pending = groups.value.filter((group) => group.sizes_pending)
  const targets = pending.length ? pending : groups.value.slice()
  enriching.value = true
  error.value = ""
  enrichDone.value = 0
  enrichTotal.value = targets.length
  const failedKeys = []
  try {
    const byKey = {}
    for (const group of groups.value) byKey[group.key] = group
    let done = 0
    for (let start = 0; start < targets.length; start += ENRICH_BATCH_SIZE) {
      const batch = targets.slice(start, start + ENRICH_BATCH_SIZE)
      let enriched = null
      for (let attempt = 0; attempt < 2 && enriched === null; attempt += 1) {
        try {
          enriched = await enrichBatch(batch)
        } catch (err) {
          enriched = null
        }
      }
      if (enriched === null) {
        failedKeys.push(...batch.map((group) => group.key))
        continue
      }
      for (const group of enriched) {
        byKey[group.key] = group
        if (keepByKey.value[group.key] === undefined) {
          keepByKey.value[group.key] = group.suggested_keep ?? 0
        }
      }
      // 按原顺序合并回 groups（只替换核对过的组，失败的组保留原样），
      // 每批合并后保存一次：离开页面回来可从这批继续核对。
      groups.value = groups.value.map((group) => byKey[group.key] || group)
      done += enriched.length
      enrichDone.value = done
      await saveState()
    }
    if (failedKeys.length) {
      setError(`核对完成，${failedKeys.length} 组失败（大小未知），可点“重新核对大小”重试`)
    }
  } finally {
    enriching.value = false
    enrichDone.value = 0
    enrichTotal.value = 0
  }
}

async function deleteGroup(group) {
  error.value = ""
  message.value = ""
  const keepIndex = keepByKey.value[group.key] ?? group.suggested_keep ?? 0
  const keepItem = group.items[keepIndex]
  const dropItems = group.items.filter((_, index) => index !== keepIndex)
  // v1.31.4：同视频组保留规范名那份——选中的这份名字在网盘已不存在
  // （video_size 查不到）、而组里另一份能查到时，删除时会改保留规范名
  // 那份；确认前先把这点写明，避免“保留：XXX”看着像按选中的删。
  const canonicalIndex = group.items.findIndex((item) => Number.isInteger(item.video_size))
  const keepStale =
    group.kind === "same-video" &&
    Boolean(keepItem) &&
    !Number.isInteger(keepItem.video_size) &&
    canonicalIndex !== -1 &&
    canonicalIndex !== keepIndex
  const lines = [
    `确定删除“${group.title}${group.season_episode ? ` ${group.season_episode}` : ""}”的重复吗？`,
    `保留：${keepItem ? keepItem.file_name : "—"}`,
    `删除 ${dropItems.length} 份：${dropItems.map((item) => item.file_name).join("；")}`,
    group.kind === "same-video"
      ? "（同一视频，只删多余 STRM + 同名 NFO，视频不动）"
      : "（不同视频：视频 + STRM + 同名 NFO 一起删；分享视频不在自己网盘，只删本地 STRM）",
    ...(keepStale
      ? [`注意：选中的这份名字在网盘已不存在，删除时将改保留规范名那份（第 ${canonicalIndex + 1} 份）。`]
      : []),
    "删后不可恢复。",
  ]
  if (!window.confirm(lines.join("\n"))) return
  deletingKey.value = group.key
  try {
    const result = unwrap(
      await props.api.post(`plugin/${props.pluginId}/duplicates/delete`, {
        group,
        keep_index: keepIndex,
      }),
    )
    if (result && result.success === false) throw new Error(result.message || "删除失败")
    const data = result || {}
    const results = data.results || []
    const adjustedKeep = data.adjusted_keep || null
    const failed = results.filter((item) => !item.success)
    const deletedCount = results.filter((item) => item.success && !item.kept).length
    let resultText = ""
    if (failed.length) {
      resultText = `删除 ${deletedCount} 份，${failed.length} 份失败：${failed
        .map((item) => `${item.file_name}（${item.message}）`)
        .join("；")}`
      setError(resultText)
    } else {
      resultText = `已删除 ${deletedCount} 份重复`
      setMessage(resultText)
    }
    // 删完重扫：quiet 模式——inline 提示写扫描完成，toast 里留删除结果。
    await scanNow(true)
    // v1.31.4：单组删除也可能纠正保留项（同视频组、原选名字已作废）；
    // 纠正说明追加到 inline 提示后，toast 同步成“删除结果 + 纠正说明”。
    if (adjustedKeep) {
      const note = `保留项已按规范名调整为第 ${adjustedKeep.to + 1} 份（原选第 ${adjustedKeep.from + 1} 份的名称在网盘已不存在）`
      message.value = message.value ? `${message.value}；${note}` : note
      showToast(failed.length ? "error" : "success", `${resultText}；${note}`)
    }
  } catch (err) {
    setError(err && err.message ? err.message : String(err))
  } finally {
    deletingKey.value = ""
  }
}

// v1.31.3：批量删除全部同视频组——一次请求发给批量接口，后端逐组
// 复用单组删除校验（重读 STRM 核对 pickcode、find_file_strict、
// playback 核对），单组失败不中断整批。成功删掉的组直接从列表里
// 移除并保存状态（不重扫整库）；失败的组留在列表里，原因写进提示。
async function deleteSameVideoBatch() {
  const targets = sameVideoGroups.value
  if (!targets.length) return
  error.value = ""
  message.value = ""
  const lines = [
    `确定批量删除 ${targets.length} 组同视频重复吗？`,
    "只删多余 STRM + 同名 NFO，视频不动；保留选择按每组当前选中的那一份。",
    "若选中的那份名字在网盘已不存在，删除时会自动改保留规范名那份。",
    "删后不可恢复。",
  ]
  if (!window.confirm(lines.join("\n"))) return
  batchDeleting.value = true
  try {
    const items = targets.map((group) => ({
      group,
      keep_index: keepByKey.value[group.key] ?? group.suggested_keep ?? 0,
    }))
    const result = unwrap(await props.api.post(`plugin/${props.pluginId}/duplicates/delete-batch`, { items }))
    const data = result || {}
    const results = data.results || []
    const deletedKeys = results.filter((item) => item.success).map((item) => item.key)
    const failed = results.filter((item) => !item.success)
    if (deletedKeys.length) {
      groups.value = groups.value.filter((group) => !deletedKeys.includes(group.key))
      for (const key of deletedKeys) delete keepByKey.value[key]
      // 组删空了也强制保存一次空状态，否则 saveState 的非空守卫会让
      // 下次打开页面恢复出已删掉的旧组。
      await saveState(!groups.value.length)
    }
    if (failed.length) {
      setError(
        `批量删除完成：删 ${deletedKeys.length} 组，${failed.length} 组失败：${failed
          .map((item) => `${item.title || item.key}（${item.message}）`)
          .join("；")}`,
      )
    } else {
      // v1.31.4：同视频组保留项被纠正过的组（原选名字已作废，改保留
      // 规范名那份）计个数写进提示。
      const adjustedCount = results.filter((item) => item.adjusted_keep).length
      setMessage(
        `批量删除完成：删 ${deletedKeys.length} 组同视频重复` +
          (adjustedCount ? `，其中 ${adjustedCount} 组保留项已按规范名调整` : ""),
      )
    }
  } catch (err) {
    setError(err && err.message ? err.message : String(err))
  } finally {
    batchDeleting.value = false
  }
}
</script>

<style scoped>
.duplicates-queue {
  max-width: 720px;
  margin: 0 auto;
  padding: 0 12px 24px;
}

/* sticky 汇总条：标题 + 统计 + 进度 + 操作，滚到下方也看得到 */
.dup-summary {
  position: sticky;
  top: 8px;
  z-index: 20;
  margin-bottom: 16px;
  padding: 14px 16px;
  background: rgb(var(--v-theme-surface));
  border: 1px solid var(--cs-border-default);
  border-radius: var(--cs-radius-lg);
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.18);
}

.dup-summary-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.dup-summary-titles {
  min-width: 0;
}

.dup-title {
  margin: 0;
  font-size: 18px;
  font-weight: 700;
  line-height: 1.3;
}

.dup-subtitle {
  margin: 4px 0 0;
  font-size: 12px;
  line-height: 1.6;
  color: rgb(var(--v-theme-on-surface-variant));
}

.dup-stats {
  display: flex;
  flex: 0 0 auto;
  gap: 8px;
}

.dup-stat {
  padding: 4px 8px;
  font-size: 12px;
  color: rgb(var(--v-theme-on-surface-variant));
  background: rgba(var(--v-theme-surface-variant), 0.4);
  border: 1px solid var(--cs-border-subtle);
  border-radius: var(--cs-radius-pill);
  white-space: nowrap;
}

.dup-stat strong {
  font-size: 14px;
  color: rgb(var(--v-theme-on-surface));
}

.dup-progress {
  margin-top: 12px;
}

.dup-progress-text {
  font-size: 12px;
  color: rgb(var(--v-theme-on-surface-variant));
}

.dup-progress-track {
  height: 4px;
  margin-top: 6px;
  overflow: hidden;
  background: rgba(var(--v-theme-surface-variant), 0.5);
  border-radius: var(--cs-radius-pill);
}

.dup-progress-fill {
  height: 100%;
  background: rgb(var(--v-theme-primary));
  border-radius: var(--cs-radius-pill);
  transition: width 0.2s ease;
}

.dup-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 12px;
}

.dup-actions .v-btn {
  min-height: 36px;
}

.dup-intro {
  margin: 0 0 12px;
  font-size: 13px;
  line-height: 1.7;
  color: rgb(var(--v-theme-on-surface-variant));
}

.dup-alert {
  margin-bottom: 12px;
}

.dup-empty {
  padding: 32px 16px;
  font-size: 14px;
  text-align: center;
  color: rgb(var(--v-theme-on-surface-variant));
  border: 1px dashed var(--cs-border-default);
  border-radius: var(--cs-radius-lg);
}

/* 分组卡片 */
.dup-group {
  margin-bottom: 12px;
}

.dup-group-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 12px;
  padding: 14px 16px 0;
}

.dup-group-titles {
  min-width: 0;
  flex: 1 1 auto;
}

.dup-group-title {
  font-size: 15px;
  font-weight: 600;
  line-height: 1.4;
  word-break: break-all;
}

.dup-group-sub {
  margin-top: 2px;
  font-size: 12px;
  color: rgb(var(--v-theme-on-surface-variant));
}

.dup-group-chips {
  display: flex;
  flex: 0 0 auto;
  gap: 6px;
}

.dup-group-body {
  padding: 4px 16px 16px;
}

.dup-note {
  margin: 8px 0 0;
  font-size: 12px;
  line-height: 1.6;
  color: rgb(var(--v-theme-on-surface-variant));
}

.dup-radios {
  margin: 0;
  padding: 0;
}

/* 每份一行：整行可点选，选中态用强调色边框 + 底色 + “保留” chip */
.dup-item {
  display: flex;
  gap: 10px;
  padding: 10px 12px;
  margin-top: 8px;
  cursor: pointer;
  border: 1px solid var(--cs-border-subtle);
  border-radius: var(--cs-radius-md);
  transition:
    border-color 0.15s ease,
    background-color 0.15s ease;
}

.dup-item:hover {
  border-color: var(--cs-border-active);
}

.dup-item--kept {
  background: rgba(var(--v-theme-primary), 0.08);
  border-color: rgba(var(--v-theme-primary), 0.45);
}

.dup-item-radio {
  flex: 0 0 auto;
  margin-top: 2px;
}

.dup-item-main {
  min-width: 0;
  flex: 1 1 auto;
}

.dup-item-name {
  font-size: 13px;
  font-weight: 500;
  line-height: 1.5;
  word-break: break-all;
}

.dup-keep-chip {
  margin-left: 6px;
  vertical-align: 1px;
}

.dup-item-video {
  margin-top: 2px;
  overflow: hidden;
  font-size: 12px;
  line-height: 1.5;
  color: rgb(var(--v-theme-on-surface-variant));
  text-overflow: ellipsis;
  white-space: nowrap;
}

.dup-item-meta {
  margin-top: 2px;
  font-size: 12px;
  line-height: 1.6;
  color: rgb(var(--v-theme-on-surface-variant));
  word-break: break-all;
}

/* 底部固定 toast：结果提示的第二落点，扫描/重扫不清它 */
.dup-toast {
  position: fixed;
  right: 12px;
  bottom: calc(76px + env(safe-area-inset-bottom, 0px));
  left: 12px;
  z-index: 100;
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 12px 14px;
  font-size: 13px;
  line-height: 1.5;
  background: rgb(var(--v-theme-surface));
  border: 1px solid var(--cs-border-default);
  border-left: 3px solid var(--cs-status-success);
  border-radius: var(--cs-radius-md);
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.25);
}

.dup-toast--error {
  border-left-color: var(--cs-status-error);
}

.dup-toast-text {
  flex: 1 1 auto;
  min-width: 0;
  word-break: break-word;
}

.dup-toast-close {
  flex: 0 0 auto;
  padding: 0 4px;
  font-size: 18px;
  line-height: 1.2;
  color: rgb(var(--v-theme-on-surface-variant));
  cursor: pointer;
  background: none;
  border: none;
}

@media (min-width: 640px) {
  .dup-toast {
    right: auto;
    bottom: 24px;
    left: 50%;
    width: min(560px, calc(100vw - 24px));
    transform: translateX(-50%);
  }
}
</style>
