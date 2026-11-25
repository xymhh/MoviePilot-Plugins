// 搜索渠道测试弹窗用的纯函数：原在 Config.vue，弹窗拆成
// ConfigSourceTestDialog.vue 后由父子两处共用（父组件的 unlockResource /
// previewResource 也要调 canPreviewResource / previewResourceKey）。

export function formatElapsed(value) {
  const seconds = Number(value)
  return Number.isFinite(seconds) ? `${seconds.toFixed(2)} 秒` : "未知"
}

function isPointUnlockResource(item) {
  return Boolean(
    item?.is_point_unlock ||
    item?.need_unlock ||
    item?.unlock_points !== undefined,
  );
}

export function testItemStatus(item) {
  if (item?.need_unlock && Number(item.unlock_points || 0) > 0) {
    return {
      label: `${Number(item.unlock_points || 0)} 积分`,
      color: "warning",
    }
  }
  if (isPointUnlockResource(item) && Number(item?.unlock_points || 0) === 0) {
    return {label: "免费", color: "success"};
  }
  if (item?.need_access) return { label: "待获取", color: "info" }
  return null
}

export function tmdbCandidateSubtitle(item) {
  const values = [item.media_type_name, item.year]
  if (item.original_title && item.original_title !== item.title) {
    values.push(item.original_title)
  }
  if (Number(item.vote_average) > 0) {
    values.push(`评分 ${Number(item.vote_average).toFixed(1)}`)
  }
  return values.filter(Boolean).join(" · ")
}

export function canPreviewResource(item) {
  if (!item) return false;
  const resType = String(item?.resource_type || "").toLowerCase();
  if (resType === "ed2k") return false;
  if (item.can_preview !== undefined) return Boolean(item.can_preview);
  return Boolean(
    item.url ||
    item.resource_ref ||
    item.provider_data?.resource_id ||
    item.pending_resolution,
  )
}

export function previewResourceKey(item) {
  if (!item) return "";
  const source = String(item?.source || "").toLowerCase()
  const providerData = item?.provider_data || {};
  const identifier =
    item.url ||
    item.resource_ref ||
    providerData.resource_id ||
    providerData.seed_id ||
    providerData.path ||
    item.id ||
    "";
  return `${source}:${item.resource_type || ""}:${identifier}`;
}

