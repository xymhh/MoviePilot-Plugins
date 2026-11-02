import { computed, onMounted, onUnmounted, ref } from "vue";

/**
 * 响应式双端视口监听组合式函数
 * Mobile-First 规范：
 * - isMobile: < 640px (手机屏)
 * - isTablet: 640px ~ 1024px (平板、折叠屏、窄屏)
 * - isDesktop: >= 1024px (桌面宽屏)
 */
export function useViewport() {
  const width = ref(typeof window !== "undefined" ? window.innerWidth : 1200);

  function update() {
    if (typeof window !== "undefined") {
      width.value = window.innerWidth;
    }
  }

  onMounted(() => {
    if (typeof window !== "undefined") {
      window.addEventListener("resize", update, { passive: true });
      update();
    }
  });

  onUnmounted(() => {
    if (typeof window !== "undefined") {
      window.removeEventListener("resize", update);
    }
  });

  const isMobile = computed(() => width.value < 640);
  const isTablet = computed(() => width.value >= 640 && width.value < 1024);
  const isDesktop = computed(() => width.value >= 1024);

  return {
    width,
    isMobile,
    isTablet,
    isDesktop,
  };
}
