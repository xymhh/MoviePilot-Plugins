import { createPluginApi } from "../api"

export function connectRuntimeStream(pluginId, handlers = {}) {
  if (typeof window === "undefined" || typeof window.EventSource !== "function") {
    return null
  }

  const pluginApi = createPluginApi(null, () => pluginId)
  const source = new window.EventSource(pluginApi.runtime.streamUrl(), { withCredentials: true })

  source.onopen = () => handlers.onOpen?.()
  source.onerror = (event) => handlers.onError?.(event)
  source.onmessage = (event) => {
    try {
      handlers.onRuntime?.(JSON.parse(event.data))
    } catch (error) {
      handlers.onInvalidData?.(error)
    }
  }
  return source
}
