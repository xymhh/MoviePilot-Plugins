<template>
  <div class="tabs-container">
    <div class="header-tabs">
      <div
        v-for="tab in tabs"
        :key="tab.value"
        class="header-tab"
        :class="{ active: !searchMode && activeTab === tab.value, disabled: searchMode }"
        @click="!searchMode && $emit('change', tab.value)">
        <v-icon v-if="tab.icon" :icon="tab.icon" size="small" class="header-tab-icon" />
        <span>{{ tab.title }}</span>
      </div>
    </div>

    <v-badge
      :content="filterCount"
      :model-value="filterCount > 0"
      color="primary"
      class="filter-badge-btn flex-shrink-0">
      <v-btn
        icon="mdi-filter-variant"
        size="small"
        :variant="filterVisible ? 'tonal' : 'text'"
        :color="filterVisible || filterCount ? 'primary' : undefined"
        title="探索筛选"
        aria-label="探索筛选"
        @click="$emit('toggle-filter')" />
    </v-badge>
  </div>
</template>

<script setup>
defineProps({
  tabs: {type: Array, default: () => []},
  activeTab: {type: String, default: ""},
  searchMode: Boolean,
  filterVisible: Boolean,
  filterCount: {type: Number, default: 0},
});
defineEmits(["change", "toggle-filter"]);
</script>

<style scoped>
.tabs-container {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin: 0 0 12px 0;
  width: 100%;
  min-height: 38px;
}

.header-tabs {
  position: relative;
  display: flex;
  flex-grow: 1;
  align-items: center;
  gap: 12px;
  min-width: 0;
  overflow-x: auto;
  padding-top: 4px;
  padding-bottom: 4px;
  padding-left: 0;
  padding-right: 0;
  margin-left: -14px;
  scrollbar-width: none;
}

.header-tabs::-webkit-scrollbar {
  display: none;
}

.header-tab-icon {
  color: rgba(var(--v-theme-on-background), 0.6);
  margin-right: 6px;
  text-shadow: 0 1px 2px rgba(0, 0, 0, 0.1);
  transition: color 0.2s ease;
}

.header-tab {
  position: relative;
  display: inline-flex;
  align-items: center;
  border-radius: 20px;
  background-color: transparent;
  color: rgba(var(--v-theme-on-background), 0.7);
  cursor: pointer;
  font-size: 0.9rem;
  font-weight: 600;
  padding: 6px 14px;
  text-shadow: 0 1px 3px rgba(0, 0, 0, 0.1);
  transition: all 0.2s ease;
  white-space: nowrap;
  user-select: none;
}

.header-tab::after {
  position: absolute;
  border-radius: 3px;
  background-color: rgb(var(--v-theme-primary));
  height: 3px;
  content: "";
  width: 70%;
  bottom: -4px;
  left: 50%;
  transform: translateX(-50%) scaleX(0);
  transition: transform 0.2s ease;
}

.header-tab.active {
  color: rgb(var(--v-theme-primary));
  text-shadow: 0 1px 3px rgba(0, 0, 0, 0.15);
}

.header-tab.active::after {
  transform: translateX(-50%) scaleX(1);
}

.header-tab.active .header-tab-icon {
  color: rgb(var(--v-theme-primary));
  text-shadow: 0 1px 3px rgba(0, 0, 0, 0.15);
}

.header-tab:hover:not(.active):not(.disabled) {
  background-color: rgba(var(--v-theme-primary), 0.05);
  color: rgba(var(--v-theme-on-background), 1);
}

.header-tab.disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.filter-badge-btn {
  margin-left: 6px;
}

@media (max-width: 600px) {
  .tabs-container {
    margin: 0 0 8px 0;
    gap: 4px;
  }

  .header-tabs {
    margin-left: -10px;
  }

  .header-tab {
    padding: 4px 10px;
    font-size: 0.82rem;
  }

  .header-tab-icon {
    font-size: 15px !important;
    margin-right: 4px;
  }
}
</style>
