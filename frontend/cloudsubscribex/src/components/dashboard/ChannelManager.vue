<template>
  <div class="channel-manager">
    <v-row dense class="mb-2">
      <v-col cols="6" sm="3">
        <v-chip size="small" variant="tonal" color="primary" prepend-icon="mdi-rss">
          渠道 {{ summary.total }}
        </v-chip>
      </v-col>
      <v-col cols="6" sm="3">
        <v-chip size="small" variant="tonal" color="success" prepend-icon="mdi-check-circle">
          启用 {{ summary.enabled }}
        </v-chip>
      </v-col>
      <v-col cols="6" sm="3">
        <v-chip size="small" variant="tonal" prepend-icon="mdi-magnify">
          搜索 {{ summary.search_enabled }}/{{ summary.search_total }}
        </v-chip>
      </v-col>
      <v-col cols="6" sm="3">
        <v-chip size="small" variant="tonal" prepend-icon="mdi-telegram">
          TG 频道 {{ summary.tg_enabled }}/{{ summary.tg_total }}
        </v-chip>
      </v-col>
    </v-row>

    <v-row dense align="center" class="mb-2">
      <v-col cols="12" md="6">
        <v-text-field
          v-model="search"
          density="compact"
          variant="outlined"
          hide-details
          clearable
          prepend-inner-icon="mdi-magnify"
          placeholder="搜索名称、类型、频道或地址"
        />
      </v-col>
      <v-col cols="12" md="6">
        <div class="channel-toolbar">
          <v-btn-toggle v-model="statusFilter" density="compact" variant="outlined" divided>
            <v-btn value="all" size="small">全部</v-btn>
            <v-btn value="enabled" size="small">已启用</v-btn>
            <v-btn value="disabled" size="small">已停用</v-btn>
          </v-btn-toggle>
          <v-btn color="primary" size="small" prepend-icon="mdi-plus" @click="openCreate">
            新增
          </v-btn>
        </div>
      </v-col>
    </v-row>

    <v-expand-transition>
      <v-card v-if="selected.size" variant="tonal" color="primary" class="mb-2 pa-2">
        <div class="d-flex align-center flex-wrap ga-2">
          <span class="text-caption">已选 {{ selected.size }} 项</span>
          <v-spacer />
          <v-btn size="small" variant="text" @click="bulkToggle(true)">启用</v-btn>
          <v-btn size="small" variant="text" @click="bulkToggle(false)">停用</v-btn>
          <v-btn size="small" variant="text" @click="selected.clear()">清空</v-btn>
        </div>
      </v-card>
    </v-expand-transition>

    <v-row v-if="filtered.length" dense>
      <v-col v-for="item in filtered" :key="item.id" cols="12" sm="6" md="4" lg="3">
        <ChannelCard
          :item="item"
          :selected="selected.has(item.id)"
          :testing="testingId === item.id"
          :scanning="scanningId === item.id"
          :type-label="typeLabel"
          :type-color="typeColor"
          :channel-line="channelLine"
          :relative-time="relativeTime"
          @toggle-select="toggleSelect"
          @set-enabled="setEnabled"
          @test="testChannel"
          @scan="scanChannel"
          @edit="openEdit"
          @delete="askDelete"
          @open-detail="openDetail" />
      </v-col>
    </v-row>
    <v-card v-else variant="tonal" class="pa-6 text-center">
      <v-icon icon="mdi-rss" size="40" class="mb-2" />
      <div class="text-body-2 mb-3">
        {{ channels.length ? "没有匹配的渠道" : "还没有渠道，先新增一个" }}
      </div>
      <v-btn color="primary" size="small" prepend-icon="mdi-plus" @click="openCreate">新增渠道</v-btn>
    </v-card>

    <ChannelDetailDrawer
      v-if="detail"
      v-model:open="detailOpen"
      :detail="detail"
      :detail-lines="detailLines"
      :type-label="typeLabel"
      :is-mobile="isMobile"
      @edit="editFromDetail" />

    <ChannelEditDialog
      v-model="dialogVisible"
      :editing="editing"
      :form="form"
      :form-section="formSection"
      :channel-options="channelOptions"
      :api="api"
      :refreshing-accounts="refreshingAccounts"
      :hdhive-oauth-action="hdhiveOauthAction"
      :type-options="typeOptions"
      :saving="saving"
      :is-mobile="isMobile"
      :load-channel-dynamic-options="loadChannelDynamicOptions"
      :refresh-channel-dynamic-options="refreshChannelDynamicOptions"
      @submit="submit"
      @type-change="onTypeChange"
      @test-source="handleFormTestSource"
      @refresh-account="refreshAccount"
      @hdhive-oauth-start="startHdhiveOAuth"
      @hdhive-oauth-exchange="exchangeHdhiveOAuth"
      @notify="notify" />

    <v-dialog v-model="deleteVisible" max-width="420">
      <v-card>
        <v-card-title>删除渠道</v-card-title>
        <v-card-text>确认删除「{{ deleting?.name }}」？删除后不再参与搜索与监控。</v-card-text>
        <v-card-actions>
          <v-btn variant="text" @click="deleteVisible = false">取消</v-btn>
          <v-spacer />
          <v-btn color="error" :loading="deletingLoading" @click="confirmDelete">删除</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <v-snackbar v-model="snack.show" :timeout="3000" :color="snack.color" location="bottom">
      {{ snack.text }}
    </v-snackbar>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, reactive, ref, watch } from "vue"
import { useDisplay } from "vuetify"

import ChannelCard from "./channel/ChannelCard.vue"
import ChannelDetailDrawer from "./channel/ChannelDetailDrawer.vue"
import ChannelEditDialog from "./channel/ChannelEditDialog.vue"
import { resolveDynamicField } from "../../config/fields/search/index.js"

const props = defineProps({
  api: { type: [Object, Function], required: true },
  pluginId: { type: String, default: "CloudSubscribeX" },
  active: { type: Boolean, default: false },
})

const display = useDisplay()
const isMobile = computed(() => display.xs.value)

const channels = ref([])
const catalog = ref([])
const schemas = ref([])
const summary = reactive({
  total: 0,
  enabled: 0,
  search_total: 0,
  search_enabled: 0,
  tg_total: 0,
  tg_enabled: 0,
  tg_monitor_enabled: false,
})
const search = ref("")
const statusFilter = ref("all")
const selected = ref(new Set())
const loading = ref(false)
const loaded = ref(false)

const dialogVisible = ref(false)
const editing = ref(null)
const saving = ref(false)
const form = reactive(defaultForm())

const deleteVisible = ref(false)
const deleting = ref(null)
const deletingLoading = ref(false)

const detailOpen = ref(false)
const detail = ref(null)

const testingId = ref("")
const scanningId = ref("")

// 传给 ConfigSection 的账户信息：配置页（Config.vue）用同一份 options
// 喂 AccountInfo；渠道页之前传的是空对象，account 字段永远显示
// “账号未连接”，登录状态和账户详情都出不来。这里按需拉取并缓存。
// 动态选项（如 PanSou 的限定频道/插件）也走同一份 options：配置页用
// resolveDynamicField + ui_options 链路填充 field.items；渠道页之前没接，
// 勾选弹窗里一直是“没有可选项”（2026-11-18 用户截图实锤，v1.18.4 补）。
const channelOptions = reactive({
  searchAccounts: {},
  pansou: {},
  dynamicOptionLoading: {},
  dynamicOptionErrors: {},
})
const refreshingAccounts = ref([])
const hdhiveOauthAction = ref("")
let hdhiveOauthWindow = null

function unwrapResponse(raw) {
  if (raw?.data && typeof raw.data === "object" && "success" in raw.data) {
    return raw.data
  }
  return raw || {}
}

async function refreshAccount(accountKey, { silent = false } = {}) {
  const normalizedKey = String(accountKey || "").trim().toLowerCase()
  if (!normalizedKey || refreshingAccounts.value.includes(normalizedKey)) return
  refreshingAccounts.value = [...refreshingAccounts.value, normalizedKey]
  try {
    const data = await request("post", `plugin/${props.pluginId}/account/refresh`, {
      key: normalizedKey,
    })
    const [category, source] = String(data?.key || normalizedKey).split(":", 2)
    const account = data?.account && typeof data.account === "object" ? data.account : {}
    if (category === "search") {
      channelOptions.searchAccounts = {
        ...channelOptions.searchAccounts,
        [source]: account,
      }
    }
    if (!silent) {
      notify(
        data?.limited ? "刷新过于频繁，已显示最近一次账户信息" : "账户信息已刷新",
        data?.limited ? "warning" : "success",
      )
    }
  } catch (error) {
    if (!silent) notify(`账户信息刷新失败：${error.message || error}`, "error")
  } finally {
    refreshingAccounts.value = refreshingAccounts.value.filter((key) => key !== normalizedKey)
  }
}

/** 按当前表单的 account 字段拉取账户信息（静默，供打开编辑时调用）。 */
async function loadChannelAccounts() {
  const keys = []
  for (const group of formSection.value.groups || []) {
    for (const field of group.fields || []) {
      if (field?.type === "account" && field.accountKey) keys.push(String(field.accountKey))
    }
  }
  for (const key of [...new Set(keys)]) {
    await refreshAccount(key, { silent: true })
  }
}

// 动态选项加载：与配置页 loadDynamicOptions/loadOptions 同款语义。
// 字段打开勾选弹窗时由 MultiSelectDialogField 触发 load-options；
// 打开编辑弹窗时也预拉一次，避免用户点开字段时还在等首轮加载。
const loadedDynamicScopes = new Set()
const dynamicScopeRequests = new Map()

async function loadChannelDynamicOptions(descriptor, { refresh = false } = {}) {
  const scope = String(descriptor?.scope || "").trim().toLowerCase()
  if (!scope) return
  if (!refresh && loadedDynamicScopes.has(scope)) return
  if (dynamicScopeRequests.has(scope)) return dynamicScopeRequests.get(scope)
  const task = (async () => {
    channelOptions.dynamicOptionLoading[scope] = true
    channelOptions.dynamicOptionErrors[scope] = ""
    try {
      const query = new URLSearchParams({ scope })
      if (refresh) query.set("refresh", "true")
      const data = await request("get", `plugin/${props.pluginId}/ui_options?${query}`)
      const payload = data?.data ?? data ?? {}
      const scopeOptions =
        payload?.[scope] && typeof payload[scope] === "object" ? payload[scope] : {}
      channelOptions[scope] = scopeOptions
      loadedDynamicScopes.add(scope)
    } catch (error) {
      channelOptions.dynamicOptionErrors[scope] = `加载失败：${error.message || error}`
    } finally {
      channelOptions.dynamicOptionLoading[scope] = false
    }
  })()
  dynamicScopeRequests.set(scope, task)
  try {
    await task
  } finally {
    dynamicScopeRequests.delete(scope)
  }
}

function refreshChannelDynamicOptions(descriptor) {
  return loadChannelDynamicOptions(descriptor, { refresh: true })
}

/** 当前表单是否含动态选项字段；有则在打开编辑时预拉一次。 */
function preloadChannelDynamicOptions() {
  const scopes = new Set()
  for (const group of formSection.value.groups || []) {
    for (const field of group.fields || []) {
      const scope = String(field?.dynamicOptions?.scope || "").trim().toLowerCase()
      if (scope) scopes.add(scope)
    }
  }
  for (const scope of scopes) {
    void loadChannelDynamicOptions({ scope })
  }
}

function hdhiveOAuthPayload() {
  return {
    client_id: String(form.config.hdhive_client_id || "").trim(),
    app_secret: String(form.config.hdhive_api_key || "").trim(),
    redirect_uri: String(form.config.hdhive_redirect_uri || "").trim(),
    response_mode: String(form.config.hdhive_response_mode || "redirect").trim(),
    scope: "query unlock write",
  }
}

async function startHdhiveOAuth() {
  if (hdhiveOauthAction.value) return
  const oauthPopup = window.open("about:blank", "hdhive-openapi-oauth", "width=560,height=760,noopener=no")
  hdhiveOauthAction.value = "start"
  try {
    const response = unwrapResponse(
      await props.api.post(`plugin/${props.pluginId}/hdhive/oauth/start`, hdhiveOAuthPayload()),
    )
    if (response.success === false) {
      throw new Error(response.message || "生成 HDHive 授权链接失败")
    }
    const data = response.data?.data || response.data || {}
    if (!data.authorize_url) throw new Error("HDHive 授权链接为空")
    if (oauthPopup) {
      oauthPopup.location.href = data.authorize_url
      hdhiveOauthWindow = oauthPopup
    } else {
      window.open(data.authorize_url, "_blank", "noopener,noreferrer")
    }
    notify(response.message || "HDHive 授权页已打开")
  } catch (error) {
    if (oauthPopup && !oauthPopup.closed) oauthPopup.close()
    notify(`发起 HDHive 授权失败：${error.message || error}`, "error")
  } finally {
    hdhiveOauthAction.value = ""
  }
}

async function exchangeHdhiveOAuth(callbackData = {}) {
  if (hdhiveOauthAction.value) return
  hdhiveOauthAction.value = "exchange"
  try {
    const response = unwrapResponse(
      await props.api.post(`plugin/${props.pluginId}/hdhive/oauth/exchange`, {
        ...hdhiveOAuthPayload(),
        callback: String(form.config.hdhive_oauth_callback || "").trim(),
        code: String(callbackData.code || "").trim(),
        state: String(callbackData.state || "").trim(),
      }),
    )
    if (response.success === false) {
      throw new Error(response.message || "HDHive 用户授权失败")
    }
    const data = response.data?.data || response.data || {}
    form.config.hdhive_access_token = data.access_token || ""
    form.config.hdhive_refresh_token = data.refresh_token || ""
    form.config.hdhive_token_expires_at = Number(data.token_expires_at || 0)
    form.config.hdhive_auth_code = ""
    form.config.hdhive_oauth_callback = ""
    if (hdhiveOauthWindow && !hdhiveOauthWindow.closed) hdhiveOauthWindow.close()
    hdhiveOauthWindow = null
    notify(data.warning || "HDHive OpenAPI 授权成功，请保存渠道使 Token 生效", data.warning ? "warning" : "success")
    await refreshAccount("search:hdhive", { silent: true })
  } catch (error) {
    notify(`完成 HDHive 授权失败：${error.message || error}`, "error")
  } finally {
    hdhiveOauthAction.value = ""
  }
}

function handleHdhiveOAuthMessage(event) {
  if (event.origin !== "https://re0.me") return
  const payload = event.data
  if (
    !payload ||
    payload.source !== "hdhive-openapi" ||
    payload.type !== "authorization_response" ||
    String(payload.client_id || "") !== String(form.config.hdhive_client_id || "")
  )
    return
  void exchangeHdhiveOAuth(payload)
}

const snack = reactive({ show: false, text: "", color: "success" })

function defaultForm() {
  return {
    name: "",
    type: "pansou",
    priority: 50,
    enabled: true,
    config: {},
    tg: {
      channel: "",
      keywords: "",
      exclude_keywords: "",
      scan_limit: 20,
    },
  }
}

function notify(text, color = "success") {
  snack.text = text
  snack.color = color
  snack.show = true
}

async function request(method, url, body) {
  const handler = props.api?.[method]
  if (typeof handler !== "function") {
    throw new Error(`宿主缺少 api.${method}`)
  }
  const result = await handler.call(props.api, url, body)
  if (result && typeof result === "object" && "success" in result && result.success === false) {
    throw new Error(result.message || "请求失败")
  }
  return result?.data ?? result
}

const typeOptions = computed(() => [
  ...catalog.value.map((entry) => ({
    label: entry.name || entry.key,
    value: entry.key,
  })),
  { label: "TG 频道", value: "tg_channel" },
])

function typeLabel(type) {
  if (type === "tg_channel") return "TG 频道"
  const entry = catalog.value.find((item) => item.key === type)
  return entry?.name || type
}

function typeColor(type) {
  if (type === "tg_channel") return "info"
  const entry = catalog.value.find((item) => item.key === type)
  return entry?.color || "primary"
}

function schemaFor(type) {
  return schemas.value.find((schema) => schema.id === type) || null
}

/** 传给 ConfigSection 的合成 section：直接复用后端下发的表单结构。
 * 动态选项字段（如 PanSou 的限定频道/插件）在这里做 resolveDynamicField
 * 注入，与配置页 createSearchSection 同款；不做这一步 field.items
 * 永远是空，勾选弹窗只会显示“没有可选项”。 */
const formSection = computed(() => {
  const schema = schemaFor(form.type)
  return {
    value: form.type,
    title: typeLabel(form.type),
    groups: (schema?.groups || []).map((group) => ({
      ...group,
      fields: (Array.isArray(group.fields) ? group.fields : []).map((field) =>
        resolveDynamicField(field, channelOptions),
      ),
    })),
  }
})

const isSearchType = computed(() => form.type !== "tg_channel")

function channelLine(item) {
  if (item.type === "tg_channel") return item.config?.channel || "未设置频道"
  const config = item.config || {}
  const first = Object.entries(config).find(
    ([, value]) => value !== "" && value !== null && value !== undefined
      && !(Array.isArray(value) && !value.length),
  )
  if (!first) return "未配置"
  const value = Array.isArray(first[1]) ? first[1].join(", ") : String(first[1])
  return value.length > 60 ? `${value.slice(0, 60)}…` : value
}

function relativeTime(value) {
  const stamp = Number(value || 0)
  if (!stamp) return "从未"
  const diff = Math.max(0, Math.floor(Date.now() / 1000 - stamp))
  if (diff < 60) return "刚刚"
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`
  return `${Math.floor(diff / 86400)} 天前`
}

const filtered = computed(() => {
  const keyword = search.value.trim().toLowerCase()
  return channels.value.filter((item) => {
    if (statusFilter.value === "enabled" && !item.enabled) return false
    if (statusFilter.value === "disabled" && item.enabled) return false
    if (!keyword) return true
    const haystack = [
      item.name,
      item.type,
      typeLabel(item.type),
      item.config?.channel,
      channelLine(item),
    ]
      .filter(Boolean)
      .join(" ")
      .toLowerCase()
    return haystack.includes(keyword)
  })
})

/** 详情抽屉里的配置行：按 schema 字段文案展示，密码类打码。 */
const detailLines = computed(() => {
  const item = detail.value
  if (!item) return []
  if (item.type === "tg_channel") {
    const config = item.config || {}
    return [
      { title: "频道", value: config.channel || "-" },
      {
        title: "包含关键词",
        value: (config.keywords || []).join(", ") || "不过滤",
      },
      {
        title: "排除关键词",
        value: (config.exclude_keywords || []).join(", ") || "-",
      },
      { title: "回溯条数", value: String(config.scan_limit || 20) },
    ]
  }
  const schema = schemaFor(item.type)
  const lines = []
  const config = item.config || {}
  const fields = (schema?.groups || []).flatMap((group) => group.fields || [])
  for (const field of fields) {
    const value = config[field.key]
    if (value === "" || value === null || value === undefined) continue
    if (Array.isArray(value) && !value.length) continue
    lines.push({
      title: field.label || field.key,
      value:
        field.type === "password"
          ? "••••••"
          : Array.isArray(value)
            ? value.join(", ")
            : String(value),
    })
  }
  return lines
})

function toggleSelect(id, value) {
  const next = new Set(selected.value)
  if (value) next.add(id)
  else next.delete(id)
  selected.value = next
}

async function load() {
  loading.value = true
  try {
    const data = await request("get", `plugin/${props.pluginId}/channels`)
    channels.value = data?.items || []
    catalog.value = data?.catalog || []
    schemas.value = data?.schemas || []
    loaded.value = true
  } catch (error) {
    notify(`加载渠道失败：${error.message}`, "error")
  } finally {
    loading.value = false
  }
}

async function loadSummary() {
  try {
    const data = await request("get", `plugin/${props.pluginId}/channels/summary`)
    Object.assign(summary, {
      total: data?.total || 0,
      enabled: data?.enabled || 0,
      search_total: data?.search_total || 0,
      search_enabled: data?.search_enabled || 0,
      tg_total: data?.tg_total || 0,
      tg_enabled: data?.tg_enabled || 0,
      tg_monitor_enabled: Boolean(data?.tg_monitor_enabled),
    })
  } catch (error) {
    /* 汇总失败不影响列表 */
  }
}

function applySchemaDefaults(config, type) {
  const schema = schemaFor(type)
  for (const group of schema?.groups || []) {
    for (const field of group.fields || []) {
      if (!field?.key) continue
      if (config[field.key] === undefined && field.default !== undefined && field.default !== null) {
        config[field.key] = JSON.parse(JSON.stringify(field.default))
      }
    }
  }
  return config
}

function openCreate() {
  editing.value = null
  const next = defaultForm()
  if (catalog.value.length) {
    next.type = catalog.value.some((entry) => entry.key === "pansou")
      ? "pansou"
      : catalog.value[0].key
    next.name = typeLabel(next.type)
  }
  Object.assign(form, next)
  form.config = applySchemaDefaults({}, form.type)
  dialogVisible.value = true
  void loadChannelAccounts()
  void preloadChannelDynamicOptions()
}

function onTypeChange(value) {
  if (editing.value) return
  form.type = value
  form.priority = value === "tg_channel" ? 30 : 50
  form.name = typeLabel(value)
  form.config = applySchemaDefaults({}, value)
  form.tg = { channel: "", keywords: "", exclude_keywords: "", scan_limit: 20 }
  void loadChannelAccounts()
  void preloadChannelDynamicOptions()
}

function openEdit(item) {
  editing.value = item
  const config = applySchemaDefaults({ ...(item.config || {}) }, item.type)
  Object.assign(form, {
    name: item.name,
    type: item.type,
    priority: item.priority ?? 0,
    enabled: !!item.enabled,
    config,
    tg: {
      channel: item.type === "tg_channel" ? String(item.config?.channel || "") : "",
      keywords: item.type === "tg_channel" ? (item.config?.keywords || []).join(", ") : "",
      exclude_keywords:
        item.type === "tg_channel" ? (item.config?.exclude_keywords || []).join(", ") : "",
      scan_limit: item.type === "tg_channel" ? Number(item.config?.scan_limit || 20) : 20,
    },
  })
  dialogVisible.value = true
  void loadChannelAccounts()
  void preloadChannelDynamicOptions()
}

function splitList(text) {
  return String(text || "")
    .split(/[,，\n]+/)
    .map((item) => item.trim())
    .filter(Boolean)
}

function buildPayload() {
  if (form.type === "tg_channel") {
    return {
      name: form.name.trim(),
      type: "tg_channel",
      priority: Number(form.priority || 0),
      enabled: !!form.enabled,
      config: {
        channel: form.tg.channel.trim(),
        keywords: splitList(form.tg.keywords),
        exclude_keywords: splitList(form.tg.exclude_keywords),
        scan_limit: Number(form.tg.scan_limit || 20),
      },
    }
  }
  return {
    name: form.name.trim(),
    type: form.type,
    priority: Number(form.priority || 0),
    enabled: !!form.enabled,
    config: { ...form.config },
  }
}

async function submit() {
  const payload = buildPayload()
  if (!payload.name) {
    notify("请填写渠道名称", "warning")
    return
  }
  if (payload.type === "tg_channel" && !payload.config.channel) {
    notify("请填写频道标识", "warning")
    return
  }
  saving.value = true
  try {
    if (editing.value) {
      await request("put", `plugin/${props.pluginId}/channels/${editing.value.id}`, payload)
      notify("渠道已更新")
    } else {
      await request("post", `plugin/${props.pluginId}/channels`, payload)
      notify("渠道已创建")
    }
    dialogVisible.value = false
    await load()
    await loadSummary()
  } catch (error) {
    notify(`保存失败：${error.message}`, "error")
  } finally {
    saving.value = false
  }
}

async function setEnabled(item, value) {
  try {
    await request("put", `plugin/${props.pluginId}/channels/${item.id}`, { enabled: !!value })
    item.enabled = !!value
    await loadSummary()
  } catch (error) {
    notify(`更新失败：${error.message}`, "error")
  }
}

async function bulkToggle(enabled) {
  const ids = Array.from(selected.value)
  if (!ids.length) return
  let ok = 0
  let fail = 0
  for (const id of ids) {
    try {
      await request("put", `plugin/${props.pluginId}/channels/${id}`, { enabled })
      ok += 1
    } catch (error) {
      fail += 1
    }
  }
  selected.value = new Set()
  notify(`成功 ${ok} / 失败 ${fail}`, fail ? "warning" : "success")
  await load()
  await loadSummary()
}

async function testChannel(item) {
  if (testingId.value) return
  testingId.value = item.id
  try {
    const data = await request("post", `plugin/${props.pluginId}/channels/${item.id}/test`)
    notify(data?.message || "测试完成")
    await load()
  } catch (error) {
    notify(`测试失败：${error.message}`, "error")
  } finally {
    testingId.value = ""
  }
}

async function scanChannel(item) {
  if (scanningId.value) return
  scanningId.value = item.id
  try {
    const data = await request("post", `plugin/${props.pluginId}/channels/${item.id}/scan`)
    notify(data?.message || "扫描完成")
    await load()
    await loadSummary()
  } catch (error) {
    notify(`扫描失败：${error.message}`, "error")
  } finally {
    scanningId.value = ""
  }
}

function handleFormTestSource() {
  if (editing.value) {
    void testChannel(editing.value)
    return
  }
  notify("请先保存渠道，再在列表里点测试", "warning")
}

function askDelete(item) {
  deleting.value = item
  deleteVisible.value = true
}

async function confirmDelete() {
  if (!deleting.value) return
  deletingLoading.value = true
  try {
    await request("delete", `plugin/${props.pluginId}/channels/${deleting.value.id}`)
    notify("渠道已删除")
    deleteVisible.value = false
    deleting.value = null
    await load()
    await loadSummary()
  } catch (error) {
    notify(`删除失败：${error.message}`, "error")
  } finally {
    deletingLoading.value = false
  }
}

function openDetail(item) {
  detail.value = item
  detailOpen.value = true
}

function editFromDetail() {
  const item = detail.value
  detailOpen.value = false
  if (item) openEdit(item)
}

watch(
  () => props.active,
  (value) => {
    if (value && !loaded.value) {
      void load()
      void loadSummary()
    }
  },
)

onMounted(() => {
  if (props.active) {
    void load()
    void loadSummary()
  }
  window.addEventListener("message", handleHdhiveOAuthMessage)
})

onUnmounted(() => {
  window.removeEventListener("message", handleHdhiveOAuthMessage)
})
</script>

<style scoped>
.channel-manager {
  display: flex;
  flex-direction: column;
  min-height: 0;
  height: 100%;
  padding: 12px;
  overflow-y: auto;
}

.channel-toolbar {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
  flex-wrap: wrap;
}

@media (max-width: 599px) {
  .channel-toolbar {
    justify-content: space-between;
  }
}

</style>
