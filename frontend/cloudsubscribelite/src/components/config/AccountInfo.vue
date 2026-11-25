<template>
  <v-sheet :class="['account-info', { 'account-info--compact': compact }]" rounded="lg" :aria-busy="loading">
    <v-progress-linear v-if="loading" class="account-loading-bar" color="primary" height="2" indeterminate />
    <div v-if="loading && !hasLoadedInfo" class="account-skeleton">
      <v-skeleton-loader type="avatar" />
      <div class="account-skeleton-lines">
        <v-skeleton-loader type="text" />
        <v-skeleton-loader type="text" width="58%" />
      </div>
    </div>
    <div v-else class="account-content">
      <v-avatar :size="compact ? 36 : 42" class="account-avatar">
        <v-img v-if="user.avatar" :src="user.avatar" :alt="user.name" />
        <v-icon v-else icon="mdi-account-circle" :size="compact ? 24 : 28" />
      </v-avatar>
      <div class="account-main">
        <div class="account-heading">
          <span class="text-body-2 font-weight-medium account-name">
            {{ account.connected ? user.name || "未知用户" : "账号未连接" }}
          </span>
          <v-chip v-if="account.connected && user.badge" color="primary" size="x-small" variant="tonal">
            {{ user.badge }}
          </v-chip>
          <v-chip
            v-if="account.connected && user.membership_supported !== false"
            :color="user.is_vip ? 'amber-darken-2' : 'grey'"
            size="x-small"
            variant="tonal">
            {{ vipText }}
          </v-chip>
          <v-chip
            v-if="account.connected && compact && hasPoints"
            class="account-points-chip"
            color="info"
            size="x-small"
            variant="tonal">
            {{ points.label || "可用积分" }} {{ formattedPoints }}
          </v-chip>
        </div>
        <div v-if="account.connected && !compact && hasPoints" class="account-points">
          <span class="text-caption text-medium-emphasis">
            {{ points.label || "可用积分" }}
          </span>
          <span class="text-body-2 font-weight-bold text-primary">
            {{ formattedPoints }}
          </span>
        </div>
        <div v-if="account.connected && hasStorageInfo" class="account-storage text-caption text-medium-emphasis">
          已用 {{ storage.used || "未知" }} / {{ storage.total || "未知" }}
          <span v-if="storage.remaining">，剩余 {{ storage.remaining }}</span>
        </div>
        <div v-if="account.connected && details.length" class="account-details">
          <div v-for="item in details" :key="`${item.label}-${item.value}`" class="account-detail">
            <span class="account-detail-label">{{ item.label }}</span>
            <span class="account-detail-value">{{ formatDetailValue(item.value) }}</span>
          </div>
        </div>
        <div v-if="!account.connected" class="text-caption text-warning account-error">
          {{ account.error || "请填写登录凭证并保存配置" }}
        </div>
      </div>
      <v-btn
        v-if="refreshable"
        class="account-refresh"
        icon="mdi-refresh"
        color="primary"
        variant="text"
        size="small"
        :loading="loading"
        :disabled="loading || disabled"
        title="刷新账户信息"
        aria-label="刷新账户信息"
        @click="emit('refresh')" />
    </div>
  </v-sheet>
</template>

<script setup>
import {computed} from "vue";

const props = defineProps({
  account: { type: Object, default: () => ({}) },
  compact: { type: Boolean, default: false },
  loading: { type: Boolean, default: false },
  refreshable: { type: Boolean, default: false },
  disabled: { type: Boolean, default: false },
})
const emit = defineEmits(["refresh"])
const user = computed(() => props.account.user || {})
const storage = computed(() => props.account.storage || {})
const points = computed(() => props.account.points || {})
const details = computed(() => {
  const list = Array.isArray(props.account.details) ? props.account.details : [];
  return list
    .filter((item) => item && typeof item === "object" && item.label && item.value !== undefined && item.value !== null && item.value !== "")
    .map((item) => ({
      ...item,
      value: formatDetailValue(item.value),
    }));
});
const hasLoadedInfo = computed(() => Boolean(props.account?.connected || props.account?.refreshed_at))
const hasPoints = computed(() => points.value.available !== undefined && points.value.available !== null)
const hasStorageInfo = computed(() => Boolean(storage.value.used || storage.value.total || storage.value.remaining))
const formattedPoints = computed(() => {
  const value = Number(points.value.available)
  return Number.isFinite(value) ? value.toLocaleString("zh-CN") : String(points.value.available || 0)
})
const vipText = computed(() => {
  if (user.value.vip_label) {
    return user.value.vip_expire_date
      ? `${user.value.vip_label} 至 ${user.value.vip_expire_date}`
      : user.value.vip_label
  }
  if (!user.value.is_vip) return "非VIP"
  if (user.value.is_forever_vip) return "永久VIP"
  return user.value.vip_expire_date ? `VIP 至 ${user.value.vip_expire_date}` : "VIP"
})

function formatDetailValue(val) {
  if (val === null || val === undefined) return "";
  const text = String(val).trim();
  if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/.test(text)) {
    const d = new Date(text);
    if (!isNaN(d.getTime())) {
      const pad = (n) => String(n).padStart(2, "0");
      const y = d.getFullYear();
      const m = pad(d.getMonth() + 1);
      const day = pad(d.getDate());
      const h = pad(d.getHours());
      const min = pad(d.getMinutes());
      const s = pad(d.getSeconds());
      return `${y}-${m}-${day} ${h}:${min}:${s}`;
    }
  }
  return text;
}
</script>

<style scoped>
.account-info {
  position: relative;
  overflow: hidden;
  min-height: 76px;
  padding: 14px 16px;
  border: 1px solid rgba(var(--v-theme-primary), 0.14);
  background:
    linear-gradient(135deg, rgba(var(--v-theme-primary), 0.075), transparent 58%), rgb(var(--v-theme-surface));
  box-shadow: 0 5px 18px rgba(var(--v-theme-on-surface), 0.055);
}

.account-info--compact {
  min-height: 68px;
  padding: 10px 12px;
}

.account-loading-bar {
  position: absolute;
  inset: 0 0 auto;
}

.account-content,
.account-skeleton {
  display: flex;
  min-width: 0;
  align-items: flex-start;
  gap: 12px;
}

.account-skeleton {
  align-items: center;
}

.account-skeleton :deep(.v-skeleton-loader) {
  background: transparent;
}

.account-skeleton-lines {
  flex: 1 1 auto;
  min-width: 0;
}

.account-avatar {
  flex: 0 0 auto;
  color: rgb(var(--v-theme-primary));
  background: rgba(var(--v-theme-primary), 0.09);
}

.account-main {
  flex: 1 1 auto;
  min-width: 0;
}

.account-heading {
  display: flex;
  min-height: 24px;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  padding-right: 30px;
}

.account-name {
  max-width: min(280px, 55vw);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.account-refresh {
  position: absolute;
  top: 7px;
  right: 7px;
}

.account-points {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin-top: 2px;
}

.account-storage,
.account-error {
  margin-top: 3px;
}

.account-details {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 8px;
  margin-top: 8px;
}

.account-info--compact .account-details {
  gap: 5px 8px;
  margin-top: 7px;
}

.account-info--compact .account-heading {
  flex-wrap: nowrap;
}

.account-info--compact .account-name {
  flex: 0 1 auto;
}

.account-points-chip {
  flex: 0 0 auto;
}

.account-detail {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  padding: 2px 8px;
  background: rgba(var(--v-theme-primary), 0.055);
  border: 1px solid rgba(var(--v-theme-primary), 0.12);
  border-radius: 6px;
  font-size: 0.72rem;
  line-height: 1.5;
  white-space: nowrap;
  user-select: none;
  transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
}

.account-detail:hover {
  background: rgba(var(--v-theme-primary), 0.09);
  border-color: rgba(var(--v-theme-primary), 0.28);
  transform: translateY(-1px);
}

.account-detail-label {
  flex: 0 0 auto;
  color: rgba(var(--v-theme-on-surface), 0.58);
  font-size: 0.72rem;
  font-weight: 500;
}

.account-detail-label::after {
  content: "：";
  margin-right: 1px;
  opacity: 0.65;
}

.account-detail-value {
  min-width: 0;
  color: rgba(var(--v-theme-on-surface), 0.92);
  font-size: 0.72rem;
  font-weight: 600;
  letter-spacing: 0.01em;
}

:global(html[data-theme="transparent"]) .account-detail,
:global(html[data-theme="glass"]) .account-detail,
:global(html[data-theme-preference="transparent"]) .account-detail,
:global(html[class*="transparent-glass"]) .account-detail,
:global(html[data-glass-appearance]) .account-detail,
:global(.v-theme--transparent) .account-detail {
  background: rgba(var(--v-theme-surface), 0.6) !important;
  border-color: rgba(255, 255, 255, 0.18) !important;
  backdrop-filter: blur(10px) !important;
}

@media (max-width: 600px) {
  .account-details,
  .account-info--compact .account-details {
    gap: 5px 6px;
  }

  .account-detail {
    font-size: 0.6875rem;
    padding: 1.5px 6px;
  }
}
</style>
