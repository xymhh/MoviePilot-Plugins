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
        <v-card variant="outlined" class="channel-card">
          <v-card-item class="pb-1">
            <template #prepend>
              <v-checkbox-btn
                :model-value="selected.has(item.id)"
                density="compact"
                hide-details
                @update:model-value="toggleSelect(item.id, $event)"
              />
            </template>
            <v-card-title class="channel-title" :title="item.name" @click="openDetail(item)">
              {{ item.name }}
            </v-card-title>
            <template #append>
              <v-chip size="x-small" :color="typeColor(item.type)" variant="flat">
                {{ typeLabel(item.type) }}
              </v-chip>
            </template>
          </v-card-item>
          <v-card-text class="py-1 channel-body" @click="openDetail(item)">
            <div class="text-caption text-truncate">{{ channelLine(item) }}</div>
            <div class="text-caption text-medium-emphasis">优先级 {{ item.priority }}</div>
          </v-card-text>
          <v-card-text class="py-1">
            <div class="d-flex flex-wrap ga-1">
              <v-chip size="x-small" :color="item.enabled ? 'success' : 'default'" variant="tonal">
                {{ item.enabled ? "已启用" : "已停用" }}
              </v-chip>
              <v-chip size="x-small" variant="tonal" prepend-icon="mdi-clock-outline">
                {{ relativeTime(item.stats?.last_scan) }}
              </v-chip>
              <v-chip size="x-small" variant="tonal">命中 {{ item.stats?.hits || 0 }}</v-chip>
              <v-chip
                v-if="item.stats?.last_error"
                size="x-small"
                color="error"
                variant="tonal"
                :title="item.stats.last_error"
              >
                异常
              </v-chip>
            </div>
          </v-card-text>
          <v-card-actions class="pt-0">
            <v-switch
              :model-value="item.enabled"
              density="compact"
              hide-details
              color="success"
              class="channel-switch"
              @update:model-value="setEnabled(item, $event)"
            />
            <v-spacer />
            <v-btn
              icon="mdi-access-point"
              size="small"
              variant="text"
              title="测试"
              :loading="testingId === item.id"
              @click="testChannel(item)"
            />
            <v-btn
              icon="mdi-radar"
              size="small"
              variant="text"
              title="立即扫描"
              :disabled="item.type !== 'tg_channel'"
              :loading="scanningId === item.id"
              @click="scanChannel(item)"
            />
            <v-btn icon="mdi-pencil" size="small" variant="text" title="编辑" @click="openEdit(item)" />
            <v-btn
              icon="mdi-delete"
              size="small"
              variant="text"
              color="error"
              title="删除"
              @click="askDelete(item)"
            />
          </v-card-actions>
        </v-card>
      </v-col>
    </v-row>

    <v-card v-else variant="tonal" class="pa-6 text-center">
      <v-icon icon="mdi-rss" size="40" class="mb-2" />
      <div class="text-body-2 mb-3">
        {{ channels.length ? "没有匹配的渠道" : "还没有渠道，先新增一个" }}
      </div>
      <v-btn color="primary" size="small" prepend-icon="mdi-plus" @click="openCreate">新增渠道</v-btn>
    </v-card>

    <v-navigation-drawer
      v-if="detail"
      :model-value="detailOpen"
      temporary
      location="right"
      :width="isMobile ? undefined : 380"
      @update:model-value="detailOpen = $event"
    >
      <div class="d-flex align-center pa-3">
        <div class="text-subtitle-1">渠道详情</div>
        <v-spacer />
        <v-btn icon="mdi-close" size="small" variant="text" @click="detailOpen = false" />
      </div>
      <v-divider />
      <v-list density="compact">
        <v-list-item title="名称" :subtitle="detail.name" />
        <v-list-item title="类型" :subtitle="typeLabel(detail.type)" />
        <v-list-item title="状态" :subtitle="detail.enabled ? '已启用' : '已停用'" />
        <v-list-item title="优先级" :subtitle="String(detail.priority)" />
        <v-list-item
          v-for="line in detailLines"
          :key="line.title"
          :title="line.title"
          :subtitle="line.value"
        />
        <v-list-item title="命中" :subtitle="String(detail.stats?.hits || 0)" />
        <v-list-item title="最近扫描" :subtitle="relativeTime(detail.stats?.last_scan)" />
        <v-list-item v-if="detail.stats?.last_error" title="最近错误" :subtitle="detail.stats.last_error" />
      </v-list>
      <div class="pa-3 d-flex ga-2">
        <v-btn variant="text" @click="detailOpen = false">关闭</v-btn>
        <v-spacer />
        <v-btn color="primary" @click="editFromDetail">编辑</v-btn>
      </div>
    </v-navigation-drawer>

    <v-dialog v-model="dialogVisible" :fullscreen="isMobile" max-width="720" scrollable>
      <v-card>
        <v-card-title>{{ editing ? "编辑渠道" : "新增渠道" }}</v-card-title>
        <v-card-subtitle>渠道保存后立即生效，参与搜索与监控</v-card-subtitle>
        <v-card-text>
          <v-row dense>
            <v-col cols="12" sm="6">
              <v-text-field v-model="form.name" label="名称" density="compact" variant="outlined" />
            </v-col>
            <v-col cols="12" sm="6">
              <v-select
                v-model="form.type"
                :items="typeOptions"
                item-title="label"
                item-value="value"
                label="类型"
                density="compact"
                variant="outlined"
                :disabled="!!editing"
                @update:model-value="onTypeChange"
              />
            </v-col>
            <v-col cols="12" sm="6">
              <v-text-field
                v-model.number="form.priority"
                label="优先级（越大越优先）"
                type="number"
                density="compact"
                variant="outlined"
              />
            </v-col>
            <v-col cols="12" sm="6" class="d-flex align-center">
              <v-switch v-model="form.enabled" label="启用" color="success" hide-details />
            </v-col>
          </v-row>

          <template v-if="form.type === 'tg_channel'">
            <v-row dense>
              <v-col cols="12">
                <v-text-field
                  v-model="form.tg.channel"
                  label="频道标识"
                  placeholder="@channel 或 https://t.me/xxx"
                  density="compact"
                  variant="outlined"
                />
              </v-col>
              <v-col cols="12" sm="6">
                <v-text-field
                  v-model.number="form.tg.scan_limit"
                  label="回溯条数"
                  type="number"
                  min="1"
                  max="200"
                  density="compact"
                  variant="outlined"
                />
              </v-col>
              <v-col cols="12">
                <v-text-field
                  v-model="form.tg.keywords"
                  label="包含关键词（逗号分隔，留空不过滤）"
                  density="compact"
                  variant="outlined"
                />
              </v-col>
              <v-col cols="12">
                <v-text-field
                  v-model="form.tg.exclude_keywords"
                  label="排除关键词（逗号分隔）"
                  density="compact"
                  variant="outlined"
                />
              </v-col>
            </v-row>
          </template>
          <ConfigSection
            v-else-if="formSection.groups.length"
            :section="formSection"
            :config="form.config"
            :options="{}"
            :api="api"
            @notify="notify"
            @test-source="handleFormTestSource"
          />
          <v-alert v-else type="info" variant="tonal" density="compact" class="mt-2">
            该搜索渠道没有可配置项，保存后即可参与搜索。
          </v-alert>
        </v-card-text>
        <v-card-actions>
          <v-btn variant="text" @click="dialogVisible = false">取消</v-btn>
          <v-spacer />
          <v-btn color="primary" :loading="saving" @click="submit">保存</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

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
import { computed, onMounted, reactive, ref, watch } from "vue"
import { useDisplay } from "vuetify"

import ConfigSection from "../config/ConfigSection.vue"

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

/** 传给 ConfigSection 的合成 section：直接复用后端下发的表单结构。 */
const formSection = computed(() => {
  const schema = schemaFor(form.type)
  return {
    value: form.type,
    title: typeLabel(form.type),
    groups: schema?.groups || [],
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
}

function onTypeChange(value) {
  if (editing.value) return
  form.type = value
  form.priority = value === "tg_channel" ? 30 : 50
  form.name = typeLabel(value)
  form.config = applySchemaDefaults({}, value)
  form.tg = { channel: "", keywords: "", exclude_keywords: "", scan_limit: 20 }
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

.channel-card {
  min-height: 150px;
}

.channel-title {
  font-size: 14px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  cursor: pointer;
}

.channel-body {
  cursor: pointer;
}

.channel-switch {
  flex: 0 0 auto;
}
</style>
