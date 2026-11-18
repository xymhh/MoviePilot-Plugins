<template>
              <div class="config-window-section wizard-panel">
                <div class="wizard-head">
                  <div class="wizard-steps">
                    <div
                      v-for="(step, index) in wizardSteps"
                      :key="step.value"
                      :class="['wizard-step-chip', {'is-active': index === wizardStep, 'is-done': index < wizardStep}]">
                      <span class="wizard-step-index">{{ index < wizardStep ? '✓' : index + 1 }}</span>
                      <span class="wizard-step-title">{{ step.title }}</span>
                    </div>
                  </div>
                  <div class="wizard-headline">{{ currentWizardStep.title }}</div>
                  <div class="wizard-subline">{{ currentWizardStep.hint }}</div>
                </div>

                <ConfigSection
                  v-if="currentWizardStep.groups.length"
                  :section="currentWizardStep"
                  :config="config"
                  :options="options"
                  :api="api"
                  :refreshing-accounts="refreshingAccounts"
                  :testing-source="testingSource"
                  :testing-auto-subscribe="testingAutoSubscribe"
                  :testing-proxy="testingProxy"
                  :testing-auto-subscribe-proxy="testingAutoSubscribeProxy"
                  :hdhive-oauth-action="hdhiveOauthAction"
                  @scan="(...args) => emit('scan', ...args)"
                  @browse-directory="(...args) => emit('browse-directory', ...args)"
                  @test-source="(...args) => emit('test-source', ...args)"
                  @test-auto-subscribe="(...args) => emit('test-auto-subscribe', ...args)"
                  @test-proxy="(...args) => emit('test-proxy', ...args)"
                  @test-auto-subscribe-proxy="(...args) => emit('test-auto-subscribe-proxy', ...args)"
                  @refresh-account="(...args) => emit('refresh-account', ...args)"
                  @hdhive-oauth-start="(...args) => emit('hdhive-oauth-start', ...args)"
                  @hdhive-oauth-exchange="(...args) => emit('hdhive-oauth-exchange', ...args)"
                  @checkin-result="(...args) => emit('checkin-result', ...args)"
                  @copy-text="(...args) => emit('copy-text', ...args)"
                  @load-options="(...args) => emit('load-options', ...args)"
                  @refresh-options="(...args) => emit('refresh-options', ...args)"
                  @notify="(...args) => emit('notify', ...args)" />

                <div v-else class="wizard-finish">
                  <div v-if="wizardDoctorLoading" class="wizard-finish-loading">
                    <v-progress-circular indeterminate size="18" width="2" color="primary" />
                    <span>正在体检…</span>
                  </div>
                  <template v-else-if="wizardDoctor">
                    <div class="wizard-finish-title">
                      自检 {{ wizardDoctor.counts?.ok || 0 }} 项正常
                      <span v-if="wizardDoctor.counts?.warn" class="is-warn">· {{ wizardDoctor.counts.warn }} 项注意</span>
                      <span v-if="wizardDoctor.counts?.error" class="is-error">· {{ wizardDoctor.counts.error }} 项故障</span>
                    </div>
                    <div v-for="item in wizardDoctor.items" :key="item.key" class="wizard-finish-row">
                      <span :class="['doctor-dot', `is-${item.status}`]" />
                      <span class="wizard-finish-name">{{ item.name }}</span>
                      <span class="wizard-finish-detail">{{ item.detail }}</span>
                    </div>
                  </template>
                  <div v-else class="wizard-finish-title">已保存，可点「重新体检」确认</div>
                </div>

                <div class="wizard-actions">
                  <v-btn v-if="wizardStep > 0" variant="text" size="small" @click="emit('prev-step')">上一步</v-btn>
                  <v-spacer />
                  <v-btn
                    v-if="wizardStep === 1 && !String(config.organize_source_path || '').trim()"
                    variant="text"
                    size="small"
                    @click="emit('next-step')">跳过这步</v-btn>
                  <v-btn
                    v-if="wizardStep < wizardSteps.length - 1"
                    color="primary"
                    variant="flat"
                    size="small"
                    :disabled="!wizardStepValid"
                    @click="emit('next-step')">下一步</v-btn>
                  <template v-else>
                    <v-btn variant="text" size="small" :loading="wizardDoctorLoading" @click="emit('run-doctor')">重新体检</v-btn>
                    <v-btn color="primary" variant="flat" size="small" :loading="saving" @click="emit('finish')">完成并保存</v-btn>
                  </template>
                </div>
              </div>
</template>

<script setup>
const props = defineProps({
  wizardStep: { type: Number, default: 0 },
  wizardSteps: { type: Array, default: () => [] },
  currentWizardStep: { type: Object, default: () => ({}) },
  config: { type: Object, default: () => ({}) },
  options: { type: Object, default: () => ({}) },
  api: { type: [Object, Function], required: true },
  refreshingAccounts: { type: Array, default: () => [] },
  testingSource: { type: String, default: "" },
  testingAutoSubscribe: { type: String, default: "" },
  testingProxy: { type: Boolean, default: false },
  testingAutoSubscribeProxy: { type: Boolean, default: false },
  hdhiveOauthAction: { type: String, default: "" },
  wizardDoctor: { type: Object, default: null },
  wizardDoctorLoading: { type: Boolean, default: false },
  saving: { type: Boolean, default: false },
  wizardStepValid: { type: Boolean, default: false },
})
const emit = defineEmits(["prev-step", "next-step", "run-doctor", "finish", "scan", "browse-directory", "test-source", "test-auto-subscribe", "test-proxy", "test-auto-subscribe-proxy", "refresh-account", "hdhive-oauth-start", "hdhive-oauth-exchange", "checkin-result", "copy-text", "load-options", "refresh-options", "notify"])
</script>

<style scoped>
/* ---------------------------------------------------------------- 配置向导 */
.wizard-panel { display: flex; flex-direction: column; gap: 14px; }

.wizard-head { display: flex; flex-direction: column; gap: 8px; }

.wizard-steps { display: flex; gap: 6px; flex-wrap: wrap; }

.wizard-step-chip {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 4px 10px; border-radius: 999px;
  background: rgba(var(--v-border-color), 0.35);
  font-size: 12px; opacity: 0.65;
}

.wizard-step-chip.is-active { background: rgba(var(--v-theme-primary), 0.14); color: rgb(var(--v-theme-primary)); opacity: 1; font-weight: 600; }

.wizard-step-chip.is-done { opacity: 0.9; }

.wizard-step-index {
  width: 16px; height: 16px; border-radius: 50%;
  display: inline-flex; align-items: center; justify-content: center;
  font-size: 10px; background: rgba(var(--v-theme-primary), 0.18);
}

.wizard-step-chip.is-done .wizard-step-index { background: rgb(var(--v-theme-success)); color: #fff; }

.wizard-headline { font-size: 16px; font-weight: 650; }

.wizard-subline { font-size: 12px; opacity: 0.65; }

.wizard-finish { display: flex; flex-direction: column; gap: 6px; padding: 10px 4px; }

.wizard-finish-loading { display: flex; align-items: center; gap: 8px; font-size: 13px; opacity: 0.75; }

.wizard-finish-title { font-size: 13px; font-weight: 600; }

.wizard-finish-title .is-warn { color: rgb(var(--v-theme-warning)); }

.wizard-finish-title .is-error { color: rgb(var(--v-theme-error)); }

.wizard-finish-row { display: flex; align-items: center; gap: 8px; font-size: 12px; }

.wizard-finish-name { font-weight: 600; min-width: 76px; }

.wizard-finish-detail { opacity: 0.7; word-break: break-all; }

.wizard-finish-row .doctor-dot { width: 7px; height: 7px; border-radius: 50%; margin-top: 0; flex: 0 0 auto; }

.wizard-finish-row .doctor-dot.is-ok { background: rgb(var(--v-theme-success)); }

.wizard-finish-row .doctor-dot.is-warn { background: rgb(var(--v-theme-warning)); }

.wizard-finish-row .doctor-dot.is-error { background: rgb(var(--v-theme-error)); }

.wizard-finish-row .doctor-dot.is-skip { background: rgba(var(--v-border-color), 0.9); }

.wizard-actions { display: flex; align-items: center; gap: 6px; padding-top: 10px; border-top: 1px solid rgba(var(--v-border-color), 0.6); }

</style>
