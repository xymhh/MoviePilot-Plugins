<template>
    <v-navigation-drawer
      :model-value="open"
      temporary
      location="right"
      :width="isMobile ? undefined : 380"
      @update:model-value="emit('update:open', $event)"
    >
      <div class="d-flex align-center pa-3">
        <div class="text-subtitle-1">渠道详情</div>
        <v-spacer />
        <v-btn icon="mdi-close" size="small" variant="text" @click="emit('update:open', false)" />
      </div>
      <v-divider />
      <v-list density="compact">
        <v-list-item title="名称" :subtitle="detail.name" />
        <v-list-item title="类型" :subtitle="typeLabel(detail.type)" />
        <v-list-item title="状态" :subtitle="detail.enabled ? '已启用' : '已停用'" />
        <v-list-item title="优先级" :subtitle="String(detail.priority)" />
        <v-list-item
          v-for="line in detailLines"
          :key="line.title"
          :title="line.title"
          :subtitle="line.value"
        />
        <v-list-item title="命中" :subtitle="String(detail.stats?.hits || 0)" />
        <v-list-item title="最近扫描" :subtitle="relativeTime(detail.stats?.last_scan)" />
        <v-list-item v-if="detail.stats?.last_error" title="最近错误" :subtitle="detail.stats.last_error" />
      </v-list>
      <div class="pa-3 d-flex ga-2">
        <v-btn variant="text" @click="emit('update:open', false)">关闭</v-btn>
        <v-spacer />
        <v-btn color="primary" @click="emit('edit')">编辑</v-btn>
      </div>
    </v-navigation-drawer>
</template>

<script setup>
const props = defineProps({
  open: { type: Boolean, default: false },
  detail: { type: Object, default: null },
  detailLines: { type: Array, default: () => [] },
  typeLabel: { type: Function, required: true },
  isMobile: { type: Boolean, default: false },
})
const emit = defineEmits(["update:open", "edit"])
</script>
