/** 网盘资源页端点：推荐榜单、媒体详情与并发资源检索。 */

export function createResourceApi(client) {
  return {
    recommend(params) {
      const query = new URLSearchParams()
      Object.entries(params || {}).forEach(([key, value]) => {
        if (value === undefined || value === null || value === "") return
        query.set(key, String(value))
      })
      const text = query.toString()
      return client.get(`resource/recommend${text ? `?${text}` : ""}`)
    },
    detail(payload, config) {
      return client.post("resource/detail", payload, config)
    },
    searchResources(payload, config) {
      // 并发渠道检索需要可中止，请求配置（AbortController signal）由本层透传
      return client.post("resource/search_resources", payload, config)
    },
  }
}
