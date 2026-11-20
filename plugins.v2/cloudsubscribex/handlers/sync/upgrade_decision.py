"""洗版判定器：三处旧判定收敛后的唯一决策点（v1.29.0 第 1 步，v1.29.4 统一）。

v1.29.0 收敛时保留了两条分支的不一致——评分制在评分相等时做原始体积
比较（``largest`` 下新文件大 1 字节就换，不设阈值、不看分辨率），而
体积/分辨率制用阈值语义（+10% 且 +200MB，看分辨率防伪 4K）且完全不看
评分。v1.29.4 按用户 2026-11-20 的决定统一成一条直线：

1. 无旧版 → ``("replace", "无现有版本")``。
2. 评分比较：新评分 > 旧评分换（``评分提升 A→B``），新评分 < 旧评分
   不换（``评分降低 A→B``）。体积/分辨率制原先不看评分，统一后评分
   更低但分辨率更高的候选不再换。
3. 评分相等 → 阈值体积比较（旧评分制“大 1 字节就换”作废）：
   - ``largest`` / ``replace`` 等非 ``smallest`` 模式：委托
     ``utils.quality_compare.compare_quality``——分辨率更高且体积不缩水
     太多才换（防伪 4K/片段），同分辨率体积须同时达到 +10% 且 +200MB；
     换时原因前缀 ``同评分按体积择优：``，不换时前缀
     ``同评分现有版本更优：``，再进第 4 步的模板保留。
   - ``smallest`` 模式：阈值的镜像——新文件须同时比旧文件小 10% 且小
     200MB 以上才换（只比体积，不看分辨率，与旧 ``smallest`` 口径一致，
     它本来就不认分辨率）。
   - 体积未知（旧/新任一为 0）时不换：旧评分制同样要求体积已知才比，
     且未知旧体积时按“更大”换会误伤 STRM 等未记录体积的旧版。
4. 模板保留：只有在前面几步都没判出“换”、且策略
   ``prefer_template_named_version`` 开着、且调用方给出了已符合重命名
   模板的旧版本名时，才返回
   ``("skip", "现有版本已符合当前重命名模板，保留 <名>")``；否则按前面
   的原因不换（``同评分现有版本更优：…``、``同评分且体积未知…``）。
   模板符合**绝不否决**第 2/3 步已判定的升级。

v1.29.0 保留的判重角落仍在：判重包装传
``template_fallback_for_any_skip=True`` 时，评分降低且模板符合也改写
原因为模板保留（旧判重外层 ``_keep_template_version`` 的行为）。
``quality_prefer`` 分支与开关（v1.29.4 删除）：体积/分辨率优先分支
不再独立存在，它的阈值语义收进了第 3 步；配置键
``quality_prefer_enabled`` 由 ``core/config_normalize.py`` 的废弃键
清理，旧值静默丢弃、不报警告，前端开关已删。

纯函数约定：输入旧版本信息 + 新候选 + 策略参数，输出
``(action, reason)``，``action`` 只能是 ``"replace"`` / ``"skip"``；
不读插件配置、不读 DB；除 ``compare_quality`` 与 ``format_size`` 的
纯计算外无副作用。模板是否符合由调用方算好以
``template_conformant_name`` 传入（需要命名服务渲染，判定器内不做），
``None`` 表示没有符合模板的旧版本。
"""

from dataclasses import dataclass
from typing import Any, Optional, Sequence, Tuple

from ...drive.common import format_size
from ...utils.quality_compare import compare_quality


@dataclass(frozen=True)
class UpgradeDecisionPolicy:
    """洗版判定策略参数（调用方从插件配置透传，判定器本身不读配置）。

    - ``mode``：评分相等时的体积模式，``"largest"`` 留大 / ``"smallest"``
      留小；其他值按 ``"largest"`` 的阈值语义比（``coexist`` 不进判定器，
      判重门整体放行）。
    - ``min_gain_percent`` / ``min_gain_mb``：阈值体积比较的增益阈值，
      体积须同时达到两者才算更好（``smallest`` 为镜像：须同时小两者）。
    - ``prefer_template_named_version``：无收益时是否保留已符合重命名模板
      的旧版本；关了则按原因直接不换。
    - ``template_fallback_for_any_skip``：评分降低时模板是否也改写原因
      （只保留旧判重外层的 ``_keep_template_version`` 行为：判重里任何
      不换的结果都会先过模板保留；评分制本体在评分降低时直接返回，不
      走模板。默认关，只有判重包装传 True）。
    """

    mode: str = "largest"
    min_gain_percent: int = 10
    min_gain_mb: int = 200
    prefer_template_named_version: bool = True
    template_fallback_for_any_skip: bool = False


def _version_size(entry: Any) -> int:
    """取旧版本条目的体积：dict 取 file_size，(名, 体积) 元组取第 2 项。"""
    try:
        if isinstance(entry, dict):
            return max(0, int(entry.get("file_size") or 0))
        return max(0, int(entry[1] or 0))
    except (TypeError, ValueError, IndexError):
        return 0


def _template_keep(
        policy: UpgradeDecisionPolicy,
        template_conformant_name: Optional[str],
) -> Optional[str]:
    """无收益且模板符合时返回模板保留原因，否则 None。"""
    if (
            policy.prefer_template_named_version
            and template_conformant_name is not None
    ):
        return (
            "现有版本已符合当前重命名模板，保留 "
            f"{template_conformant_name}"
        )
    return None


def decide_upgrade(
        *,
        has_existing: bool,
        old_score: int = 0,
        new_score: int = 0,
        old_size: int = 0,
        new_size: int = 0,
        old_file_name: str = "",
        new_file_name: str = "",
        old_versions: Optional[Sequence[Any]] = None,
        template_conformant_name: Optional[str] = None,
        policy: Optional[UpgradeDecisionPolicy] = None,
) -> Tuple[str, str]:
    """洗版唯一判定入口，返回 ``(action, reason)``。

    :param has_existing: 是否已有旧版本。
    :param old_score/new_score: 旧/新版本的规则评分。
    :param old_size/new_size: 旧/新文件体积（字节）。
    :param old_file_name/new_file_name: 旧/新文件名（分辨率从新文件名解析；
        旧文件名只用于 ``old_versions`` 为空时的回退）。
    :param old_versions: 旧版本列表（评分相等时传给 ``compare_quality``
        的原始版本集合；不传时用 ``(old_file_name, old_size)`` 回退）。
    :param template_conformant_name: 已符合当前重命名模板的旧版本名，
        没有时为 ``None``（空字符串表示符合模板但文件名为空，仍算符合，
        与旧 ``_keep_template_version`` 的行为一致）。
    :param policy: 策略参数，见 :class:`UpgradeDecisionPolicy`。
    """
    policy = policy or UpgradeDecisionPolicy()
    old_score = int(old_score or 0)
    new_score = int(new_score or 0)
    old_size = max(0, int(old_size or 0))
    new_size = max(0, int(new_size or 0))
    old_file_name = str(old_file_name or "").strip()
    new_file_name = str(new_file_name or "").strip()

    # (1) 无旧版 → 换。
    if not has_existing:
        return "replace", "无现有版本"

    # (2) 评分比较（优先；体积/分辨率制原先不看评分，统一后不再换）。
    if new_score > old_score:
        return "replace", f"评分提升 {old_score}→{new_score}"
    if new_score < old_score:
        # 判重外层的旧行为：任何不换的结果都先过模板保留；评分制本体则
        # 在此直接返回。开关只由判重包装开（见策略字段说明）。
        if policy.template_fallback_for_any_skip:
            keep_reason = _template_keep(policy, template_conformant_name)
            if keep_reason is not None:
                return "skip", keep_reason
        return "skip", f"评分降低 {old_score}→{new_score}"

    # (3) 评分相等 → 阈值体积比较（旧“大 1 字节就换”作废）。调用方可能
    # 只传 old_versions 而不单传 old_size（如判重只给版本列表），体积
    # 未知与否以旧版本集合的最大体积为准。
    if not old_size and old_versions:
        old_size = max((_version_size(v) for v in old_versions), default=0)
    mode = str(policy.mode or "").strip().lower()
    if mode == "smallest":
        # 镜像阈值：新文件须同时比旧文件小 min_gain_percent 且小
        # min_gain_mb 才算更好；只比体积，不看分辨率。
        if old_size and new_size:
            try:
                percent = max(0, int(policy.min_gain_percent or 0))
            except (TypeError, ValueError):
                percent = 0
            try:
                min_mb = max(0, int(policy.min_gain_mb or 0))
            except (TypeError, ValueError):
                min_mb = 0
            need_by_percent = int(old_size * (1 - percent / 100.0))
            need_by_mb = old_size - min_mb * 1024 * 1024
            need = min(need_by_percent, need_by_mb)
            if new_size <= need and new_size < old_size:
                gain = (old_size - new_size) / old_size if old_size else 0
                return (
                    "replace",
                    "同评分按体积择优：体积 "
                    f"{format_size(old_size)}→{format_size(new_size)}"
                    f"（-{gain:.0%}）",
                )
            keep_reason = _template_keep(policy, template_conformant_name)
            if keep_reason is not None:
                return "skip", keep_reason
            return (
                "skip",
                "同评分现有版本更优：体积缩减不足"
                f"（{format_size(old_size)}→{format_size(new_size)}，"
                f"需 ≤ {format_size(max(0, need))}）",
            )
        keep_reason = _template_keep(policy, template_conformant_name)
        if keep_reason is not None:
            return "skip", keep_reason
        return "skip", "同评分且体积未知，保留现有版本"

    # largest / replace 等其他模式：委托 compare_quality 的阈值语义
    #（分辨率 > 体积 + 阈值）。
    if old_size and new_size:
        versions = (
            list(old_versions)
            if old_versions is not None
            else [(old_file_name, old_size)]
        )
        action, reason = compare_quality(
            new_file_name,
            new_size,
            versions,
            int(policy.min_gain_percent or 0),
            int(policy.min_gain_mb or 0),
        )
        if action == "upgrade":
            return "replace", f"同评分按体积择优：{reason}"
        keep_reason = _template_keep(policy, template_conformant_name)
        if keep_reason is not None:
            return "skip", keep_reason
        return "skip", f"同评分现有版本更优：{reason}"
    keep_reason = _template_keep(policy, template_conformant_name)
    if keep_reason is not None:
        return "skip", keep_reason
    return "skip", "同评分且体积未知，保留现有版本"
