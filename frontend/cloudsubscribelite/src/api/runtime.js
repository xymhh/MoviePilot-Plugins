/** 运行态与页面数据端点。 */

import { buildQuery } from "./client"

export function createRuntimeApi(client) {
  return {
    runtime() {
      return client.get("runtime")
    },
    overview(params) {
      return client.get(`overview${buildQuery(params)}`)
    },
    pageData(params) {
      return client.get(`page_data${buildQuery(params)}`)
    },
    streamUrl() {
      // SSE 需要完整地址（带 /api/v1 前缀），不走 api 客户端的 get/post
      return `/api/v1/plugin/${encodeURIComponent(client.pluginId())}/runtime/stream`
    },
  }
}
