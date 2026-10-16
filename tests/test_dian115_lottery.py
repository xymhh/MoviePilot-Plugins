"""Dian115 签到「幸运转盘」配置接线测试。

背景（2026-10-13）：签到链路复用的客户端由 ``Dian115SearchService`` 自建，
漏传 ``lottery_enabled`` / ``lottery_count`` → ``checkin()`` 里转盘分支恒不执行，
插件配置打开「启用幸运转盘」也无效（线上签到记录 lottery_* 恒为 null）。
本测试锁死「配置 → owner 属性 → 客户端 → 转盘行为」整条链路。
"""

import types
import unittest
from unittest.mock import MagicMock

from plugin_env import OwnerDelegator, load_module, register_mock, register_module

# ---- app.* 与 cryptography 占位（测试环境无 MoviePilot 运行时） ----
register_module("app", package=True)
register_module("app.log", logger=MagicMock())
register_module("app.schemas", package=True)
register_mock("app.schemas.types")

for _name in (
        "cryptography",
        "cryptography.hazmat",
        "cryptography.hazmat.primitives",
        "cryptography.hazmat.primitives.asymmetric",
        "cryptography.hazmat.primitives.asymmetric.utils",
):
    register_mock(_name)


class _FakeGate:
    """替代 RequestGate / AccountActionGate：只保留 matches_config 用到的属性。"""

    def __init__(self, request_interval: float = 1.0, max_actions: int = 6):
        self.request_interval = request_interval
        self.max_actions = max_actions

    @classmethod
    def shared(cls, *args, **kwargs):
        return cls(
            request_interval=kwargs.get("request_interval", 1.0),
            max_actions=kwargs.get("max_actions", 6),
        )


# ---- cloudsubscribex 包结构占位 ----
register_module("cloudsubscribex", package=True)
register_module(
    "cloudsubscribex.core",
    package=True,
    OwnerDelegator=OwnerDelegator,
    SearchQuery=MagicMock(),
    format_search_label=MagicMock(),
)
register_module("cloudsubscribex.core.media", tmdb_id_of=MagicMock())
register_module("cloudsubscribex.utils", package=True)
register_mock("cloudsubscribex.utils.cache", create_platform_ttl_cache=MagicMock())
register_mock("cloudsubscribex.utils.file_parser", MediaFileParser=MagicMock())
register_module("cloudsubscribex.search", package=True)
register_module("cloudsubscribex.search.dian115", package=True)
register_mock("cloudsubscribex.search.dian115.security", turnstile_token=MagicMock())
register_mock("cloudsubscribex.search.dian115.resource", Dian115ResourceService=MagicMock())
register_mock("cloudsubscribex.search.budget", PointBudgetLedger=MagicMock())
register_mock("cloudsubscribex.search.matching", unique_texts=MagicMock())
register_mock("cloudsubscribex.search.cloudflare", is_cloudflare_challenge=MagicMock())
register_module(
    "cloudsubscribex.search.http_client",
    AccountActionGate=_FakeGate,
    RequestGate=_FakeGate,
    gated_idempotent_request=MagicMock(),
    normalize_proxies=lambda value: "direct" if value is None else value,
    requests=MagicMock(Session=MagicMock(side_effect=lambda **kwargs: MagicMock())),
)

# ---- 动态加载真实模块 ----
_search_core = load_module(
    "cloudsubscribex.core.search",
    "plugins.v2/cloudsubscribex/core/search.py",
)
load_module(
    "cloudsubscribex.core.definitions",
    "plugins.v2/cloudsubscribex/core/definitions.py",
)
client_module = load_module(
    "cloudsubscribex.search.dian115.client",
    "plugins.v2/cloudsubscribex/search/dian115/client.py",
)
service_module = load_module(
    "cloudsubscribex.search.dian115.service",
    "plugins.v2/cloudsubscribex/search/dian115/service.py",
)
load_module(
    "cloudsubscribex.search.dian115.provider",
    "plugins.v2/cloudsubscribex/search/dian115/provider.py",
)
definition_module = load_module(
    "cloudsubscribex.search.dian115.definition",
    "plugins.v2/cloudsubscribex/search/dian115/definition.py",
)

Dian115Client = client_module.Dian115Client
Dian115SearchService = service_module.Dian115SearchService
Dian115SourceDefinition = definition_module.Dian115SourceDefinition
SearchCapability = _search_core.SearchCapability


def _make_owner(**config):
    """按 definition 的约定装配 owner 属性（与 handlers/search/service.py 一致）。

    ``configure_owner`` 从 ``owner.__dict__`` 读取配置，因此测试先铺平配置键。
    """
    owner = types.SimpleNamespace(
        get_data=MagicMock(),
        save_data=MagicMock(),
        _search_proxy=None,
        _resource_type_order_config=("115",),
    )
    for key, value in config.items():
        setattr(owner, key, value)
    Dian115SourceDefinition.configure_owner(owner, config)
    return owner


def _base_config(**overrides):
    config = {
        "dian115_email": "user@example.com",
        "dian115_password": "secret",
        "dian115_request_interval": 1,
        "dian115_unlocks_per_minute": 6,
        "dian115_lottery_enabled": True,
        "dian115_lottery_count": 20,
    }
    config.update(overrides)
    return config


def _make_checkin_client(lottery_enabled: bool, lottery_count: int,
                         used_today: int = 1, max_plays: int = 20):
    """构造不触网的真实客户端（绕过 __init__ 的会话初始化）。"""
    client = Dian115Client.__new__(Dian115Client)
    client._lottery_enabled = lottery_enabled
    client._lottery_count = lottery_count
    client._email = "user@example.com"
    client._password = "secret"
    client._proxies = "direct"
    client._request_gate = _FakeGate(1.0, 6)
    client._unlock_gate = _FakeGate(1.0, 6)
    client._session = MagicMock()
    wheel_calls = []

    def request_json(method, path, *args, **kwargs):
        wheel_calls.append((method, path))
        return {"prize": {"points": 5}, "cost": 5, "new_balance": 100}

    client.request_json = request_json
    client.get_game_status = lambda: {
        "items": {"daily_wheel": {"used_today": used_today, "max_plays": max_plays}}
    }
    client._signin = lambda mode: {
        "success": True,
        "already_checked_in": False,
        "status": "签到成功",
        "message": "签到 +4",
        "award_points": 4,
        "signin_days": 3,
    }
    client.get_account_info = lambda: {"points": 100, "consecutive_signin": 3}
    return client, wheel_calls


class TestDian115LotteryWiring(unittest.TestCase):
    """配置 → owner 属性 → 客户端 的接线契约。"""

    def test_configure_owner_exposes_lottery_config(self):
        """开关与次数必须落到 owner 属性上（此前完全缺失）。"""
        owner = _make_owner(**_base_config())
        self.assertTrue(owner._dian115_lottery_enabled)
        self.assertEqual(owner._dian115_lottery_count, 20)

    def test_configure_owner_clamps_and_defaults_lottery(self):
        """次数默认 1、上限 20，脏数据不炸。"""
        self.assertFalse(_make_owner(**_base_config(
            dian115_lottery_enabled=False))._dian115_lottery_enabled)
        self.assertEqual(_make_owner(**_base_config(
            dian115_lottery_count=99))._dian115_lottery_count, 20)
        self.assertEqual(_make_owner(**_base_config(
            dian115_lottery_count="bad"))._dian115_lottery_count, 1)
        self.assertEqual(_make_owner(**_base_config(
            dian115_lottery_count=None))._dian115_lottery_count, 1)

    def test_config_keys_cover_checkin_group_fields(self):
        """签到分组里的渠道配置（幸运转盘）必须算作配置键。

        宿主处理器只按 get_config_keys() 播种运行期属性；漏键会让转盘开关静默
        退回默认 False（1.6.36 线上仍不跑转盘的根因）。
        """
        keys = Dian115SourceDefinition.get_config_keys()
        for key in ("dian115_email", "dian115_lottery_enabled",
                    "dian115_lottery_count"):
            self.assertIn(key, keys)

    def test_host_seeding_wiring_keeps_lottery_enabled(self):
        """端到端复刻宿主播种：插件属性 → handler 属性 → 客户端转盘开关。"""
        plugin = types.SimpleNamespace(
            **{f"_{key}": value for key, value in _base_config().items()}
        )
        owner = types.SimpleNamespace(
            _plugin=plugin,
            get_data=MagicMock(),
            save_data=MagicMock(),
            _search_proxy=None,
            _resource_type_order_config=("115",),
        )
        for key in Dian115SourceDefinition.get_config_keys():
            # 等价于 handlers/search/service.py 的 get_val：优先插件私有属性
            setattr(owner, f"_{key}", getattr(plugin, f"_{key}", None))
        Dian115SourceDefinition.configure_owner(owner, {})
        self.assertTrue(owner._dian115_lottery_enabled)
        self.assertEqual(owner._dian115_lottery_count, 20)
        client = Dian115SearchService(owner).get_client()
        self.assertTrue(client._lottery_enabled)
        self.assertEqual(client._lottery_count, 20)

    def test_configure_owner_falls_back_to_plugin_config(self):
        """owner 属性快照缺键时回退宿主插件配置，不静默取默认值。"""
        plugin = types.SimpleNamespace(
            _dian115_email="user@example.com",
            _dian115_password="secret",
            _dian115_lottery_enabled=True,
            _dian115_lottery_count=7,
        )
        owner = types.SimpleNamespace(_plugin=plugin)
        Dian115SourceDefinition.configure_owner(owner, {})
        self.assertTrue(owner._dian115_lottery_enabled)
        self.assertEqual(owner._dian115_lottery_count, 7)
        self.assertEqual(owner._dian115_email, "user@example.com")

    def test_service_client_carries_lottery_config(self):
        """签到链路复用的客户端必须带上转盘配置。"""
        owner = _make_owner(**_base_config())
        service = Dian115SearchService(owner)
        client = service.get_client()
        self.assertIsInstance(client, Dian115Client)
        self.assertTrue(client._lottery_enabled)
        self.assertEqual(client._lottery_count, 20)

    def test_service_rebuilds_client_when_lottery_toggled(self):
        """改配置后必须重建客户端，否则要重启插件才生效。"""
        owner = _make_owner(**_base_config())
        service = Dian115SearchService(owner)
        first = service.get_client()
        owner._dian115_lottery_enabled = False
        second = service.get_client()
        self.assertIsNot(first, second)
        self.assertFalse(second._lottery_enabled)
        first._session.close.assert_called_once_with()

    def test_provider_checkin_capability_has_lottery_enabled(self):
        """Provider 的签到能力对象就是带转盘配置的客户端（端到端）。"""
        owner = _make_owner(**_base_config())
        provider = Dian115SourceDefinition.create_provider(
            None, None, _base_config(), {"owner": owner}
        )
        checkin_client = provider.require(SearchCapability.CHECKIN)
        self.assertIsInstance(checkin_client, Dian115Client)
        self.assertTrue(checkin_client._lottery_enabled)
        self.assertEqual(checkin_client._lottery_count, 20)


class TestDian115LotteryBehaviour(unittest.TestCase):
    """转盘执行行为（真实 run_lottery / checkin 逻辑）。"""

    def test_checkin_runs_lottery_up_to_target(self):
        """目标 3 次、当天已用 1 次 → 只补 2 次。"""
        client, wheel_calls = _make_checkin_client(True, 3, used_today=1)
        result = client.checkin("normal")
        self.assertEqual([path for _, path in wheel_calls],
                         ["/api/portal/lottery/wheel"] * 2)
        self.assertIn("转盘 3/3", result["message"])
        self.assertTrue(result["success"])
        self.assertEqual(result["lottery"]["executed"], 2)
        self.assertEqual(result["lottery"]["used_after"], 3)

    def test_checkin_respects_daily_cap(self):
        """当天已用满 → 一次都不补。"""
        client, wheel_calls = _make_checkin_client(True, 5, used_today=5)
        result = client.checkin("normal")
        self.assertEqual(wheel_calls, [])
        self.assertIn("转盘 5/5", result["message"])
        self.assertEqual(result["lottery"]["executed"], 0)
        self.assertEqual(result["lottery"]["message"], "今日转盘已完成 5 次")

    def test_checkin_skips_lottery_when_disabled(self):
        """关掉开关时文案与行为都不带转盘。"""
        client, wheel_calls = _make_checkin_client(False, 20)
        result = client.checkin("normal")
        self.assertEqual(wheel_calls, [])
        self.assertIsNone(result["lottery"])
        self.assertEqual(result["message"], "签到 +4")

    def test_matches_config_compares_lottery_dimensions(self):
        """lottery 维度参与比对；不传则不参与（兼容其它调用方）。"""
        client, _ = _make_checkin_client(True, 3)
        self.assertTrue(client.matches_config(
            "user@example.com", "secret", None, 1.0, 6))
        self.assertFalse(client.matches_config(
            "user@example.com", "secret", None, 1.0, 6,
            lottery_enabled=True, lottery_count=5))
        self.assertFalse(client.matches_config(
            "user@example.com", "secret", None, 1.0, 6,
            lottery_enabled=False, lottery_count=3))
        self.assertTrue(client.matches_config(
            "user@example.com", "secret", None, 1.0, 6,
            lottery_enabled=True, lottery_count=3))


if __name__ == "__main__":
    unittest.main()
