export const enabled = (key) => (config) => Boolean(config[key])

export function createCloudDriveItems(options) {
  return Array.isArray(options?.cloudDrives) ? options.cloudDrives : []
}

/** 资源类型选项由后端下发（options.resourceTypes），这里只按当前网盘能力过滤。 */
export function createResourceTypeItems(options, config) {
  const cloudDriveItems = createCloudDriveItems(options)
  const catalog = Array.isArray(options.resourceTypes) ? options.resourceTypes : []
  const activeDrive = cloudDriveItems.find((item) => item.value === (config.cloud_drive || "115"))
  const supportedTypes = new Set(activeDrive?.resource_types || ["115", "ed2k", "magnet"])
  const targetCanUpload = activeDrive?.capabilities?.includes("local_upload")
  if (config.cross_transfer_enabled && targetCanUpload) {
    cloudDriveItems.forEach((drive) => {
      const capabilities = new Set(drive.capabilities || [])
      if (
        drive.value === activeDrive?.value ||
        !capabilities.has("share_transfer") ||
        !capabilities.has("file_download")
      ) {
        return
      }
      ;(drive.resource_types || []).forEach((value) => {
        if (!["ed2k", "magnet"].includes(value)) supportedTypes.add(value)
      })
    })
  }
  return catalog
    .filter((item) => supportedTypes.has(item?.value))
    .map((item) => ({ title: item.name, value: item.value, icon: item.icon }))
}

/** 搜索渠道选项由后端下发（options.sources）。 */
export function createSourceItems(options) {
  return (Array.isArray(options.sources) ? options.sources : []).map((item) => ({
    title: item.name,
    value: item.key,
    icon: item.icon,
  }))
}

const STRM_SAMPLE_VALUES = {
  file_name: "示例剧集.S01E01.mkv",
  file_path: "/电视剧/示例剧集 (2026)/Season 01/示例剧集.S01E01.mkv",
}

/** 仅代理短链模板（含播放标识变量）走独立监听端口。 */
const STRM_PROXY_TEMPLATE_RE = /\{(?:pickcode|file_id)\}/

/** 基础地址是否已显式带端口（兼容 IPv6 字面量写法）。 */
function hasExplicitPort(baseUrl) {
  const match = /^[a-z][a-z0-9+.-]*:\/\/([^/?#]*)/i.exec(String(baseUrl || ""))
  return match ? /:\d+$/.test(match[1]) : false
}

/** 与后端 quote(path, safe) 一致的编码：不编码字母数字与 _.~ 及 safe 字符。 */
function quoteLikeBackend(value, safe = "") {
  const keep = new Set(`${safe}ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-~`)
  let out = ""
  for (const ch of String(value ?? "")) {
    if (keep.has(ch)) {
      out += ch
      continue
    }
    out += Array.from(new TextEncoder().encode(ch))
      .map((byte) => `%${byte.toString(16).toUpperCase().padStart(2, "0")}`)
      .join("")
  }
  return out
}

/** 生效入口地址：代理模板且基础地址未带端口时补上监听端口。 */
export function resolveStrmBaseUrl(config) {
  const base = String(config?.strm_base_url || "").replace(/\/+$/, "")
  const template = String(config?.strm_url_template || "")
  const port = Math.trunc(Number(config?.strm_proxy_port || 0)) || 0
  if (!STRM_PROXY_TEMPLATE_RE.test(template) || port <= 0 || hasExplicitPort(base)) {
    return base
  }
  return `${base}:${port}`
}

/** 当前目标网盘的可用 STRM 模板变量提示（由后端下发）。 */
export function strmVariableHint(options) {
  const variables = Array.isArray(options?.strmVariables) ? options.strmVariables : []
  return variables.length
    ? `可用变量：${variables.map((name) => `{${name}}`).join("、")}`
    : "支持 {base_url}、{file_path} 及各网盘 ID 变量"
}

/** 按当前表单状态渲染 STRM 示例：file_name 原样输出，file_path 按 URL 规则编码。 */
export function renderStrmSample(config) {
  const template = String(config?.strm_url_template || "")
  if (!template) return ""
  const values = {
    base_url: resolveStrmBaseUrl(config),
    file_name: STRM_SAMPLE_VALUES.file_name,
    file_path: quoteLikeBackend(STRM_SAMPLE_VALUES.file_path, "/"),
  }
  return template.replace(/\{(\w+)\}/g, (match, name) => (values[name] ? values[name] : `<${name}>`))
}

/** 代理模式下模板未指向代理入口时，给出建议模板（不改写用户配置）。 */
export function strmProxySuggestion(config, options) {
  if (!config?.strm_proxy_enabled) return ""
  const template = String(config?.strm_url_template || "")
  const identifier = String(options?.strmIdentifierVariable || "")
  const route = String(config?.strm_proxy_route || options?.strmProxyRoute || "d").replace(/^\/+|\/+$/g, "") || "d"
  const shortRoute = `/${route}/{${identifier}}`
  const suggested = identifier ? `{base_url}${shortRoute}?/{file_name}` : ""
  if (!identifier) {
    return "当前网盘不支持短链代理（需要可直取链的播放标识变量）"
  }
  if (options?.strmProxyPortMissing) {
    return "未填写监听端口，代理短链不可用（0=关闭独立监听）"
  }
  if (template.includes(shortRoute)) return ""
  return `当前模板未指向代理，建议填写：${suggested}`
}

/** 代理监听状态文案：未启用/端口 0/被占用/运行中。 */
export function strmProxyStatusLine(config, options) {
  if (!config?.strm_proxy_enabled) return ""
  const port = Number(config?.strm_proxy_port || 0)
  if (port <= 0) return "监听已关闭（端口 0）：短链不可用"
  const server = options?.strmProxyServer || {}
  if (server.running && Number(server.port) === port) {
    return `监听运行中：端口 ${port}`
  }
  return `⚠ 监听未启动：${server.error || `端口 ${port} 被占用`}（配置可保存，代理不生效）`
}
