<template>
    <v-dialog :model-value="modelValue" max-width="560" class="auto-subscribe-test-dialog" @update:model-value="emit('update:modelValue', $event)">
      <v-card class="auto-subscribe-test-card">
        <v-card-title class="d-flex align-center ga-2">
          <v-icon icon="mdi-flask-outline" color="primary" />
          <span>{{ providerName }}测试</span>
          <v-spacer />
          <v-btn icon="mdi-close" size="small" variant="text" title="关闭" @click="emit('update:modelValue', false)" />
        </v-card-title>
        <v-card-text class="auto-subscribe-test-body">
          <v-alert v-if="error" type="error" variant="tonal" density="compact" class="mb-3">
            {{ error }}
          </v-alert>
          <div v-if="loading" class="auto-subscribe-test-loading">
            <v-progress-circular indeterminate color="primary" size="36" />
            <span class="text-body-2">正在抓取榜单示例</span>
          </div>
          <template v-else-if="result">
            <div class="text-body-2 mb-2">{{ message }}</div>
            <v-list v-if="items.length" density="compact" lines="two" class="auto-subscribe-test-list">
              <v-list-item v-for="(item, index) in items" :key="`${item.title}-${index}`">
                <template #prepend>
                  <v-avatar size="30" color="primary" variant="tonal">{{ index + 1 }}</v-avatar>
                </template>
                <v-list-item-title>{{ item.title }}</v-list-item-title>
                <v-list-item-subtitle>
                  <span v-if="item.year">{{ item.year }}</span>
                  <span v-if="item.media_type">· {{ item.media_type === "tv" ? "电视剧" : "电影" }}</span>
                  <span v-if="item.season != null">· 第 {{ item.season }} 季</span>
                </v-list-item-subtitle>
              </v-list-item>
            </v-list>
            <v-alert v-else type="info" variant="tonal" density="compact">榜单已连通，但没有示例数据。</v-alert>
          </template>
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="emit('update:modelValue', false)">关闭</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>
</template>

<script setup>
const props = defineProps({
  modelValue: { type: Boolean, default: false },
  providerName: { type: String, default: "榜单" },
  loading: { type: Boolean, default: false },
  error: { type: String, default: "" },
  result: { type: Object, default: null },
  message: { type: String, default: "测试完成" },
  items: { type: Array, default: () => [] },
})
const emit = defineEmits(["update:modelValue"])
</script>

<style scoped>
.auto-subscribe-test-card {
  max-height: min(620px, calc(100dvh - 48px));
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.auto-subscribe-test-body {
  min-height: 0;
  overflow: hidden;
}

.auto-subscribe-test-loading {
  min-height: 180px;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-direction: column;
  gap: 12px;
}

.auto-subscribe-test-list {
  max-height: min(420px, 52dvh);
  overflow-y: auto;
  border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
  border-radius: 8px;
}

</style>
