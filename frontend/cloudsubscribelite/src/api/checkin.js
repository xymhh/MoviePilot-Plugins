/** 签到服务端点。 */

import { buildQuery } from "./client"

export function createCheckinApi(client) {
  return {
    overview(days = 7) {
      return client.get(`checkin/overview${buildQuery({ days })}`)
    },
    history(limit = 60) {
      return client.get(`checkin/history${buildQuery({ limit })}`)
    },
    providerHistory(provider, limit = 60) {
      return client.get(`checkin/${encodeURIComponent(provider)}/history${buildQuery({ limit })}`)
    },
    runProvider(provider, payload) {
      return client.post(`checkin/${encodeURIComponent(provider)}`, payload)
    },
  }
}
