<template>
  <div class="tg-channel-rules">
    <div class="d-flex align-center flex-wrap ga-2 mb-2">
      <v-btn
        size="small"
        variant="tonal"
        color="primary"
        prepend-icon="mdi-tune-variant"
        @click="openDialog">
        配置频道规则
      </v-btn>
      <span class="text-caption text-medium-emphasis">
        已配置 {{ rules.length }} 个频道
        <template v-if="enabledCount < rules.length">（{{ enabledCount }} 个启用）</template>
      </span>
      <v-spacer />
      <v-btn
        v-if="rules.length"
        size="x-small"
        variant="text"
        color="error"
        @click="clearAll">
        清空
      </v-btn>
    </div>

    <div v-if="rules.length" class="d-flex flex-wrap ga-2">
      <v-chip
        v-for="rule in rules"
        :key="rule.channel"
        size="small"
        :color="rule.enabled ? 'primary' : undefined"
        :variant="rule.enabled ? 'tonal' : 'outlined'"
        @click="openDialog">
        <v-icon
          :icon="rule.enabled ? 'mdi-broadcast' : 'mdi-broadcast-off'"
          size="14"
          start />
        {{ rule.channel }}
        <span v-if="rule.keywords.length" class="ml-1 text-caption">
          · {{ rule.keywords.length }} 关键词
        </span>
        <span v-if="!rule.enabled" class="ml-1 text-caption">（停用）</span>
        <v-icon
          v-if="rule.auto_transfer"
          icon="mdi-download"
          size="13"
          class="ml-1" />
      </v-chip>
    </div>
    <div v-else class="text-caption text-medium-emphasis">
      还没有频道规则；点「配置频道规则」为每个频道单独设置关键词、排除词和回溯条数。
    </div>

    <div v-if="field.hint" class="text-caption text-medium-emphasis mt-2">
      {{ field.hint }}
    </div>

    <v-dialog v-model="dialogVisible" max-width="760" :fullscreen="isMobile" scrollable>
      <v-card :rounded="isMobile ? 0 : undefined">
        <v-card-title class="d-flex align-center">
          <v-icon icon="mdi-telegram" class="mr-2" />
          频道规则
          <v-spacer />
          <v-btn icon="mdi-close" variant="text" size="small" @click="dialogVisible = false" />
        </v-card-title>
        <v-divider />
        <v-card-text :class="isMobile ? 'px-3' : ''">
          <div v-if="!draft.length" class="text-caption text-medium-emphasis mb-3">
            每个频道一条规则；关键词命中任意一个才收录，排除词命中就跳过。
          </div>
          <v-card
            v-for="(rule, index) in draft"
            :key="rule._key"
            variant="outlined"
            class="mb-3">
            <v-card-text :class="isMobile ? 'pa-3' : ''">
              <div class="rule-head mb-2">
                <v-text-field
                  v-model="rule.channel"
                  label="频道"
                  placeholder="@channel / t.me/channel / -1001234567890"
                  density="compact"
                  variant="outlined"
                  hide-details
                  class="rule-channel" />
                <div class="rule-toggles">
                  <v-switch
                    v-model="rule.enabled"
                    label="启用"
                    density="compact"
                    color="primary"
                    hide-details />
                  <v-switch
                    v-model="rule.auto_transfer"
                    label="自动转存"
                    density="compact"
                    color="primary"
                    hide-details />
                  <v-btn
                    icon="mdi-delete-outline"
                    variant="text"
                    size="small"
                    color="error"
                    title="删除此规则"
                    @click="draft.splice(index, 1)" />
                </div>
              </div>
              <div class="rule-fields">
                <v-textarea
                  v-model="rule.keywordsText"
                  label="包含关键词"
                  placeholder="每行一个，或用逗号分隔；留空=收录所有带链接消息"
                  rows="2"
                  auto-grow
                  density="compact"
                  variant="outlined"
                  hide-details
                  class="rule-keywords" />
                <v-textarea
                  v-model="rule.excludeText"
                  label="排除关键词"
                  placeholder="每行一个，或用逗号分隔；命中就跳过"
                  rows="2"
                  auto-grow
                  density="compact"
                  variant="outlined"
                  hide-details
                  class="rule-keywords" />
                <v-text-field
                  v-model.number="rule.scan_limit"
                  label="回溯条数"
                  type="number"
                  min="1"
                  max="200"
                  density="compact"
                  variant="outlined"
                  hide-details
                  class="rule-limit" />
              </div>
              <div v-if="rule.auto_transfer" class="auto-row mt-2">
                <v-select
                  v-model.number="rule.auto_subscribe_id"
                  :items="subscribeOptions"
                  item-title="title"
                  item-value="id"
                  label="自动转存到订阅"
                  density="compact"
                  variant="outlined"
                  hide-details
                  :loading="loadingSubscribes"
                  no-data-text="没有可用订阅，请先在「订阅任务」创建"
                  class="auto-subscribe" />
                <v-btn
                  size="small"
                  variant="text"
                  icon="mdi-refresh"
                  :loading="loadingSubscribes"
                  title="刷新订阅列表"
                  @click="loadSubscribes(true)" />
              </div>
              <div
                v-if="rule.auto_transfer && !rule.auto_subscribe_id"
                class="text-caption text-warning mt-1">
                开启自动转存后必须选择一个目标订阅，否则扫描时只会记录不转存。
              </div>
            </v-card-text>
          </v-card>
          <v-btn
            variant="tonal"
            prepend-icon="mdi-plus"
            size="small"
            @click="addRule">
            添加频道
          </v-btn>
        </v-card-text>
        <v-divider />
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="dialogVisible = false">取消</v-btn>
          <v-btn color="primary" variant="flat" @click="save">保存</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>
  </div>
</template>

<script setup>
import {computed, onMounted, ref} from "vue"
import {useDisplay} from "vuetify"

const display = useDisplay()
const isMobile = computed(() => display.smAndDown)

const props = defineProps({
  modelValue: {type: [Array, String, Object], default: () => []},
  field: {type: Object, required: true},
  config: {type: Object, default: () => ({})},
  api: {type: [Object, Function], default: null},
})

const emit = defineEmits(["update:modelValue"])
const dialogVisible = ref(false)
const draft = ref([])
const subscribes = ref([])
const loadingSubscribes = ref(false)
let keySeed = 0

const subscribeOptions = computed(() => subscribes.value.map((sub) => ({
  id: Number(sub.id || sub.subscribe_id || 0),
  title: `#${sub.id || sub.subscribe_id} ${sub.name || sub.title || "未命名"}`
    + (sub.season ? ` · S${String(sub.season).padStart(2, "0")}` : "")
    + (sub.type === "电影" ? " · 电影" : ""),
})))

function splitTerms(value) {
  if (Array.isArray(value)) {
    return value.map((item) => String(item || "").trim()).filter(Boolean)
  }
  const text = String(value || "")
    .replace(/[，、;；,]/g, "\n")
  return text.split(/\r?\n/).map((item) => item.trim()).filter(Boolean)
}

function parsePayload(value) {
  if (Array.isArray(value)) return value
  if (value && typeof value === "object") return [value]
  const text = String(value || "").trim()
  if (!text) return []
  if (text.startsWith("[") || text.startsWith("{")) {
    try {
      const parsed = JSON.parse(text)
      return Array.isArray(parsed) ? parsed : [parsed]
    } catch (error) {
      return []
    }
  }
  return text.split(/\r?\n/).map((item) => item.trim()).filter(Boolean)
}

function legacyRules(config) {
  const channels = splitTerms(config?.tg_channels)
  if (!channels.length) return []
  const keywords = splitTerms(config?.tg_monitor_keywords)
  const exclude = splitTerms(config?.tg_monitor_exclude_keywords)
  const limit = Number(config?.tg_monitor_scan_limit || 20)
  return channels.map((channel) => ({
    channel,
    enabled: true,
    keywords,
    exclude_keywords: exclude,
    scan_limit: limit,
  }))
}

function normalizeRule(row) {
  if (typeof row === "string") {
    return {
      channel: row.trim(),
      enabled: true,
      auto_transfer: false,
      auto_subscribe_id: 0,
      keywordsText: "",
      excludeText: "",
      scan_limit: 20,
    }
  }
  const rule = row || {}
  return {
    channel: String(rule.channel || rule.url || rule.name || "").trim(),
    enabled: rule.enabled !== false,
    auto_transfer: rule.auto_transfer === true,
    auto_subscribe_id: Number(rule.auto_subscribe_id || 0) || 0,
    keywordsText: splitTerms(rule.keywords).join("\n"),
    excludeText: splitTerms(rule.exclude_keywords).join("\n"),
    scan_limit: Number(rule.scan_limit || 20) || 20,
  }
}

function withKey(rule) {
  return {...normalizeRule(rule), _key: ++keySeed}
}

const rules = computed(() => {
  const parsed = parsePayload(props.modelValue)
  const source = parsed.length ? parsed : legacyRules(props.config)
  return source
    .map((row) => {
      const rule = normalizeRule(row)
      return {
        channel: rule.channel,
        enabled: rule.enabled,
        auto_transfer: rule.auto_transfer,
        auto_subscribe_id: rule.auto_subscribe_id,
        keywords: splitTerms(rule.keywordsText),
        exclude_keywords: splitTerms(rule.excludeText),
        scan_limit: rule.scan_limit,
      }
    })
    .filter((rule) => rule.channel)
})

const enabledCount = computed(() => rules.value.filter((rule) => rule.enabled).length)

async function loadSubscribes(force = false) {
  if (!props.api || loadingSubscribes.value) return
  if (!force && subscribes.value.length) return
  loadingSubscribes.value = true
  try {
    const result = await props.api.get("plugin/CloudSubscribeX/tg/subscribes")
    if (result?.success) {
      subscribes.value = result.data?.subscribes || []
    }
  } catch (error) {
    // 下拉为空时保留手动输入能力，不阻塞配置保存
  } finally {
    loadingSubscribes.value = false
  }
}

onMounted(() => loadSubscribes())

function openDialog() {
  loadSubscribes()
  draft.value = rules.value.map(withKey)
  if (!draft.value.length) addRule()
  dialogVisible.value = true
}

function addRule() {
  draft.value.push(withKey({
    channel: "",
    enabled: true,
    keywordsText: "",
    excludeText: "",
    scan_limit: 20,
  }))
}

function save() {
  const seen = new Set()
  const rows = []
  for (const rule of draft.value) {
    const channel = String(rule.channel || "").trim()
    if (!channel || seen.has(channel)) continue
    seen.add(channel)
    const limit = Math.max(1, Math.min(200, Number(rule.scan_limit || 20) || 20))
    rows.push({
      channel,
      enabled: rule.enabled !== false,
      auto_transfer: rule.auto_transfer === true,
      auto_subscribe_id: Number(rule.auto_subscribe_id || 0) || 0,
      keywords: splitTerms(rule.keywordsText),
      exclude_keywords: splitTerms(rule.excludeText),
      scan_limit: limit,
    })
  }
  emit("update:modelValue", rows)
  dialogVisible.value = false
}

function clearAll() {
  emit("update:modelValue", [])
}
</script>

<style scoped>
.tg-channel-rules :deep(.v-chip) {
  cursor: pointer;
}

.rule-head {
  display: flex;
  align-items: center;
  gap: 8px;
}

.rule-channel {
  flex: 1 1 auto;
  min-width: 0;
}

.rule-toggles {
  display: flex;
  align-items: center;
  gap: 4px;
  flex: 0 0 auto;
}

.rule-fields {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  flex-wrap: wrap;
}

.rule-keywords {
  flex: 1 1 240px;
  min-width: 0;
}

.rule-limit {
  flex: 0 0 130px;
}

.auto-row {
  display: flex;
  align-items: center;
  gap: 8px;
}

.auto-subscribe {
  flex: 1 1 auto;
  min-width: 0;
}

@media (max-width: 700px) {
  .rule-head {
    flex-direction: column;
    align-items: stretch;
  }

  .rule-toggles {
    justify-content: space-between;
    width: 100%;
  }

  .rule-toggles :deep(.v-label) {
    white-space: nowrap;
  }

  .rule-fields {
    flex-direction: column;
  }

  .rule-keywords,
  .rule-limit {
    flex: 1 1 auto;
    width: 100%;
    max-width: none;
  }

  .auto-row {
    flex-direction: column;
    align-items: stretch;
  }
}
</style>
