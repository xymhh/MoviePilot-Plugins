<template>
  <v-container class="dashboard-page" fluid>
    <div class="dashboard-page__header">
      <div class="text-h6">网盘订阅仪表盘</div>
      <div class="text-caption text-medium-emphasis">转存概览与频道监控状态</div>
    </div>

    <!-- 转存概览：复用已有的 Dashboard 卡片。 -->
    <div class="dashboard-page__overview">
      <Dashboard :api="api" :plugin-id="pluginId" :allow-refresh="true" :show-stats="false" />
    </div>

    <!-- 频道监控状态：只读摘要，详情与操作在频道监控页。 -->
    <v-card class="dashboard-page__tg" variant="outlined">
      <v-card-item>
        <template #prepend>
          <v-avatar color="primary" variant="tonal" size="36">
            <v-icon icon="mdi-telegram" size="20" />
          </v-avatar>
        </template>
        <v-card-title class="text-subtitle-1">频道监控</v-card-title>
        <v-card-subtitle>
          <span v-if="tgError" class="text-error text-caption">状态加载失败：{{ tgError }}</span>
          <span v-else-if="tgLoaded">
            {{ tgStatus.enabled ? "已启用" : "未启用" }} · {{ tgStatus.channel_count || 0 }} 个频道 ·
            {{ tgStatus.item_count || 0 }} 条抓取
          </span>
          <span v-else class="text-caption">正在加载状态…</span>
        </v-card-subtitle>
        <template #append>
          <v-btn
            variant="tonal"
            size="small"
            href="#/plugin-app/CloudSubscribeLite/tg-monitor">
            打开频道监控
          </v-btn>
        </template>
      </v-card-item>
    </v-card>

    <!-- 快捷入口：只做链接，不做任何操作。 -->
    <div class="dashboard-page__links">
      <v-btn variant="outlined" href="#/plugin-app/CloudSubscribeLite/main">
        打开网盘订阅
      </v-btn>
      <v-btn variant="outlined" href="#/plugin-app/CloudSubscribeLite/tg-monitor">
        打开频道监控
      </v-btn>
    </div>
  </v-container>
</template>

<script setup>
import { onMounted, ref } from "vue";
import Dashboard from "../Dashboard.vue";

const props = defineProps({
  api: { type: [Object, Function], default: () => ({}) },
  pluginId: { type: String, default: "CloudSubscribeLite" },
});

const api = props.api;
const pluginId = props.pluginId;

const tgStatus = ref({ enabled: false, channel_count: 0, item_count: 0 });
const tgLoaded = ref(false);
const tgError = ref("");

async function loadTgStatus() {
  if (!api?.get) {
    tgError.value = "接口不可用";
    return;
  }
  try {
    const result = await api.get(`plugin/${pluginId}/tg/status`);
    if (result?.success) {
      tgStatus.value = result.data || tgStatus.value;
      tgLoaded.value = true;
    } else {
      tgError.value = result?.message || "获取数据失败";
    }
  } catch (e) {
    tgError.value = e?.message || "获取数据失败";
  }
}

onMounted(() => {
  loadTgStatus();
});
</script>

<style scoped>
.dashboard-page {
  max-width: 960px;
  padding-top: 16px;
  padding-bottom: 24px;
}

.dashboard-page__header {
  margin-bottom: 16px;
}

.dashboard-page__overview {
  margin-bottom: 16px;
}

.dashboard-page__tg {
  margin-bottom: 16px;
}

.dashboard-page__links {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
}
</style>
