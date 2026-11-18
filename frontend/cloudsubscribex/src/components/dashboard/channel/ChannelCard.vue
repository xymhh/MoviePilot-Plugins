<template>
        <v-card variant="outlined" class="channel-card">
          <v-card-item class="pb-1">
            <template #prepend>
              <v-checkbox-btn
                :model-value="selected"
                density="compact"
                hide-details
                @update:model-value="emit('toggle-select', item.id, $event)"
              />
            </template>
            <v-card-title class="channel-title" :title="item.name" @click="emit('open-detail', item)">
              {{ item.name }}
            </v-card-title>
            <template #append>
              <v-chip size="x-small" :color="typeColor(item.type)" variant="flat">
                {{ typeLabel(item.type) }}
              </v-chip>
            </template>
          </v-card-item>
          <v-card-text class="py-1 channel-body" @click="emit('open-detail', item)">
            <div class="text-caption text-truncate">{{ channelLine(item) }}</div>
            <div class="text-caption text-medium-emphasis">优先级 {{ item.priority }}</div>
          </v-card-text>
          <v-card-text class="py-1">
            <div class="d-flex flex-wrap ga-1">
              <v-chip size="x-small" :color="item.enabled ? 'success' : 'default'" variant="tonal">
                {{ item.enabled ? "已启用" : "已停用" }}
              </v-chip>
              <v-chip size="x-small" variant="tonal" prepend-icon="mdi-clock-outline">
                {{ relativeTime(item.stats?.last_scan) }}
              </v-chip>
              <v-chip size="x-small" variant="tonal">命中 {{ item.stats?.hits || 0 }}</v-chip>
              <v-chip
                v-if="item.stats?.last_error"
                size="x-small"
                color="error"
                variant="tonal"
                :title="item.stats.last_error"
              >
                异常
              </v-chip>
            </div>
          </v-card-text>
          <v-card-actions class="pt-0">
            <v-switch
              :model-value="item.enabled"
              density="compact"
              hide-details
              color="success"
              class="channel-switch"
              @update:model-value="emit('set-enabled', item, $event)"
            />
            <v-spacer />
            <v-btn
              icon="mdi-access-point"
              size="small"
              variant="text"
              title="测试"
              :loading="testing"
              @click="emit('test', item)"
            />
            <v-btn
              icon="mdi-radar"
              size="small"
              variant="text"
              title="立即扫描"
              :disabled="item.type !== 'tg_channel'"
              :loading="scanning"
              @click="emit('scan', item)"
            />
            <v-btn icon="mdi-pencil" size="small" variant="text" title="编辑" @click="emit('edit', item)" />
            <v-btn
              icon="mdi-delete"
              size="small"
              variant="text"
              color="error"
              title="删除"
              @click="emit('delete', item)"
            />
          </v-card-actions>
        </v-card>
</template>

<script setup>
const props = defineProps({
  item: { type: Object, required: true },
  selected: { type: Boolean, default: false },
  testing: { type: Boolean, default: false },
  scanning: { type: Boolean, default: false },
  typeLabel: { type: Function, required: true },
  typeColor: { type: Function, required: true },
  channelLine: { type: Function, required: true },
  relativeTime: { type: Function, required: true },
})
const emit = defineEmits(["toggle-select", "set-enabled", "test", "scan", "edit", "delete", "open-detail"])
</script>

<style scoped>
.channel-card {
  min-height: 150px;
}

.channel-title {
  font-size: 14px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  cursor: pointer;
}

.channel-body {
  cursor: pointer;
}

.channel-switch {
  flex: 0 0 auto;
}

</style>
