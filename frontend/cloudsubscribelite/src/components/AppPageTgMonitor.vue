<template>
  <!-- 1.13.2：频道监控从渠道页独立成主侧栏导航（TgMonitor 是运行态工作列表，与渠道配置不是同一意图）；实现就是 TgMonitor 本身。 -->
  <TgMonitor :api="api" :active="true" :plugin-id="pluginId" />
</template>

<script setup>
import TgMonitor from "./dashboard/TgMonitor.vue";

const props = defineProps({
  api: { type: [Object, Function], default: () => ({}) },
  pluginId: { type: String, default: "CloudSubscribeLite" },
  navKey: { type: String, default: "tg-monitor" },
  // MoviePilot 宿主以全屏弹窗打开插件页时传入；独立页面路由不传。
  onClose: { type: Function, default: null },
});

const api = props.api;
const pluginId = props.pluginId;
const navKey = props.navKey;
const onClose = props.onClose;
</script>
