"""洗版判定器：三处旧判定收敛后的唯一决策点（v1.29.0 第 1 步）。

本模块只做收敛、不改行为。旧判定有三处——评分制
（``UpgradeRuleScoringService._should_upgrade_candidate``）、体积/分辨率制
（``utils.quality_compare.compare_quality``）、季集判重里的组合判定
（``EpisodeDedupService._episode_dedup_decision``）——三者现全部委托给
:func:`decide_upgrade`，不得再有第二份判定逻辑。

固定判定顺序（与旧行为逐项一致）：

1. 无旧版 → ``("replace", "无现有版本")``。
2. 体积/分辨率优先（仅当策略 ``quality_prefer`` 为真，即旧判重里
   ``_quality_prefer_enabled`` 开着的分支）：直接走 ``compare_quality``
   的分辨率/体积比较，**不看评分**——分辨率更高且体积不缩水太多才换
   （防伪 4K/片段），同分辨率体积须达到增益阈值；换时原因前缀
   ``按体积择优：``，不换时前缀 ``现有版本更优：``，再进第 4 步的模板保留。
3. 评分比较（``quality_prefer`` 为假时）：新评分 > 旧评分换
   （``评分提升 A→B``），新评分 < 旧评分不换（``评分降低 A→B``）。
4. 评分相等时按模式比体积：``largest`` 模式新文件更大就换
   （``同评分保留更大文件``），``smallest`` 模式新文件更小就换
   （``同评分保留更小文件``）；其他模式不比体积。
5. 模板保留：只有在前面几步都没判出“换”、且策略
   ``prefer_template_named_version`` 开着、且调用方给出了已符合重命名
   模板的旧版本名时，才返回
   ``("skip", "现有版本已符合当前重命名模板，保留 <名>")``；否则按前面
   的原因不换（``同评分且候选文件未更大/未更小``、``同评分不执行替换``、
   或第 2 步的 ``现有版本更优：…``）。模板符合**绝不否决**第 2/3/4 步
   已判定的升级。

为什么第 2 步与第 3/4 步是两条分支、而不是一条直线（旧三处判定之间
的不一致，见下）：

- 旧评分制在评分相等时做的是**原始体积比较**（``largest`` 下新文件大
  1 字节就换，原因 ``同评分保留更大文件``；不设增益阈值、不看分辨率），
  而旧体积/分辨率制用的是阈值语义（体积须同时达到 +10% 且 +200MB，
  且看分辨率防伪 4K）。两者对同一输入会给出不同动作与不同原因。
- 旧体积/分辨率制完全不看评分，旧评分制则以评分为先。
- 把两条分支压成一条直线会改变其中一边的行为（例如 ``largest`` 模式
  下 +5% 的候选、或评分更低但分辨率更高的候选），那是行为变更、须另
  行决策，不属于本次“零行为变化”的收敛。第 1 步只保证：同一个调用点
  在旧代码与新判定器下得到完全相同的 ``(action, reason)``；两条分支
  的统一留给后续版本决策。

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
      留小，其他值不比体积。
    - ``min_gain_percent`` / ``min_gain_mb``：体积/分辨率制（``compare_quality``）
      的增益阈值，体积须同时达到两者才算更好。
    - ``prefer_template_named_version``：无收益时是否保留已符合重命名模板
      的旧版本；关了则按原因直接不换。
    - ``quality_prefer``：是否走体积/分辨率优先分支（旧判重里
      ``_quality_prefer_enabled`` 开着的分支）；关了走评分分支。
    - ``template_fallback_for_any_skip``：评分降低时模板是否也改写原因
      （只保留旧判重外层的 ``_keep_template_version`` 行为：判重里任何
      不换的结果都会先过模板保留；评分制本体在评分降低时直接返回，不
      走模板。默认关，只有判重包装传 True）。
    """

    mode: str = "largest"
    min_gain_percent: int = 10
    min_gain_mb: int = 200
    prefer_template_named_version: bool = True
    quality_prefer: bool = False
    template_fallback_for_any_skip: bool = False


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
        旧文件名只用于体积/分辨率优先分支里 ``old_versions`` 为空时的回退）。
    :param old_versions: 旧版本列表（体积/分辨率优先分支传给
        ``compare_quality`` 的原始版本集合；不传时用 ``(old_file_name,
        old_size)`` 回退）。
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

    # (2) 体积/分辨率优先分支（旧判重 quality_prefer 分支）：不看评分。
    if policy.quality_prefer:
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
            return "replace", f"按体积择优：{reason}"
        if policy.prefer_template_named_version and template_conformant_name is not None:
            return (
                "skip",
                f"现有版本已符合当前重命名模板，保留 {template_conformant_name}",
            )
        return "skip", f"现有版本更优：{reason}"

    # (3) 评分比较。
    if new_score > old_score:
        return "replace", f"评分提升 {old_score}→{new_score}"
    if new_score < old_score:
        # 判重外层的旧行为：任何不换的结果都先过模板保留；评分制本体则
        # 在此直接返回。开关只由判重包装开（见策略字段说明）。
        if (
                policy.template_fallback_for_any_skip
                and policy.prefer_template_named_version
                and template_conformant_name is not None
        ):
            return (
                "skip",
                f"现有版本已符合当前重命名模板，保留 {template_conformant_name}",
            )
        return "skip", f"评分降低 {old_score}→{new_score}"

    # (4) 评分相等：按模式比体积（旧评分制的原始比较语义，无阈值、不看分辨率）。
    mode = str(policy.mode or "").strip().lower()
    if mode == "largest":
        if old_size and new_size and new_size > old_size:
            return (
                "replace",
                f"同评分保留更大文件 {format_size(old_size)}→{format_size(new_size)}",
            )
        if policy.prefer_template_named_version and template_conformant_name is not None:
            return (
                "skip",
                f"现有版本已符合当前重命名模板，保留 {template_conformant_name}",
            )
        return "skip", "同评分且候选文件未更大"
    if mode == "smallest":
        if old_size and new_size and new_size < old_size:
            return (
                "replace",
                f"同评分保留更小文件 {format_size(old_size)}→{format_size(new_size)}",
            )
        if policy.prefer_template_named_version and template_conformant_name is not None:
            return (
                "skip",
                f"现有版本已符合当前重命名模板，保留 {template_conformant_name}",
            )
        return "skip", "同评分且候选文件未更小"
    if policy.prefer_template_named_version and template_conformant_name is not None:
        return (
            "skip",
            f"现有版本已符合当前重命名模板，保留 {template_conformant_name}",
        )
    return "skip", "同评分不执行替换"
