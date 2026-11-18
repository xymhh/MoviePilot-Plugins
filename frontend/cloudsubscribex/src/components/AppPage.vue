<template>
  <!-- v1.23.0：总览模式已删，插件主页直接进失败记录页（默认只看失败）；实现见 PluginWorkbench。 -->
  <PluginWorkbench :api="api" :plugin-id="pluginId" :nav-key="navKey" :on-close="onClose" />
</template>

<script setup>
import PluginWorkbench from "./PluginWorkbench.vue";

const props = defineProps({
  api: { type: [Object, Function], default: () => ({}) },
  pluginId: { type: String, default: "CloudSubscribeX" },
  navKey: { type: String, default: "main" },
  // MoviePilot 宿主以全屏弹窗打开插件页时传入；独立页面路由不传。
  onClose: { type: Function, default: null },
});

const api = props.api;
const pluginId = props.pluginId;
const navKey = props.navKey;
const onClose = props.onClose;
</script>
