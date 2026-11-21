<template>
  <!-- v1.29.7 阶段 1：整理队列（网盘整理漏斗）独立成主侧栏导航；实现就是 FunnelQueue 本身。 -->
  <FunnelQueue :api="api" :active="true" :plugin-id="pluginId" />
</template>

<script setup>
import FunnelQueue from "./dashboard/FunnelQueue.vue";

const props = defineProps({
  api: { type: [Object, Function], default: () => ({}) },
  pluginId: { type: String, default: "CloudSubscribeX" },
  navKey: { type: String, default: "funnel" },
  // MoviePilot 宿主以全屏弹窗打开插件页时传入；独立页面路由不传。
  onClose: { type: Function, default: null },
});

const api = props.api;
const pluginId = props.pluginId;
const navKey = props.navKey;
const onClose = props.onClose;
</script>
