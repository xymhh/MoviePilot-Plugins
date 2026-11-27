<template>
  <div class="tg-monitor">
    <div class="tg-toolbar d-flex align-center justify-space-between flex-wrap ga-2">
      <div class="tg-status d-flex align-center ga-2">
        <span :class="['status-badge', initialLoading ? 'status-badge-loading' : status.enabled ? 'status-badge-active' : 'status-badge-idle']">
          <span class="status-dot"></span>
          {{ initialLoading ? "加载中" : status.enabled ? "正在监控" : "未启用" }}
        </span>
        <span v-if="!initialLoading" class="tg-meta text-caption text-medium-emphasis">
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
        <v-btn
          v-if="!status.configured || (status.importable_channels || 0) > 0"
          class="tg-import-x"
          size="small"
          variant="tonal"
          prepend-icon="mdi-database-import-outline"
          :loading="importing"
          title="把同一台机器上 CloudSubscribeX 的 TG 监控配置（登录会话、频道、关键词、画质偏好）搬过来，不用重新登录"
          @click="importFromX">
          从 X 导入配置
        </v-btn>
        <v-menu location="bottom end">
          <template #activator="{ props: menuProps }">
            <v-btn v-bind="menuProps" size="small" variant="tonal" icon="mdi-dots-vertical" title="TG 账号设置" />
          </template>
          <v-list density="compact">
            <v-list-item
              prepend-icon="mdi-account-key-outline"
              :title="status.configured ? '重新登录 Telegram' : '登录 Telegram'"
              @click="openLogin" />
            <v-list-item v-if="status.configured" prepend-icon="mdi-logout" title="退出登录" @click="logout" />
          </v-list>
        </v-menu>
      </div>
    </div>

    <v-alert v-if="status.restart_required" type="info" variant="tonal" density="compact" class="mt-2">
      插件代码已更新，但进程里仍是旧模块（{{ restartDroppedText }}）。正在跑的转存可能被中断或降级， 重启 MoviePilot
      容器后彻底生效；被中断的条目重启后会自动续跑。
    </v-alert>

    <v-alert v-if="status.last_error" type="warning" variant="tonal" density="compact" class="mt-2">
      {{ status.last_error }}
    </v-alert>
    <v-alert v-if="!status.enabled && !initialLoading" type="info" variant="tonal" density="compact" class="mt-2">
      频道监控未启用：到「配置 → 频道监控」填写 TG api_id / api_hash、登录，并添加频道后启用。
    </v-alert>

    <!-- v1.25.0 Issue B1：首次加载只显示骨架，不放全零占位与未启用提示 -->
    <v-skeleton-loader v-if="initialLoading" type="card, list-item@3" class="mt-3" />

    <!-- 摘要：数字即筛选入口 -->
    <div v-if="!initialLoading" class="tg-stats mt-3">
      <button
        v-for="stat in summaryStats"
        :key="stat.value"
        type="button"
        :class="['tg-stat', `tg-stat-${stat.tone}`, { 'is-active': statusFilter === stat.value }]"
        @click="selectFilter(stat.value)">
        <span class="tg-stat-value">{{ stat.count }}</span>
        <span class="tg-stat-label">{{ stat.title }}</span>
      </button>
      <button
        type="button"
        :class="['tg-stat', 'tg-stat-plain', { 'is-active': statusFilter === 'all' }]"
        @click="selectFilter('all')">
        <span class="tg-stat-value">{{ items.length }}</span>
        <span class="tg-stat-label">全部</span>
      </button>
    </div>
    <!-- v1.25.0 Issue B3：“失败”是“待处理”的子集（待处理含 failed），数字会被重复计，这里注明口径 -->
    <div v-if="!initialLoading" class="text-caption text-medium-emphasis mt-1">失败已计入待处理</div>

    <!-- v1.28.0：多选批量转存（错过的条目补转） -->
    <div v-if="selectedCount && !initialLoading" class="tg-batch-bar mt-2">
      <span class="text-body-2">已选 {{ selectedCount }} 条</span>
      <span class="flex-grow-1" />
      <v-btn size="small" variant="text" @click="clearSelection">取消选择</v-btn>
      <v-btn
        class="tg-batch-transfer"
        size="small"
        color="primary"
        variant="flat"
        prepend-icon="mdi-cloud-download-outline"
        :loading="batchTransferring"
        @click="transferSelected">
        转存所选
      </v-btn>
    </div>

    <!-- 未转存：先按原因分类，再按订阅折叠，避免 50+ 条重复分享铺屏 -->
    <div v-if="statusFilter === 'skipped' && !initialLoading" class="mt-3">
      <div v-if="skipReasonStats.length" class="tg-reasons">
        <button
          type="button"
          :class="['tg-reason', { 'is-active': !reasonFilter }]"
          @click="reasonFilter = ''">
          全部原因 <b>{{ skippedItems.length }}</b>
        </button>
        <button
          v-for="reason in skipReasonStats"
          :key="reason.key"
          type="button"
          :class="['tg-reason', `tg-reason-${reason.tone}`, { 'is-active': reasonFilter === reason.key }]"
          @click="reasonFilter = reasonFilter === reason.key ? '' : reason.key">
          {{ reason.label }} <b>{{ reason.count }}</b>
        </button>
      </div>
      <div class="tg-groups">
        <div v-for="group in skippedGroups" :key="group.key" class="tg-group rounded border">
          <button type="button" class="tg-group-head" @click="toggleGroup(group.key)">
            <span class="tg-group-title">{{ group.name }}</span>
            <span class="tg-group-meta">{{ group.items.length }} 个分享 · {{ group.reasonText }}</span>
            <span class="text-caption text-medium-emphasis flex-shrink-0 d-none d-sm-inline">
              最近 {{ formatRelative(group.latest) }}
            </span>
            <v-icon :icon="expandedGroup === group.key ? 'mdi-chevron-up' : 'mdi-chevron-down'" size="18" />
          </button>
          <div v-if="expandedGroup === group.key" class="tg-group-body">
            <div class="tg-group-tools">
              <v-btn size="x-small" variant="text" prepend-icon="mdi-checkbox-multiple-marked-outline" @click="selectGroup(group)">
                选择本组可转存
              </v-btn>
            </div>
            <div v-for="item in group.items" :key="item.id" class="tg-group-item">
              <div class="tg-group-row" @click="toggleDetail(item.id)">
                <v-checkbox-btn
                  v-if="canTransfer(item)"
                  class="tg-select flex-shrink-0"
                  density="compact"
                  :model-value="isSelected(item)"
                  @click.stop
                  @update:model-value="toggleSelect(item)" />
                <span class="tg-group-row-title text-truncate">{{ item.title }}</span>
                <v-chip
                  size="x-small"
                  variant="tonal"
                  :color="reasonTone(item)"
                  class="flex-shrink-0 d-none d-sm-inline-flex">
                  {{ reasonLabel(item) }}
                </v-chip>
                <span class="text-caption text-medium-emphasis flex-shrink-0 d-none d-sm-inline">
                  {{ formatRelative(item.published_at || item.created_at) }}
                </span>
                <v-btn
                  v-if="canTransfer(item)"
                  class="tg-transfer-btn flex-shrink-0"
                  size="x-small"
                  color="primary"
                  variant="tonal"
                  :loading="isBusy(item)"
                  @click.stop="openTransfer(item)">
                  转存
                </v-btn>
                <v-icon
                  :icon="expandedItem === item.id ? 'mdi-chevron-up' : 'mdi-chevron-down'"
                  size="16"
                  class="text-medium-emphasis flex-shrink-0" />
              </div>
              <TgItemDetail
                v-if="expandedItem === item.id"
                :item="item"
                :subscribe-name="subscribeLabel(item)"
                :can-transfer="canTransfer(item)"
                :transfer-hint="transferBlockReason(item)"
                :busy="isBusy(item)"
                :format-time="formatTime"
                @transfer="openTransfer"
                @ignore="ignore"
                @remove="remove"
                @copy="copyLink" />
            </div>
          </div>
        </div>
        <div v-if="!skippedGroups.length" class="tg-empty">
          {{ reasonFilter ? "这个原因下没有记录。" : "还没有「未转存」记录。" }}
        </div>
      </div>
    </div>

    <!-- 条目：默认只显示待处理 + 失败，一行一个重点 -->
    <div v-else-if="filteredItems.length && !initialLoading" class="tg-radar-stream mt-3">
      <div v-for="item in pagedItems" :key="item.id" class="tg-row-wrap">
        <div class="tg-row d-flex align-center ga-2 px-3 rounded border" @click="toggleDetail(item.id)">
          <v-checkbox-btn
            v-if="canTransfer(item)"
            class="tg-select flex-shrink-0"
            density="compact"
            :model-value="isSelected(item)"
            @click.stop
            @update:model-value="toggleSelect(item)" />
          <span :class="['tg-dot', `tg-dot-${item.status}`]" />
          <span class="tg-row-title text-body-2 font-weight-bold text-truncate">{{ item.title || "（无标题）" }}</span>
          <v-chip v-if="qualityLabel(item)" size="x-small" variant="tonal" color="deep-purple" class="flex-shrink-0">
            {{ qualityLabel(item) }}
          </v-chip>
          <span class="tg-row-meta text-caption text-medium-emphasis d-none d-sm-inline">
            {{ item.channel_title || "频道" }}
          </span>
          <span class="tg-row-meta text-caption text-medium-emphasis d-none d-md-inline">
            {{ formatRelative(item.published_at || item.created_at) }}
          </span>
          <span class="flex-grow-1" />
          <v-chip v-if="item.status === 'failed'" size="x-small" color="error" variant="flat" class="flex-shrink-0">
            {{ failureStageLabel(item) }}
          </v-chip>
          <v-btn
            v-if="canTransfer(item)"
            size="x-small"
            color="primary"
            variant="flat"
            class="tg-transfer-btn flex-shrink-0"
            :loading="isBusy(item)"
            @click.stop="openTransfer(item)">
            转存
          </v-btn>
          <v-icon v-else-if="item.status === 'transferred'" icon="mdi-check-circle" color="success" size="18" />
          <v-progress-circular
            v-else-if="IN_PROGRESS.includes(item.status)"
            indeterminate
            size="16"
            width="2"
            color="primary"
            class="flex-shrink-0" />
          <v-icon
            :icon="expandedItem === item.id ? 'mdi-chevron-up' : 'mdi-chevron-down'"
            size="18"
            class="text-medium-emphasis" />
        </div>
        <TgItemDetail
          v-if="expandedItem === item.id"
          :item="item"
          :subscribe-name="subscribeLabel(item)"
          :can-transfer="canTransfer(item)"
          :transfer-hint="transferBlockReason(item)"
          :busy="isBusy(item)"
          :format-time="formatTime"
          @transfer="openTransfer"
          @ignore="ignore"
          @remove="remove"
          @copy="copyLink" />
      </div>
      <div v-if="pageCount > 1" class="tg-pager">
        <v-pagination v-model="page" :length="pageCount" :total-visible="5" density="compact" size="small" />
        <span class="tg-pager-meta">第 {{ page }}/{{ pageCount }} 页 · 共 {{ filteredItems.length }} 条</span>
      </div>
    </div>
    <div v-else-if="!initialLoading" class="tg-empty">
      {{
        items.length
          ? "当前筛选下没有记录，换个状态看看。"
          : "还没有抓到资源。启用监控后点「立即扫描」，或等后台定时任务抓取。"
      }}
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
            转存会放回频道监控转存队列，按该订阅的季集规则识别、转存、整理；结果和原因会写回这条记录。
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
import { computed, onMounted, onUnmounted, ref, watch } from "vue"
import TgItemDetail from "./TgItemDetail.vue"

const props = defineProps({
  api: { type: Object, required: true },
  notify: { type: Function, default: () => {} },
  active: { type: Boolean, default: false },
  pluginId: { type: String, default: "CloudSubscribeLite" },
})
const status = ref({
  enabled: false,
  configured: false,
  importable_channels: 0,
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
const statusFilter = ref("todo")
// v1.25.0 Issue B1：首次加载状态。status 默认 enabled:false，数据回来前
// 不能当成“未启用”来展示（否则全零统计卡 + 未启用提示先闪一下）。
// 只覆盖首次加载，之后每 60 秒的轮询刷新不触发。
const initialLoading = ref(true)
const expandedItem = ref("")
const expandedGroup = ref("")
const reasonFilter = ref("")
const page = ref(1)
const PAGE_SIZE = 30
const scanning = ref(false)
const retrying = ref(false)
const importing = ref(false)
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
// v1.28.0 手动转存：选中项、提交中的条目、提交后快速轮询跟踪的条目。
const selectedIds = ref([])
const busyIds = ref([])
const batchTransferring = ref(false)
const watchedIds = ref([])
let watchTimer = null
let watchStartedAt = 0
const WATCH_INTERVAL_MS = 5000
const WATCH_MAX_MS = 15 * 60 * 1000
// 与后端 MANUAL_TRANSFER_STATUSES 同口径：这些状态可以手动（再）转存。
const MANUAL_TRANSFER_STATUSES = ["new", "pending", "skipped", "failed", "ignored"]
const IN_PROGRESS = ["queued", "transferring", "processing"]

const hasApi = computed(() => Boolean(status.value.has_api))

// 「有动作的」= 待处理 + 已转存 + 失败；未转存和已忽略属于历史/噪音，默认不展示，
// 否则 170 条里 150 多条是这两类，真实待办的几条会被淹没。
// 「待处理」把失败也收进来：这两类才需要人动手，其余只做历史。
const FILTERS = [
  { value: "todo", title: "待处理", test: (item) => ["queued", "new", "failed"].includes(item.status) },
  // v1.25.0 Issue B3：进行中（transferring/processing）既无卡也无筛选，这里补上。
  { value: "in-flight", title: "进行中", test: (item) => ["transferring", "processing"].includes(item.status) },
  { value: "failed", title: "失败", test: (item) => item.status === "failed" },
  // Phase 4 第 7 项：失败分阶段筛选。老数据没有 failure_stage 字段，
  // 默认按订阅失败算（历史上唯一的失败写入方是 _apply_group_outcome）。
  { value: "failed-subscribe", title: "订阅失败", test: (item) => item.status === "failed" && item.failure_stage !== "postprocess" },
  { value: "failed-postprocess", title: "后处理失败", test: (item) => item.status === "failed" && item.failure_stage === "postprocess" },
  { value: "transferred", title: "已转存", test: (item) => item.status === "transferred" },
  { value: "skipped", title: "未转存", test: (item) => item.status === "skipped" },
  { value: "ignored", title: "已忽略", test: (item) => item.status === "ignored" },
  { value: "all", title: "全部", test: () => true },
]

// Phase 4 第 7 项：失败按阶段显示——订阅处理阶段失败与后处理失败分开。
function failureStageLabel(item) {
  if (item.failure_stage === "postprocess") return "后处理失败"
  if (item.failure_stage === "subscribe") return "订阅失败"
  return "失败"
}

// 顶部统计卡：待处理 / 进行中 / 已转存 / 未转存 / 已忽略 / 失败。数字本身就是筛选按钮。
// “失败”是“待处理”的子集（待处理含 failed），卡下方已注明口径。
const summaryStats = computed(() => {
  const rows = items.value || []
  return [
    {
      value: "todo",
      title: "待处理",
      tone: "primary",
      count: rows.filter((i) => ["queued", "new", "failed"].includes(i.status)).length,
    },
    {
      value: "in-flight",
      title: "进行中",
      tone: "primary",
      count: rows.filter((i) => ["transferring", "processing"].includes(i.status)).length,
    },
    {
      value: "transferred",
      title: "已转存",
      tone: "success",
      count: rows.filter((i) => i.status === "transferred").length,
    },
    { value: "skipped", title: "未转存", tone: "muted", count: rows.filter((i) => i.status === "skipped").length },
    // v1.25.0 Issue B3：FILTERS 里早有“已忽略”筛选，这里补上对应的统计卡。
    { value: "ignored", title: "已忽略", tone: "muted", count: rows.filter((i) => i.status === "ignored").length },
    { value: "failed", title: "失败", tone: "error", count: rows.filter((i) => i.status === "failed").length },
    // Phase 4 第 7 项：失败按阶段拆两张卡（与 FILTERS 的分阶段筛选对应）。
    {
      value: "failed-subscribe",
      title: "订阅失败",
      tone: "error",
      count: rows.filter((i) => i.status === "failed" && i.failure_stage !== "postprocess").length,
    },
    {
      value: "failed-postprocess",
      title: "后处理失败",
      tone: "error",
      count: rows.filter((i) => i.status === "failed" && i.failure_stage === "postprocess").length,
    },
  ]
})

const failedCount = computed(() => (items.value || []).filter((item) => item.status === "failed").length)

const subscribeNameById = computed(() => {
  const map = new Map()
  for (const sub of subscribes.value || []) {
    map.set(Number(sub.id), sub)
  }
  return map
})

// 未转存的原因分类：同步给的订阅级原因 > 旧数据兜底。
const REASON_KINDS = {
  invalid: { label: "分享失效", tone: "error" },
  empty: { label: "无可用文件", tone: "warning" },
  mismatch: { label: "剧名不符", tone: "warning" },
  episode_miss: { label: "集号不符", tone: "muted" },
  unparsable: { label: "无法解析集号", tone: "muted" },
  subscribe_submitted: { label: "订阅已接管", tone: "muted" },
}

// 历史数据里存的是长文案，这里按同一套规则压成「定性词 · 证据」短句。
const LEGACY_EPISODE_MISS = /资源集号与缺失不符（S\d+）：资源含 (.*?)，缺 (.*)$/

// 「缺哪些集」永远完整，被压缩的是前面「有哪些集」。
function formatEpisodeMiss(have, miss) {
  const head = "集号不符 · "
  const list = String(have).trim()
  const first = list.split(/[、，,]/)[0].trim()
  const haveShort = first === list ? list : `${first} 等`
  for (const candidate of [`${head}有 ${haveShort}，缺 ${miss}`, `${head}缺 ${miss}`]) {
    if (candidate.length <= 26) return candidate
  }
  return `${head}缺 ${miss}`.slice(0, 24) + "…"
}

const LEGACY_REASON_RULES = [
  [/分享根目录与订阅标题不符（根目录：(.*?)）/, "剧名不符 · 分享是《$1》"],
  [/解析不出目标季集号（含 (\d+) 个视频）/, "认不出集号 · $1 个视频"],
  [/未发现需要转存的新资源/, "本轮无新集"],
  [/库中已完整存在，无需转存/, "已入库"],
  [/分享地址已失效/, "分享失效"],
  [/库中已有或未匹配到可下载资源/, "无可用资源"],
  [/订阅搜索已提交网盘任务，其中 (\d+) 个网盘文件等待/, "订阅已接管 · $1 个文件等待"],
]

function reasonText(item) {
  let text = String(item?.skip_reason || item?.auto_transfer_error || "")
    .replace(/^未转存：/, "")
    .trim()
  if (!text) return "无原因"
  const episode = text.match(LEGACY_EPISODE_MISS)
  if (episode) return formatEpisodeMiss(episode[1], episode[2])
  for (const [pattern, replacement] of LEGACY_REASON_RULES) {
    if (pattern.test(text)) {
      text = text.replace(pattern, replacement)
      break
    }
  }
  return text.length > 26 ? `${text.slice(0, 24)}…` : text
}

function reasonKey(item) {
  const kind = String(item?.skip_kind || "")
  return REASON_KINDS[kind] ? kind : reasonText(item)
}

function reasonLabel(item) {
  const kind = String(item?.skip_kind || "")
  return REASON_KINDS[kind]?.label || reasonText(item)
}

function reasonTone(item) {
  return REASON_KINDS[String(item?.skip_kind || "")]?.tone || "muted"
}

const skippedItems = computed(() => (items.value || []).filter((item) => item.status === "skipped"))

const skipReasonStats = computed(() => {
  const map = new Map()
  for (const item of skippedItems.value) {
    const key = reasonKey(item)
    const entry = map.get(key) || { key, label: reasonLabel(item), tone: reasonTone(item), count: 0 }
    entry.count += 1
    map.set(key, entry)
  }
  return [...map.values()].sort((a, b) => b.count - a.count)
})

// 未转存按订阅+季归并：52 条重复分享压成十几个可折叠分组。
const skippedGroups = computed(() => {
  const groups = new Map()
  for (const item of skippedItems.value) {
    if (reasonFilter.value && reasonKey(item) !== reasonFilter.value) continue
    const id = Number(item.subscribe_id || item.matched_subscribe_id || 0)
    const season = Number(item.quality?.season || 0)
    const key = `${id}:${season}`
    if (!groups.has(key)) {
      const sub = subscribeNameById.value.get(id)
      const reason = String(item.match_reason || "")
      const matched = reason.match(/匹配到 #\d+ (.+?)（/)
      const name = sub
        ? `${sub.name}${sub.season ? ` S${String(sub.season).padStart(2, "0")}` : ""}`
        : matched
          ? matched[1]
          : id
            ? `订阅 #${id}`
            : "未关联订阅"
      groups.set(key, { key, name, items: [], latest: 0 })
    }
    const group = groups.get(key)
    group.items.push(item)
    group.latest = Math.max(group.latest, Number(item.published_at || item.created_at || 0))
  }
  const rows = [...groups.values()]
  rows.forEach((group) => {
    const reasons = new Map()
    group.items.forEach((item) => {
      const label = reasonLabel(item)
      reasons.set(label, (reasons.get(label) || 0) + 1)
    })
    const [topLabel, topCount] = [...reasons.entries()].sort((a, b) => b[1] - a[1])[0] || ["", 0]
    group.reasonText = topLabel ? `${topLabel} ${topCount}` : ""
    group.items.sort(
      (a, b) => Number(b.published_at || b.created_at || 0) - Number(a.published_at || a.created_at || 0),
    )
  })
  return rows.sort((a, b) => b.latest - a.latest)
})

function toggleDetail(id) {
  expandedItem.value = expandedItem.value === id ? "" : id
}

function toggleGroup(key) {
  expandedGroup.value = expandedGroup.value === key ? "" : key
}

// v1.28.0：未转存 / 失败 / 已忽略 / 待处理都能手动转存（错过的条目补转）；
// 排队中、转存中、已转存不行——transferBlockReason 给出不可转存的原因。
function canTransfer(item) {
  return MANUAL_TRANSFER_STATUSES.includes(item?.status) && (item?.links || []).length > 0
}

function transferBlockReason(item) {
  if (canTransfer(item)) return ""
  if (item?.status === "transferred") return "已转存"
  if (item?.status === "queued") return "已在转存队列中"
  if (["transferring", "processing"].includes(item?.status)) return "正在转存，请稍候"
  if (!(item?.links || []).length) return "该消息没有网盘链接，无法转存"
  return "当前状态不支持转存"
}

function subscribeLabel(item) {
  const id = Number(item?.manual_subscribe_id || item?.matched_subscribe_id || item?.subscribe_id || 0)
  const sub = subscribeNameById.value.get(id)
  if (!sub) return ""
  return `${sub.name}${sub.season ? ` S${String(sub.season).padStart(2, "0")}` : ""}`
}

const selectedCount = computed(() => selectedIds.value.length)

function isSelected(item) {
  return selectedIds.value.includes(item?.id)
}

function toggleSelect(item) {
  const id = item?.id
  if (!id) return
  selectedIds.value = isSelected(item)
    ? selectedIds.value.filter((value) => value !== id)
    : [...selectedIds.value, id]
}

function selectGroup(group) {
  const ids = new Set(selectedIds.value)
  for (const item of group.items) {
    if (canTransfer(item)) ids.add(item.id)
  }
  selectedIds.value = [...ids]
}

function clearSelection() {
  selectedIds.value = []
}

function isBusy(item) {
  return busyIds.value.includes(item?.id)
}

async function copyLink(link) {
  const text = String(link || "")
  if (!text) return
  try {
    let copied = false
    if (navigator.clipboard?.writeText) {
      try {
        await navigator.clipboard.writeText(text)
        copied = true
      } catch (_) {
        // 局域网 HTTP 页面可能暴露 API 但拒绝调用，继续使用兼容方式。
      }
    }
    if (!copied) {
      const input = document.createElement("textarea")
      input.value = text
      input.setAttribute("readonly", "")
      input.style.position = "fixed"
      input.style.opacity = "0"
      document.body.appendChild(input)
      input.select()
      const ok = document.execCommand("copy")
      document.body.removeChild(input)
      if (!ok) throw new Error("浏览器拒绝访问剪贴板")
    }
    props.notify("链接已复制", "success")
  } catch (error) {
    props.notify(`复制失败：${error.message || error}`, "error")
  }
}

const restartDroppedText = computed(() => {
  const dropped = status.value.restart_dropped || []
  return dropped.length ? `已忽略参数：${dropped.join("、")}` : "部分参数被忽略"
})

const filteredItems = computed(() => {
  const active = FILTERS.find((f) => f.value === statusFilter.value) || FILTERS[0]
  return (items.value || []).filter(active.test)
})

const pageCount = computed(() => Math.max(1, Math.ceil(filteredItems.value.length / PAGE_SIZE)))

const pagedItems = computed(() => {
  const start = (page.value - 1) * PAGE_SIZE
  return filteredItems.value.slice(start, start + PAGE_SIZE)
})

function selectFilter(value) {
  statusFilter.value = value
  reasonFilter.value = ""
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

function formatRelative(timestamp) {
  if (!timestamp) return "-"
  let date
  if (typeof timestamp === "number" || /^\d+$/.test(String(timestamp).trim())) {
    const num = Number(timestamp)
    date = new Date(num > 1e11 ? num : num * 1000)
  } else {
    date = new Date(timestamp)
  }
  if (isNaN(date.getTime())) return "-"
  const diff = Math.max(0, Math.floor((Date.now() - date.getTime()) / 1000))
  if (diff < 60) return "刚刚"
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`
  if (diff < 86400 * 7) return `${Math.floor(diff / 86400)} 天前`
  return formatTime(timestamp)
}

function qualityLabel(item) {
  const label = item?.quality?.label
  return label && label !== "未知" ? label : ""
}

async function loadStatus() {
  const result = await props.api.get(`plugin/${props.pluginId}/tg/status`)
  if (result?.success) status.value = result.data || status.value
}

async function loadItems() {
  const result = await props.api.get(`plugin/${props.pluginId}/tg/items`)
  if (result?.success) {
    items.value = result.data?.items || []
    if (result.data?.status) status.value = result.data.status
  }
}

async function refreshAll() {
  // 首次加载失败也不抛：onMounted 的 finally 会退出加载态，页面按真实
  // 状态（未启用/旧数据）显示，不可一直转圈。
  try {
    await Promise.all([loadStatus(), loadItems()])
  } catch (error) {
    console.warn("[TgMonitor] 刷新失败：", error)
  }
}

async function scan() {
  if (scanning.value) return
  scanning.value = true
  try {
    const result = await props.api.post(`plugin/${props.pluginId}/tg/scan`, {})
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
    const result = await props.api.post(`plugin/${props.pluginId}/tg/retry_failed`, {})
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

async function importFromX() {
  if (importing.value) return
  importing.value = true
  try {
    const result = await props.api.post(`plugin/${props.pluginId}/tg/import_from_x`, {})
    if (result?.success) {
      props.notify(result.message || "导入完成", "success")
      await refreshAll()
    } else {
      props.notify(result?.message || "导入失败", "error")
    }
  } finally {
    importing.value = false
  }
}

async function ensureSubscribes() {
  if (subscribes.value.length) return
  loadingSubscribes.value = true
  try {
    const result = await props.api.get(`plugin/${props.pluginId}/tg/subscribes`)
    if (result?.success) subscribes.value = result.data?.subscribes || []
  } finally {
    loadingSubscribes.value = false
  }
}

async function openTransfer(item) {
  if (!canTransfer(item)) {
    props.notify(transferBlockReason(item) || "当前状态不支持转存", "warning")
    return
  }
  currentItem.value = item
  await ensureSubscribes()
  const matchedId = Number(item?.manual_subscribe_id || item?.matched_subscribe_id || item?.subscribe_id || 0)
  // 1.7.113：扫描阶段已经把订阅匹配好（matched_subscribe_id），订阅还在就
  // 直接用匹配结果转存，不再让人重新挑一遍；订阅被删/没匹配才弹窗手选。
  const matchedExists = matchedId > 0 && subscribes.value.some((row) => Number(row.id) === matchedId)
  targetSubscribeId.value = matchedExists ? matchedId : null
  if (matchedExists) {
    await postTransfer([item.id], 0)
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
    const ok = await postTransfer([currentItem.value?.id], targetSubscribeId.value)
    if (ok) transferVisible.value = false
  } finally {
    transferring.value = false
  }
}

async function transferSelected() {
  const ids = selectedIds.value.filter((id) => {
    const item = (items.value || []).find((row) => row.id === id)
    return item && canTransfer(item)
  })
  if (!ids.length) {
    props.notify("所选条目都不能转存", "warning")
    clearSelection()
    return
  }
  batchTransferring.value = true
  try {
    const ok = await postTransfer(ids, 0)
    if (ok) clearSelection()
  } finally {
    batchTransferring.value = false
  }
}

// v1.28.0：/tg/transfer 只负责把条目放回转存队列；真正转存由后端 worker
// 完成并把结果写回条目，这里提交后快速轮询，跟踪到终态再提示结果。
async function postTransfer(ids, subscribeId) {
  const wanted = ids.filter(Boolean)
  if (!wanted.length) return false
  busyIds.value = [...new Set([...busyIds.value, ...wanted])]
  try {
    const payload = wanted.length === 1 ? { id: wanted[0] } : { ids: wanted }
    if (subscribeId) payload.subscribe_id = subscribeId
    const result = await props.api.post(`plugin/${props.pluginId}/tg/transfer`, payload)
    if (result?.success) {
      props.notify(result.message || "已加入转存队列", "success")
      const accepted = (result.data?.results || []).filter((row) => row.ok).map((row) => row.id)
      startWatching(accepted.length ? accepted : wanted)
      await refreshAll()
      return true
    }
    if (wanted.length === 1 && result?.message === "请选择要转存到的订阅") {
      currentItem.value = (items.value || []).find((row) => row.id === wanted[0]) || currentItem.value
      targetSubscribeId.value = null
      await ensureSubscribes()
      transferVisible.value = true
      return false
    }
    props.notify(result?.message || "转存提交失败", "error")
    return false
  } catch (error) {
    props.notify(`转存提交失败：${error?.message || error}`, "error")
    return false
  } finally {
    busyIds.value = busyIds.value.filter((id) => !wanted.includes(id))
  }
}

function startWatching(ids) {
  watchedIds.value = [...new Set([...watchedIds.value, ...ids])]
  watchStartedAt = Date.now()
  if (watchTimer !== null) return
  watchTimer = window.setInterval(() => void pollWatched(), WATCH_INTERVAL_MS)
}

function stopWatching() {
  if (watchTimer !== null) window.clearInterval(watchTimer)
  watchTimer = null
  watchedIds.value = []
}

async function pollWatched() {
  if (!watchedIds.value.length) {
    stopWatching()
    return
  }
  await refreshAll()
  const rows = (items.value || []).filter((row) => watchedIds.value.includes(row.id))
  const done = rows.filter((row) => !IN_PROGRESS.includes(row.status))
  // 记录被删掉的也算结束跟踪。
  const finishedIds = new Set([
    ...done.map((row) => row.id),
    ...watchedIds.value.filter((id) => !rows.some((row) => row.id === id)),
  ])
  if (done.length) announceOutcome(done)
  watchedIds.value = watchedIds.value.filter((id) => !finishedIds.has(id))
  if (!watchedIds.value.length || Date.now() - watchStartedAt > WATCH_MAX_MS) stopWatching()
}

function announceOutcome(rows) {
  const transferred = rows.filter((row) => row.status === "transferred")
  const failed = rows.filter((row) => row.status === "failed")
  const skipped = rows.filter((row) => !["transferred", "failed"].includes(row.status))
  const parts = []
  if (transferred.length) parts.push(`已转存 ${transferred.length} 条`)
  if (skipped.length) parts.push(`未转存 ${skipped.length} 条（${reasonLabel(skipped[0])}）`)
  if (failed.length) {
    const reason = String(failed[0].auto_transfer_error || "转存失败").slice(0, 40)
    parts.push(`失败 ${failed.length} 条（${reason}）`)
  }
  const tone = failed.length ? "error" : transferred.length ? "success" : "warning"
  props.notify(`转存完成：${parts.join("，")}`, tone)
}

async function ignore(item) {
  const result = await props.api.post(`plugin/${props.pluginId}/tg/ignore`, { ids: [item.id] })
  if (result?.success) await refreshAll()
}

async function remove(item) {
  const result = await props.api.post(`plugin/${props.pluginId}/tg/delete`, { ids: [item.id] })
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
      const result = await props.api.post(`plugin/${props.pluginId}/tg/login/start`, { phone: phone.value })
      if (result?.success) {
        loginStep.value = "code"
        props.notify(result.data?.message || "验证码已发送", "success")
      } else {
        props.notify(result?.message || "发送验证码失败", "error")
      }
      return
    }
    const payload = loginStep.value === "code" ? { code: code.value } : { password: password.value }
    const result = await props.api.post(`plugin/${props.pluginId}/tg/login/code`, payload)
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
  const result = await props.api.post(`plugin/${props.pluginId}/tg/logout`, {})
  if (result?.success) {
    props.notify("已清除 TG 会话", "success")
    await refreshAll()
  }
}

onMounted(async () => {
  // v1.25.0 Issue B1：首次加载完成前保持加载态；失败也退出加载态
  // （refreshAll 内部已吞异常），不让页面一直转圈。
  try {
    await refreshAll()
  } finally {
    initialLoading.value = false
  }
  // 「未转存」折叠分组要显示订阅名，进页面时顺带取一次。
  void ensureSubscribes()
  timer = window.setInterval(() => {
    if (props.active) void refreshAll()
  }, 60000)
})
onUnmounted(() => {
  if (timer !== null) window.clearInterval(timer)
  stopWatching()
})
</script>

<style scoped>
.tg-stats {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 8px;
}
.tg-stat {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 2px;
  padding: 8px 10px;
  border-radius: 8px;
  border: 1px solid rgba(var(--v-theme-on-surface), 0.12);
  background: transparent;
  cursor: pointer;
  text-align: left;
  transition:
    border-color 0.15s,
    background-color 0.15s;
}
.tg-stat:hover {
  background: rgba(var(--v-theme-on-surface), 0.04);
}
.tg-stat.is-active {
  border-color: rgb(var(--v-theme-primary));
  box-shadow: inset 0 0 0 1px rgb(var(--v-theme-primary));
}
.tg-stat-value {
  font-size: 20px;
  font-weight: 700;
  line-height: 1.1;
}
.tg-stat-label {
  font-size: 11px;
  opacity: 0.65;
}
.tg-stat-primary .tg-stat-value {
  color: rgb(var(--v-theme-primary));
}
.tg-stat-success .tg-stat-value {
  color: rgb(var(--v-theme-success));
}
.tg-stat-error .tg-stat-value {
  color: rgb(var(--v-theme-error));
}
.tg-stat-muted .tg-stat-value {
  color: rgba(var(--v-theme-on-surface), 0.55);
}

.tg-row {
  min-height: 46px;
  background: rgba(var(--v-theme-surface), 0.6);
  border-color: rgba(var(--v-theme-on-surface), 0.08) !important;
  cursor: pointer;
  transition:
    background-color 0.15s,
    border-color 0.15s;
}
.tg-row:hover {
  background: rgba(var(--v-theme-surface-variant), 0.35);
  border-color: rgba(var(--v-theme-primary), 0.3) !important;
}
.tg-row-title {
  min-width: 0;
  flex: 0 1 auto;
  max-width: 62%;
}
.tg-row-meta {
  white-space: nowrap;
}
.tg-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
  background: rgba(var(--v-theme-on-surface), 0.3);
}
.tg-dot-new,
.tg-dot-queued {
  background: rgb(var(--v-theme-primary));
}
.tg-dot-transferred {
  background: rgb(var(--v-theme-success));
}
.tg-dot-failed {
  background: rgb(var(--v-theme-error));
}
.tg-dot-skipped {
  background: rgba(var(--v-theme-on-surface), 0.25);
}
.tg-dot-ignored {
  background: rgba(var(--v-theme-on-surface), 0.18);
}

.tg-reasons {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 8px;
}
.tg-reason {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 4px 10px;
  font-size: 12px;
  line-height: 1.2;
  border-radius: 999px;
  border: 1px solid rgba(var(--v-theme-on-surface), 0.14);
  background: transparent;
  color: rgba(var(--v-theme-on-surface), 0.72);
  cursor: pointer;
  transition:
    border-color 0.15s,
    color 0.15s,
    background-color 0.15s;
}
.tg-reason b {
  font-weight: 600;
  opacity: 0.85;
}
.tg-reason:hover {
  border-color: rgba(var(--v-theme-primary), 0.45);
}
.tg-reason.is-active {
  border-color: rgb(var(--v-theme-primary));
  color: rgb(var(--v-theme-primary));
  background: rgba(var(--v-theme-primary), 0.08);
}
.tg-reason-error.is-active {
  border-color: rgb(var(--v-theme-error));
  color: rgb(var(--v-theme-error));
  background: rgba(var(--v-theme-error), 0.08);
}
.tg-reason-warning.is-active {
  border-color: rgb(var(--v-theme-warning));
  color: rgb(var(--v-theme-warning));
  background: rgba(var(--v-theme-warning), 0.08);
}
.tg-groups {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.tg-group {
  background: rgba(var(--v-theme-surface), 0.6);
  border-color: rgba(var(--v-theme-on-surface), 0.08) !important;
}
.tg-group-head {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 10px 12px;
  background: transparent;
  border: 0;
  cursor: pointer;
  text-align: left;
}
.tg-group-title {
  flex: 1 1 auto;
  min-width: 0;
  font-weight: 600;
  font-size: 13px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.tg-group-meta {
  margin-left: auto;
  font-size: 11px;
  opacity: 0.6;
  white-space: nowrap;
}
.tg-group-body {
  border-top: 1px dashed rgba(var(--v-theme-on-surface), 0.1);
  padding: 6px 12px 8px;
}
.tg-group-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 3px 0;
  font-size: 12px;
}
.tg-group-row-title {
  flex: 1;
  min-width: 0;
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

.status-badge-loading {
  background: rgba(var(--v-theme-primary), 0.12);
  color: rgb(var(--v-theme-primary));
  border: 1px solid rgba(var(--v-theme-primary), 0.25);
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
  transition:
    background-color 0.15s,
    border-color 0.15s;
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

.tg-batch-bar {
  position: sticky;
  top: 0;
  z-index: 2;
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  padding: 6px 10px;
  border-radius: 8px;
  border: 1px solid rgba(var(--v-theme-primary), 0.35);
  background: rgb(var(--v-theme-surface));
}
.tg-group-tools {
  display: flex;
  justify-content: flex-end;
  margin-bottom: 2px;
}
.tg-group-item {
  min-width: 0;
}
.tg-group-row {
  cursor: pointer;
  border-radius: 6px;
}
.tg-group-row:hover {
  background: rgba(var(--v-theme-on-surface), 0.04);
}
.tg-select {
  flex: 0 0 auto;
}

@media (max-width: 600px) {
  .tg-monitor {
    padding: 10px;
  }
  .tg-stats {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .tg-row {
    min-height: 44px;
  }
  .tg-row-title {
    max-width: 100%;
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
