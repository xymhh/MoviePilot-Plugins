<template>
  <div class="stats-grid" aria-label="转存统计">
    <div
      v-for="stat in stats"
      :key="stat.title"
      :class="[
        'stat-card',
        `tone-${stat.color}`,
        {
          'stat-card--desktop-only': ['成功', '失败'].includes(stat.title),
          'stat-card--clickable': stat.filter !== undefined,
          'is-zero': Number(stat.value) === 0,
        },
      ]"
      :role="stat.filter !== undefined ? 'button' : undefined"
      :tabindex="stat.filter !== undefined ? 0 : undefined"
      :title="stat.filter !== undefined ? `查看${stat.title}对应的历史记录` : ''"
      @click="stat.filter !== undefined && emit('select', stat)"
      @keydown.enter="stat.filter !== undefined && emit('select', stat)"
      @keydown.space.prevent="stat.filter !== undefined && emit('select', stat)">
      <span class="stat-label">{{ stat.title }}</span>
      <span class="stat-value">{{ stat.value }}</span>
    </div>
  </div>
</template>

<script setup>
defineProps({ stats: { type: Array, default: () => [] } })
const emit = defineEmits(["select"])
</script>

<style scoped>
/* 内容优先：去掉渐变底、流光条、彩色图标盒，只留数字 */
.stats-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
}

.stat-card {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
  padding: 12px 14px 12px 16px;
  background: rgb(var(--v-theme-surface));
  border: 1px solid rgba(var(--v-theme-on-surface), 0.08);
  border-radius: 10px;
  transition: border-color 0.18s ease;
}

.stat-card::before {
  content: "";
  position: absolute;
  top: 12px;
  bottom: 12px;
  left: 0;
  width: 2px;
  border-radius: 0 2px 2px 0;
  background: rgba(var(--v-theme-on-surface), 0.12);
}

.tone-success::before {
  background: rgb(var(--v-theme-success));
}

.tone-error::before {
  background: rgb(var(--v-theme-error));
}

.stat-card--clickable {
  cursor: pointer;
}

.stat-card--clickable:hover {
  border-color: rgba(var(--v-theme-on-surface), 0.2);
}

.stat-card--clickable:focus-visible {
  outline: 2px solid rgb(var(--v-theme-primary));
  outline-offset: 2px;
}

.stat-label {
  color: rgba(var(--v-theme-on-surface), 0.6);
  font-size: 0.75rem;
  font-weight: 500;
  line-height: 1.2;
  letter-spacing: 0.02em;
}

.stat-value {
  color: rgb(var(--v-theme-on-surface));
  font-size: 1.75rem;
  font-weight: 650;
  line-height: 1.15;
  letter-spacing: -0.02em;
  font-variant-numeric: tabular-nums;
}

/* 只有成功/失败的数字着色，总量类保持中性 */
.tone-success .stat-value {
  color: rgb(var(--v-theme-success));
}

.tone-error .stat-value {
  color: rgb(var(--v-theme-error));
}

/* 失败为 0 时不抢注意力 */
.tone-error .stat-value:empty,
.stat-card.tone-error.is-zero .stat-value {
  color: rgba(var(--v-theme-on-surface), 0.5);
}

@media (max-width: 600px) {
  .stat-card--desktop-only {
    display: none;
  }

  .stats-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 8px;
  }

  .stat-card {
    padding: 10px 12px 10px 14px;
  }

  .stat-value {
    font-size: 1.4rem;
  }
}
</style>
