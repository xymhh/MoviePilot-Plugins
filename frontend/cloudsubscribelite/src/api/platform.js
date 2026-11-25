/** MoviePilot 平台级端点（非插件自有路径）。 */

import { readValue } from "./client"

export function createPlatformApi(api) {
  const resolveApi = () => readValue(api) || {}
  return {
    form(pluginId) {
      return resolveApi().get(`plugin/form/${pluginId}`)
    },
    history(pluginId, params) {
      return resolveApi().get(`plugin/history/${pluginId}`, params)
    },
    detail(pluginId) {
      return resolveApi().get(`plugin/${pluginId}`)
    },
    market(params) {
      return resolveApi().get("plugin/", params)
    },
  }
}
