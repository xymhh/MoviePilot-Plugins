/** 转存历史端点。 */

export function createHistoryApi(client) {
  return {
    summary() {
      return client.get("history/summary")
    },
    play(itemId) {
      return client.get(`history/play/${encodeURIComponent(itemId)}`)
    },
    retry(payload) {
      return client.post("history/retry", payload)
    },
    upgrade(payload) {
      return client.post("history/upgrade", payload)
    },
    notify(payload) {
      return client.post("history/notify", payload)
    },
    remove(payload) {
      return client.post("history/delete", payload)
    },
    removeBatch(payload) {
      return client.post("history/delete_batch", payload)
    },
    clear(payload) {
      return client.post("history/clear", payload)
    },
    reconcileStatus() {
      return client.post("history/reconcile_status", {})
    },
  }
}
