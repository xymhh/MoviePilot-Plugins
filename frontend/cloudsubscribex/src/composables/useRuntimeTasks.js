import {computed, onMounted, onUnmounted, ref} from "vue";
import {connectRuntimeStream} from "../utils/runtimeStream.js";
import {isTaskActive} from "../utils/taskUtils.js";

/**
 * 轻量运行态订阅：只关心「有哪些任务在跑」。
 *
 * 主页面用 useRuntimeData 管全局状态机，资源页只需要一份任务列表；
 * 两者共用 /runtime 快照 + SSE，互不影响。
 */
export function useRuntimeTasks(api, pluginId = "CloudSubscribeX") {
  const tasks = ref([]);
  const status = ref("idle");
  const ready = ref(false);

  let stream = null;
  let fallbackTimer = null;
  let revision = -1;
  let failures = 0;
  let disposed = false;

  const activeTasks = computed(() =>
    (tasks.value || [])
      .filter((task) => isTaskActive(task))
      .sort((left, right) => {
        const leftQueued = String(left?.status || "") === "queued" ? 1 : 0;
        const rightQueued = String(right?.status || "") === "queued" ? 1 : 0;
        if (leftQueued !== rightQueued) return leftQueued - rightQueued;
        return Number(left?.queued_at || 0) - Number(right?.queued_at || 0);
      }),
  );

  const running = computed(
    () => activeTasks.value.length > 0 || ["starting", "running", "stopping"].includes(status.value),
  );

  function applySnapshot(snapshot) {
    if (!snapshot || typeof snapshot !== "object") return;
    const nextRevision = Number(snapshot.revision);
    if (Number.isFinite(nextRevision)) {
      if (nextRevision < revision) return;
      revision = nextRevision;
    }
    if (typeof snapshot.status === "string") status.value = snapshot.status;
    if (Array.isArray(snapshot.tasks)) tasks.value = snapshot.tasks;
    ready.value = true;
  }

  function isPageVisible() {
    return typeof document === "undefined" || document.visibilityState !== "hidden";
  }

  function clearFallback() {
    if (fallbackTimer === null) return;
    window.clearTimeout(fallbackTimer);
    fallbackTimer = null;
  }

  function scheduleFallback(delay = 10000) {
    clearFallback();
    if (disposed || !isPageVisible()) return;
    fallbackTimer = window.setTimeout(async () => {
      fallbackTimer = null;
      await load();
      if (!stream) scheduleFallback(activeTasks.value.length ? 10000 : 30000);
    }, Math.max(2000, Number(delay) || 0));
  }

  function closeStream() {
    if (!stream) return;
    try {
      stream.close();
    } catch (_) {
      // EventSource 已关闭时忽略
    }
    stream = null;
  }

  function openStream() {
    if (disposed || stream || !isPageVisible()) return;
    stream = connectRuntimeStream(pluginId, {
      onOpen() {
        failures = 0;
        clearFallback();
      },
      onRuntime: applySnapshot,
      onError() {
        failures += 1;
        if (failures < 2) return;
        closeStream();
        scheduleFallback(2000);
      },
    });
    if (!stream) scheduleFallback(2000);
  }

  async function load() {
    try {
      const result = await api.get(`plugin/${pluginId}/runtime`);
      if (result?.success) applySnapshot(result.data || {});
    } catch (_) {
      // 运行态不可用不影响页面其它功能
    }
  }

  async function stopTask(taskId) {
    try {
      const task = (tasks.value || []).find((item) => item.id === taskId);
      if (task) {
        task.status = "stopping";
        task.phase = "等待安全停止";
      }
      const result = await api.post(`plugin/${pluginId}/sync/task/stop`, {
        task_id: taskId,
      });
      if (!result?.success) throw new Error(result?.message || "停止任务失败");
      await load();
      return true;
    } catch (error) {
      await load();
      return false;
    }
  }

  function handleVisibilityChange() {
    if (!isPageVisible()) {
      clearFallback();
      closeStream();
      return;
    }
    void load().finally(openStream);
  }

  onMounted(() => {
    document.addEventListener("visibilitychange", handleVisibilityChange);
    void load().finally(() => {
      openStream();
      scheduleFallback(activeTasks.value.length ? 10000 : 60000);
    });
  });

  onUnmounted(() => {
    disposed = true;
    document.removeEventListener("visibilitychange", handleVisibilityChange);
    clearFallback();
    closeStream();
  });

  return {tasks, activeTasks, running, status, ready, load, stopTask};
}
