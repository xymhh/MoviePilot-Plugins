/** 搜索渠道相关端点：候选查询、渠道测试、预览、解锁、索引重建与代理测试。 */

export function createSearchApi(client) {
  return {
    tmdb(payload) {
      return client.post("search/tmdb", payload)
    },
    test(payload) {
      return client.post("search/test", payload)
    },
    preview(payload) {
      return client.post("search/preview", payload)
    },
    unlock(payload) {
      return client.post("search/unlock", payload)
    },
    rebuild(payload) {
      return client.post("search/rebuild", payload)
    },
    proxyTest(payload) {
      return client.post("search/proxy/test", payload)
    },
    autoSubscribeTest(payload) {
      return client.post("auto_subscribe/test", payload)
    },
    autoSubscribeProxyTest(payload) {
      return client.post("auto_subscribe/proxy/test", payload)
    },
  }
}
