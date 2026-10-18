<template>
  <div class="tg-monitor">
    <div class="tg-toolbar">
      <div class="tg-status">
        <v-chip size="small" :color="status.enabled ? 'success' : 'grey'" variant="tonal">
          {{ status.enabled ? "监控中" : "未启用" }}
        </v-chip>
        <v-chip v-if="!status.configured" size="small" color="warning" variant="tonal">未登录 TG</v-chip>
        <span class="tg-meta">
          频道 {{ status.channel_count || 0 }} · 资源 {{ status.item_count || 0 }} · 待处理 {{ status.new_count || 0 }}
          <template v-if="status.last_scan_at">· 上次扫描 {{ formatTime(status.last_scan_at) }}</template>
        </span>
      </div>
      <div class="tg-actions">
        <v-btn size="small" variant="tonal" prepend-icon="mdi-account-key-outline" @click="openLogin">
          {{ status.configured ? "重新登录" : "登录 TG" }}
        </v-btn>
        <v-btn
          size="small"
          variant="tonal"
          prepend-icon="mdi-delete-outline"
          :disabled="!status.configured"
          @click="logout">
          退出登录
        </v-btn>
        <v-btn size="small" color="primary" variant="flat" prepend-icon="mdi-radar" :loading="scanning" @click="scan">
          立即扫描
        </v-btn>
      </div>
    </div>

    <v-alert v-if="status.last_error" type="warning" variant="tonal" density="compact" class="mt-2">
      {{ status.last_error }}
    </v-alert>
    <v-alert v-if="!status.enabled" type="info" variant="tonal" density="compact" class="mt-2">
      频道监控未启用：到「配置 → 频道监控」填写 TG api_id / api_hash、登录，并添加频道后启用。
    </v-alert>

    <v-table v-if="items.length" density="compact" class="tg-table mt-2">
      <thead>
        <tr>
          <th style="width: 96px">时间</th>
          <th style="width: 160px">频道</th>
          <th>资源</th>
          <th style="width: 120px">类型</th>
          <th style="width: 80px">状态</th>
          <th style="width: 150px">操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="item in items" :key="item.id">
          <td class="text-caption">{{ formatTime(item.published_at || item.created_at) }}</td>
          <td class="text-caption">{{ item.channel_title }}</td>
          <td>
            <div class="tg-title">{{ item.title || "（无标题）" }}</div>
            <div class="tg-links">
              <a v-for="link in item.links" :key="link" :href="link" target="_blank" rel="noreferrer">
                {{ shortLink(link) }}
              </a>
              <a v-if="item.message_url" :href="item.message_url" target="_blank" rel="noreferrer" class="tg-msg">
                原消息
              </a>
            </div>
            <div
              v-if="item.match_reason"
              class="text-caption mt-1"
              :class="item.match_status === 'pending' ? 'text-warning' : 'text-medium-emphasis'">
              自动匹配：{{ item.match_reason }}
            </div>
            <div
              v-if="item.auto_transfer_error"
              class="text-caption mt-1"
              :class="item.status === 'skipped' ? 'text-medium-emphasis' : 'text-error'">
              {{ item.status === "skipped" ? `未转存：${item.auto_transfer_error}` : item.auto_transfer_error }}
            </div>
          </td>
          <td class="text-caption">{{ typeText(item.types) }}</td>
          <td>
            <v-chip size="x-small" :color="statusColor(item.status)" variant="tonal">
              {{ statusText(item.status) }}
            </v-chip>
          </td>
          <td>
            <v-btn
              size="x-small"
              variant="text"
              color="primary"
              :disabled="item.status === 'transferred'"
              @click="openTransfer(item)">
              转存
            </v-btn>
            <v-btn size="x-small" variant="text" @click="ignore(item)">忽略</v-btn>
            <v-btn size="x-small" variant="text" color="error" @click="remove(item)">删除</v-btn>
          </td>
        </tr>
      </tbody>
    </v-table>
    <div v-else class="tg-empty">还没有抓到资源。启用监控后点「立即扫描」，或等后台定时任务抓取。</div>

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
import { computed, onMounted, onUnmounted, ref } from "vue"

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
  last_scan_at: 0,
  last_error: "",
})
const items = ref([])
const scanning = ref(false)
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
const subscribeOptions = computed(() =>
  subscribes.value.map((sub) => ({
    value: sub.id,
    title: `${sub.name}${sub.season ? ` S${String(sub.season).padStart(2, "0")}` : ""}（#${sub.id}）`,
  })),
)

function formatTime(timestamp) {
  if (!timestamp) return "-"
  const date = new Date(Number(timestamp) * 1000)
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

const STATUS_TEXT = {
  new: "待处理",
  transferred: "已转存",
  skipped: "未转存",
  ignored: "已忽略",
}
const STATUS_COLOR = {
  new: "primary",
  transferred: "success",
  skipped: "warning",
  ignored: "grey",
}
function statusText(value) {
  return STATUS_TEXT[value] || value || "-"
}
function statusColor(value) {
  return STATUS_COLOR[value] || "grey"
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

async function openTransfer(item) {
  currentItem.value = item
  targetSubscribeId.value = null
  transferVisible.value = true
  if (!subscribes.value.length) {
    loadingSubscribes.value = true
    try {
      const result = await props.api.get(`plugin/${PLUGIN}/tg/subscribes`)
      if (result?.success) subscribes.value = result.data?.subscribes || []
    } finally {
      loadingSubscribes.value = false
    }
  }
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
.tg-links {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  font-size: 12px;
  margin-top: 2px;
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
</style>
