/** 媒体库端点：媒体服务器列表与内容检索。 */

import { buildQuery } from "./client"

export function createMediaApi(client) {
  return {
    servers() {
      return client.get("media/servers")
    },
    content(params) {
      return client.get(`media/content${buildQuery(params)}`)
    },
  }
}
