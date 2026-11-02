#!/usr/bin/env python3
"""校验前端单入口：AppPage 是唯一实现，Page 只做转发。

背景（1.7.90）：MoviePilot `nav_key=main` 先加载 `./AppPage`，`./Page` 仅作
fallback。此前两个组件各维护一份模板与逻辑，2026-10-27 出现「频道监控」
页签只在 Page.vue 里、侧边栏进入看不到的分叉事故。

用法：python3 scripts/check_frontend_entry.py [项目根]
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

EXPECTED_TABS = ["hub", "tasks", "history", "tg", "organize"]


def main(root: Path) -> int:
    components = root / "frontend" / "cloudsubscribex" / "src" / "components"
    app_page = components / "AppPage.vue"
    page = components / "Page.vue"
    errors: list[str] = []

    for path in (app_page, page):
        if not path.exists():
            errors.append(f"缺少文件：{path}")
    if errors:
        return report(errors)

    app_text = app_page.read_text(encoding="utf-8")
    page_text = page.read_text(encoding="utf-8")

    tabs = re.findall(r'<v-tab[^>]*value="([^"]+)"', app_text)
    if tabs != EXPECTED_TABS:
        errors.append(
            f"AppPage 页签应为 {EXPECTED_TABS}，实际 {tabs}"
        )

    if "TgMonitor" not in app_text:
        errors.append("AppPage 未挂载 TgMonitor（频道监控页签会空白）")

    # 1.9.32：资源账本与历史页合二为一为「转存台账」，在台账内支持全局与单剧排障明细
    if "CandidateLedgerPanel" not in app_text and "CandidateLedgerPanel" not in (components / "dashboard" / "HistoryTable.vue").read_text(encoding="utf-8"):
        errors.append("AppPage / HistoryTable 未挂载 CandidateLedgerPanel（未转存账本排障会缺失）")

    for ref in sorted(set(re.findall(r'@click="(\w*Visible) = true"', app_text))):
        if f'v-model="{ref}"' not in app_text:
            errors.append(f"AppPage 里 {ref} 没有对应的 v-model 弹窗（按钮会点了没反应）")

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
    print(f"前端单入口校验通过：AppPage {len(EXPECTED_TABS)} 个页签 + 历史双子视图，Page 为转发壳")
    return 0


if __name__ == "__main__":
    base = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()
    sys.exit(main(base))
