<template>
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
}
</style>
