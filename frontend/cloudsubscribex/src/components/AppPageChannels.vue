<template>
  <div class="cloud-channels-page">
    <ChannelManager :api="api" :plugin-id="pluginId" :active="true" />
    <!-- 1.10.3：频道监控与签到状态并进渠道页（TG 频道本身就是渠道对象），插件主页不再单独立页签。 -->
    <v-divider class="my-4" />
    <section class="channels-extra" aria-label="频道监控">
      <TgMonitor :api="api" :active="true" />
    </section>
    <v-divider class="my-4" />
    <section class="channels-extra" aria-label="签到状态">
      <CheckinDashboard :api="api" />
    </section>
  </div>
</template>

<script setup>
import ChannelManager from "./dashboard/ChannelManager.vue";
import TgMonitor from "./dashboard/TgMonitor.vue";
import CheckinDashboard from "./dashboard/CheckinDashboard.vue";

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

.channels-extra {
  min-width: 0;
  flex: 0 0 auto;
}
</style>
