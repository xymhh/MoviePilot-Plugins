<template>
  <!-- 网盘订阅仪表盘：侧栏唯一入口。内层是 DashboardPage（转存概览 + 频道监控状态 + 快捷入口）。 -->
  <DashboardPage :api="api" :plugin-id="pluginId" />
</template>

<script setup>
import DashboardPage from "./dashboard/DashboardPage.vue";

const props = defineProps({
  api: { type: [Object, Function], default: () => ({}) },
  pluginId: { type: String, default: "CloudSubscribeLite" },
  navKey: { type: String, default: "dashboard" },
  // MoviePilot 宿主以全屏弹窗打开插件页时传入；独立页面路由不传。
  onClose: { type: Function, default: null },
});

const api = props.api;
const pluginId = props.pluginId;
const navKey = props.navKey;
const onClose = props.onClose;
</script>
