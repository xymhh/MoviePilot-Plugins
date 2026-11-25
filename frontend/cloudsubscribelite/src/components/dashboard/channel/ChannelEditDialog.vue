<template>
    <v-dialog :model-value="modelValue" :fullscreen="isMobile" max-width="720" scrollable @update:model-value="emit('update:modelValue', $event)">
      <v-card>
        <v-card-title>{{ editing ? "编辑渠道" : "新增渠道" }}</v-card-title>
        <v-card-subtitle>渠道保存后立即生效，参与搜索与监控</v-card-subtitle>
        <v-card-text>
          <v-row dense>
            <v-col cols="12" sm="6">
              <v-text-field v-model="form.name" label="名称" density="compact" variant="outlined" />
            </v-col>
            <v-col cols="12" sm="6">
              <v-select
                v-model="form.type"
                :items="typeOptions"
                item-title="label"
                item-value="value"
                label="类型"
                density="compact"
                variant="outlined"
                :disabled="!!editing"
                @update:model-value="emit('type-change', $event)"
              />
            </v-col>
            <v-col cols="12" sm="6">
              <v-text-field
                v-model.number="form.priority"
                label="优先级（越大越优先）"
                type="number"
                density="compact"
                variant="outlined"
              />
            </v-col>
            <v-col cols="12" sm="6" class="d-flex align-center">
              <v-switch v-model="form.enabled" label="启用" color="success" hide-details />
            </v-col>
          </v-row>

          <template v-if="form.type === 'tg_channel'">
            <v-row dense>
              <v-col cols="12">
                <v-text-field
                  v-model="form.tg.channel"
                  label="频道标识"
                  placeholder="@channel 或 https://t.me/xxx"
                  density="compact"
                  variant="outlined"
                />
              </v-col>
              <v-col cols="12" sm="6">
                <v-text-field
                  v-model.number="form.tg.scan_limit"
                  label="回溯条数"
                  type="number"
                  min="1"
                  max="200"
                  density="compact"
                  variant="outlined"
                />
              </v-col>
              <v-col cols="12">
                <v-text-field
                  v-model="form.tg.keywords"
                  label="包含关键词（逗号分隔，留空不过滤）"
                  density="compact"
                  variant="outlined"
                />
              </v-col>
              <v-col cols="12">
                <v-text-field
                  v-model="form.tg.exclude_keywords"
                  label="排除关键词（逗号分隔）"
                  density="compact"
                  variant="outlined"
                />
              </v-col>
            </v-row>
          </template>
          <ConfigSection
            v-else-if="formSection.groups.length"
            :section="formSection"
            :config="form.config"
            :options="channelOptions"
            :api="api"
            :refreshing-accounts="refreshingAccounts"
            :hdhive-oauth-action="hdhiveOauthAction"
            @notify="(...args) => emit('notify', ...args)"
            @test-source="emit('test-source')"
            @refresh-account="(...args) => emit('refresh-account', ...args)"
            @load-options="loadChannelDynamicOptions"
            @refresh-options="refreshChannelDynamicOptions"
            @hdhive-oauth-start="emit('hdhive-oauth-start')"
            @hdhive-oauth-exchange="(...args) => emit('hdhive-oauth-exchange', ...args)"
          />
          <v-alert v-else type="info" variant="tonal" density="compact" class="mt-2">
            该搜索渠道没有可配置项，保存后即可参与搜索。
          </v-alert>
        </v-card-text>
        <v-card-actions>
          <v-btn variant="text" @click="emit('update:modelValue', false)">取消</v-btn>
          <v-spacer />
          <v-btn color="primary" :loading="saving" @click="emit('submit')">保存</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>
</template>

<script setup>
import ConfigSection from "../../config/ConfigSection.vue"

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  editing: { type: Object, default: null },
  form: { type: Object, required: true },
  formSection: { type: Object, required: true },
  channelOptions: { type: Object, required: true },
  api: { type: [Object, Function], required: true },
  refreshingAccounts: { type: Array, default: () => [] },
  hdhiveOauthAction: { type: String, default: "" },
  typeOptions: { type: Array, default: () => [] },
  saving: { type: Boolean, default: false },
  isMobile: { type: Boolean, default: false },
  loadChannelDynamicOptions: { type: Function, required: true },
  refreshChannelDynamicOptions: { type: Function, required: true },
})
const emit = defineEmits(["update:modelValue", "submit", "type-change", "test-source", "refresh-account", "hdhive-oauth-start", "hdhive-oauth-exchange", "notify"])
</script>
