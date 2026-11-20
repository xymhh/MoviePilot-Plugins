<template>
        <aside class="config-sidebar">
          <div class="sidebar-brand">
            <div class="sidebar-brand-main">
              <div class="sidebar-brand-badge">
                <v-icon icon="mdi-cloud-cog-outline" color="primary" size="18" class="sidebar-brand-icon" />
              </div>
              <div class="sidebar-brand-text">
                <span class="sidebar-brand-title">网盘订阅助手</span>
                <span class="sidebar-brand-subtitle">CloudSubscribeX</span>
              </div>
            </div>
            <!-- 手机端顶部右侧操作栏（位置正确且直接置顶） -->
            <div class="sidebar-mobile-actions d-md-none">
              <v-btn
                v-if="showSwitch"
                class="mobile-header-btn"
                variant="text"
                size="small"
                density="compact"
                prepend-icon="mdi-arrow-left"
                title="返回详情"
                @click="emit('switch')">
                返回
              </v-btn>
              <v-btn
                class="mobile-header-btn"
                variant="text"
                size="small"
                density="compact"
                icon="mdi-close"
                title="关闭"
                @click="emit('close')" />
            </div>
          </div>
          <nav class="sidebar-nav" aria-label="配置导航">
            <!-- 电脑端：带精美分组层级 -->
            <template v-if="!isMobile">
              <div
                v-for="group in navGroups"
                :key="group.name"
                class="sidebar-nav-group">
                <div class="sidebar-group-title">{{ group.name }}</div>
                <button
                  v-for="section in group.items"
                  :key="section.value"
                  type="button"
                  class="sidebar-nav-item"
                  :class="{ 'sidebar-nav-item--active': activeTab === section.value }"
                  @click="emit('nav-click', section.value, $event)">
                  <span v-if="activeTab === section.value" class="sidebar-active-pill"></span>
                  <v-icon :icon="section.icon" size="17" class="sidebar-nav-icon" />
                  <span class="sidebar-nav-title">{{ section.title }}</span>
                </button>
              </div>
            </template>
            <!-- 手机端：第一行分组、第二行当前分组的子项（两行固定高度，层级与 PC 一致） -->
            <template v-else>
              <div class="mobile-nav-groups">
                <button
                  v-for="group in navGroups"
                  :key="group.name"
                  type="button"
                  class="mobile-group-tab"
                  :class="{ 'mobile-group-tab--active': mobileGroupName === group.name }"
                  @click="emit('mobile-group-click', group)">
                  {{ group.name }}
                </button>
              </div>
              <div ref="mobileNavItemsRef" class="mobile-nav-items">
                <button
                  v-for="section in mobileGroupItems"
                  :key="section.value"
                  type="button"
                  class="sidebar-nav-item sidebar-nav-item--mobile"
                  :class="{ 'sidebar-nav-item--active': activeTab === section.value }"
                  @click="emit('nav-click', section.value, $event)">
                  <v-icon :icon="section.icon" size="16" class="sidebar-nav-icon" />
                  <span class="sidebar-nav-title">{{ section.title }}</span>
                </button>
              </div>
            </template>
          </nav>
          <!-- 1.10.3：高级设置折叠开关（默认只露常用 6 组） -->
          <div class="sidebar-advanced-toggle">
            <v-switch
              :model-value="showAdvanced"
              @update:model-value="emit('update:showAdvanced', $event)"
              density="compact"
              hide-details
              color="primary"
              label="显示高级设置" />
          </div>
          <!-- 电脑端左下角全高状态看板卡片（排版饱满高级，不显空旷） -->
          <div class="sidebar-footer">
            <div class="sidebar-status-panel">
              <!-- 顶部：运行状态与版本标签 -->
              <div class="status-panel-top">
                <div class="status-live-badge">
                  <span class="status-live-dot"></span>
                  <span class="status-live-text">运行中</span>
                </div>
                <span v-if="pluginVersion" class="status-version-tag">v{{ pluginVersion }}</span>
              </div>

              <!-- 中部：元数据指标卡片网格 -->
              <div class="status-info-grid">
                <div v-if="pluginAuthor" class="status-info-row">
                  <span class="status-info-label">作者</span>
                  <span class="status-info-value">{{ pluginAuthor }}</span>
                </div>
              </div>

              <!-- 底部：开源社区链接与支持 -->
              <div class="status-panel-links">
                <a
                  v-if="repoUrl"
                  :href="repoUrl"
                  target="_blank"
                  rel="noopener noreferrer"
                  class="status-action-link"
                  title="访问 GitHub 开源项目仓库">
                  <v-icon icon="mdi-github" size="13" class="mr-1" />
                  <span>开源仓库</span>
                  <v-icon icon="mdi-open-in-new" size="10" class="ml-auto opacity-50" />
                </a>
                <a
                  v-if="issuesUrl"
                  :href="issuesUrl"
                  target="_blank"
                  rel="noopener noreferrer"
                  class="status-action-link"
                  title="提交问题反馈或功能建议">
                  <v-icon icon="mdi-help-circle-outline" size="13" class="mr-1" />
                  <span>反馈支持</span>
                  <v-icon icon="mdi-chevron-right" size="12" class="ml-auto opacity-50" />
                </a>
                <a
                  v-else-if="authorUrl && !repoUrl"
                  :href="authorUrl"
                  target="_blank"
                  rel="noopener noreferrer"
                  class="status-action-link"
                  title="访问插件作者主页">
                  <v-icon icon="mdi-account-outline" size="13" class="mr-1" />
                  <span>作者主页</span>
                  <v-icon icon="mdi-open-in-new" size="10" class="ml-auto opacity-50" />
                </a>
              </div>
            </div>
          </div>
        </aside>
</template>

<script setup>
import {nextTick, onMounted, ref, watch} from "vue";

const props = defineProps({
  navGroups: { type: Array, default: () => [] },
  activeTab: { type: String, default: "" },
  isMobile: { type: Boolean, default: false },
  mobileGroupName: { type: String, default: "" },
  mobileGroupItems: { type: Array, default: () => [] },
  showAdvanced: { type: Boolean, default: false },
  pluginVersion: { type: String, default: "" },
  pluginAuthor: { type: String, default: "" },
  repoUrl: { type: String, default: "" },
  issuesUrl: { type: String, default: "" },
  authorUrl: { type: String, default: "" },
  showSwitch: { type: Boolean, default: false },
})
const emit = defineEmits(["update:showAdvanced", "nav-click", "mobile-group-click", "switch", "close"])

const mobileNavItemsRef = ref(null)

/** v1.29.3：手机端第二行子项是横向滚动行。分组切换后 scrollLeft
    会残留（上一分组滚过的位置），首项会被截掉一半；激活项变化后
    把它滚到可见位置（inline nearest：已可见就不动）。 */
function scrollActiveNavItemIntoView() {
  const container = mobileNavItemsRef.value
  if (!container) return
  const active = container.querySelector(".sidebar-nav-item--active")
  if (active) active.scrollIntoView({behavior: "smooth", inline: "nearest", block: "nearest"})
}

watch(
  [() => props.activeTab, () => props.mobileGroupItems],
  () => nextTick(scrollActiveNavItemIntoView),
)
onMounted(() => nextTick(scrollActiveNavItemIntoView))
</script>

<style scoped>
/* 侧边栏：经典干练的 162px 宽度、清晰层次感左对齐排版 */
.config-sidebar {
  width: 162px !important;
  min-width: 162px !important;
  max-width: 162px !important;
  flex: 0 0 162px !important;
  flex-shrink: 0 !important;
  height: 100%;
  min-height: 0;
  display: flex;
  flex-direction: column;
  background: rgba(var(--v-theme-on-surface), 0.026) !important;
  border-right: 1px solid rgba(var(--v-border-color), 0.12);
  overflow-y: auto;
  overflow-x: hidden;
  scrollbar-width: thin;
}

:global(html[data-theme="transparent"]) .config-sidebar,
:global(html[data-theme="glass"]) .config-sidebar,
:global(html[data-theme-preference="transparent"]) .config-sidebar,
:global(html[class*="transparent-glass"]) .config-sidebar,
:global(html[data-glass-appearance]) .config-sidebar,
:global(.v-theme--transparent) .config-sidebar {
  background: rgba(var(--v-theme-on-surface), 0.035) !important;
  border-right: 1px solid rgba(var(--v-border-color), 0.1) !important;
}

/* 顶部品牌微徽章与双行排版 */
.sidebar-brand {
  height: 52px;
  min-height: 52px;
  max-height: 52px;
  display: flex;
  align-items: center;
  justify-content: flex-start;
  padding: 0 12px;
  flex-shrink: 0;
  border-bottom: 1px solid rgba(var(--v-border-color), 0.08);
  box-sizing: border-box;
}

.sidebar-brand-main {
  display: flex;
  align-items: center;
  justify-content: flex-start;
  gap: 8px;
  overflow: hidden;
}

.sidebar-brand-badge {
  width: 26px;
  height: 26px;
  border-radius: 6px;
  background: rgba(var(--v-theme-primary), 0.12);
  border: 1px solid rgba(var(--v-theme-primary), 0.2);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}

.sidebar-brand-text {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  line-height: 1.15;
  overflow: hidden;
}

.sidebar-brand-title {
  font-size: 0.835rem;
  font-weight: 700;
  color: rgb(var(--v-theme-on-surface));
  white-space: nowrap;
  letter-spacing: -0.01em;
  text-align: left;
}

.sidebar-brand-subtitle {
  font-size: 0.58rem;
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  color: rgba(var(--v-theme-on-surface), 0.42);
  letter-spacing: 0.02em;
  text-align: left;
}

/* 结构化分组与导航列表 */
.sidebar-nav {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 8px 6px 10px;
  flex: 1 1 auto;
  box-sizing: border-box;
}

.sidebar-nav-group {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.sidebar-group-title {
  padding: 4px 10px 2px;
  text-align: left;
  font-size: 0.6875rem;
  font-weight: 600;
  letter-spacing: 0.04em;
  color: rgba(var(--v-theme-on-surface), 0.42);
  text-transform: uppercase;
  user-select: none;
}

.sidebar-nav-item {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: flex-start;
  width: 100%;
  height: 35px;
  padding: 0 10px;
  gap: 8px;
  border: none;
  border-radius: 6px;
  background: transparent;
  color: rgba(var(--v-theme-on-surface), 0.72);
  font-size: 0.8125rem;
  font-weight: 500;
  cursor: pointer;
  text-align: left;
  user-select: none;
  transition: all 0.16s cubic-bezier(0.4, 0, 0.2, 1);
  box-sizing: border-box;
}

.sidebar-nav-icon {
  margin-right: 0;
  color: inherit;
  opacity: 0.75;
  flex-shrink: 0;
  font-size: 16px !important;
  transition: transform 0.16s ease, opacity 0.16s ease;
}

.sidebar-nav-title {
  flex: 1 1 auto;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  text-align: left;
}

.sidebar-nav-item:hover:not(.sidebar-nav-item--active) {
  background: rgba(var(--v-theme-on-surface), 0.05);
  color: rgb(var(--v-theme-on-surface));
}

.sidebar-nav-item:hover:not(.sidebar-nav-item--active) .sidebar-nav-icon {
  opacity: 0.95;
  transform: scale(1.06);
}

/* 激活状态：微渐变高亮胶囊与左侧内嵌精致小标记 */
.sidebar-nav-item--active {
  background: linear-gradient(90deg, rgba(var(--v-theme-primary), 0.14) 0%, rgba(var(--v-theme-primary), 0.06) 100%) !important;
  color: rgb(var(--v-theme-primary)) !important;
  font-weight: 600;
}

.sidebar-nav-item--active .sidebar-nav-icon {
  opacity: 1;
  color: rgb(var(--v-theme-primary));
}

.sidebar-active-pill {
  position: absolute;
  left: 0;
  top: 9px;
  bottom: 9px;
  width: 3px;
  border-radius: 0 3px 3px 0;
  background: rgb(var(--v-theme-primary));
  box-shadow: 0 0 5px rgba(var(--v-theme-primary), 0.4);
}

/* 左下角全高状态看板卡片（占满下方空白，排版饱满高级） */
.sidebar-advanced-toggle {
  flex: 0 0 auto;
  padding: 4px 12px 8px;
  border-top: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
}

.sidebar-advanced-toggle :deep(.v-switch) {
  font-size: 0.85rem;
}

/* v1.29.2：侧栏只有 162px 宽，开关标签“显示高级设置”此前会折成两行
   （“显示高级设/置”）。标签不换行 + 字号略降，保证一行内放得下。 */
.sidebar-advanced-toggle :deep(.v-label) {
  font-size: 0.8125rem;
  white-space: nowrap;
}

.sidebar-footer {
  flex: 1 1 auto;
  display: flex;
  flex-direction: column;
  justify-content: flex-end;
  padding: 8px 8px 12px;
  box-sizing: border-box;
}

.sidebar-status-panel {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 9px 10px;
  background: rgba(var(--v-theme-on-surface), 0.025);
  border: 1px solid rgba(var(--v-border-color), 0.08);
  border-radius: 9px;
  user-select: none;
  transition: all 0.2s ease;
}

.sidebar-status-panel:hover {
  background: rgba(var(--v-theme-on-surface), 0.045);
  border-color: rgba(var(--v-border-color), 0.16);
}

.status-panel-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.status-live-badge {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}

.status-live-dot {
  width: 6.5px;
  height: 6.5px;
  border-radius: 50%;
  background-color: #10b981;
  box-shadow: 0 0 0 2px rgba(16, 185, 129, 0.2);
  animation: live-pulse 2s infinite ease-in-out;
}

@keyframes live-pulse {
  0%, 100% {
    transform: scale(1);
    box-shadow: 0 0 0 2px rgba(16, 185, 129, 0.25);
  }
  50% {
    transform: scale(1.15);
    box-shadow: 0 0 0 4px rgba(16, 185, 129, 0.1);
  }
}

.status-live-text {
  font-size: 0.72rem;
  font-weight: 600;
  color: rgba(var(--v-theme-on-surface), 0.75);
}

.status-version-tag {
  font-size: 0.6875rem;
  font-weight: 600;
  padding: 1px 5px;
  border-radius: 4px;
  background: rgba(var(--v-theme-primary), 0.12);
  color: rgb(var(--v-theme-primary));
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
}

.status-info-grid {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 5px 0;
  border-top: 1px dashed rgba(var(--v-border-color), 0.1);
  border-bottom: 1px dashed rgba(var(--v-border-color), 0.1);
}

.status-info-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 0.6875rem;
  line-height: 1.3;
}

.status-info-label {
  color: rgba(var(--v-theme-on-surface), 0.45);
}

.status-info-value {
  color: rgba(var(--v-theme-on-surface), 0.78);
  font-weight: 500;
}

.status-panel-links {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.status-action-link {
  display: flex;
  align-items: center;
  font-size: 0.6875rem;
  color: rgba(var(--v-theme-on-surface), 0.65);
  text-decoration: none;
  padding: 3px 4px;
  border-radius: 4px;
  transition: all 0.15s ease;
}

.status-action-link:hover {
  color: rgb(var(--v-theme-primary)) !important;
  background: rgba(var(--v-theme-primary), 0.08);
}

:global(html[data-theme="transparent"]) .sidebar-status-panel,
:global(html[data-theme="glass"]) .sidebar-status-panel,
:global(html[data-theme-preference="transparent"]) .sidebar-status-panel,
:global(html[class*="transparent-glass"]) .sidebar-status-panel,
:global(html[data-glass-appearance]) .sidebar-status-panel,
:global(.v-theme--transparent) .sidebar-status-panel {
  background: rgba(var(--v-theme-surface), 0.38) !important;
  border-color: rgba(255, 255, 255, 0.12) !important;
  backdrop-filter: blur(10px) !important;
}

@media (max-width: 768px) {
  .config-sidebar {
    width: 100% !important;
    min-width: 0 !important;
    max-width: 100% !important;
    flex: 0 0 auto !important;
    height: auto !important;
    min-height: auto !important;
    border-right: none !important;
    border-bottom: 1px solid rgba(var(--v-border-color), 0.08) !important;
    background: transparent !important;
    overflow: hidden !important;
  }
  /* 移动端彻底隐藏左下角元数据卡片 */
  .sidebar-footer {
    display: none !important;
  }
  .sidebar-brand {
    display: flex !important;
    align-items: center !important;
    justify-content: space-between !important;
    padding: 10px 12px 6px !important;
    border-bottom: none !important;
    margin-bottom: 0 !important;
  }
  .sidebar-brand-badge {
    width: 24px;
    height: 24px;
    border-radius: 6px;
  }
  .sidebar-brand-main {
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .sidebar-brand-title {
    font-size: 0.875rem;
    font-weight: 600;
  }
  .sidebar-mobile-actions {
    display: flex;
    align-items: center;
    gap: 2px;
  }
  .mobile-header-btn {
    font-size: 0.78125rem !important;
    padding: 0 6px !important;
    height: 28px !important;
    min-height: 28px !important;
    color: rgba(var(--v-theme-on-surface), 0.75) !important;
    border-radius: 6px !important;
  }
  /* 1.7.108：两行导航。第一行 4 个分组（互斥高亮），第二行只渲染当前分组的 1~5 个子项，
     总高度固定 ≈ 88px，把横向滑不到、标题被截断、层级丢失三个问题一起解掉。 */
  /* v1.29.3：两行 chip 导航收紧间距/高度，省纵向空间；子项行加
     scroll-padding，激活项滚到可见时两端各留 10px，不贴边截断。 */
  .sidebar-nav {
    display: flex;
    flex-direction: column;
    gap: 4px;
    padding: 2px 10px 6px;
    overflow: visible;
    white-space: normal;
  }
  .sidebar-nav::-webkit-scrollbar {
    display: none;
  }
  .mobile-nav-groups {
    display: flex;
    gap: 4px;
    overflow-x: auto;
    scrollbar-width: none;
    scroll-padding-inline: 10px;
  }
  .mobile-nav-groups::-webkit-scrollbar {
    display: none;
  }
  .mobile-group-tab {
    flex: 0 0 auto;
    height: 30px;
    padding: 0 12px;
    border-radius: 999px;
    border: 1px solid rgba(var(--v-border-color), 0.16);
    background: transparent;
    color: rgba(var(--v-theme-on-surface), 0.7);
    font-size: 0.8125rem;
    font-weight: 500;
    white-space: nowrap;
    transition: background-color 0.18s ease, color 0.18s ease, border-color 0.18s ease;
  }
  .mobile-group-tab--active {
    background: rgba(var(--v-theme-primary), 0.14);
    border-color: rgba(var(--v-theme-primary), 0.4);
    color: rgb(var(--v-theme-primary));
    font-weight: 600;
  }
  .mobile-nav-items {
    display: flex;
    gap: 4px;
    overflow-x: auto;
    scrollbar-width: none;
    scroll-padding-inline: 10px;
  }
  .mobile-nav-items::-webkit-scrollbar {
    display: none;
  }
  .sidebar-nav-item {
    width: 100%;
    height: 44px;
    padding: 0 14px;
    gap: 6px;
    justify-content: center;
    font-size: 0.8125rem;
    border-radius: 8px;
    text-align: center;
  }
  .sidebar-nav-item--mobile {
    flex: 0 0 auto;
    width: auto;
    min-width: 76px;
    max-width: 46vw;
    height: 40px;
    white-space: nowrap;
  }
  .sidebar-nav-title {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  /* 移动端横向 Tab 栏严禁显示 > 指示箭头与侧边指示条 */
  .sidebar-active-indicator,
  .sidebar-active-pill {
    display: none !important;
  }
  .sidebar-brand-subtitle {
    display: none !important;
  }
  .sidebar-nav-item--active::before {
    display: none !important;
  }
  .sidebar-nav-icon {
    margin-right: 5px;
    font-size: 15px !important;
  }
}
</style>
