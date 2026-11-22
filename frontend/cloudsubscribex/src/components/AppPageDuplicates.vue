<template>
  <!-- v1.31.0：重复扫描（同集多份 .strm）独立成主侧栏导航；实现就是 DuplicatesQueue 本身。 -->
  <DuplicatesQueue :api="api" :active="true" :plugin-id="pluginId" />
</template>

<script setup>
import DuplicatesQueue from "./dashboard/DuplicatesQueue.vue";

const props = defineProps({
  api: { type: [Object, Function], default: () => ({}) },
  pluginId: { type: String, default: "CloudSubscribeX" },
  navKey: { type: String, default: "duplicates" },
  // MoviePilot 宿主以全屏弹窗打开插件页时传入；独立页面路由不传。
  onClose: { type: Function, default: null },
});

const api = props.api;
const pluginId = props.pluginId;
const navKey = props.navKey;
const onClose = props.onClose;
</script>
