/** 配置页相关端点：选项下发、保存、端口探测、账号与 OAuth。 */

import { buildQuery } from "./client"

export function createConfigApi(client) {
  return {
    uiOptions(scope = "base", { refresh = false } = {}) {
      return client.get(`ui_options${buildQuery({ scope, refresh: refresh || undefined })}`)
    },
    save(payload) {
      return client.post("config/save", payload)
    },
    portCheck(port) {
      return client.get(`strm/port_check${buildQuery({ port })}`)
    },
    refreshAccount(payload) {
      return client.post("account/refresh", payload)
    },
    clearCache(payload) {
      return client.post("cache/clear", payload)
    },
    hdhiveOAuthStart(payload) {
      return client.post("hdhive/oauth/start", payload)
    },
    hdhiveOAuthExchange(payload) {
      return client.post("hdhive/oauth/exchange", payload)
    },
  }
}
