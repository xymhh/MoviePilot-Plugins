#!/usr/bin/env python3
"""校验前端入口：PluginWorkbench 是唯一实现，AppPage/AppPageHistory 只做薄壳转发。

背景（1.10.3）：插件 UI 按流程复用 MP 界面——
- 插件主页（AppPage）瘦成总览：统计 + 转存动态，不再有页签；
- 转存记录（AppPageHistory）独立成主侧栏导航；
- 频道监控与签到并进渠道页（AppPageChannels）；
- 网盘资源页（AppPageResource）删除，走 MP 探索页；追更列表走 MP 订阅页。
1.10.4：网盘整理不新增导航（1.10.3 的 AppPageOrganize 入口已撤），
整理设置只留在配置页（organize 分组，高级设置里），整理台
（OrganizePanel）随导航一起删除；网盘整理页不得复活。
1.13.2：1.10.3 的渠道页合并已废止——渠道页只留 ChannelManager，
频道监控（TgMonitor）独立成主侧栏导航（AppPageTgMonitor），
签到状态（CheckinDashboard）只留 MP 首页 checkin widget。
1.13.3：转存记录页瘦身为失败记录页——HistoryTable 只列重试索引里的
失败与待修复行（扁平表，不再按媒体分组/展开子记录，不再有洗版/补发
通知/Emby 播放入口）。
v1.23.0（§11 前端收敛第 1 项）：总览模式已删——插件主页（AppPage）
直接进失败记录页（默认只看失败），mode prop 已删，只剩 history
一种模式；总览统计卡片（StatsGrid）与转存动态（MediaHub）不再挂载，
MP 首页 Dashboard widget 是唯一总览；Page 只做转发。

用法：python3 scripts/check_frontend_entry.py [项目根]
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


def main(root: Path) -> int:
    components = root / "frontend" / "cloudsubscribex" / "src" / "components"
    workbench = components / "PluginWorkbench.vue"
    app_page = components / "AppPage.vue"
    app_history = components / "AppPageHistory.vue"
    app_channels = components / "AppPageChannels.vue"
    app_tg_monitor = components / "AppPageTgMonitor.vue"
    page = components / "Page.vue"
    errors: list[str] = []

    for path in (workbench, app_page, app_history, app_channels, app_tg_monitor, page):
        if not path.exists():
            errors.append(f"缺少文件：{path}")
    if errors:
        return report(errors)

    # 1.10.3 已删的网盘资源页不得复活
    resource_page = components / "AppPageResource.vue"
    if resource_page.exists():
        errors.append("AppPageResource.vue 应已删除（网盘资源走 MP 探索页）")
    if (components / "resource").exists():
        errors.append("components/resource/ 应已删除（网盘资源走 MP 探索页）")

    # 1.10.4 已撤的网盘整理页不得复活（整理设置只留在配置页）
    if (components / "AppPageOrganize.vue").exists():
        errors.append("AppPageOrganize.vue 应已删除（1.10.4 撤掉网盘整理导航）")
    if (components / "dashboard" / "OrganizePanel.vue").exists():
        errors.append("OrganizePanel.vue 应已删除（1.10.4 随导航一起撤掉）")

    wb_text = workbench.read_text(encoding="utf-8")
    app_text = app_page.read_text(encoding="utf-8")
    history_text = app_history.read_text(encoding="utf-8")
    channels_text = app_channels.read_text(encoding="utf-8")
    tg_monitor_text = app_tg_monitor.read_text(encoding="utf-8")
    page_text = page.read_text(encoding="utf-8")

    # 薄壳：不含页签，只转发 PluginWorkbench；v1.23.0 起 mode prop 已删，
    # 两壳都不再传 mode（AppPage 即失败记录页）。
    for name, text in (
        ("AppPage", app_text),
        ("AppPageHistory", history_text),
    ):
        if re.search(r"<v-tab\b", text):
            errors.append(f"{name} 不应再有页签（1.10.3 已并掉）")
        if "PluginWorkbench" not in text:
            errors.append(f"{name} 应转发 PluginWorkbench")
        if re.search(r"\bmode=", text):
            errors.append(f"{name} 不应再传 mode（v1.23.0 已删 mode prop）")

    # 1.13.2：渠道页只留渠道管理；频道监控独立成导航；签到只留首页 widget。
    if "ChannelManager" not in channels_text:
        errors.append("AppPageChannels 未挂载 ChannelManager")
    for component in ("TgMonitor", "CheckinDashboard"):
        if component in channels_text:
            errors.append(f"AppPageChannels 不应再挂载 {component}（1.13.2 已拆出）")
    if re.search(r"<v-tab\b", channels_text):
        errors.append("AppPageChannels 不应有页签")
    if "TgMonitor" not in tg_monitor_text:
        errors.append("AppPageTgMonitor 未挂载 TgMonitor")
    for component in ("ChannelManager", "CheckinDashboard"):
        if component in tg_monitor_text:
            errors.append(f"AppPageTgMonitor 不应挂载 {component}")
    if re.search(r"<v-tab\b", tg_monitor_text):
        errors.append("AppPageTgMonitor 不应有页签")

    # 唯一实现：失败记录整表在 PluginWorkbench，不再有页签；v1.23.0 起
    # 总览已删，不再挂载统计卡片与转存动态，不再有 mode/overview 代码。
    if re.search(r"<v-tab\b", wb_text):
        errors.append("PluginWorkbench 不应再有页签（1.10.3 已并掉）")
    if "HistoryTable" not in wb_text:
        errors.append("PluginWorkbench 未挂载 HistoryTable")
    for gone in ("TgMonitor", "OrganizePanel", "StatsGrid", "MediaHub"):
        if gone in wb_text:
            errors.append(f"PluginWorkbench 不应再挂载 {gone}（已移出插件主页）")
    if re.search(r"\bmode\b", wb_text):
        errors.append("PluginWorkbench 不应再有 mode（v1.23.0 已删总览模式）")
    for gone in ("overviewGroups", "overviewFilter", "overviewEmptyTitle", "overviewEmptyHint", "overviewListTitle"):
        if gone in wb_text:
            errors.append(f"PluginWorkbench 不应再有 {gone}（v1.23.0 已删总览）")

    for ref in sorted(set(re.findall(r'@click="(\w*Visible) = true"', wb_text))):
        if f'v-model="{ref}"' not in wb_text:
            errors.append(f"PluginWorkbench 里 {ref} 没有对应的 v-model 弹窗（按钮会点了没反应）")

    vite = (root / "frontend" / "cloudsubscribex" / "vite.config.js").read_text(encoding="utf-8")
    if '"./AppPageHistory": "./src/components/AppPageHistory.vue"' not in vite:
        errors.append("vite 联邦缺少暴露：./AppPageHistory")
    if '"./AppPageTgMonitor": "./src/components/AppPageTgMonitor.vue"' not in vite:
        errors.append("vite 联邦缺少暴露：./AppPageTgMonitor（1.13.2 频道监控导航）")
    if "AppPageOrganize" in vite:
        errors.append("vite 联邦不应再暴露 ./AppPageOrganize（1.10.4 已删除）")
    if "AppPageResource" in vite:
        errors.append("vite 联邦不应再暴露 ./AppPageResource（已删除）")

    status_module = components.parent / "utils" / "status.js"
    if not status_module.exists():
        errors.append(f"缺少状态词表：{status_module}")
    else:
        status_text = status_module.read_text(encoding="utf-8")
        for word in ("排队", "进行中", "待确认", "成功", "失败", "已跳过", "忽略"):
            if word not in status_text:
                errors.append(f"状态词表缺少统一词「{word}」")

    if re.search(r"<v-tab\b", page_text) or len(page_text.splitlines()) > 40:
        errors.append(
            "Page.vue 又长成了完整实现（应只有转发壳，"
            f"当前 {len(page_text.splitlines())} 行）"
        )
    if "export default AppPage" not in page_text:
        errors.append("Page.vue 未转发 AppPage（export default AppPage）")

    return report(errors)


def report(errors: list[str]) -> int:
    if errors:
        print("前端单入口校验失败：")
        for line in errors:
            print(f"  - {line}")
        return 1
    print("前端单入口校验通过：PluginWorkbench 单页（失败记录）+ 侧栏历史/渠道/频道监控页，Page 为转发壳")
    return 0


if __name__ == "__main__":
    base = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()
    sys.exit(main(base))
