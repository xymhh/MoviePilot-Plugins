"""通知模板渲染。

把通知文案从硬编码字符串改成用户可在 UI 编辑的 Jinja2 模板：
模板为空或渲染失败时一律回退到调用方给的原始文案，保证通知永远发得出去。
"""

from datetime import datetime
from typing import Any, Dict, List, Mapping

from app.log import logger
from jinja2 import Environment, TemplateError

_ENV = Environment(autoescape=False, keep_trailing_newline=True)

#: 各类通知的默认模板（UI 里可编辑，这里是首次安装与「恢复默认」时的取值）
DEFAULT_TEMPLATES: Dict[str, str] = {
    "notify_template_transfer": (
        "📦 本次{{ action }} {{ count }} 个文件"
        "{% if total_size %} · 合计 {{ total_size }}{% endif %}\n"
        "\n"
        "{% for label in labels %}{{ label }}\n"
        "{% endfor %}\n"
        "🕐 {{ time }}"
    ),
    "notify_template_cross_transfer": (
        "🔀 本次{{ action }} {{ count }} 个文件"
        "{% if total_size %} · 合计 {{ total_size }}{% endif %}\n"
        "\n"
        "{% for label in labels %}{{ label }}\n"
        "{% endfor %}\n"
        "🕐 {{ time }}"
    ),
    "notify_template_upgrade": (
        "⬆️ 本次{{ action }} {{ count }} 个文件"
        "{% if total_size %} · 合计 {{ total_size }}{% endif %}\n"
        "\n"
        "{% for label in labels %}{{ label }}\n"
        "{% endfor %}\n"
        "🕐 {{ time }}"
    ),
    "notify_template_checkin": (
        "{{ '✅' if success else '❌' }} {{ provider }} 签到{{ '成功' if success else '失败' }}\n"
        "\n"
        "{% for line in lines %}· {{ line }}\n"
        "{% endfor %}\n"
        "🕐 {{ time }}"
    ),
    "notify_template_organize": (
        "🗂️ 115 网盘整理完成\n"
        "\n"
        "📁 {{ source }}\n"
        "🧩 分组 {{ groups }}｜可执行 {{ ready }}\n"
        "✅ 成功 {{ done }}｜⏭️ 跳过 {{ skipped }}｜❌ 失败 {{ failed }}"
        "{% if removed_dirs %}\n🧹 清理空目录 {{ removed_dirs }} 个{% endif %}\n"
        "\n"
        "🕐 {{ time }}"
    ),
    "notify_template_delete": (
        "🗑️ 深度联动删除完成\n"
        "\n"
        "{{ message }}\n"
        "\n"
        "🕐 {{ time }}"
    ),
}

#: 历史版本的默认模板：用户没改过就必须随版本升级，否则新排版永远不生效。
#: 每代默认值都留档，升级时逐个比对（1.7.72–1.7.74 一代、1.7.75 一代）。
LEGACY_DEFAULT_TEMPLATES: Dict[str, List[str]] = {
    "notify_template_transfer": [
        # 1.7.72–1.7.74
        (
            "📦 本次{{ action }} {{ count }} 个文件\n"
            "\n"
            "{% for label in labels %}▫️ {{ label }}\n"
            "{% endfor %}"
            "\n"
            "🕐 {{ time }}"
        ),
        # 1.7.75
        (
            "📦 本次{{ action }} {{ count }} 个文件"
            "{% if total_size %} · 合计 {{ total_size }}{% endif %}\n"
            "\n"
            "{% for label in labels %}{{ label }}\n"
            "{% endfor %}"
            "🕐 {{ time }}"
        ),
    ],
    "notify_template_cross_transfer": [
        (
            "📦 本次{{ action }} {{ count }} 个文件\n"
            "\n"
            "{% for label in labels %}🔀 {{ label }}\n"
            "{% endfor %}"
            "\n"
            "🕐 {{ time }}"
        ),
        (
            "🔀 本次{{ action }} {{ count }} 个文件"
            "{% if total_size %} · 合计 {{ total_size }}{% endif %}\n"
            "\n"
            "{% for label in labels %}{{ label }}\n"
            "{% endfor %}"
            "🕐 {{ time }}"
        ),
    ],
    "notify_template_upgrade": [
        (
            "🔁 本次{{ action }} {{ count }} 个文件\n"
            "\n"
            "{% for label in labels %}⭐ {{ label }}\n"
            "{% endfor %}"
            "\n"
            "🕐 {{ time }}"
        ),
        (
            "⬆️ 本次{{ action }} {{ count }} 个文件"
            "{% if total_size %} · 合计 {{ total_size }}{% endif %}\n"
            "\n"
            "{% for label in labels %}{{ label }}\n"
            "{% endfor %}"
            "🕐 {{ time }}"
        ),
    ],
    "notify_template_checkin": [
        (
            "{{ '✅' if success else '❌' }} {{ provider }} 签到{{ '成功' if success else '失败' }}\n"
            "\n"
            "{% for line in lines %}▫️ {{ line }}\n"
            "{% endfor %}"
            "\n"
            "🕐 {{ time }}"
        ),
        (
            "{{ '✅' if success else '❌' }} {{ provider }} 签到{{ '成功' if success else '失败' }}\n"
            "\n"
            "{% for line in lines %}· {{ line }}\n"
            "{% endfor %}"
            "🕐 {{ time }}"
        ),
    ],
    "notify_template_organize": [
        (
            "🗂 115 网盘整理完成\n"
            "\n"
            "📁 源目录：{{ source }}\n"
            "📊 分组 {{ groups }} · 可执行 {{ ready }}\n"
            "✅ 成功 {{ done }} · ⏭ 跳过 {{ skipped }} · ❌ 失败 {{ failed }}"
            "{% if removed_dirs %}\n🧹 清理空目录 {{ removed_dirs }} 个{% endif %}\n"
            "\n"
            "🕐 {{ time }}"
        ),
        (
            "🗂️ 115 网盘整理完成\n"
            "\n"
            "📁 {{ source }}\n"
            "🧩 分组 {{ groups }}｜可执行 {{ ready }}\n"
            "✅ 成功 {{ done }}｜⏭️ 跳过 {{ skipped }}｜❌ 失败 {{ failed }}"
            "{% if removed_dirs %}\n🧹 清理空目录 {{ removed_dirs }} 个{% endif %}\n"
            "\n"
            "🕐 {{ time }}"
        ),
    ],
    "notify_template_delete": [
        (
            "🗑 深度联动删除完成\n"
            "\n"
            "{{ message }}\n"
            "\n"
            "🕐 {{ time }}"
        ),
        (
            "🗑️ 深度联动删除完成\n"
            "\n"
            "{{ message }}\n"
            "\n"
            "🕐 {{ time }}"
        ),
    ],
}

#: 模板变量说明，前端 hint 与文档共用
TEMPLATE_VARIABLES: Dict[str, str] = {
    "notify_template_transfer": (
        "action 动作名 / count 文件数 / total_size 合计大小 / labels 每行媒体描述"
        "（含图标、类型、大小、网盘来源）/ items 明细列表 / time 时间"
    ),
    "notify_template_cross_transfer": (
        "action 动作名 / count 文件数 / total_size 合计大小 / labels 每行媒体描述"
        "（含图标、类型、大小、来源→目标）/ items 明细列表 / time 时间"
    ),
    "notify_template_upgrade": (
        "action 动作名 / count 文件数 / total_size 合计大小 / labels 每行媒体描述"
        "（含图标、类型、大小、网盘来源）/ items 明细列表 / time 时间"
    ),
    "notify_template_checkin": "provider 网盘名 / success 是否成功 / lines 结果行 / time 时间",
    "notify_template_organize": "source 源目录 / groups 分组数 / ready 可执行数 / done 成功数 / skipped 跳过数 / failed 失败数 / removed_dirs 清理空目录数 / time 时间",
    "notify_template_delete": "message 删除结果描述 / time 时间",
}


def now_text() -> str:
    """统一的通知时间格式，模板里直接用 {{ time }}。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def render_template(template: Any, fallback: str, **context: Any) -> str:
    """渲染模板；模板为空或渲染失败时回退到 fallback 文案。"""
    source = str(template or "").strip()
    if not source:
        return fallback
    context.setdefault("time", now_text())
    try:
        return _ENV.from_string(source).render(**context).strip()
    except TemplateError as error:
        logger.warning(f"通知模板渲染失败，已回退默认文案：{error}")
        return fallback
    except Exception as error:  # noqa: BLE001 - 任何异常都不能让通知发不出去
        logger.warning(f"通知模板渲染异常，已回退默认文案：{error}")
        return fallback


def resolve_template(
        templates: Mapping[str, Any], key: str, fallback: str = ""
) -> str:
    """取模板：配置里没写就用内置默认模板。"""
    value = str((templates or {}).get(key) or "").strip()
    return value or DEFAULT_TEMPLATES.get(key) or fallback


def _normalize(template: Any) -> str:
    return "\n".join(
        line.rstrip() for line in str(template or "").strip().splitlines()
    )


def upgrade_legacy_templates(config: Mapping[str, Any]) -> Dict[str, str]:
    """把仍是旧默认值的模板换成新默认值，返回被替换的键值。

    用户自己改过的模板（与旧默认值不同）一律不动；这样升级能拿到新排版，
    又不会覆盖定制文案。键缺失的情况由 ``_ensure_notify_template_defaults`` 处理。
    """
    updated: Dict[str, str] = {}
    for key, default in DEFAULT_TEMPLATES.items():
        stored = str((config or {}).get(key) or "").strip()
        if not stored:
            continue
        normalized_default = _normalize(default)
        for legacy in LEGACY_DEFAULT_TEMPLATES.get(key, []):
            # 两代默认值相同（如 organize/delete 本版未改）时不必回写
            if _normalize(legacy) == normalized_default:
                continue
            if _normalize(stored) == _normalize(legacy):
                updated[key] = default
                break
    return updated
