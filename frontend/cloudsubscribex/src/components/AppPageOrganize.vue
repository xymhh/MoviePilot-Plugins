<template>
  <!-- 1.10.3：网盘整理独立成主侧栏导航（MP 没有 115 盘内整理界面）。 -->
  <div class="cloud-organize-page">
    <OrganizePanel :api="api" />
  </div>
</template>

<script setup>
import OrganizePanel from "./dashboard/OrganizePanel.vue";

const props = defineProps({
  api: { type: [Object, Function], default: () => ({}) },
  pluginId: { type: String, default: "CloudSubscribeX" },
  navKey: { type: String, default: "organize" },
  onClose: { type: Function, default: null },
});

const api = props.api;
const pluginId = props.pluginId;
</script>

<style scoped>
.cloud-organize-page {
  display: flex;
  flex-direction: column;
  min-height: 0;
  height: 100%;
  overflow-y: auto;
  padding: 16px;
}
</style>
