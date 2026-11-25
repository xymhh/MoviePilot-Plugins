/**
 * 插件 API 客户端：统一拼装 `plugin/<插件ID>/` 前缀与查询串。
 *
 * 组件与组合式函数只调用本目录下按域拆分的方法，不再自己拼端点字符串，
 * 端点变更时只需改这里。
 */

/**
 * 入参可能是：api 实例（可调用对象，如 axios）、取值函数（`() => props.pluginId`）、ref/computed 或原始值。
 * api 实例自带 get/post，必须先识别，否则会被当作取值函数调用而打到空路径。
 */
export function readValue(source) {
  if (source === null || source === undefined) return source
  if (typeof source.get === "function" || typeof source.post === "function") return source
  if (typeof source === "function") return readValue(source())
  if (typeof source === "object" && "value" in source) return readValue(source.value)
  return source
}

export function buildQuery(params) {
  const query = new URLSearchParams()
  Object.entries(params || {}).forEach(([key, value]) => {
    if (value === undefined || value === null || value === "") return
    query.set(key, String(value))
  })
  const text = query.toString()
  return text ? `?${text}` : ""
}

/**
 * 统一解包平台响应：插件端点的业务体可能被外层包一层 data。
 *
 * 幂等：对已解包的对象再调用返回自身，因此调用方无需关心是否已被本层处理。
 */
export function unwrapResponse(response) {
  if (!response || typeof response !== "object") return response || {}
  if (response.success !== undefined) return response
  if (response.data && typeof response.data === "object" && response.data.success !== undefined) {
    return response.data
  }
  return response
}

export function createApiClient(api, pluginId) {
  const resolveApi = () => readValue(api) || {}
  const resolvePluginId = () => String(readValue(pluginId) || "CloudSubscribe")
  const path = (endpoint) => `plugin/${resolvePluginId()}/${String(endpoint || "").replace(/^\//, "")}`

  return {
    pluginId: resolvePluginId,
    path,
    get: (endpoint, config) => Promise.resolve(resolveApi().get(path(endpoint), config)).then(unwrapResponse),
    post: (endpoint, body, config) =>
      Promise.resolve(resolveApi().post(path(endpoint), body, config)).then(unwrapResponse),
  }
}
