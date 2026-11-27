"""TG 频道监控作为「推送型」搜索渠道的自描述规范。

频道监控与其它搜索渠道在**产品层**并列：同一份渠道目录、同一套开关与统计；
但它是常驻推送流（游标、flood wait、验证码机器人都在 ``core/tg_monitor.py``），
不是「按关键词拉取」的查询源，因此：

* ``capabilities`` 里只有 ``push`` / ``notify``，没有 ``query``；
* ``create_provider`` 返回 ``None``——搜索组装会跳过它（``search/registry.py``）；
* 配置表单由本模块直接声明：Lite 前端没有独立的「频道监控」分组/页面
  （X 版的 ``TgMonitor.vue`` 未移植），不在此声明字段，搜索渠道里的
  「频道监控」标签页就会是空白。

注意：本表单只覆盖开关、TG 账号、扫描间隔与画质偏好；频道列表、关键词
与 TG 登录（手机号 + 验证码）仍在监控页面，Lite 暂未提供。

架构见 ``docs/plan/2026-10-28-频道监控并入搜索渠道-架构.md``。
"""

from __future__ import annotations

from typing import Any, Dict, FrozenSet, List, Optional

from ...core.definitions import FieldSpec, GroupSpec, SearchSourceDefinition


class TgChannelsSourceDefinition(SearchSourceDefinition):
    """频道监控渠道：推送发现，不提供关键词搜索。"""

    id = "tgchannels"
    name = "频道监控"
    icon = "mdi-telegram"
    color = "light-blue"
    order = 90
    capabilities: FrozenSet[str] = frozenset({"push", "notify"})

    @classmethod
    def get_config_groups(cls, context: Optional[Dict[str, Any]] = None) -> List[GroupSpec]:
        """声明 TG 账号 / 扫描 / 画质偏好三组表单字段。

        字段与 X 版 ``frontend/cloudsubscribex/src/config/fields/tg.js`` 对齐；
        其中 ``tg_monitor_keep_unmatched`` 在 Lite 与 X 的后端默认值里原本
        都缺失，见 ``core/config.py``。
        """
        return [
            GroupSpec(
                tab="tgchannels",
                title="TG 账号",
                icon="mdi-account-key-outline",
                fields=[
                    FieldSpec(
                        key="tg_monitor_enabled",
                        label="启用频道监控",
                        type="switch",
                        default=False,
                        hint="用你的 TG 账号读取指定频道的新消息，抽取网盘分享链接后进入搜索渠道。注意：Lite 暂无频道监控页面，TG 登录、频道列表与关键词暂时无法配置",
                        cols=4,
                    ),
                    FieldSpec(
                        key="tg_api_id",
                        label="api_id",
                        type="number",
                        default=0,
                        min=0,
                        placeholder="1234567",
                        hint="在 my.telegram.org → API development tools 申请",
                        cols=4,
                    ),
                    FieldSpec(
                        key="tg_api_hash",
                        label="api_hash",
                        type="password",
                        default="",
                        placeholder="32 位十六进制",
                        hint="与 api_id 配套，仅用于登录和读取频道消息",
                        cols=4,
                    ),
                    FieldSpec(
                        key="tg_session",
                        label="登录会话",
                        type="password",
                        default="",
                        hint="留空表示未登录。登录成功后会话会自动写回这里；注意：Lite 暂无 TG 登录页面，暂时无法登录",
                        cols=12,
                    ),
                ],
            ),
            GroupSpec(
                tab="tgchannels",
                title="监控频道",
                icon="mdi-broadcast",
                fields=[
                    FieldSpec(
                        key="tg_monitor_interval_minutes",
                        label="扫描间隔（分钟）",
                        type="number",
                        default=10,
                        min=1,
                        max=1440,
                        hint="所有频道共用的后台扫描间隔",
                        cols=6,
                        show_condition="config.tg_monitor_enabled",
                    ),
                    FieldSpec(
                        key="tg_monitor_max_items",
                        label="最多保留条数",
                        type="number",
                        default=500,
                        min=50,
                        max=5000,
                        hint="超过后丢弃最旧的记录（已转存的记录也会被清理）",
                        cols=6,
                        show_condition="config.tg_monitor_enabled",
                    ),
                    FieldSpec(
                        key="tg_monitor_keep_unmatched",
                        label="未命中订阅的消息也入池",
                        type="switch",
                        default=False,
                        hint="关闭：只保留命中订阅的消息；打开：没匹配上订阅的分享也存进列表，可在页面手动挑选转存",
                        cols=6,
                        show_condition="config.tg_monitor_enabled",
                    ),
                ],
            ),
            GroupSpec(
                tab="tgchannels",
                title="画质偏好（全局）",
                icon="mdi-high-definition",
                fields=[
                    FieldSpec(
                        key="tg_quality_preference_enabled",
                        label="启用画质偏好",
                        type="switch",
                        default=True,
                        hint="同一次扫描里，同一订阅同集有多条候选时，先转最优画质；低画质保留兜底，最优失败下一轮自动回退。识别不到画质的资源默认放行，不空等、不拦截",
                        cols=12,
                        show_condition="config.tg_monitor_enabled",
                    ),
                    FieldSpec(
                        key="tg_quality_resolution_order",
                        label="分辨率优先级",
                        type="text",
                        default="4K,1080P,720P",
                        placeholder="4K,1080P,720P",
                        hint="从高到低，逗号分隔；2160P/UHD 会自动当成 4K。没有分辨率标签的资源不参与排序、直接放行",
                        cols=6,
                        show_condition="config.tg_monitor_enabled",
                    ),
                    FieldSpec(
                        key="tg_quality_codec_order",
                        label="视频编码优先级",
                        type="text",
                        default="HEVC,AVC,AV1",
                        placeholder="HEVC,AVC,AV1",
                        hint="从高到低，逗号分隔；H.265/x265 会自动当成 HEVC，H.264/x264 当成 AVC",
                        cols=6,
                        show_condition="config.tg_monitor_enabled",
                    ),
                    FieldSpec(
                        key="tg_quality_hdr_order",
                        label="HDR 优先级",
                        type="text",
                        default="DOLBY VISION,HDR10+,HDR10,HDR,SDR",
                        placeholder="DOLBY VISION,HDR10+,HDR10,HDR,SDR",
                        hint="从高到低，逗号分隔；DoVi/DV 会自动当成 DOLBY VISION",
                        cols=12,
                        show_condition="config.tg_monitor_enabled",
                    ),
                    FieldSpec(
                        key="tg_quality_preferred_groups",
                        label="优先字幕组/压制组",
                        type="textarea",
                        default="",
                        placeholder="每行一个，例如：FRDS\nQHstudIo",
                        hint="可留空。同分辨率/编码/HDR 时，标题里包含这些组的资源优先；只做优先，不做排除",
                        cols=12,
                        show_condition="config.tg_monitor_enabled",
                    ),
                ],
            ),
        ]

    @classmethod
    def create_provider(
            cls,
            service: Any,
            client: Any,
            config: Dict[str, Any],
            context: Optional[Any] = None,
    ) -> Any:
        """推送型渠道没有 SearchProvider，返回 None 让搜索组装跳过。"""
        return None
