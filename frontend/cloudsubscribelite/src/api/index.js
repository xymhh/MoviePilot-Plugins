/**
 * 插件 API 统一入口。
 *
 * 用法：`const pluginApi = usePluginApi(props.api, () => props.pluginId)`
 * 之后通过 `pluginApi.search.tmdb(...)` 之类的域方法访问，禁止在组件里拼端点。
 */

import { createApiClient } from "./client"
import { createCheckinApi } from "./checkin"
import { createConfigApi } from "./config"
import { createDriveApi } from "./drive"
import { createHistoryApi } from "./history"
import { createMediaApi } from "./media"
import { createOfflineApi } from "./offline"
import { createPlatformApi } from "./platform"
import { createResourceApi } from "./resource"
import { createRuntimeApi } from "./runtime"
import { createSearchApi } from "./search"
import { createSyncApi } from "./sync"

export function createPluginApi(api, pluginId) {
  const client = createApiClient(api, pluginId)
  return {
    client,
    config: createConfigApi(client),
    search: createSearchApi(client),
    resource: createResourceApi(client),
    history: createHistoryApi(client),
    offline: createOfflineApi(client),
    checkin: createCheckinApi(client),
    drive: createDriveApi(client),
    media: createMediaApi(client),
    sync: createSyncApi(client),
    runtime: createRuntimeApi(client),
    platform: createPlatformApi(api),
  }
}
