<template>
  <div class="tg-monitor">
    <div class="tg-toolbar d-flex align-center justify-space-between flex-wrap ga-2">
      <div class="tg-status d-flex align-center ga-2">
        <span :class="['status-badge', status.enabled ? 'status-badge-active' : 'status-badge-idle']">
          <span class="status-dot"></span>
          {{ status.enabled ? "正在监控" : "未启用" }}
        </span>
        <span class="tg-meta text-caption text-medium-emphasis">
          {{ status.channel_count || 0 }} 个频道 · {{ status.item_count || 0 }} 条抓取
          <template v-if="status.last_scan_at">· {{ formatTime(status.last_scan_at) }}</template>
        </span>
      </div>
      <div class="tg-actions d-flex align-center ga-2">
        <v-btn
          class="tg-scan"
          size="small"
          color="primary"
          variant="flat"
          prepend-icon="mdi-radar"
          :loading="scanning"
          @click="scan">
          立即扫描
        </v-btn>
        <v-btn
          v-if="failedCount"
          class="tg-retry-failed"
          size="small"
          color="warning"
          variant="tonal"
          prepend-icon="mdi-restart-alert"
          :loading="retrying"
          @click="retryFailed">
          重试失败 ({{ failedCount }})
        </v-btn>
        <v-menu location="bottom end">
          <template #activator="{ props: menuProps }">
            <v-btn
              v-bind="menuProps"
              size="small"
              variant="tonal"
              icon="mdi-dots-vertical"
              title="TG 账号设置" />
          </template>
          <v-list density="compact">
            <v-list-item
              prepend-icon="mdi-account-key-outline"
              :title="status.configured ? '重新登录 Telegram' : '登录 Telegram'"
              @click="openLogin" />
            <v-list-item
              v-if="status.configured"
              prepend-icon="mdi-logout"
              title="退出登录"
              @click="logout" />
          </v-list>
        </v-menu>
      </div>
    </div>

    <v-alert
      v-if="status.restart_required"
      type="info"
      variant="tonal"
      density="compact"
      class="mt-2">
      插件代码已更新，但进程里仍是旧模块（{{ restartDroppedText }}）。正在跑的转存可能被中断或降级，
      重启 MoviePilot 容器后彻底生效；被中断的条目重启后会自动续跑。
    </v-alert>

    <v-alert v-if="status.last_error" type="warning" variant="tonal" density="compact" class="mt-2">
      {{ status.last_error }}
    </v-alert>
    <v-alert v-if="!status.enabled" type="info" variant="tonal" density="compact" class="mt-2">
      频道监控未启用：到「配置 → 频道监控」填写 TG api_id / api_hash、登录，并添加频道后启用。
    </v-alert>

    <div v-if="items.length" class="tg-filter-row mt-2">
      <v-chip
        v-for="option in filterOptions"
        :key="option.value"
        size="small"
        :variant="statusFilter === option.value ? 'flat' : 'tonal'"
        :color="statusFilter === option.value ? 'primary' : undefined"
        class="tg-filter-chip"
        @click="selectFilter(option.value)">
        {{ option.title }}
        <span class="tg-filter-count">{{ option.count }}</span>
      </v-chip>
    </div>

    <!-- 紧凑单行雷达信息流 -->
    <div v-if="filteredItems.length" class="tg-radar-stream mt-3">
      <div
        v-for="item in pagedItems"
        :key="item.id"
        class="tg-stream-item d-flex align-center justify-space-between pa-2 px-3 rounded border">
        
        <!-- 左侧：状态徽标 + 频道标签 -->
        <div class="d-flex align-center ga-2 flex-shrink-0 mr-2 tg-stream-left">
          <v-chip size="x-small" :color="statusColor(item.status)" variant="flat" class="font-weight-bold status-pill">
            {{ statusText(item.status) }}
          </v-chip>
          <v-chip size="x-small" color="primary" variant="tonal" class="channel-pill" prepend-icon="mdi-bullhorn-outline">
            {{ item.channel_title || "频道" }}
          </v-chip>
        </div>

        <!-- 中部：片名主标题 + 标签 + 气泡提示 -->
        <div class="tg-stream-body min-w-0 flex-grow-1 mr-3">
          <div class="d-flex align-center ga-1 min-w-0 flex-wrap">
            <span class="tg-stream-title text-body-2 font-weight-bold text-truncate" :title="item.title">
              {{ item.title || "（无标题）" }}
            </span>
            <v-chip
              v-if="typeText(item.types)"
              size="x-small"
              variant="tonal"
              color="info"
              class="quality-tag flex-shrink-0">
              {{ typeText(item.types) }}
            </v-chip>
            <v-chip
              v-for="tag in qualityTags(item).slice(0, 2)"
              :key="tag"
              size="x-small"
              :color="tagColor(tag)"
              variant="tonal"
              class="quality-tag flex-shrink-0 d-none d-md-inline-flex">
              {{ tag }}
            </v-chip>
          </div>
          <!-- 命中说明或失败说明（紧凑辅助行） -->
          <div v-if="item.match_reason || item.auto_transfer_error" class="tg-stream-hint text-caption text-truncate mt-0.5">
            <span v-if="item.match_reason" class="text-primary mr-2">
              <v-icon icon="mdi-target" size="12" class="mr-0.5" />
              {{ item.match_reason }}
            </span>
            <span v-if="item.auto_transfer_error" :class="item.status === 'skipped' ? 'text-medium-emphasis' : 'text-error'">
              <v-icon icon="mdi-alert-circle-outline" size="12" class="mr-0.5" />
              {{ item.status === 'skipped' ? `未转存：${item.auto_transfer_error}` : item.auto_transfer_error }}
            </span>
          </div>
        </div>

        <!-- 右侧：外链 + 时间 + 一键转存 + 更多 -->
        <div class="d-flex align-center ga-2 flex-shrink-0 tg-stream-actions">
          <span class="text-caption text-medium-emphasis d-none d-sm-inline">
            {{ formatTime(item.published_at || item.created_at) }}
          </span>

          <!-- 外链图标组 -->
          <div class="d-flex align-center ga-1">
            <v-btn
              v-for="link in item.links.slice(0, 1)"
              :key="link"
              icon="mdi-cloud-download-outline"
              size="x-small"
              variant="text"
              color="secondary"
              :href="link"
              target="_blank"
              title="打开网盘链接" />
            <v-btn
              v-if="item.message_url"
              icon="mdi-send-outline"
              size="x-small"
              variant="text"
              color="medium-emphasis"
              :href="item.message_url"
              target="_blank"
              title="查看 Telegram 原消息" />
          </div>

          <!-- 一键转存 -->
          <v-btn
            size="small"
            :color="item.status === 'transferred' ? 'success' : 'primary'"
            :variant="item.status === 'transferred' ? 'tonal' : 'flat'"
            :prepend-icon="item.status === 'transferred' ? 'mdi-check' : 'mdi-cloud-upload-outline'"
            :disabled="item.status === 'transferred'"
            @click="openTransfer(item)">
            {{ item.status === "transferred" ? "已转存" : "转存" }}
          </v-btn>

          <!-- 更多操作菜单（忽略 / 删除） -->
          <v-menu location="bottom end">
            <template #activator="{ props: menuProps }">
              <v-btn v-bind="menuProps" icon="mdi-dots-vertical" size="small" variant="text" />
            </template>
            <v-list density="compact">
              <v-list-item prepend-icon="mdi-eye-off-outline" title="忽略此消息" @click="ignore(item)" />
              <v-list-item prepend-icon="mdi-delete-outline" color="error" title="删除记录" @click="remove(item)" />
            </v-list>
          </v-menu>
        </div>
      </div>
    </div>
    <div v-if="filteredItems.length && pageCount > 1" class="tg-pager">
      <v-pagination
        v-model="page"
        :length="pageCount"
        :total-visible="5"
        density="compact"
        size="small" />
      <span class="tg-pager-meta">
        第 {{ page }}/{{ pageCount }} 页 · 共 {{ filteredItems.length }} 条
      </span>
    </div>
    <div v-if="!filteredItems.length" class="tg-empty">
      {{ items.length ? "当前筛选下没有记录，换个状态看看。" : "还没有抓到资源。启用监控后点「立即扫描」，或等后台定时任务抓取。" }}
    </div>

    <v-dialog v-model="transferVisible" max-width="560">
      <v-card>
        <v-card-title class="text-subtitle-1">转存到订阅</v-card-title>
        <v-card-text>
          <div class="text-caption mb-2">{{ currentItem?.title }}</div>
          <v-select
            v-model="targetSubscribeId"
            :items="subscribeOptions"
            item-title="title"
            item-value="value"
            label="选择订阅"
            density="compact"
            hide-details
            :loading="loadingSubscribes" />
          <v-alert type="info" variant="tonal" density="compact" class="mt-3">
            转存会复用「手动添加」流程：按该订阅的季集规则识别、转存、整理。
          </v-alert>
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="transferVisible = false">取消</v-btn>
          <v-btn color="primary" variant="flat" :loading="transferring" @click="submitTransfer">转存</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <v-dialog v-model="loginVisible" max-width="460">
      <v-card>
        <v-card-title class="text-subtitle-1">登录 Telegram</v-card-title>
        <v-card-text>
          <v-alert v-if="!hasApi" type="warning" variant="tonal" density="compact" class="mb-3">
            请先在「配置 → 频道监控」填写 api_id / api_hash 并保存。
          </v-alert>
          <template v-if="loginStep === 'phone'">
            <v-text-field
              v-model="phone"
              label="手机号（含国家码）"
              placeholder="+8613800000000"
              density="compact"
              hide-details />
          </template>
          <template v-else-if="loginStep === 'code'">
            <v-text-field v-model="code" label="验证码" density="compact" hide-details autofocus />
          </template>
          <template v-else>
            <v-text-field
              v-model="password"
              label="两步验证密码"
              type="password"
              density="compact"
              hide-details
              autofocus />
          </template>
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="loginVisible = false">取消</v-btn>
          <v-btn color="primary" variant="flat" :loading="loginLoading" @click="submitLogin">下一步</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>
  </div>
</template>

<script setup>
import {STATUS, statusColor as statusColorOf, statusText as statusTextOf} from "../../utils/status.js";
import { computed, onMounted, onUnmounted, ref, watch } from "vue"

const props = defineProps({
  api: { type: Object, required: true },
  notify: { type: Function, default: () => {} },
  active: { type: Boolean, default: false },
})

const PLUGIN = "CloudSubscribeX"
const status = ref({
  enabled: false,
  configured: false,
  channel_count: 0,
  item_count: 0,
  new_count: 0,
  failed_count: 0,
  restart_required: false,
  restart_dropped: [],
  last_scan_at: 0,
  last_error: "",
})
const items = ref([])
const statusFilter = ref("active")
const page = ref(1)
const PAGE_SIZE = 30
const scanning = ref(false)
const retrying = ref(false)
const loadingSubscribes = ref(false)
const subscribes = ref([])
const transferVisible = ref(false)
const transferring = ref(false)
const currentItem = ref(null)
const targetSubscribeId = ref(null)
const loginVisible = ref(false)
const loginStep = ref("phone")
const phone = ref("")
const code = ref("")
const password = ref("")
const loginLoading = ref(false)
let timer = null

const hasApi = computed(() => Boolean(status.value.has_api))

// 「有动作的」= 待处理 + 已转存 + 失败；未转存和已忽略属于历史/噪音，默认不展示，
// 否则 170 条里 150 多条是这两类，真实待办的几条会被淹没。
const FILTERS = [
  { value: "active", title: "待办与已转存",
    test: (item) =>
      ["queued", "new", "failed", "transferred"].includes(item.status) },
  { value: "new", title: "待处理",
    test: (item) => ["queued", "new"].includes(item.status) },
  { value: "failed", title: "失败", test: (item) => item.status === "failed" },
  { value: "transferred", title: "已转存", test: (item) => item.status === "transferred" },
  { value: "skipped", title: "未转存", test: (item) => item.status === "skipped" },
  { value: "ignored", title: "已忽略", test: (item) => item.status === "ignored" },
  { value: "all", title: "全部", test: () => true },
]

const filterOptions = computed(() => {
  const rows = items.value || []
  return FILTERS.map((filter) => ({
    value: filter.value,
    title: filter.title,
    count: rows.filter(filter.test).length,
  }))
})

const failedCount = computed(
  () => (items.value || []).filter((item) => item.status === "failed").length
)

const restartDroppedText = computed(() => {
  const dropped = status.value.restart_dropped || []
  return dropped.length ? `已忽略参数：${dropped.join("、")}` : "部分参数被忽略"
})

const filteredItems = computed(() => {
  const active = FILTERS.find((f) => f.value === statusFilter.value) || FILTERS[0]
  return (items.value || []).filter(active.test)
})

const pageCount = computed(() =>
  Math.max(1, Math.ceil(filteredItems.value.length / PAGE_SIZE))
)

const pagedItems = computed(() => {
  const start = (page.value - 1) * PAGE_SIZE
  return filteredItems.value.slice(start, start + PAGE_SIZE)
})

function selectFilter(value) {
  statusFilter.value = value
  page.value = 1
}

watch(filteredItems, () => {
  if (page.value > pageCount.value) page.value = pageCount.value
})
const subscribeOptions = computed(() =>
  subscribes.value.map((sub) => ({
    value: sub.id,
    title: `${sub.name}${sub.season ? ` S${String(sub.season).padStart(2, "0")}` : ""}（#${sub.id}）`,
  })),
)

function formatTime(timestamp) {
  if (!timestamp) return "-"
  let date
  if (typeof timestamp === "number" || /^\d+$/.test(String(timestamp).trim())) {
    const num = Number(timestamp)
    date = new Date(num > 1e11 ? num : num * 1000)
  } else {
    date = new Date(timestamp)
  }
  if (isNaN(date.getTime())) return String(timestamp).slice(5, 16) || "-"
  const pad = (value) => String(value).padStart(2, "0")
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}

function shortLink(link) {
  try {
    const url = new URL(link)
    return `${url.hostname}${url.pathname}`.slice(0, 48)
  } catch {
    return String(link).slice(0, 48)
  }
}

const TYPE_TEXT = {
  quark: "夸克",
  115: "115",
  alipan: "阿里",
  tianyi: "天翼",
  baidu: "百度",
  uc: "UC",
  123: "123",
  guangya: "光鸭",
  yun139: "移动",
  xunlei: "迅雷",
  magnet: "磁力",
  ed2k: "电驴",
  pikpak: "PikPak",
}
function typeText(types) {
  return (types || []).map((type) => TYPE_TEXT[type] || type).join(" / ") || "-"
}

function qualityLabel(item) {
  const label = item?.quality?.label
  return label && label !== "未知" ? label : ""
}

function qualityTags(item) {
  const tags = item?.quality?.tags
  if (Array.isArray(tags) && tags.length) return tags
  const label = qualityLabel(item)
  return label ? [label] : []
}

function tagColor(tag) {
  if (tag === "免费") return "success"
  if (/^(8K|4K|UHD|1080P|720P|480P)$/.test(tag)) return "deep-purple"
  if (tag === "杜比视界" || tag === "杜比全景声") return "purple"
  if (/^HDR|^SDR/.test(tag)) return "indigo"
  if (tag === "中字" || tag === "内封简繁英" || tag === "内封简繁") return "info"
  return "grey"
}

// 状态词统一走 utils/status.js，避免「排队中 / 待处理 / 未转存」各说各话。
const STATUS_TEXT = {
  queued: STATUS.QUEUED,
  new: STATUS.QUEUED,
  transferred: STATUS.SUCCESS,
  skipped: STATUS.SKIPPED,
  failed: STATUS.FAILED,
  ignored: STATUS.IGNORED,
}
function statusText(value) {
  return STATUS_TEXT[value] || statusTextOf(value, "-")
}
function statusColor(value) {
  return statusColorOf(value)
}

function errorClass(item) {
  if (item.status === "skipped" || item.failure_kind === "interrupted") {
    return "text-medium-emphasis"
  }
  return "text-error"
}

async function loadStatus() {
  const result = await props.api.get(`plugin/${PLUGIN}/tg/status`)
  if (result?.success) status.value = result.data || status.value
}

async function loadItems() {
  const result = await props.api.get(`plugin/${PLUGIN}/tg/items`)
  if (result?.success) {
    items.value = result.data?.items || []
    if (result.data?.status) status.value = result.data.status
  }
}

async function refreshAll() {
  await Promise.all([loadStatus(), loadItems()])
}

async function scan() {
  if (scanning.value) return
  scanning.value = true
  try {
    const result = await props.api.post(`plugin/${PLUGIN}/tg/scan`, {})
    if (result?.success) {
      props.notify(result.message || "扫描完成", "success")
      await refreshAll()
    } else {
      props.notify(result?.message || "扫描失败", "error")
    }
  } finally {
    scanning.value = false
  }
}

async function retryFailed() {
  if (retrying.value) return
  retrying.value = true
  try {
    // 1.7.114：批量把失败条目塞回队列（后端会沿用已匹配的订阅，单次上限 50 条）。
    const result = await props.api.post(`plugin/${PLUGIN}/tg/retry_failed`, {})
    if (result?.success) {
      props.notify(result.message || "已重新排队", "success")
      await refreshAll()
    } else {
      props.notify(result?.message || "重试失败", "error")
    }
  } finally {
    retrying.value = false
  }
}

async function ensureSubscribes() {
  if (subscribes.value.length) return
  loadingSubscribes.value = true
  try {
    const result = await props.api.get(`plugin/${PLUGIN}/tg/subscribes`)
    if (result?.success) subscribes.value = result.data?.subscribes || []
  } finally {
    loadingSubscribes.value = false
  }
}

async function openTransfer(item) {
  currentItem.value = item
  await ensureSubscribes()
  const matchedId = Number(item?.matched_subscribe_id || 0)
  // 1.7.113：扫描阶段已经把订阅匹配好（matched_subscribe_id），订阅还在就
  // 直接用匹配结果转存，不再让人重新挑一遍；订阅被删/没匹配才弹窗手选。
  const matchedExists =
    matchedId > 0 && subscribes.value.some((row) => Number(row.id) === matchedId)
  targetSubscribeId.value = matchedExists ? matchedId : null
  if (matchedExists) {
    await submitTransfer()
    return
  }
  transferVisible.value = true
}

async function submitTransfer() {
  if (!targetSubscribeId.value) {
    props.notify("请选择订阅", "warning")
    return
  }
  transferring.value = true
  try {
    const result = await props.api.post(`plugin/${PLUGIN}/tg/transfer`, {
      id: currentItem.value?.id,
      subscribe_id: targetSubscribeId.value,
    })
    if (result?.success) {
      props.notify(result.message || "已提交转存", "success")
      transferVisible.value = false
      await refreshAll()
    } else {
      props.notify(result?.message || "转存失败", "error")
    }
  } finally {
    transferring.value = false
  }
}

async function ignore(item) {
  const result = await props.api.post(`plugin/${PLUGIN}/tg/ignore`, { ids: [item.id] })
  if (result?.success) await refreshAll()
}

async function remove(item) {
  const result = await props.api.post(`plugin/${PLUGIN}/tg/delete`, { ids: [item.id] })
  if (result?.success) await refreshAll()
}

function openLogin() {
  loginStep.value = "phone"
  code.value = ""
  password.value = ""
  loginVisible.value = true
}

async function submitLogin() {
  loginLoading.value = true
  try {
    if (loginStep.value === "phone") {
      const result = await props.api.post(`plugin/${PLUGIN}/tg/login/start`, { phone: phone.value })
      if (result?.success) {
        loginStep.value = "code"
        props.notify(result.data?.message || "验证码已发送", "success")
      } else {
        props.notify(result?.message || "发送验证码失败", "error")
      }
      return
    }
    const payload = loginStep.value === "code" ? { code: code.value } : { password: password.value }
    const result = await props.api.post(`plugin/${PLUGIN}/tg/login/code`, payload)
    if (result?.success && result.data?.need_password) {
      loginStep.value = "password"
      props.notify("需要两步验证密码", "info")
      return
    }
    if (result?.success) {
      props.notify("登录成功，会话已保存", "success")
      loginVisible.value = false
      await refreshAll()
    } else {
      props.notify(result?.message || "登录失败", "error")
    }
  } finally {
    loginLoading.value = false
  }
}

async function logout() {
  const result = await props.api.post(`plugin/${PLUGIN}/tg/logout`, {})
  if (result?.success) {
    props.notify("已清除 TG 会话", "success")
    await refreshAll()
  }
}

onMounted(async () => {
  await refreshAll()
  timer = window.setInterval(() => {
    if (props.active) void refreshAll()
  }, 60000)
})
onUnmounted(() => {
  if (timer !== null) window.clearInterval(timer)
})
</script>

<style scoped>
.tg-filter-row {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 8px;
}
.tg-filter-chip {
  cursor: pointer;
}
.tg-filter-count {
  margin-left: 6px;
  font-size: 0.6875rem;
  opacity: 0.72;
}
.tg-pager {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
  padding: 10px 0 4px;
}
.tg-pager-meta {
  font-size: 0.6875rem;
  color: rgba(var(--v-theme-on-surface), 0.6);
}
.tg-monitor {
  padding: 12px;
  overflow: auto;
  height: 100%;
}
.tg-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.tg-status {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.tg-status-chips {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.tg-meta {
  color: rgb(var(--v-theme-on-surface-variant));
  font-size: 12px;
}
.tg-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.tg-table :deep(td) {
  vertical-align: top;
}
.tg-title {
  font-size: 13px;
  line-height: 1.4;
  word-break: break-all;
}
.tg-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.tg-links {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  font-size: 12px;
  margin-top: 2px;
}
.tg-links a {
  word-break: break-all;
}
.tg-msg {
  opacity: 0.7;
}
.tg-empty {
  padding: 36px 12px;
  text-align: center;
  color: rgb(var(--v-theme-on-surface-variant));
  font-size: 13px;
}

.status-badge {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 1px 7px;
  border-radius: 999px;
  font-size: 0.72rem;
  font-weight: 500;
}

.status-badge-active {
  background: rgba(var(--v-theme-success), 0.12);
  color: rgb(var(--v-theme-success));
  border: 1px solid rgba(var(--v-theme-success), 0.25);
}

.status-badge-idle {
  background: rgba(var(--v-theme-on-surface), 0.08);
  color: rgba(var(--v-theme-on-surface), 0.6);
}

.status-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: currentColor;
}

.tg-radar-stream {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.tg-stream-item {
  background: rgba(var(--v-theme-surface), 0.6);
  border-color: rgba(var(--v-theme-on-surface), 0.08) !important;
  transition: background-color 0.15s, border-color 0.15s;
}

.tg-stream-item:hover {
  background: rgba(var(--v-theme-surface-variant), 0.35);
  border-color: rgba(var(--v-theme-primary), 0.3) !important;
}

.tg-stream-title {
  max-width: 580px;
}

.status-pill {
  min-width: 52px;
  justify-content: center;
}

.channel-pill {
  max-width: 130px;
}

.quality-tag {
  height: 18px !important;
  font-size: 10px !important;
  padding: 0 5px !important;
}

.tg-error-bubble {
  background: rgba(var(--v-theme-warning), 0.1);
  color: rgb(var(--v-theme-warning));
  border-left: 3px solid rgb(var(--v-theme-warning));
}

@media (max-width: 600px) {
  .tg-monitor {
    padding: 10px;
  }
  .tg-toolbar {
    flex-direction: column;
    align-items: stretch;
    gap: 10px;
  }
  .tg-stream-item {
    flex-direction: column !important;
    align-items: stretch !important;
    padding: 10px 12px !important;
    gap: 8px !important;
  }
  .tg-stream-left {
    justify-content: flex-start;
    margin-right: 0 !important;
  }
  .channel-pill {
    max-width: 160px;
  }
  .tg-stream-body {
    margin-right: 0 !important;
    width: 100%;
  }
  .tg-stream-title {
    max-width: 100% !important;
    white-space: normal !important;
    line-height: 1.4 !important;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }
  .tg-stream-actions {
    justify-content: space-between;
    width: 100%;
    padding-top: 6px;
    border-top: 1px dashed rgba(var(--v-theme-on-surface), 0.08);
  }
  .tg-status {
    align-items: flex-start;
    flex-direction: column;
    gap: 5px;
  }
  .tg-meta {
    font-size: 11.5px;
    line-height: 1.45;
  }
  .tg-empty {
    padding: 28px 8px;
    line-height: 1.6;
  }
}
</style>
