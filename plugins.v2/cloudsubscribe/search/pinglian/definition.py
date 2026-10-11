"""盘链搜索渠道自描述规范与表单声明。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .client import PinglianClient, PinglianError
from .provider import create_pinglian_provider
from .service import PinglianSearchService
from ...core.definitions import (
    CheckinDefinition,
    FieldSpec,
    GroupSpec,
    SearchSourceDefinition,
    build_checkin_definition,
)


class PinglianSourceDefinition(SearchSourceDefinition):
    """盘链搜索渠道规范。"""

    id = "pinglian"
    name = "盘链"
    icon = "mdi-link-variant"
    color = "cyan"
    order = 50

    @classmethod
    def create_client(cls, config: Dict[str, Any], context: Optional[Any] = None) -> Any:
        ctx = context or {}
        owner = ctx.get("storage_owner")
        username = str(cls.config_value(config, "pinglian_username", "") or "").strip()
        password = str(cls.config_value(config, "pinglian_password", "") or "")
        if not username or not password:
            return None
        return PinglianClient(
            base_url=str(
                cls.config_value(config, "pinglian_base_url", PinglianClient.BASE_URL) or PinglianClient.BASE_URL),
            username=username,
            password=password,
            proxy=ctx.get("proxy"),
            request_timeout=int(cls.config_value(config, "pinglian_timeout", 60) or 60),
            request_interval=float(cls.config_value(config, "pinglian_request_interval", 2.0) or 2.0),
            unlocks_per_minute=int(cls.config_value(config, "pinglian_unlocks_per_minute", 5) or 5),
            get_data_func=getattr(owner, "get_data", None),
            save_data_func=getattr(owner, "save_data", None),
        )
    @classmethod
    def get_checkin_definition(cls) -> Optional[CheckinDefinition]:
        """自动注册盘链签到契约。"""
        return build_checkin_definition(
            cls,
            icon="mdi-link-variant",
            credential_attrs=("_pinglian_username", "_pinglian_password"),
            credential_keys=("pinglian_username", "pinglian_password"),
            error_types=(PinglianError,),
            group_title="盘链签到",
            points_label="配额",
            track_days=True,
            enable_cols=12,
        )

    @classmethod
    def get_config_groups(cls, context: Optional[Dict[str, Any]] = None) -> List[GroupSpec]:
        return [
            GroupSpec(
                tab="pinglian",
                title="盘链账号",
                icon="mdi-link-variant",
                hint="使用盘链网页登录账号搜索并解析网盘分享链接。",
                fields=[
                    FieldSpec(
                        key="pinglian_account_info",
                        type="account",
                        account_key="search:pinglian",
                        compact=True,
                        cols=12,
                    ),
                    FieldSpec(
                        key="pinglian_base_url",
                        label="服务地址",
                        placeholder="https://pinglian.lol",
                        default="https://pinglian.lol",
                        cols=12,
                    ),
                    FieldSpec(
                        key="pinglian_username",
                        label="网页登录账号",
                        cols=6,
                    ),
                    FieldSpec(
                        key="pinglian_password",
                        label="网页登录密码",
                        type="password",
                        cols=6,
                    ),
                    FieldSpec(
                        key="test_pinglian",
                        label="测试搜索",
                        type="test-source",
                        source="pinglian",
                        cols=12,
                    ),
                ],
            ),
            GroupSpec(
                tab="pinglian",
                title="搜索与风控",
                icon="mdi-tune-variant",
                fields=[
                    FieldSpec(
                        key="pinglian_result_limit",
                        label="候选上限",
                        type="number",
                        default=10,
                        min=1,
                        max=10,
                        cols=3,
                    ),
                    FieldSpec(
                        key="pinglian_unlocks_per_minute",
                        label="每分钟解锁",
                        hint="频次限制，默认 5 次",
                        type="number",
                        default=5,
                        min=1,
                        max=20,
                        step=1,
                        suffix="次",
                        cols=3,
                    ),
                    FieldSpec(
                        key="pinglian_request_interval",
                        label="请求间隔",
                        type="number",
                        default=2.0,
                        min=1,
                        max=10,
                        step=0.5,
                        suffix="秒",
                        cols=3,
                    ),
                    FieldSpec(
                        key="pinglian_timeout",
                        label="搜索超时",
                        type="number",
                        default=60,
                        min=5,
                        max=120,
                        suffix="秒",
                        cols=3,
                    ),
                ],
            ),
        ]
    @classmethod
    def create_provider(
            cls, service: Any, client: Any, config: Dict[str, Any], context: Optional[Any] = None
    ) -> Any:
        ctx = context or {}
        resource_types = ctx.get("resource_types", ())
        if not client or not getattr(client, "is_configured", False) or not resource_types:
            return None
        limit = int(cls.config_value(config, "pinglian_result_limit", 10) or 10)
        svc = PinglianSearchService(client, resource_types, limit)
        return create_pinglian_provider(
            svc, client, resource_types, {"result_limit": limit}
        )
