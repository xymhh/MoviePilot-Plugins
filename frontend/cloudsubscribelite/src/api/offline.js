/** 离线任务端点。 */

export function createOfflineApi(client) {
  return {
    list() {
      return client.get("offline")
    },
    refresh() {
      return client.post("offline/refresh")
    },
    retry(payload) {
      return client.post("offline/retry", payload)
    },
    remove(payload) {
      return client.post("offline/delete", payload)
    },
    removeBatch(payload) {
      return client.post("offline/delete_batch", payload)
    },
  }
}
