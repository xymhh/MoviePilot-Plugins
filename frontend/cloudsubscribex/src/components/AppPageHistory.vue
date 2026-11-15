<template>
  <!-- 1.10.3：转存记录独立成主侧栏导航（MP 历史页没有插件的失败分类与重试）；实现见 PluginWorkbench。 -->
  <PluginWorkbench :api="api" :plugin-id="pluginId" :nav-key="navKey" :on-close="onClose" mode="history" />
</template>

<script setup>
import PluginWorkbench from "./PluginWorkbench.vue";

const props = defineProps({
  api: { type: [Object, Function], default: () => ({}) },
  pluginId: { type: String, default: "CloudSubscribeX" },
  navKey: { type: String, default: "history" },
  // MoviePilot 宿主以全屏弹窗打开插件页时传入；独立页面路由不传。
  onClose: { type: Function, default: null },
});

const api = props.api;
const pluginId = props.pluginId;
const navKey = props.navKey;
const onClose = props.onClose;
</script>
