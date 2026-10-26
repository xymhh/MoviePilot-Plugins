"""通知模板渲染。

把通知文案从硬编码字符串改成用户可在 UI 编辑的 Jinja2 模板：
模板为空或渲染失败时一律回退到调用方给的原始文案，保证通知永远发得出去。
"""

from datetime import datetime
from typing import Any, Dict, Mapping

from app.log import logger
from jinja2 import Environment, TemplateError

_ENV = Environment(autoescape=False, keep_trailing_newline=True)

#: 各类通知的默认模板（UI 里可编辑，这里是首次安装与「恢复默认」时的取值）
DEFAULT_TEMPLATES: Dict[str, str] = {
    "notify_template_transfer": (
        "📦 本次{{ action }} {{ count }} 个文件\n"
        "\n"
        "{% for label in labels %}▫️ {{ label }}\n"
        "{% endfor %}"
        "\n"
        "🕐 {{ time }}"
    ),
    "notify_template_cross_transfer": (
        "📦 本次{{ action }} {{ count }} 个文件\n"
        "\n"
        "{% for label in labels %}🔀 {{ label }}\n"
        "{% endfor %}"
        "\n"
        "🕐 {{ time }}"
    ),
    "notify_template_upgrade": (
        "🔁 本次{{ action }} {{ count }} 个文件\n"
        "\n"
        "{% for label in labels %}⭐ {{ label }}\n"
        "{% endfor %}"
        "\n"
        "🕐 {{ time }}"
    ),
    "notify_template_checkin": (
        "{{ '✅' if success else '❌' }} {{ provider }} 签到{{ '成功' if success else '失败' }}\n"
        "\n"
        "{% for line in lines %}▫️ {{ line }}\n"
        "{% endfor %}"
        "\n"
        "🕐 {{ time }}"
    ),
    "notify_template_organize": (
        "🗂 115 网盘整理完成\n"
        "\n"
        "📁 源目录：{{ source }}\n"
        "📊 分组 {{ groups }} · 可执行 {{ ready }}\n"
        "✅ 成功 {{ done }} · ⏭ 跳过 {{ skipped }} · ❌ 失败 {{ failed }}"
        "{% if removed_dirs %}\n🧹 清理空目录 {{ removed_dirs }} 个{% endif %}\n"
        "\n"
        "🕐 {{ time }}"
    ),
    "notify_template_delete": (
        "🗑 深度联动删除完成\n"
        "\n"
        "{{ message }}\n"
        "\n"
        "🕐 {{ time }}"
    ),
}

#: 模板变量说明，前端 hint 与文档共用
TEMPLATE_VARIABLES: Dict[str, str] = {
    "notify_template_transfer": "action 动作名 / count 文件数 / labels 每行媒体描述 / items 明细列表 / time 时间",
    "notify_template_cross_transfer": "action 动作名 / count 文件数 / labels 每行媒体描述 / items 明细列表 / time 时间",
    "notify_template_upgrade": "action 动作名 / count 文件数 / labels 每行媒体描述 / items 明细列表 / time 时间",
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
