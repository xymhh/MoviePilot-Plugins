<template>
  <div class="stats-grid" aria-label="转存统计">
    <div
      v-for="(stat, index) in stats"
      :key="stat.title"
      :class="['stat-card', { 'stat-card--desktop-only': ['成功', '失败'].includes(stat.title) }]"
      :style="{
        '--stat-color': `var(--v-theme-${stat.color})`,
        '--stat-delay': `${index * 40}ms`,
      }">
      <!-- 顶部精细流光光效条 -->
      <div class="stat-glow-bar" aria-hidden="true" />

      <div class="stat-main">
        <div class="stat-header">
          <span class="stat-indicator" aria-hidden="true" />
          <span class="stat-label">{{ stat.title }}</span>
        </div>
        <div class="stat-value-wrap">
          <span class="stat-value">{{ stat.value }}</span>
        </div>
      </div>

      <div class="stat-icon-box">
        <v-icon :icon="stat.icon" size="20" />
      </div>
    </div>
  </div>
</template>

<script setup>
defineProps({stats: {type: Array, default: () => []}});
</script>

<style scoped>
.stats-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
}

.stat-card {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  min-width: 0;
  min-height: 72px;
  overflow: hidden;
  padding: 12px 16px;
  color: rgb(var(--v-theme-on-surface));
  background: linear-gradient(135deg, rgba(var(--stat-color), 0.08) 0%, rgba(var(--v-theme-surface), 0.7) 100%);
  border: 1px solid rgba(var(--stat-color), 0.18);
  border-radius: 12px;
  box-shadow: 0 2px 8px -2px rgba(var(--v-theme-on-surface), 0.04);
  backdrop-filter: blur(8px);
  transition: all 0.22s cubic-bezier(0.4, 0, 0.2, 1);
  animation: stat-enter 240ms cubic-bezier(0.16, 1, 0.3, 1) both;
  animation-delay: var(--stat-delay);
}

.stat-card:hover {
  transform: translateY(-2px);
  border-color: rgba(var(--stat-color), 0.36);
  box-shadow: 0 8px 20px -4px rgba(var(--stat-color), 0.2);
}

/* 顶部极细渐变微光，赋予科技质感 */
.stat-glow-bar {
  position: absolute;
  top: 0;
  left: 12px;
  right: 12px;
  height: 2px;
  border-radius: 2px;
  background: linear-gradient(90deg, transparent, rgb(var(--stat-color)), transparent);
  opacity: 0.65;
  transition: opacity 0.22s ease;
}

.stat-card:hover .stat-glow-bar {
  opacity: 1;
}

.stat-main {
  display: flex;
  flex-direction: column;
  justify-content: center;
  gap: 4px;
  min-width: 0;
}

.stat-header {
  display: flex;
  align-items: center;
  gap: 6px;
}

.stat-indicator {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background-color: rgb(var(--stat-color));
  box-shadow: 0 0 6px rgba(var(--stat-color), 0.6);
}

.stat-label {
  color: rgba(var(--v-theme-on-surface), 0.7);
  font-size: 0.78rem;
  font-weight: 500;
  line-height: 1;
  letter-spacing: 0.02em;
}

.stat-value-wrap {
  display: flex;
  align-items: baseline;
}

.stat-value {
  color: rgb(var(--v-theme-on-surface));
  font-size: 1.55rem;
  font-weight: 750;
  line-height: 1.1;
  letter-spacing: -0.03em;
  font-variant-numeric: tabular-nums;
}

/* 现代立体微质感图标容器 */
.stat-icon-box {
  display: grid;
  flex: 0 0 auto;
  width: 38px;
  height: 38px;
  place-items: center;
  color: rgb(var(--stat-color));
  background: linear-gradient(135deg, rgba(var(--stat-color), 0.18), rgba(var(--stat-color), 0.06));
  border: 1px solid rgba(var(--stat-color), 0.16);
  border-radius: 10px;
  box-shadow: 0 2px 6px rgba(var(--stat-color), 0.08);
  transition: transform 0.22s ease;
}

.stat-card:hover .stat-icon-box {
  transform: scale(1.06);
}

@keyframes stat-enter {
  from {
    opacity: 0;
    transform: translateY(6px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

@media (prefers-reduced-motion: reduce) {
  .stat-card {
    animation: none;
    transition: none;
  }

  .stat-card:hover {
    transform: none;
  }
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
    min-height: 64px;
    padding: 10px 12px;
  }

  .stat-icon-box {
    width: 32px;
    height: 32px;
  }

  .stat-value {
    font-size: 1.35rem;
  }
}
</style>
