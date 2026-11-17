<template>
  <!-- 1.13.2：渠道页只留渠道管理。频道监控独立成主侧栏导航，
    签到状态只留 MP 首页 widget——三者不再摞在一个滚动页里。 -->
  <div class="cloud-channels-page">
    <ChannelManager :api="api" :plugin-id="pluginId" :active="true" />
  </div>
</template>

<script setup>
import ChannelManager from "./dashboard/ChannelManager.vue";

const props = defineProps({
  api: { type: [Object, Function], default: () => ({}) },
  pluginId: { type: String, default: "CloudSubscribeX" },
  navKey: { type: String, default: "channels" },
  onClose: { type: Function, default: null },
});

const api = props.api;
const pluginId = props.pluginId;
</script>

<style scoped>
.cloud-channels-page {
  display: flex;
  flex-direction: column;
  min-height: 0;
  height: 100%;
  overflow-y: auto;
}
</style>
