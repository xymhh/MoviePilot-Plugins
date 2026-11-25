<template>
  <div class="cloud-subscribe-config">
    <v-card flat class="border rounded-lg config-shell">
      <div class="config-layout">
        <!-- 左侧通顶侧边栏：融合品牌标题，结构化层级与精致底座 -->
    <ConfigSidebar
      :nav-groups="navGroups"
      :active-tab="activeTab"
      :is-mobile="isMobile"
      :mobile-group-name="mobileGroupName"
      :mobile-group-items="mobileGroupItems"
      v-model:show-advanced="showAdvanced"
      :plugin-version="pluginVersion"
      :plugin-author="pluginAuthor"
      :repo-url="repoUrl"
      :issues-url="issuesUrl"
      :author-url="authorUrl"
      :show-switch="showSwitch"
      @nav-click="onNavClick"
      @mobile-group-click="onMobileGroupClick"
      @switch="emit('switch')"
      @close="emit('close')" />

        <!-- 右侧主界面 -->
        <main class="config-main">
          <!-- 电脑端极简顶栏：展示当前模块名称，右侧放置返回/关闭操作；手机端已置顶整合 -->
          <header class="config-main-header d-none d-md-flex">
            <div class="config-main-title">
              <v-icon
                v-if="currentSection?.icon"
                :icon="currentSection.icon"
                size="20"
                color="primary"
                class="mr-2 flex-shrink-0" />
              <span>{{ currentSectionTitle }}</span>
            </div>
            <div class="config-header-actions">
              <v-btn
                v-if="showSwitch"
                class="config-header-btn"
                variant="text"
                size="small"
                prepend-icon="mdi-arrow-left"
                title="返回详情"
                @click="emit('switch')">
                返回详情
              </v-btn>
              <v-btn
                class="config-header-btn"
                variant="text"
                size="small"
                prepend-icon="mdi-close"
                title="关闭"
                @click="emit('close')">
                关闭
              </v-btn>
            </div>
          </header>

          <!-- v1.29.5：设置搜索（仅搜静态 227 项，按 label / key / hint 匹配；有查询时替代下方分组视图） -->
          <div class="config-search-bar">
            <v-text-field
              v-model="searchQuery"
              density="compact"
              variant="outlined"
              prepend-inner-icon="mdi-magnify"
              placeholder="搜索设置：名称 / 键名 / 说明，如 洗版、cron、strm"
              clearable
              hide-details
              @click:clear="searchQuery = ''" />
          </div>

          <!-- 内容展示容器：带丝滑淡入过渡动画，仅渲染当前激活的 section -->
          <div class="config-main-body">
            <div v-if="isSearching" class="config-search-results">
              <div v-if="searchResultSection" class="config-search-meta text-caption text-medium-emphasis">
                找到 {{ searchResultSection.matchCount }} 项设置 · 分布在 {{ searchResultSection.matchGroupCount }} 个任务组
              </div>
              <div v-if="searchResultSection" class="config-search-body">
                <ConfigSection
                  :section="searchResultSection"
                  :config="config"
                  :options="options"
                  :api="api"
                  :refreshing-accounts="refreshingAccounts"
                  :testing-source="testingSource"
                  :testing-auto-subscribe="testingAutoSubscribe"
                  :testing-proxy="testingProxy"
                  :testing-auto-subscribe-proxy="testingAutoSubscribeProxy"
                  :hdhive-oauth-action="hdhiveOauthAction"
                  @scan="openQrCode"
                  @browse-directory="openDirectoryPicker"
                  @test-source="openSourceTest"
                  @test-auto-subscribe="testAutoSubscribe"
                  @test-proxy="testSearchProxy"
                  @test-auto-subscribe-proxy="testAutoSubscribeProxy"
                  @refresh-account="refreshAccount"
                  @hdhive-oauth-start="startHdhiveOAuth"
                  @hdhive-oauth-exchange="exchangeHdhiveOAuth"
                  @checkin-result="handleCheckinResult"
                  @copy-text="copyText"
                  @load-options="loadDynamicOptions"
                  @refresh-options="refreshDynamicOptions"
                  @notify="notify" />
              </div>
              <div v-else class="config-search-empty">
                <v-icon icon="mdi-magnify" size="28" class="mb-2" />
                <div>没有找到与「{{ (searchQuery || "").trim() }}」相关的设置</div>
                <div class="text-caption text-medium-emphasis mt-1">仅搜索静态设置项；网盘驱动与搜索渠道的后端动态项暂不支持搜索</div>
              </div>
            </div>
            <transition v-else name="fade-slide" mode="out-in">
              <div :key="activeTab" class="config-window-section">
                <ConfigSection
                  v-if="currentSection"
                  :section="currentSection"
                  :config="config"
                  :options="options"
                  :api="api"
                  :refreshing-accounts="refreshingAccounts"
                  :testing-source="testingSource"
                  :testing-auto-subscribe="testingAutoSubscribe"
                  :testing-proxy="testingProxy"
                  :testing-auto-subscribe-proxy="testingAutoSubscribeProxy"
                  :hdhive-oauth-action="hdhiveOauthAction"
                  @scan="openQrCode"
                  @browse-directory="openDirectoryPicker"
                  @test-source="openSourceTest"
                  @test-auto-subscribe="testAutoSubscribe"
                  @test-proxy="testSearchProxy"
                  @test-auto-subscribe-proxy="testAutoSubscribeProxy"
                  @refresh-account="refreshAccount"
                  @hdhive-oauth-start="startHdhiveOAuth"
                  @hdhive-oauth-exchange="exchangeHdhiveOAuth"
                  @checkin-result="handleCheckinResult"
                  @copy-text="copyText"
                  @load-options="loadDynamicOptions"
                  @refresh-options="refreshDynamicOptions"
                  @notify="notify" />
              </div>
            </transition>
          </div>

          <!-- 底部操作栏 -->
          <footer class="config-actions">
            <v-progress-linear v-if="saving" class="save-progress" color="primary" indeterminate />
            <v-slide-y-transition>
              <div v-if="saving" class="save-state" aria-live="polite">
                <v-progress-circular indeterminate size="16" width="2" color="primary" />
                正在保存配置
              </div>
            </v-slide-y-transition>
            <div class="actions-spacer"></div>
            <v-btn
              color="primary"
              class="save-config-button"
              variant="flat"
              elevation="1"
              prepend-icon="mdi-content-save-check-outline"
              :loading="saving"
              @click="save">
              保存配置
            </v-btn>
          </footer>
        </main>
      </div>
    </v-card>
    <QrCodeDialog v-show="qrVisible" v-model="qrVisible" :api="api" :provider="qrProvider" :plugin-id="pluginId || sourcePluginId || 'CloudSubscribeX'" @success="handleQrSuccess" />
    <DirectoryDialog
      v-show="directoryVisible"
      v-model="directoryVisible"
      :api="api"
      :mode="directoryMode"
      :provider="directoryProvider"
      :initial-path="directoryInitialPath"
      @select="selectDirectory" />
    <ConfigSourceTestDialog
      v-model="sourceTestVisible"
      :source-test="sourceTest"
      :source-names="sourceNames"
      :searching-tmdb="searchingTmdb"
      :tmdb-searched="tmdbSearched"
      :tmdb-candidates="tmdbCandidates"
      :selected-tmdb-id="selectedTmdbId"
      :testing-source="testingSource"
      :test-result="testResult"
      :test-submitted="testSubmitted"
      :test-error="testError"
      :test-elapsed="testElapsed"
      :test-resource-types="testResourceTypes"
      :test-fansubs="testFansubs"
      v-model:selected-test-resource-type="selectedTestResourceType"
      v-model:selected-test-fansub="selectedTestFansub"
      :filtered-test-items="filteredTestItems"
      :previewing-url="previewingUrl"
      @search-tmdb="searchTmdbCandidates"
      @test-source="testSource"
      @preview-resource="previewResource"
      @copy-text="copyText"
      @confirm-unlock="confirmUnlock" />
    <ConfigAutoSubscribeTestDialog
      v-model="autoSubscribeTestVisible"
      :provider-name="autoSubscribeProviderNames[autoSubscribeTestProvider] || '榜单'"
      :loading="autoSubscribeTestLoading"
      :error="autoSubscribeTestError"
      :result="autoSubscribeTestResult"
      :message="autoSubscribeTestMessage"
      :items="autoSubscribeTestItems" />
    <ConfigResourcePreviewDialog
      v-model="previewVisible"
      :preview-meta="previewMeta"
      :preview-items="previewItems"
      :preview-breadcrumbs="previewBreadcrumbs"
      :preview-loading="previewLoading"
      :preview-error="previewError"
      @copy-text="copyText"
      @open-folder="openPreviewFolder"
      @open-breadcrumb="openPreviewBreadcrumb" />
    <ConfigUnlockDialog
      v-model="unlockVisible"
      :unlock-item="unlockItem"
      :unlocking="unlocking"
      :unlock-error="unlockError"
      @confirm-unlock="unlockResource" />
    <v-snackbar v-model="messageVisible" :color="messageType" :style="{ color: `rgb(var(--v-theme-on-${messageType || 'success'}))` }" location="top" timeout="3500" variant="elevated">
      {{ message }}
      <template #actions>
        <v-btn icon="mdi-close" size="small" variant="text" @click="messageVisible = false" />
      </template>
    </v-snackbar>
  </div>
</template>

<script setup>
import {computed, defineAsyncComponent, onBeforeUnmount, onMounted, reactive, ref, watch} from "vue";
import {useDisplay} from "vuetify";
import ConfigSection from "./config/ConfigSection.vue";
import ConfigSidebar from "./config/ConfigSidebar.vue";
import ConfigSourceTestDialog from "./config/ConfigSourceTestDialog.vue";
import ConfigAutoSubscribeTestDialog from "./config/ConfigAutoSubscribeTestDialog.vue";
import ConfigResourcePreviewDialog from "./config/ConfigResourcePreviewDialog.vue";
import ConfigUnlockDialog from "./config/ConfigUnlockDialog.vue";
import { canPreviewResource, previewResourceKey } from "../utils/sourceTestHelpers.js";
import {createConfigSections} from "../config/fields.js";
import {buildSearchResultSection} from "../config/search.js";

const QrCodeDialog = defineAsyncComponent(() => import("./dialogs/QrCodeDialog.vue"))
const DirectoryDialog = defineAsyncComponent(() => import("./dialogs/DirectoryDialog.vue"));

const props = defineProps({
  api: { type: [Object, Function], required: true },
  initialConfig: { type: Object, default: () => ({}) },
  pluginId: {type: String, default: ""},
  sourcePluginId: {type: String, default: ""},
  showSwitch: { type: Boolean, default: true },
})
const emit = defineEmits(["save", "close", "switch", "layout"])
const api = props.api

const pluginDetail = ref(null);

/** 将 raw.githubusercontent.com 插件地址转换为可访问的 GitHub 项目主页（对齐 MoviePilot 平台 PluginCard.vue 逻辑） */
function normalizePluginRepoUrl(repoUrl) {
  if (!repoUrl || typeof repoUrl !== "string" || !repoUrl.includes("raw.githubusercontent.com")) return repoUrl || "";
  try {
    const rawUrl = new URL(repoUrl);
    const [user, repo] = rawUrl.pathname.split("/").filter(Boolean);
    if (user && repo) return `https://github.com/${user}/${repo}`;
  } catch (error) {
    console.error(error);
  }
  return repoUrl;
}

function hasRemoteRepoUrl(plugin) {
  return Boolean(plugin?.repo_url && !plugin.repo_url.startsWith("local://"));
}

function resolvePluginPageUrl(plugin) {
  if (!plugin) return "";
  const url = hasRemoteRepoUrl(plugin) ? normalizePluginRepoUrl(plugin.repo_url) : plugin.author_url;
  return url || plugin.author_url || "";
}

async function fetchMarketPlugin(pluginId) {
  if (!pluginId) return null;
  try {
    const marketPlugins = await props.api.get("plugin/", {
      params: {state: "market", force: false},
    });
    const list = Array.isArray(marketPlugins?.data)
      ? marketPlugins.data
      : Array.isArray(marketPlugins)
        ? marketPlugins
        : [];
    return list.find((p) => p.id === pluginId) || null;
  } catch (error) {
    console.error("从插件市场获取插件详情失败:", error);
    return null;
  }
}

async function fetchInstalledPluginDetail() {
  const currentPluginId = props.pluginId || props.sourcePluginId || "CloudSubscribeX";
  let detail = null;
  try {
    const historyRes = await props.api.get(`plugin/history/${currentPluginId}`, {
      params: {force: false},
    });
    const historyPlugin = historyRes?.data || historyRes;
    if (historyPlugin && typeof historyPlugin === "object") {
      detail = historyPlugin;
    }
  } catch (error) {
    // 忽略未安装或获取异常
  }

  if (!hasRemoteRepoUrl(detail)) {
    const marketPlugin = await fetchMarketPlugin(currentPluginId);
    if (marketPlugin) {
      detail = {...(detail || {}), ...marketPlugin};
    }
  }

  if (detail) {
    pluginDetail.value = detail;
  }
}

onMounted(() => {
  fetchInstalledPluginDetail();
});

const pluginVersion = computed(() => {
  return (
    pluginDetail.value?.plugin_version ||
    props.initialConfig?.plugin_version ||
    props.initialConfig?.version ||
    ""
  );
});

const pluginAuthor = computed(() => {
  return pluginDetail.value?.plugin_author || props.initialConfig?.plugin_author || "";
});

const authorUrl = computed(() => {
  return pluginDetail.value?.author_url || props.initialConfig?.author_url || "";
});

const repoUrl = computed(() => {
  if (pluginDetail.value) {
    const pageUrl = resolvePluginPageUrl(pluginDetail.value);
    if (pageUrl) return pageUrl;
  }
  const initial = props.initialConfig || {};
  const rawRepo = initial.repo_url && !initial.repo_url.startsWith("local://") ? initial.repo_url : "";
  if (rawRepo) return normalizePluginRepoUrl(rawRepo);
  return "";
});

const issuesUrl = computed(() => {
  const base = repoUrl.value;
  if (!base) return "";
  try {
    const parsed = new URL(base);
    const segments = parsed.pathname.split("/").filter(Boolean);
    if (segments.length >= 2) {
      return `${base.replace(/\/+$/, "")}/issues`;
    }
  } catch {
    // ignore
  }
  return "";
});
let display = null;
try {
  display = useDisplay();
} catch {
  display = null;
}
const isMobile = computed(() => (display?.smAndDown?.value ?? false) || (typeof window !== "undefined" && window.innerWidth <= 768));
const config = reactive(JSON.parse(JSON.stringify(props.initialConfig || {})))
const activeTab = ref("common")

// v1.29.5：高级折叠改为字段级（本地记忆开关）；只影响分组浏览视图的
// 字段展示，不影响保存、常用设置取字段与搜索。
const SHOW_ADVANCED_KEY = "csx-config-show-advanced";
const showAdvanced = ref(false);
try {
  showAdvanced.value = window.localStorage?.getItem(SHOW_ADVANCED_KEY) === "1";
} catch {}
watch(showAdvanced, (value) => {
  try {
    window.localStorage?.setItem(SHOW_ADVANCED_KEY, value ? "1" : "0");
  } catch {}
  const visible = sections.value || [];
  if (!visible.some((s) => s.value === activeTab.value) && visible[0]) {
    activeTab.value = visible[0].value;
  }
});

function onNavClick(val, event) {
  activeTab.value = val;
  if (event?.currentTarget) {
    event.currentTarget.scrollIntoView({behavior: "smooth", inline: "center", block: "nearest"});
  }
}
// v1.29.5：6 个任务组与 ui_options scope 的映射；一个页签可能要多个
// scope（通知与签到同时要 notify 与 checkin）。
const optionScopeByTab = Object.freeze({
  common: ["base"],
  tracking: ["base"],
  transfer: ["subscriptions"],
  playback: ["base"],
  notify: ["notify", "checkin"],
  channels: ["search"],
  drive: ["drive"],
})
const loadedOptionScopes = new Set()
const optionScopeRequests = new Map()
const qrVisible = ref(false),
  qrProvider = ref("115"),
  directoryVisible = ref(false),
  directoryMode = ref("cloud"),
  directoryField = ref(""),
  directoryInitialPath = ref("/"),
  directoryProvider = ref("115"),
  saving = ref(false),
  refreshingAccounts = ref([]),
  testingSource = ref(""),
  testingAutoSubscribe = ref(""),
  autoSubscribeTestVisible = ref(false),
  autoSubscribeTestProvider = ref(""),
  autoSubscribeTestLoading = ref(false),
  autoSubscribeTestResult = ref(null),
  autoSubscribeTestError = ref(""),
  testingProxy = ref(false),
  testingAutoSubscribeProxy = ref(false),
  searchingTmdb = ref(false),
  tmdbSearched = ref(false),
  tmdbCandidates = ref([]),
  selectedTmdbId = ref(0),
  testResult = ref({}),
  selectedTestResourceType = ref(""),
  selectedTestFansub = ref(""),
  sourceTestVisible = ref(false),
  testSubmitted = ref(false),
  testError = ref(""),
  testElapsed = ref(null),
  previewVisible = ref(false),
  previewingUrl = ref(""),
  previewLoading = ref(false),
  previewError = ref(""),
  previewItems = ref([]),
  previewMeta = ref({}),
  previewBreadcrumbs = ref([]),
  previewResourceType = ref(""),
  previewShareUrl = ref(""),
  previewSource = ref(""),
  previewJuyingResourceId = ref(""),
  previewProviderData = ref({}),
  previewPendingResource = ref({}),
  previewTargetSeason = ref(null),
  previewTargetEpisodes = ref([]),
  unlockVisible = ref(false),
  unlockItem = ref(null),
  unlocking = ref(false),
  unlockError = ref(""),
  message = ref(""),
  messageType = ref("success"),
  messageVisible = ref(false)
let previewRequestId = 0
const options = reactive({
  sections: [],
  defaults: {},
  subscribes: [],
  mediaservers: [],
  mediaLibraryWebhookUrls: {},
  notificationTypes: [],
  cloudDrives: [],
  resourceTypes: [],
  sources: [],
  account: {},
  accounts: {},
  searchAccounts: {},
  pansou: {},
  dynamicOptionLoading: {},
  dynamicOptionErrors: {},
  driverSchemas: [],
  searchSchemas: [],
  checkinSchemas: {providers: [], groups: []},
  rsshubInstances: [],
  rsshubLoading: false,
})
const baseSections = computed(() => createConfigSections(options, config))
const commonSection = computed(() => buildCommonSection(baseSections.value))
const allSections = computed(() => [commonSection.value, ...(baseSections.value || [])])
// v1.29.5：高级折叠改为字段级——origin 属于旧高级分组的字段（field.advanced）
// 默认收起，仅当“显示高级设置”打开才显示；只作用于分组浏览视图，
// 常用设置（common）与搜索结果不受影响。
function filterAdvancedFields(section) {
  if (showAdvanced.value) return section;
  const groups = (section.groups || [])
    .map((group) => ({
      ...group,
      fields: (group.fields || []).filter((field) => !field?.advanced),
    }))
    .filter((group) => (group.fields || []).length > 0);
  return {...section, groups};
}
const sections = computed(() =>
  (allSections.value || []).map((section) =>
    section.value === "common" ? section : filterAdvancedFields(section),
  ),
)

// v1.29.5：设置搜索（Config 页顶部输入框；仅搜静态 227 项，按 label /
// key / hint 匹配，命中字段按所属任务组展示；搜索时替代当前分组视图）。
const searchQuery = ref("")
const isSearching = computed(() => Boolean(String(searchQuery.value || "").trim()))
const searchResultSection = computed(() =>
  isSearching.value ? buildSearchResultSection(baseSections.value, searchQuery.value) : null,
)

// ---------------------------------------------------------------- 常用设置
// v1.29.3：向导已删，取而代之的是常驻的“常用设置”section：
// 字段按 key 从既有 sections 里挑，不复制定义，避免两处漂移。
// v1.29.5：常用设置从 8 项扩到 20 项（分组表 GROUPING.md 开头所列）。
// 取字段时读 baseSections（全部 6 个任务 section，未按高级折叠过滤），
// 否则取不到 enable_cloud_upgrade（洗版设置，高级）。
// v1.30.0 阶段 4：organize_after_transfer 与 organize_source_path
// 随漏斗接管退役，由 funnel_enabled 与 funnel_scan_dry_run 顶替。
const COMMON_FIELD_KEYS = Object.freeze([
  "enabled",
  "cloud_drive",
  "cron",
  "platform_download_policy",
  "cross_transfer_enabled",
  "funnel_enabled",
  "funnel_scan_dry_run",
  "enable_cloud_upgrade",
  "tg_monitor_enabled",
  "strm_generate_enabled",
  "notify",
  "auto_subscribe_enabled",
  "media_server_refresh_enabled",
  "emby_proxy_enabled",
  "search_concurrency",
  "cross_transfer_daily_upload_limit_gb",
  "transfer_risk_cooldown",
  "upgrade_episode_limit",
  "checkin_auto_retry",
  "batch_size",
])

/** 从全部 section 里按 key 取字段定义（取不到就跳过，不报错） */
function pickFields(keys) {
  const wanted = new Set(keys)
  const picked = []
  for (const section of baseSections.value || []) {
    for (const group of section.groups || []) {
      for (const field of group.fields || []) {
        if (field?.key && wanted.has(field.key)) picked.push(field)
      }
    }
  }
  return picked
}

/** 按 key 分组拼出“常用设置”section：字段对象直接复用原定义 */
function buildCommonSection(list) {
  const byKey = new Map()
  for (const section of list || []) {
    for (const group of section.groups || []) {
      for (const field of group.fields || []) {
        if (field?.key) byKey.set(field.key, field)
      }
    }
  }
  const pick = (keys) => keys.map((key) => byKey.get(key)).filter(Boolean)
  const groups = []
  const baseFields = pick(["enabled", "cloud_drive", "cron", "platform_download_policy"])
  if (baseFields.length) {
    groups.push({title: "基础", icon: "mdi-cog-outline", fields: baseFields})
  }
  const transferFields = pick([
    "funnel_enabled",
    "funnel_scan_dry_run",
    "cross_transfer_enabled",
    "enable_cloud_upgrade",
    "upgrade_episode_limit",
    "cross_transfer_daily_upload_limit_gb",
    "transfer_risk_cooldown",
    "batch_size",
  ])
  if (transferFields.length) {
    groups.push({title: "转存与洗版", icon: "mdi-cloud-download-outline", fields: transferFields})
  }
  const playbackFields = pick([
    "tg_monitor_enabled",
    "strm_generate_enabled",
    "media_server_refresh_enabled",
    "emby_proxy_enabled",
  ])
  if (playbackFields.length) {
    groups.push({title: "监控与播放", icon: "mdi-file-video-outline", fields: playbackFields})
  }
  const subscribeFields = pick([
    "auto_subscribe_enabled",
    "notify",
    "search_concurrency",
    "checkin_auto_retry",
  ])
  if (subscribeFields.length) {
    groups.push({title: "订阅与通知", icon: "mdi-bell-outline", fields: subscribeFields})
  }
  return {
    value: "common",
    title: "常用设置",
    icon: "mdi-star-outline",
    groups,
  }
}

const navGroups = computed(() => {
  const all = sections.value || [];
  const map = new Map(all.map((s) => [s.value, s]));
  // v1.29.5：导航不再用 4 组白名单，6 个任务组直出；常用设置是默认页签，
  // 单独成组排第一（v1.29.3 起默认打开）。
  const groups = [
    {
      name: "常用设置",
      items: ["common"].map((v) => map.get(v)).filter(Boolean),
    },
    {
      name: "追更",
      items: ["tracking"].map((v) => map.get(v)).filter(Boolean),
    },
    {
      name: "转存",
      items: ["transfer"].map((v) => map.get(v)).filter(Boolean),
    },
    {
      name: "播放",
      items: ["playback"].map((v) => map.get(v)).filter(Boolean),
    },
    {
      name: "通知与签到",
      items: ["notify"].map((v) => map.get(v)).filter(Boolean),
    },
    {
      name: "渠道",
      items: ["channels"].map((v) => map.get(v)).filter(Boolean),
    },
    {
      name: "网盘与基础",
      items: ["drive"].map((v) => map.get(v)).filter(Boolean),
    },
  ];
  return groups.filter((g) => g.items.length > 0);
});
// 移动端与 PC 端共用同一份顺序：先按分组次序展开，再平铺渲染，
// 避免两端出现「选项不同、顺序不同」的问题。
// 手机端导航（1.7.108）：第一行分组、第二行只显示当前分组的子项。
const mobileGroupName = computed(() => {
  const group = (navGroups.value || []).find((row) =>
    row.items.some((section) => section.value === activeTab.value)
  );
  return group?.name || navGroups.value?.[0]?.name || "";
});
const mobileGroupItems = computed(() => {
  const group = (navGroups.value || []).find((row) => row.name === mobileGroupName.value);
  return group?.items || [];
});
function onMobileGroupClick(group) {
  // 当前页已经属于该分组时只切换高亮，不跳转；否则进入该分组第一项，避免点空。
  if (group.items.some((section) => section.value === activeTab.value)) return;
  const first = group.items[0];
  if (first) onNavClick(first.value);
}
const currentSection = computed(() => {
  return sections.value.find((s) => s.value === activeTab.value) || sections.value[0];
});
const currentSectionTitle = computed(() => {
  const current = sections.value.find((s) => s.value === activeTab.value);
  return current?.title || "配置详情";
});
const testResourceTypes = computed(() => {
  const declared = Array.isArray(testResult.value?.resource_types)
    ? testResult.value.resource_types
    : [];
  if (declared.length) return declared;
  const items = Array.isArray(testResult.value?.items) ? testResult.value.items : [];
  const counts = new Map();
  items.forEach((item) => {
    const value = String(item?.resource_type || "unknown").toLowerCase();
    const title = item?.resource_type_name || value;
    const current = counts.get(value) || {value, title, count: 0};
    current.count += 1;
    counts.set(value, current);
  });
  return [...counts.values()];
});
watch(testResourceTypes, (types) => {
  if (!types.some((item) => item.value === selectedTestResourceType.value)) {
    selectedTestResourceType.value = types[0]?.value || "";
  }
}, {immediate: true});
const testFansubs = computed(() => {
  const items = Array.isArray(testResult.value?.items) ? testResult.value.items : [];
  const counts = new Map();
  items.forEach((item) => {
    const fansub = String(item?.fansub || "").trim();
    if (fansub) {
      const current = counts.get(fansub) || {value: fansub, title: fansub, count: 0};
      current.count += 1;
      counts.set(fansub, current);
    }
  });
  return [...counts.values()].sort((a, b) => b.count - a.count);
});
watch(testFansubs, (fansubs) => {
  if (selectedTestFansub.value && !fansubs.some((item) => item.value === selectedTestFansub.value)) {
    selectedTestFansub.value = "";
  }
});
const filteredTestItems = computed(() => {
  const items = Array.isArray(testResult.value?.items) ? testResult.value.items : []
  let allItems = items;
  const merged = testResult.value?.merged_by_type;
  if (!allItems.length && merged && typeof merged === "object") {
    allItems = Object.entries(merged).flatMap(([resourceType, rows]) => {
      if (!Array.isArray(rows)) return [];
      return rows.map((row) => {
        const value = row && typeof row === "object" ? row : {};
        const title = String(value.title || value.note || "未命名资源");
        return {
          ...value,
          title,
          url: value.url || value.share_url || "",
          resource_type: value.resource_type || resourceType,
          resource_type_name: value.resource_type_name || value.resource_type || resourceType,
          size: value.size || value.size_human || 0,
          tags: Array.isArray(value.tags) ? value.tags : [],
        }
      })
    })
  }
  let result = allItems;
  if (selectedTestResourceType.value) {
    result = result.filter(
      (item) => String(item?.resource_type || "unknown").toLowerCase() === selectedTestResourceType.value,
    );
  }
  if (selectedTestFansub.value) {
    result = result.filter((item) => String(item?.fansub || "").trim() === selectedTestFansub.value);
  }
  return result;
})

const sourceNames = computed(() => {
  // v1.29.5：搜索渠道 subtabs 随 search 并入任务组“渠道”。
  const searchSec = allSections.value.find((s) => s.value === "channels");
  const result = {}
  ;(searchSec?.subtabs || []).forEach((t) => {
    if (t.value && t.value !== "common") result[t.value] = t.title;
  });
  return result;
});

const autoSubscribeProviderNames = computed(() => {
  // v1.29.5：榜单源 subtabs 随“订阅源”并入任务组“追更”。
  const subSec = allSections.value.find((s) => s.value === "tracking");
  const result = {}
  ;(subSec?.subtabs || []).forEach((t) => {
    if (t.value && t.value !== "global") result[t.value] = `${t.title}榜单`;
  });
  return result;
});

const autoSubscribeTestItems = computed(() => {
  const items = autoSubscribeTestResult.value?.data?.items || autoSubscribeTestResult.value?.items || [];
  return Array.isArray(items) ? items : [];
})
const autoSubscribeTestMessage = computed(() => {
  return autoSubscribeTestResult.value?.message || "测试完成";
})
const sourceTest = reactive({
  source: "",
  title: "",
  season: 1,
})

function unwrapResponse(raw) {
  if (raw?.data && typeof raw.data === "object" && "success" in raw.data) {
    return raw.data
  }
  return raw || {}
}

function applyOptions(data) {
  Object.entries(data.defaults || {}).forEach(([key, value]) => {
    if (!(key in config)) config[key] = value
  })
  // 1.10.5：把后端默认值表留下来，供 ConfigSection 的“恢复默认值”
  // 与分组“有修改”圆点用（以前 options.defaults 从没赋值过，恢复时
  // 只能按类型猜默认值，文本框会被错恢复成空串）。
  if (data.defaults && typeof data.defaults === "object") {
    options.defaults = data.defaults
  }
  if ("sections" in data && Array.isArray(data.sections)) {
    options.sections = data.sections;
  }
  if ("subscribes" in data) {
    options.subscribes = Array.isArray(data.subscribes) ? data.subscribes : []
  }
  if ("mediaservers" in data) {
    options.mediaservers = Array.isArray(data.mediaservers) ? data.mediaservers : []
  }
  if ("media_library_webhook_urls" in data) {
    options.mediaLibraryWebhookUrls =
      data.media_library_webhook_urls && typeof data.media_library_webhook_urls === "object"
        ? data.media_library_webhook_urls
        : {}
  }
  if ("notification_types" in data) {
    options.notificationTypes = Array.isArray(data.notification_types) ? data.notification_types : []
  }
  if ("cloud_drives" in data) {
    options.cloudDrives = Array.isArray(data.cloud_drives) ? data.cloud_drives : []
  }
  if ("resource_types" in data) {
    options.resourceTypes = Array.isArray(data.resource_types) ? data.resource_types : [];
  }
  if ("sources" in data) {
    options.sources = Array.isArray(data.sources) ? data.sources : [];
  }
  if ("account" in data) {
    options.account = data.account && typeof data.account === "object" ? data.account : {}
  }
  if ("accounts" in data) {
    options.accounts = data.accounts && typeof data.accounts === "object" ? data.accounts : {}
  }
  if ("search_accounts" in data) {
    options.searchAccounts =
      data.search_accounts && typeof data.search_accounts === "object" ? data.search_accounts : {}
  }
  if ("pansou" in data) {
    options.pansou = data.pansou && typeof data.pansou === "object" ? data.pansou : {}
  }
  if ("driver_schemas" in data) {
    options.driverSchemas = Array.isArray(data.driver_schemas) ? data.driver_schemas : [];
    options.driverSchemas.forEach((schema) => {
      (schema.groups || []).forEach((group) => {
        (group.fields || []).forEach((field) => {
          if (field.key && field.default !== undefined && field.default !== null) {
            if (!(field.key in config) || config[field.key] === "" || config[field.key] === null || config[field.key] === undefined) {
              config[field.key] = field.default;
            }
          }
        });
      });
    });
  }
  if ("search_schemas" in data) {
    options.searchSchemas = Array.isArray(data.search_schemas) ? data.search_schemas : [];
    options.searchSchemas.forEach((schema) => {
      (schema.groups || []).forEach((group) => {
        (group.fields || []).forEach((field) => {
          if (field.key && field.default !== undefined && field.default !== null) {
            if (!(field.key in config) || config[field.key] === "" || config[field.key] === null || config[field.key] === undefined) {
              config[field.key] = field.default;
            }
          }
        });
      });
    });
  }
  if ("checkin_schemas" in data) {
    options.checkinSchemas =
      data.checkin_schemas && typeof data.checkin_schemas === "object"
        ? data.checkin_schemas
        : {providers: [], groups: []};
    (options.checkinSchemas.groups || []).forEach((group) => {
      (group.fields || []).forEach((field) => {
        if (field.key && field.default !== undefined && field.default !== null) {
          if (!(field.key in config) || config[field.key] === "" || config[field.key] === null || config[field.key] === undefined) {
            config[field.key] = field.default;
          }
        }
      });
    });
  }
}

function notify(text, type = "success") {
  message.value = text
  messageType.value = type
  messageVisible.value = true
}

async function testSearchProxy() {
  if (testingProxy.value) return
  const proxy = String(config.search_proxy || "").trim()
  if (!proxy) {
    notify("请先填写搜索渠道代理地址", "warning")
    return
  }
  testingProxy.value = true
  try {
    const response = unwrapResponse(
      await api.post("plugin/CloudSubscribeX/search/proxy/test", {
        proxy,
        username: String(config.search_proxy_username || "").trim(),
        password: String(config.search_proxy_password || ""),
      }),
    )
    if (response.success === false) {
      throw new Error(response.message || "代理测试失败")
    }
    const data = response.data?.data || response.data || {}
    const details = [
      Number.isFinite(Number(data.latency_ms)) ? `${Number(data.latency_ms)} ms` : "",
      data.ip ? `IP ${data.ip}` : "",
      data.loc ? `地区 ${data.loc}` : "",
      data.colo ? `节点 ${data.colo}` : "",
    ].filter(Boolean)
    notify(`代理连接成功${details.length ? `：${details.join(" · ")}` : ""}`)
  } catch (error) {
    notify(error?.response?.data?.message || error.message || String(error), "error")
  } finally {
    testingProxy.value = false
  }
}

async function testAutoSubscribeProxy() {
  if (testingAutoSubscribeProxy.value) return;
  const proxy = String(config.auto_subscribe_proxy || "").trim();
  if (!proxy) {
    notify("请先填写榜单代理地址", "warning");
    return;
  }
  testingAutoSubscribeProxy.value = true;
  try {
    const response = unwrapResponse(
      await api.post("plugin/CloudSubscribeX/auto_subscribe/proxy/test", {
        proxy,
        username: String(config.auto_subscribe_proxy_username || "").trim(),
        password: String(config.auto_subscribe_proxy_password || ""),
      }),
    )
    if (response.success === false) throw new Error(response.message || "代理测试失败");
    notify(response.message || "榜单代理连接成功");
  } catch (error) {
    notify(error?.response?.data?.message || error.message || String(error), "error");
  } finally {
    testingAutoSubscribeProxy.value = false;
  }
}

async function copyText(value, label = "Webhook URL") {
  const text = String(value || "")
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
      const copied = document.execCommand("copy")
      document.body.removeChild(input)
      if (!copied) throw new Error("浏览器拒绝访问剪贴板")
    }
    notify(`${label}已复制`)
  } catch (error) {
    notify(`复制失败：${error.message || error}`, "error")
  }
}

async function requestResourceUrl(item) {
  const response = unwrapResponse(
    await api.post("plugin/CloudSubscribeX/search/unlock", {
      source: item.source || sourceTest.source,
      item,
      config: sourceTestConfig(item.source || sourceTest.source),
    }),
  )
  if (response.success === false) throw new Error(response.message || "资源链接获取失败")
  const data = response.data?.data || response.data || {}
  if (!data.url) throw new Error(response.message || "资源链接获取失败")
  applySearchAccountPoints(item.source, data.deducted_points);
  item.url = data.url
  item.need_access = false
  item.need_unlock = false
  item.is_unlocked = true
  return response.message || "资源链接已获取"
}

function applySearchAccountPoints(source, deductedPoints) {
  const normalizedSource = String(source || "")
    .trim()
    .toLowerCase();
  const points = Number(deductedPoints);
  if (!normalizedSource || !Number.isFinite(points) || points <= 0) return;
  const account = options.searchAccounts?.[normalizedSource];
  const available = Number(account?.points?.available);
  if (!account || !Number.isFinite(available)) return;
  options.searchAccounts = {
    ...options.searchAccounts,
    [normalizedSource]: {
      ...account,
      points: {
        ...(account.points || {}),
        available: Math.max(0, available - points),
      },
    },
  }
}

const previewHdhiveResourceRef = ref("");
const previewHdhiveUnlocked = ref(false);

async function previewResource(item) {
  if (!canPreviewResource(item)) return
  if (previewingUrl.value) return;
  const requestId = ++previewRequestId
  previewingUrl.value = previewResourceKey(item)
  const shareUrl = String(item.url || "")
  previewVisible.value = true
  previewLoading.value = false
  previewError.value = ""
  previewItems.value = []
  previewResourceType.value = String(item.resource_type || "").toLowerCase()
  previewShareUrl.value = shareUrl
  previewSource.value = String(item.source || "").toLowerCase()
  previewJuyingResourceId.value = String(item.provider_data?.resource_id || "");
  previewHdhiveResourceRef.value = String(item.resource_ref || "");
  previewProviderData.value = {...(item.provider_data || {})};
  previewPendingResource.value = {
    pending_resolution: Boolean(item.pending_resolution),
    provider_data: {...(item.provider_data || {})},
    // HDHaven 降级展示所需的元数据
    episode_range: String(item.episode_range || item.remark || ""),
    unlock_points: Number(item.unlock_points || 0),
  }
  previewHdhiveUnlocked.value = Boolean(item.is_unlocked)
  previewTargetSeason.value = item.target_season ?? null
  previewTargetEpisodes.value = Array.isArray(item.target_episodes) ? [...item.target_episodes] : []
  previewMeta.value = {
    provider_name: "",
    resource_type_name: String(item.resource_type_name || item.resource_type || ""),
    share_url: previewShareUrl.value,
  }
  const breadcrumbs = [{ id: "", name: "根目录" }]
  previewBreadcrumbs.value = breadcrumbs
  await loadPreviewDirectory("", breadcrumbs, requestId)
  if (requestId === previewRequestId && previewShareUrl.value) {
    item.url = previewShareUrl.value
    item.need_access = false
    item.need_unlock = false
    item.is_unlocked = true
    item.pending_resolution = false
  }
  if (requestId === previewRequestId) previewingUrl.value = ""
}

async function loadPreviewDirectory(parentId, breadcrumbs, requestId = ++previewRequestId) {
  const isPending =
    !previewShareUrl.value &&
    (Boolean(previewJuyingResourceId.value) ||
      Boolean(previewHdhiveResourceRef.value) ||
      Boolean(previewPendingResource.value.pending_resolution));
  if (!previewShareUrl.value && !isPending) return;
  const resourceType = previewResourceType.value
  const shareUrl = previewShareUrl.value
  previewLoading.value = true
  previewError.value = ""
  try {
    const response = unwrapResponse(
      await api.post("plugin/CloudSubscribeX/search/preview", {
        resource_type: resourceType,
        url: shareUrl,
        parent_id: parentId || "",
        source: previewSource.value,
        resource_ref: previewHdhiveResourceRef.value,
        provider_data: previewProviderData.value,
        is_unlocked: previewHdhiveUnlocked.value,
        target_season: previewTargetSeason.value,
        target_episodes: previewTargetEpisodes.value,
        ...previewPendingResource.value,
        episode_range: previewPendingResource.value.episode_range || "",
        unlock_points: previewPendingResource.value.unlock_points || 0,
        config: isPending ? sourceTestConfig(previewSource.value) : undefined,
      }),
    )
    if (requestId !== previewRequestId || !previewVisible.value) return
    if (response.success === false) throw new Error(response.message || "资源预览失败")
    const data = response.data?.data || response.data || {}
    if (data.resource_type) {
      previewResourceType.value = String(data.resource_type).toLowerCase()
    }
    if (data.share_url) {
      previewShareUrl.value = String(data.share_url)
      previewPendingResource.value.pending_resolution = false
    }
    previewItems.value = Array.isArray(data.items) ? data.items : []
    previewMeta.value = {
      provider_name: String(data.provider_name || ""),
      resource_type_name: String(data.resource_type_name || ""),
      display_name: String(data.display_name || ""),
      info_hash: String(data.info_hash || ""),
      size: Number(data.size || 0),
      share_url: String(data.share_url || shareUrl),
    }
    previewBreadcrumbs.value = breadcrumbs
  } catch (error) {
    if (requestId !== previewRequestId || !previewVisible.value) return
    previewItems.value = []
    previewError.value = error?.response?.data?.message || error.message || String(error)
  } finally {
    if (requestId === previewRequestId) previewLoading.value = false
  }
}

function openPreviewFolder(file) {
  if (!file?.can_enter || previewLoading.value) return
  loadPreviewDirectory(String(file.id || ""), [
    ...previewBreadcrumbs.value,
    { id: String(file.id || ""), name: String(file.name || "未命名目录") },
  ])
}

function openPreviewBreadcrumb(index) {
  const breadcrumb = previewBreadcrumbs.value[index]
  if (!breadcrumb || previewLoading.value) return
  loadPreviewDirectory(String(breadcrumb.id || ""), previewBreadcrumbs.value.slice(0, index + 1))
}

function confirmUnlock(item) {
  unlockItem.value = item || null
  unlockError.value = ""
  unlockVisible.value = Boolean(item)
}

async function unlockResource() {
  const item = unlockItem.value
  if (!item || unlocking.value) return
  unlocking.value = true
  unlockError.value = ""
  let shouldPreview = false
  try {
    const message = await requestResourceUrl(item)
    unlockVisible.value = false
    shouldPreview = canPreviewResource(item)
    notify(shouldPreview ? `${message}，正在打开预览` : `${message}，现在可以复制`)
  } catch (error) {
    unlockError.value = error?.response?.data?.message || error.message || String(error)
  } finally {
    unlocking.value = false
  }
  if (shouldPreview) await previewResource(item)
}

function sourceTestConfig(source) {
  return JSON.parse(JSON.stringify(config));
}

function openQrCode(provider) {
  qrProvider.value = String(provider || "115")
  qrVisible.value = true
}

async function handleQrSuccess(payload) {
  const provider = String(payload?.provider || qrProvider.value || "")
    .trim()
    .toLowerCase()
  const credentials = payload?.credentials
  if (credentials && typeof credentials === "object") {
    Object.assign(config, credentials)
  }
  try {
    if (provider) {
      await refreshAccount(`drive:${provider}`, { silent: true })
    }
  } catch (error) {
    notify(`账号信息刷新失败：${error.message || error}`, "warning")
  }
}

function openDirectoryPicker(fieldKey, provider, isLocal = false) {
  directoryField.value = fieldKey
  directoryMode.value = isLocal ? "local" : "cloud";
  directoryProvider.value = String(provider || config.cloud_drive || "115")
  directoryInitialPath.value = String(config[fieldKey] || "/").trim() || "/"
  directoryVisible.value = true
}

function selectDirectory(path) {
  if (directoryField.value) config[directoryField.value] = path || "/"
  directoryVisible.value = false
}

function optionScopesForTab(tab = activeTab.value) {
  return optionScopeByTab[String(tab || "tracking")] || ["base"]
}

async function loadOptions(scope = "base", {force = false, refresh = false} = {}) {
  const normalizedScope = String(scope || "base")
    .trim()
    .toLowerCase()
  if (!force && loadedOptionScopes.has(normalizedScope)) return
  if (optionScopeRequests.has(normalizedScope)) {
    return optionScopeRequests.get(normalizedScope)
  }
  const request = (async () => {
    if (normalizedScope === "base") options.rsshubLoading = true;
    const query = new URLSearchParams({ scope: normalizedScope })
    if (refresh) query.set("refresh", "true");
    const response = unwrapResponse(await api.get(`plugin/CloudSubscribeX/ui_options?${query}`))
    if (response.success === false) {
      throw new Error(response.message || "加载配置选项失败")
    }
    applyOptions(response.data?.data || response.data || response)
    loadedOptionScopes.add(normalizedScope)
    if (normalizedScope === "base") options.rsshubLoading = false;
  })()
  optionScopeRequests.set(normalizedScope, request)
  try {
    return await request
  } finally {
    if (normalizedScope === "base") options.rsshubLoading = false;
    optionScopeRequests.delete(normalizedScope)
  }
}

async function loadDynamicOptions(descriptor, {refresh = false} = {}) {
  const scope = String(descriptor?.scope || "").trim().toLowerCase();
  if (!scope) return;
  options.dynamicOptionLoading[scope] = true;
  options.dynamicOptionErrors[scope] = "";
  try {
    await loadOptions(scope, {force: refresh, refresh});
  } catch (error) {
    const message = error?.response?.data?.message || error.message || String(error);
    options.dynamicOptionErrors[scope] = `加载失败：${message}`;
  } finally {
    options.dynamicOptionLoading[scope] = false;
  }
}

function refreshDynamicOptions(descriptor) {
  return loadDynamicOptions(descriptor, {refresh: true});
}

async function reloadVisibleOptionScopes() {
  loadedOptionScopes.clear()
  const scopes = [...new Set(["base", ...optionScopesForTab()])];
  await Promise.all(scopes.map((scope) => loadOptions(scope, { force: true })))
}

async function refreshAccount(accountKey, { silent = false } = {}) {
  const normalizedKey = String(accountKey || "")
    .trim()
    .toLowerCase()
  if (!normalizedKey || refreshingAccounts.value.includes(normalizedKey)) return
  refreshingAccounts.value = [...refreshingAccounts.value, normalizedKey]
  try {
    const response = unwrapResponse(
      await api.post("plugin/CloudSubscribeX/account/refresh", {
        key: normalizedKey,
      }),
    )
    if (response.success === false) {
      throw new Error(response.message || "账户信息刷新失败")
    }
    const data = response.data?.data || response.data || {}
    const [category, source] = String(data.key || normalizedKey).split(":", 2)
    const account = data.account && typeof data.account === "object" ? data.account : {}
    if (category === "drive") {
      options.accounts = { ...options.accounts, [source]: account }
      if (String(config.cloud_drive || "") === source) {
        options.account = account
      }
    } else if (category === "search") {
      options.searchAccounts = { ...options.searchAccounts, [source]: account }
    }
    if (!silent) {
      notify(
        response.message || (data.limited ? "刷新过于频繁，已显示最近一次账户信息" : "账户信息已刷新"),
        data.limited ? "warning" : "success",
      )
    }
  } catch (error) {
    if (!silent) {
      notify(`账户信息刷新失败：${error.message || error}`, "error")
    }
  } finally {
    refreshingAccounts.value = refreshingAccounts.value.filter((key) => key !== normalizedKey)
  }
}

async function save() {
  if (saving.value) return false
  saving.value = true
  messageVisible.value = false
  try {
    const payload = JSON.parse(JSON.stringify(config))
    delete payload.hdhive_oauth_callback
    const response = unwrapResponse(await api.post("plugin/CloudSubscribeX/config/save", payload))
    if (response.success === false) {
      throw new Error(response.message || "保存配置失败")
    }
    const savedConfig = response.data?.data || response.data
    if (savedConfig && typeof savedConfig === "object") {
      Object.assign(config, JSON.parse(JSON.stringify(savedConfig)))
    }
    await reloadVisibleOptionScopes()
    notify(response.message || "配置已保存")
    return true
  } catch (e) {
    notify(`保存配置失败：${e.message || e}`, "error")
    return false
  } finally {
    saving.value = false
  }
}

async function handleCheckinResult(result) {
  const providerName = result?.providerName || "签到服务"
  if (result?.success && result?.providerKey) {
    const key = result.providerKey;
    if (options.searchAccounts?.[key] && result.points_after !== undefined) {
      const current = options.searchAccounts[key];
      current.points = {
        ...(current.points || {}),
        available: result.points_after,
      };
    }
  }
  notify(
    result?.message || (result?.success ? `${providerName} 签到完成` : `${providerName} 签到失败`),
    result?.success ? "success" : "error",
  )
}

let hdhiveOauthWindow = null;
const hdhiveOauthAction = ref("");

function hdhiveOAuthPayload() {
  return {
    client_id: String(config.hdhive_client_id || "").trim(),
    app_secret: String(config.hdhive_api_key || "").trim(),
    redirect_uri: String(config.hdhive_redirect_uri || "").trim(),
    response_mode: String(config.hdhive_response_mode || "redirect").trim(),
    scope: "query unlock write",
  }
}

async function startHdhiveOAuth() {
  if (hdhiveOauthAction.value) return
  const oauthPopup = window.open("about:blank", "hdhive-openapi-oauth", "width=560,height=760,noopener=no")
  hdhiveOauthAction.value = "start"
  try {
    const response = unwrapResponse(await api.post("plugin/CloudSubscribeX/hdhive/oauth/start", hdhiveOAuthPayload()))
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
      await api.post("plugin/CloudSubscribeX/hdhive/oauth/exchange", {
        ...hdhiveOAuthPayload(),
        callback: String(config.hdhive_oauth_callback || "").trim(),
        code: String(callbackData.code || "").trim(),
        state: String(callbackData.state || "").trim(),
      }),
    )
    if (response.success === false) {
      throw new Error(response.message || "HDHive 用户授权失败")
    }
    const data = response.data?.data || response.data || {}
    config.hdhive_access_token = data.access_token || ""
    config.hdhive_refresh_token = data.refresh_token || ""
    config.hdhive_token_expires_at = Number(data.token_expires_at || 0)
    config.hdhive_auth_code = ""
    config.hdhive_oauth_callback = ""
    if (hdhiveOauthWindow && !hdhiveOauthWindow.closed) hdhiveOauthWindow.close()
    hdhiveOauthWindow = null
    notify(data.warning || "HDHive OpenAPI 授权成功，请保存配置使 Token 生效", data.warning ? "warning" : "success")
  } catch (error) {
    notify(`完成 HDHive 授权失败：${error.message || error}`, "error")
  } finally {
    hdhiveOauthAction.value = ""
  }
}

function handleHdhiveOAuthMessage(event) {
  if (event.origin !== "https://re0.me") return;
  const payload = event.data
  if (
    !payload ||
    payload.source !== "hdhive-openapi" ||
    payload.type !== "authorization_response" ||
    String(payload.client_id || "") !== String(config.hdhive_client_id || "")
  )
    return
  exchangeHdhiveOAuth(payload)
}

function openSourceTest(source) {
  if (testingSource.value || !sourceNames.value[source]) return;
  sourceTest.source = source
  tmdbCandidates.value = []
  tmdbSearched.value = false
  selectedTmdbId.value = 0
  testResult.value = {}
  selectedTestResourceType.value = "";
  selectedTestFansub.value = "";
  testSubmitted.value = false
  testError.value = ""
  testElapsed.value = null
  sourceTestVisible.value = true
}

async function searchTmdbCandidates() {
  if (searchingTmdb.value || testingSource.value) return
  const title = String(sourceTest.title || "").trim()
  if (!title) {
    testError.value = "请输入媒体名称"
    return
  }
  searchingTmdb.value = true
  tmdbSearched.value = false
  tmdbCandidates.value = []
  selectedTmdbId.value = 0
  testResult.value = {}
  selectedTestResourceType.value = "";
  selectedTestFansub.value = "";
  testSubmitted.value = false
  testError.value = ""
  try {
    const response = unwrapResponse(await api.post("plugin/CloudSubscribeX/search/tmdb", { title }))
    if (response.success === false) {
      throw new Error(response.message || "TMDB 查询失败")
    }
    const data = response.data?.data || response.data || {}
    tmdbCandidates.value = Array.isArray(data.items) ? data.items : []
    tmdbSearched.value = true
  } catch (e) {
    testError.value = e?.response?.data?.message || e.message || String(e)
  } finally {
    searchingTmdb.value = false
  }
}

async function testSource(candidate) {
  if (testingSource.value || !candidate?.tmdb_id) return
  testingSource.value = sourceTest.source
  selectedTmdbId.value = Number(candidate.tmdb_id || 0)
  testError.value = ""
  testElapsed.value = null
  testSubmitted.value = false
  messageVisible.value = false
  try {
    const response = unwrapResponse(
      await api.post("plugin/CloudSubscribeX/search/test", {
        source: sourceTest.source,
        title: candidate.title,
        original_title: candidate.original_title || "",
        year: candidate.year || null,
        tmdb_id: candidate.tmdb_id,
        imdb_id: candidate.imdb_id || null,
        tvdb_id: candidate.tvdb_id || null,
        douban_id: candidate.douban_id || null,
        bangumi_id: candidate.bangumi_id || null,
        anilist_id: candidate.anilist_id || null,
        media_type: candidate.media_type,
        season: candidate.media_type === "tv" ? sourceTest.season : null,
        config: sourceTestConfig(sourceTest.source),
      }),
    )
    if (response.success === false) {
      testElapsed.value = response.data?.elapsed_seconds ?? null
      throw new Error(response.message || "搜索渠道测试失败")
    }
    testResult.value = response.data?.data || response.data || {}
    testElapsed.value = testResult.value.elapsed_seconds ?? null
    testSubmitted.value = true
    notify(response.message || "搜索渠道测试完成")
  } catch (e) {
    const status = Number(e?.response?.status || 0)
    const errorData = e?.response?.data?.data || e?.response?.data || {}
    testElapsed.value = errorData.elapsed_seconds ?? testElapsed.value
    testError.value =
      status === 502
        ? `${sourceNames.value[sourceTest.source] || "搜索渠道"} 测试请求被网关中断（HTTP 502），请检查渠道服务状态及反向代理超时`
        : e?.response?.data?.message || e.message || String(e)
  } finally {
    testingSource.value = ""
    selectedTmdbId.value = 0
  }
}

async function testAutoSubscribe(provider) {
  if (testingAutoSubscribe.value) return;
  testingAutoSubscribe.value = provider;
  autoSubscribeTestProvider.value = provider;
  autoSubscribeTestVisible.value = true;
  autoSubscribeTestLoading.value = true;
  autoSubscribeTestResult.value = null;
  autoSubscribeTestError.value = "";
  try {
    const response = unwrapResponse(
      await api.post("plugin/CloudSubscribeX/auto_subscribe/test", {
        provider_id: provider,
        config: JSON.parse(JSON.stringify(config)),
      }),
    )
    autoSubscribeTestResult.value = response;
    if (response.success === false) autoSubscribeTestError.value = response.message || "榜单测试失败";
  } catch (error) {
    autoSubscribeTestError.value = error?.response?.data?.message || error.message || String(error);
  } finally {
    autoSubscribeTestLoading.value = false;
    testingAutoSubscribe.value = "";
  }
}

onMounted(async () => {
  window.addEventListener("message", handleHdhiveOAuthMessage)
  emit("layout", { maxWidth: "62rem" })
  try {
    await Promise.all([loadOptions("base"), loadOptions("search")]);
  } catch (e) {
    notify(`加载配置选项失败：${e.message || e}`, "warning")
  }
})

watch(activeTab, (tab) => {
  Promise.all(
    optionScopesForTab(tab).map((scope) =>
      loadOptions(scope).catch((error) => {
        notify(`加载配置选项失败：${error.message || error}`, "warning")
      }),
    ),
  )
})

onBeforeUnmount(() => {
  previewRequestId += 1
  window.removeEventListener("message", handleHdhiveOAuthMessage)
  if (hdhiveOauthWindow && !hdhiveOauthWindow.closed) hdhiveOauthWindow.close()
})

watch(previewVisible, (visible) => {
  if (visible) return
  previewRequestId += 1
  previewingUrl.value = ""
  previewLoading.value = false
  previewError.value = ""
  previewItems.value = []
  previewMeta.value = {}
  previewBreadcrumbs.value = []
  previewResourceType.value = ""
  previewShareUrl.value = ""
  previewSource.value = ""
  previewJuyingResourceId.value = ""
  previewHdhiveResourceRef.value = "";
  previewHdhiveUnlocked.value = false
  previewProviderData.value = {};
  previewPendingResource.value = {};
  previewTargetSeason.value = null
  previewTargetEpisodes.value = []
})

watch(
  () => props.initialConfig,
  (value) => {
    Object.assign(config, JSON.parse(JSON.stringify(value || {})));
    normalizeAutoSubscribeYears(config);
  },
  { deep: true },
)

watch(
  [() => config.cloud_drive, () => config.cross_transfer_enabled, () => options.cloudDrives, () => options.resourceTypes],
  ([provider, crossTransfer, drives], [previousProvider, previousCrossTransfer, previousDrives]) => {
    if (provider === previousProvider && crossTransfer === previousCrossTransfer && drives === previousDrives) return
    const supported = new Set(createResourceTypeItems(options, config).map((item) => item.value));
    config.resource_type_order = (config.resource_type_order || []).filter((value) => supported.has(value))
  },
)
</script>

<style scoped>
.cloud-subscribe-config {
  display: flex;
  width: min(62rem, calc(100vw - 32px));
  max-width: min(62rem, 100%);
  min-width: 0;
  height: min(800px, calc(100dvh - 64px));
  max-height: min(800px, calc(100dvh - 64px));
  min-height: 0;
  flex-direction: column;
  overflow: hidden;
}

:global(.v-overlay__content:has(.cloud-subscribe-config)) {
  overflow: hidden !important;
}

:global(.v-overlay__content:has(.cloud-subscribe-config) > *) {
  min-height: 0;
  max-height: 100%;
  overflow: hidden !important;
}

.config-shell {
  width: 100%;
  max-width: 100%;
  min-width: 0;
  height: auto;
  max-height: 100%;
  min-height: 0;
  flex: 1 1 auto;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.config-header {
  min-width: 0;
  flex-wrap: nowrap;
}

.config-title {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.cloud-subscribe-config {
  display: flex;
  width: min(62rem, calc(100vw - 32px));
  max-width: min(62rem, 100%);
  min-width: 0;
  height: min(800px, calc(100dvh - 56px));
  max-height: min(800px, calc(100dvh - 56px));
  min-height: 0;
  flex-direction: column;
  overflow: hidden;
}

:global(.v-overlay__content:has(.cloud-subscribe-config)) {
  overflow: hidden !important;
  border-radius: 8px !important;
}

:global(.v-overlay__content:has(.cloud-subscribe-config) > *) {
  min-height: 0;
  max-height: 100%;
  overflow: hidden !important;
}

.config-shell {
  width: 100%;
  max-width: 100%;
  min-width: 0;
  height: 100%;
  max-height: 100%;
  min-height: 0;
  flex: 1 1 auto;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  border-radius: 12px !important;
  border: 1px solid rgba(var(--v-border-color), 0.12) !important;
  background-color: rgb(var(--v-theme-surface)) !important;
}

/* 毛玻璃主题深度适配：透光柔和优雅，避免纯黑死黑，与普通样式绝不混淆 */
:global(html[data-theme="transparent"]) .config-shell,
:global(html[data-theme="glass"]) .config-shell,
:global(html[data-theme-preference="transparent"]) .config-shell,
:global(html[class*="transparent-glass"]) .config-shell,
:global(html[data-glass-appearance]) .config-shell,
:global(.v-theme--transparent) .config-shell {
  background-color: rgba(var(--v-theme-surface), var(--transparent-opacity-heavy, 0.78)) !important;
  backdrop-filter: blur(var(--transparent-blur-heavy, 16px)) saturate(140%) !important;
  -webkit-backdrop-filter: blur(var(--transparent-blur-heavy, 16px)) saturate(140%) !important;
  border: 1px solid rgba(var(--v-border-color), 0.16) !important;
  box-shadow: 0 16px 48px rgba(0, 0, 0, 0.35) !important;
}

.config-layout {
  display: flex;
  flex-direction: row;
  width: 100%;
  height: 100%;
  min-height: 0;
  flex: 1 1 auto;
  overflow: hidden;
}

/* 右侧主区域与内容架构 */
.config-main {
  flex: 1 1 0;
  min-width: 0 !important;
  height: 100%;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.config-main-header {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 24px 6px;
}

:global(html[data-theme="transparent"]) .config-main-header,
:global(html[data-theme="glass"]) .config-main-header,
:global(html[data-theme-preference="transparent"]) .config-main-header,
:global(html[class*="transparent-glass"]) .config-main-header,
:global(html[data-glass-appearance]) .config-main-header,
:global(.v-theme--transparent) .config-main-header {
  background: transparent !important;
}

.config-main-title {
  font-size: 1.05rem;
  font-weight: 600;
  color: rgb(var(--v-theme-on-surface));
}

.config-header-actions {
  display: flex;
  align-items: center;
  gap: 4px;
}

.config-header-btn {
  font-size: 0.8125rem;
  border-radius: 8px !important;
  transition: all 0.2s ease !important;
}

.config-header-btn:hover {
  background-color: rgba(var(--v-theme-on-surface), 0.06) !important;
  transform: translateY(-0.5px);
}

.config-main-body {
  flex: 1 1 0;
  min-height: 0;
  width: 100%;
  height: 100%;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  position: relative;
}

/* v1.29.5：设置搜索框与搜索结果区 */
.config-search-bar {
  flex: 0 0 auto;
  padding: 10px 24px 0;
}

.config-search-results {
  flex: 1 1 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  padding: 6px 24px 14px;
}

.config-search-meta {
  flex: 0 0 auto;
  padding: 4px 0 8px;
}

.config-search-body {
  flex: 1 1 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.config-search-empty {
  flex: 1 1 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  text-align: center;
  padding: 24px 0;
}

.config-window-section {
  box-sizing: border-box;
  width: 100%;
  max-width: 100%;
  min-width: 0;
  height: 100%;
  min-height: 0;
  flex: 1 1 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  padding: 6px 24px 14px;
}

/* 主内容区切换平滑滑动淡入动画 */
.fade-slide-enter-active,
.fade-slide-leave-active {
  transition: opacity 0.22s cubic-bezier(0.4, 0, 0.2, 1), transform 0.22s cubic-bezier(0.4, 0, 0.2, 1);
}

.fade-slide-enter-from {
  opacity: 0;
  transform: translateY(6px);
}

.fade-slide-leave-to {
  opacity: 0;
  transform: translateY(-6px);
}

.config-actions {
  position: relative;
  min-height: 64px;
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 12px;
  padding: 10px 24px;
  border-top: 1px solid rgba(var(--v-border-color), 0.06);
  background-color: rgb(var(--v-theme-surface));
  flex-shrink: 0;
}

:global(html[data-theme="transparent"]) .config-actions,
:global(html[data-theme="glass"]) .config-actions,
:global(html[data-theme-preference="transparent"]) .config-actions,
:global(html[class*="transparent-glass"]) .config-actions,
:global(html[data-glass-appearance]) .config-actions,
:global(.v-theme--transparent) .config-actions {
  background-color: transparent !important;
  border-top-color: rgba(var(--v-border-color), 0.1) !important;
}

.actions-spacer {
  flex: 1 1 auto;
}

.save-progress {
  position: absolute;
  inset: 0 0 auto;
}

.save-state {
  display: flex;
  align-items: center;
  gap: 8px;
  color: rgb(var(--v-theme-primary));
  font-size: 0.875rem;
}

.save-config-button {
  min-width: 136px;
  height: 42px !important;
  font-weight: 600 !important;
  font-size: 0.875rem !important;
  letter-spacing: 0.02em !important;
  border-radius: 10px !important;
  background: linear-gradient(135deg, rgb(var(--v-theme-primary)) 0%, #7c4dff 100%) !important;
  color: #ffffff !important;
  border: 1px solid rgba(255, 255, 255, 0.16) !important;
  box-shadow: 0 4px 14px rgba(var(--v-theme-primary), 0.35), inset 0 1px 0 rgba(255, 255, 255, 0.22) !important;
  transition: all 0.25s cubic-bezier(0.4, 0, 0.2, 1) !important;
}

.save-config-button:hover {
  box-shadow: 0 6px 20px rgba(var(--v-theme-primary), 0.48), inset 0 1px 0 rgba(255, 255, 255, 0.32) !important;
  transform: translateY(-2px);
  filter: brightness(1.04);
}

.save-config-button:active {
  transform: translateY(0);
  box-shadow: 0 2px 6px rgba(var(--v-theme-primary), 0.3) !important;
}

.save-config-button :deep(.v-icon) {
  transition: transform 0.25s ease;
}

.save-config-button:hover :deep(.v-icon) {
  transform: scale(1.12);
}

.save-config-button:active {
  transform: translateY(0);
}

.config-window {
  width: 100%;
  max-width: 100%;
  min-width: 0;
  min-height: 0;
  flex: 1 1 0;
  display: flex;
  overflow: hidden;
}

@media (max-width: 768px) {
  :global(.v-overlay__content:has(.cloud-subscribe-config)) {
    width: 100vw !important;
    max-width: 100vw !important;
    height: 100dvh !important;
    max-height: 100dvh !important;
    margin: 0 !important;
  }
  .cloud-subscribe-config {
    width: 100%;
    max-width: 100%;
    height: 100%;
    max-height: 100%;
  }
  .config-shell {
    border-radius: 0 !important;
    border: none !important;
  }
  .config-layout {
    flex-direction: column !important;
  }
  /* 移动端已将返回和关闭置于顶部 Brand 栏右侧，隐藏第三行冗余的 main-header */
  .config-main-header {
    display: none !important;
  }
  .config-window-section {
    padding: 8px 12px 16px;
  }
  .config-search-bar {
    padding: 8px 12px 0;
  }
  .config-search-results {
    padding: 8px 12px 16px;
  }
  .config-actions {
    min-height: 60px;
    padding-inline: 12px !important;
  }
}
</style>
