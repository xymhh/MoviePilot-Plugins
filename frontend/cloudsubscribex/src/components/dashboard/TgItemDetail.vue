<template>
  <!-- v1.28.0：频道监控条目详情（列表视图与「未转存」分组视图共用） -->
  <div class="tg-detail">
    <div class="tg-detail-title text-body-2">{{ item.title || "（无标题）" }}</div>
    <dl class="tg-detail-grid">
      <dt>状态</dt>
      <dd>
        <v-chip size="x-small" variant="tonal" :color="statusTone">{{ statusLabel }}</v-chip>
      </dd>
      <template v-if="reasonFull">
        <dt>原因</dt>
        <dd :class="reasonClass">{{ reasonFull }}</dd>
      </template>
      <dt>频道</dt>
      <dd>{{ channelText }}</dd>
      <dt>消息时间</dt>
      <dd>{{ timeText }}</dd>
      <dt>匹配订阅</dt>
      <dd>{{ subscribeText }}</dd>
      <template v-if="item.match_reason">
        <dt>匹配说明</dt>
        <dd class="text-medium-emphasis">{{ item.match_reason }}</dd>
      </template>
    </dl>
    <div class="tg-detail-links">
      <div v-for="link in item.links || []" :key="link" class="tg-link-row">
        <span class="tg-link-text">{{ link }}</span>
        <v-btn
          size="x-small"
          variant="text"
          icon="mdi-content-copy"
          title="复制链接"
          @click.stop="$emit('copy', link)" />
        <v-btn
          size="x-small"
          variant="text"
          icon="mdi-open-in-new"
          title="打开链接"
          :href="link"
          target="_blank"
          @click.stop />
      </div>
      <div v-if="!(item.links || []).length" class="text-caption text-medium-emphasis">没有网盘链接</div>
    </div>
    <div class="tg-detail-actions">
      <v-btn
        v-if="canTransfer"
        class="tg-transfer-btn"
        size="small"
        color="primary"
        variant="flat"
        prepend-icon="mdi-cloud-download-outline"
        :loading="busy"
        @click.stop="$emit('transfer', item)">
        转存
      </v-btn>
      <span v-else-if="transferHint" class="tg-detail-hint text-caption text-medium-emphasis">
        {{ transferHint }}
      </span>
      <v-btn
        v-if="item.message_url"
        size="small"
        variant="text"
        prepend-icon="mdi-send-outline"
        :href="item.message_url"
        target="_blank"
        @click.stop>
        TG 原消息
      </v-btn>
      <span class="flex-grow-1" />
      <v-btn
        v-if="item.status !== 'ignored'"
        size="small"
        variant="text"
        prepend-icon="mdi-eye-off-outline"
        @click.stop="$emit('ignore', item)">
        忽略
      </v-btn>
      <v-btn
        size="small"
        variant="text"
        color="error"
        prepend-icon="mdi-delete-outline"
        @click.stop="$emit('remove', item)">
        删除
      </v-btn>
    </div>
  </div>
</template>

<script setup>
import { computed } from "vue"

const props = defineProps({
  item: { type: Object, required: true },
  subscribeName: { type: String, default: "" },
  canTransfer: { type: Boolean, default: false },
  transferHint: { type: String, default: "" },
  busy: { type: Boolean, default: false },
  formatTime: { type: Function, required: true },
})
defineEmits(["transfer", "ignore", "remove", "copy"])

const STATUS = {
  new: ["待处理", "primary"],
  pending: ["待处理", "primary"],
  queued: ["排队中", "primary"],
  transferring: ["转存中", "primary"],
  processing: ["转存中", "primary"],
  transferred: ["已转存", "success"],
  skipped: ["未转存", "grey"],
  ignored: ["已忽略", "grey"],
  failed: ["失败", "error"],
}

const statusLabel = computed(() => {
  const base = STATUS[props.item.status]?.[0] || props.item.status || "未知"
  if (props.item.status === "failed" && props.item.failure_stage === "postprocess") return "后处理失败"
  if (props.item.status === "failed" && props.item.failure_stage === "subscribe") return "订阅失败"
  return base
})
const statusTone = computed(() => STATUS[props.item.status]?.[1] || "grey")

// 完整原因：不压缩，排障用（列表上的短标签只是摘要）。
const reasonFull = computed(() => {
  const item = props.item
  const text = String(item.auto_transfer_error || "").trim()
  if (text) return text
  return String(item.skip_reason || "").trim()
})
const reasonClass = computed(() =>
  props.item.status === "failed" && props.item.failure_kind !== "interrupted"
    ? "text-error"
    : "text-medium-emphasis",
)
const channelText = computed(() => {
  const title = String(props.item.channel_title || "").trim()
  const channel = String(props.item.channel || "").trim()
  if (title && channel && title !== channel) return `${title}（${channel}）`
  return title || channel || "-"
})
const timeText = computed(() => props.formatTime(props.item.published_at || props.item.created_at))
const subscribeText = computed(() => {
  const id = Number(
    props.item.manual_subscribe_id || props.item.matched_subscribe_id || props.item.subscribe_id || 0,
  )
  if (!id) return "未匹配"
  return props.subscribeName ? `${props.subscribeName}（#${id}）` : `#${id}`
})
</script>

<style scoped>
.tg-detail {
  margin: 2px 0 6px;
  padding: 10px 12px;
  border-left: 2px solid rgba(var(--v-theme-primary), 0.35);
  background: rgba(var(--v-theme-surface-variant), 0.18);
  border-radius: 0 6px 6px 0;
  min-width: 0;
}
.tg-detail-title {
  font-weight: 600;
  line-height: 1.45;
  word-break: break-all;
  margin-bottom: 6px;
}
.tg-detail-grid {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  column-gap: 12px;
  row-gap: 4px;
  margin: 0;
  font-size: 12px;
  line-height: 1.5;
}
.tg-detail-grid dt {
  color: rgba(var(--v-theme-on-surface), 0.6);
  white-space: nowrap;
}
.tg-detail-grid dd {
  margin: 0;
  min-width: 0;
  word-break: break-all;
}
.tg-detail-links {
  margin-top: 8px;
}
.tg-link-row {
  display: flex;
  align-items: center;
  gap: 2px;
  min-width: 0;
}
.tg-link-text {
  flex: 1;
  min-width: 0;
  font-size: 12px;
  font-family: monospace;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.tg-detail-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  margin-top: 8px;
}
.tg-detail-hint {
  padding: 0 4px;
}
</style>
