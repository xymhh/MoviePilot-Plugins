import {computed, reactive, ref} from "vue";
import {applyDisplayCatalog} from "./resourceUtils";

// v1.13.3：转存记录页瘦身为失败记录页——后端只返回重试索引里的失败与
// 跨盘成功待修复行（failures 扁平列表），不再有按媒体分组的 history_groups、
// 订阅进度 subscribes_progress 与 Emby 播放项 emby_play_items。
export function useHistoryPageData(api, notify, pluginId = "CloudSubscribeX") {
  const failures = ref([]);
  const loading = ref(false);
  const historyPage = reactive({
    page: 1,
    pageSize: 10,
    total: 0,
    totalPages: 1,
    filterOptions: {
      resourceTypes: [],
      sources: [],
    },
    enableCloudUpgrade: false,
  })
  const historyStats = reactive({
    failed: 0,
    repair: 0,
    processing: 0,
    total: 0,
  })
  const historyQuery = reactive({
    page: 1,
    pageSize: 10,
    keyword: "",
    // 行状态过滤："" 全部 / "失败" / "成功"（待修复）/ "处理中"
    status: "",
  })
  const stats = computed(() => [
    {
      title: "失败",
      value: historyStats.failed,
      color: "error",
      icon: "mdi-alert-circle-outline",
      filter: "失败",
    },
    {
      title: "待修复",
      value: historyStats.repair,
      color: "warning",
      icon: "mdi-wrench-outline",
      filter: "成功",
    },
    {
      title: "处理中",
      value: historyStats.processing,
      color: "info",
      icon: "mdi-progress-clock",
      filter: "处理中",
    },
    {
      title: "全部记录",
      value: historyStats.total,
      color: "primary",
      icon: "mdi-clipboard-text-clock-outline",
      filter: "",
    },
  ])

  let pageRequestSequence = 0;
  let summaryRequestSequence = 0;

  function pageQueryString() {
    const query = new URLSearchParams({
      page: String(historyQuery.page),
      page_size: String(historyQuery.pageSize),
    })
    if (historyQuery.keyword) query.set("keyword", historyQuery.keyword);
    if (historyQuery.status) query.set("status", historyQuery.status);
    return query.toString();
  }

  async function loadPage(showLoading = true) {
    const requestId = ++pageRequestSequence;
    if (showLoading) loading.value = true;
    try {
      const result = await api.get(`plugin/${pluginId}/page_data?${pageQueryString()}`);
      if (!result?.success) throw new Error(result?.message || "加载失败");
      if (requestId !== pageRequestSequence) return false;
      const data = result.data || {};
      applyDisplayCatalog(data);
      const pageData = data.history_page || {};
      failures.value = Array.isArray(data.failures) ? data.failures : [];
      Object.assign(historyPage, {
        page: Math.max(1, Number(pageData.page || 1)),
        pageSize: Math.max(1, Number(pageData.page_size || 10)),
        total: Math.max(0, Number(pageData.total || 0)),
        totalPages: Math.max(1, Number(pageData.total_pages || 1)),
        filterOptions: {
          resourceTypes: Array.isArray(pageData.filter_options?.resource_types)
            ? pageData.filter_options.resource_types
            : [],
          sources: Array.isArray(pageData.filter_options?.sources) ? pageData.filter_options.sources : [],
        },
        enableCloudUpgrade: Boolean(pageData.enable_cloud_upgrade),
      })
      historyQuery.page = historyPage.page;
      historyQuery.pageSize = historyPage.pageSize;
      return true;
    } catch (error) {
      if (requestId === pageRequestSequence && showLoading) {
        notify(error.message || "加载失败", "error");
      }
      return false;
    } finally {
      if (requestId === pageRequestSequence) loading.value = false;
    }
  }

  async function loadSummary(showError = true) {
    const requestId = ++summaryRequestSequence;
    try {
      const result = await api.get(`plugin/${pluginId}/history/summary`);
      if (!result?.success) {
        throw new Error(result?.message || "加载失败记录摘要失败");
      }
      if (requestId !== summaryRequestSequence) return false;
      const data = result.data || {};
      Object.assign(historyStats, {
        failed: Math.max(0, Number(data.failed || 0)),
        repair: Math.max(0, Number(data.repair || 0)),
        processing: Math.max(0, Number(data.processing || 0)),
        total: Math.max(0, Number(data.total || 0)),
      })
      return true;
    } catch (error) {
      if (requestId === summaryRequestSequence && showError) {
        notify(error.message || "加载失败记录摘要失败", "error");
      }
      return false;
    }
  }

  async function updateHistoryQuery(nextQuery = {}) {
    const normalized = {
      page: Math.max(1, Number(nextQuery.page ?? historyQuery.page) || 1),
      pageSize: Math.min(50, Math.max(1, Number(nextQuery.pageSize ?? historyQuery.pageSize) || 10)),
      keyword: String(nextQuery.keyword ?? historyQuery.keyword).trim(),
      status: String(nextQuery.status ?? historyQuery.status).trim(),
    }
    const unchanged = Object.entries(normalized).every(([key, value]) => value === historyQuery[key])
    if (unchanged) return false;
    Object.assign(historyQuery, normalized);
    return loadPage();
  }

  async function clearHistory(force = false, clearPointsHistory = false) {
    const result = await api.post(`plugin/${pluginId}/history/clear`, {
      force: Boolean(force),
      clear_points_history: Boolean(clearPointsHistory),
    })
    if (!result?.success) throw new Error(result?.message || "清空失败");
    await Promise.all([loadPage(false), loadSummary(false)]);
    return result.message || "失败记录已清空";
  }

  // v1.13.3：删除只删重试索引行，不删网盘文件与 STRM；delete_linked_files
  // 参数已废弃，后端不再接受，前端不再发送。
  async function deleteHistory(record) {
    const result = await api.post(`plugin/${pluginId}/history/delete`, {
      time: record.time,
      share_url: record.share_url,
      file_name: record.file_name,
      tmdb_id: record.tmdb_id,
      season: record.season,
      episode: record.episode,
    })
    if (!result?.success) throw new Error(result?.message || "删除失败");
    await Promise.all([loadPage(false), loadSummary(false)]);
    return result.message || "失败记录已删除";
  }

  async function deleteHistoryBatch(records) {
    const identities = records.map((record) => ({
      record_id: record.record_id,
      time: record.time,
      share_url: record.share_url,
      file_name: record.file_name,
      tmdb_id: record.tmdb_id,
      season: record.season,
      episode: record.episode,
    }))
    const result = await api.post(`plugin/${pluginId}/history/delete_batch`, {
      records: identities,
    })
    if (!result?.success) throw new Error(result?.message || "批量删除失败");
    await Promise.all([loadPage(false), loadSummary(false)]);
    return result.message || "所选失败记录已删除";
  }

  return {
    failures,
    historyPage,
    historyStats,
    loading,
    stats,
    loadPage,
    loadSummary,
    updateHistoryQuery,
    clearHistory,
    deleteHistory,
    deleteHistoryBatch,
  }
}
