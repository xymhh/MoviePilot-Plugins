/** 网盘路径与二维码登录端点。 */

import { buildQuery } from "./client"

export function createDriveApi(client) {
  return {
    directories({ local = false, path = "/", provider = "", refresh = false } = {}) {
      const endpoint = local ? "local/directories" : "cloud/directories"
      return client.get(
        `${endpoint}${buildQuery({ path, provider: local ? undefined : provider, refresh: refresh || undefined })}`,
      )
    },
    createDirectory({ local = false, path = "/", name = "", provider = "" } = {}) {
      const endpoint = local ? "local/directories/create" : "cloud/directories/create"
      const payload = { path, name }
      if (!local) payload.provider = provider
      return client.post(endpoint, payload)
    },
    qrcode({ provider = "", clientType = "" } = {}) {
      return client.get(`qrcode${buildQuery({ provider, client_type: clientType })}`)
    },
    qrcodeCheck(payload) {
      return client.post("qrcode/check", payload)
    },
  }
}
