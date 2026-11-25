/** 同步与手动订阅端点。 */

export function createSyncApi(client) {
  return {
    start(payload) {
      return client.post("sync/start", payload)
    },
    stop() {
      return client.post("sync/stop")
    },
    stopTask(payload) {
      return client.post("sync/task/stop", payload)
    },
    manual(payload) {
      return client.post("sync/manual", payload)
    },
    manualResolve(payload) {
      return client.post("sync/manual/resolve", payload)
    },
  }
}
