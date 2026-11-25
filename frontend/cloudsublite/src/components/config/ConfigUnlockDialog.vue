<template>
    <v-dialog :model-value="modelValue" max-width="440" :persistent="unlocking" @update:model-value="emit('update:modelValue', $event)">
      <v-card>
        <v-card-title class="d-flex align-center ga-2">
          <v-icon icon="mdi-lock-open-outline" color="warning" />
          解锁资源
        </v-card-title>
        <v-card-text>
          <p class="mb-3">
            <template v-if="Number(unlockItem?.unlock_points || 0) > 0">
              确认消耗
              {{ Number(unlockItem?.unlock_points || 0) }} 积分解锁此资源？
            </template>
            <template v-else>此资源免费，确认获取资源链接？</template>
          </p>
          <div class="text-body-2 text-medium-emphasis text-truncate" :title="unlockItem?.title || ''">
            {{ unlockItem?.title || "未命名资源" }}
          </div>
          <v-alert v-if="unlockError" type="error" variant="tonal" density="compact" class="mt-4">
            {{ unlockError }}
          </v-alert>
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" :disabled="unlocking" @click="emit('update:modelValue', false)">取消</v-btn>
          <v-btn
            color="warning"
            variant="flat"
            prepend-icon="mdi-lock-open-outline"
            :loading="unlocking"
            @click="emit('confirm-unlock')">
            确认解锁
          </v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>
</template>

<script setup>
const props = defineProps({
  modelValue: { type: Boolean, default: false },
  unlockItem: { type: Object, default: null },
  unlocking: { type: Boolean, default: false },
  unlockError: { type: String, default: "" },
})
const emit = defineEmits(["update:modelValue", "confirm-unlock"])
</script>
