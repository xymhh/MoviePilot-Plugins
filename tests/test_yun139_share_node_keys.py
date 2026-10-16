"""1.6.41 回归：移动云盘分享条目字段大小写（``caID``/``coID``）导致目录/文件全部丢失。

线上现象（1.6.40 真机，用户反馈「预览弹窗打开但显示『当前目录为空或未解析到文件』」）：
1. 资源页「移动云盘」条目点「预览」→ 后端回 ``读取到 0 个项目``（不报错）；
2. 直连 ``share-kd-njs.yun.139.com`` 分享接口实测：根目录 ``caLst`` 有 1 个子目录、
   子目录 ``coLst`` 有 11 个文件，字段名分别是 ``caID`` / ``coID``（大写 D）；
3. 插件 ``_share_node`` 读的是 ``caId`` / ``coId``（小写 d）→ 取不到节点 ID →
   ``if not node_id`` 命中 → 每个条目都被判成 None 丢掉 →
   ``list_share_directory`` 返回空、``list_share_files`` 也返回空
   （转存因此「没有可转存的文件」）。

本测试用线上真实响应结构（键名逐字取自实测响应）固定：
1. 根目录子目录可见、节点 ID 正确且可回解；
2. 子目录内文件可见、名称与大小正确；
3. 递归遍历 ``list_share_files`` 能取到文件；
4. 分享校验能带回分享名与条目数；
5. 历史小写键（``caId``/``coId``）仍兼容；
6. 缺 ID 的脏条目仍被丢弃。
"""

import os
import sys
import unittest
from unittest.mock import MagicMock

from plugin_env import (
    OwnerDelegator,
    load_module,
    register_mock,
    register_module,
    register_package,
)

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
plugins_v2_path = os.path.join(project_root, "plugins.v2")
if plugins_v2_path not in sys.path:
    sys.path.insert(0, plugins_v2_path)


# ---- app.* 占位依赖 ----
register_module("app", package=True)
register_module("app.log", logger=MagicMock())

# ---- 真实 core.cloud（CloudFile / ShareLinkStatus）----
# 注意：本文件在 discover 里排在最后，注册会覆盖其它测试的桩，因此占位包一律用
# 「属性自动返回 Mock」的形式（AutoMock / MagicMock），避免打断别人的导入。
register_mock(
    "cloudsubscribex.core",
    OwnerDelegator=OwnerDelegator,
    CloudDriveCapability=MagicMock(),
    CloudDriveProvider=MagicMock(),
    CloudFile=MagicMock(),
    SearchCapability=MagicMock(),
)
load_module(
    "cloudsubscribex.core.cloud",
    "plugins.v2/cloudsubscribex/core/cloud.py",
)
register_mock("cloudsubscribex.core.transfer", HttpFileDownloadService=MagicMock())

# ---- drive 包层级与占位依赖 ----
register_module("cloudsubscribex.drive", package=True)
register_module("cloudsubscribex.drive.yun139", package=True)


class Yun139ApiError(RuntimeError):
    """与线上实现同名的异常类型。"""


register_package("cloudsubscribex.drive.yun139.client", Yun139ApiError=Yun139ApiError)


def _safe_int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


register_package(
    "cloudsubscribex.drive.common",
    safe_int=_safe_int,
    CloudDriveFileServiceBase=object,
)

share_module = load_module(
    "cloudsubscribex.drive.yun139.share",
    "plugins.v2/cloudsubscribex/drive/yun139/share.py",
)
Yun139ShareService = share_module.Yun139ShareService

ROOT_NODE_ID = share_module.ROOT_NODE_ID

SHARE_URL = "https://yun.139.com/shareweb/#/w/i/2ygbc0FV4Nshu"
LINK_ID = "2ygbc0FV4Nshu"

# 实测：根目录下 1 个子目录
ROOT_FOLDER_ID = "FtARUxvxHKiIX_-YfPpxAiq7ZBQEPxw0i"
ROOT_FOLDER_NAME = "The Lament of Autumn (2026)"
# 实测：子目录下 1 个子目录 + 11 个文件，这里取前两个文件
SUB_FOLDER_ID = "FiqSvBp-tlXi9jCod3CtIBZ9Uqfi7rYB4"
SUB_FOLDER_NAME = "ISO视频文件安卓播放器安装包"
FILE_2_ID = "FggsW7HtF6a0OP5enZEZA35f7EBJfRfyt"
FILE_1_ID = "Fh0GS86umdm7swqzASK9LJZKxlA2PpmNu"

#: 线上真实根目录响应（键名逐字来自实测，仅保留本测试关心的字段）
ROOT_RESPONSE = {
    "resultCode": 0,
    "desc": "",
    "data": {
        "lkName": "The Lament of Autumn (2026)",
        "nodNum": 1,
        "caLst": [
            {
                "caID": ROOT_FOLDER_ID,
                "caName": ROOT_FOLDER_NAME,
                "ctTime": "20261012235603",
                "udTime": "20261012235604",
                "dlTimes": None,
                "path": f"FvBzCigrmBQrvrP_oQElO2abBk035ZiIK/{ROOT_FOLDER_ID}",
                "illegalType": 1,
                "illegalResult": 1,
                "updateStatus": None,
            }
        ],
        "coLst": None,
        "password": "ifzr",
        "owner": "18689291741",
    },
}

#: 线上真实子目录响应
SUB_RESPONSE = {
    "resultCode": 0,
    "desc": "",
    "data": {
        "lkName": "美人余（2026）",
        "caLst": [
            {
                "caID": SUB_FOLDER_ID,
                "caName": SUB_FOLDER_NAME,
                "udTime": "20261013174233",
                "path": f"FvghDEiTk5FECal719CJHg6v4RewA5PFR/{SUB_FOLDER_ID}",
            }
        ],
        "coLst": [
            {
                "coID": FILE_2_ID,
                "coName": "第2集.iso",
                "coSuffix": "iso",
                "coType": 0,
                "coSize": 801308672,
                "udTime": "20261009170752",
                "cdnDownLoadUrl": None,
                "presentURL": None,
            },
            {
                "coID": FILE_1_ID,
                "coName": "第1集.iso",
                "coSuffix": "iso",
                "coType": 0,
                "coSize": 944439296,
                "udTime": "20261009170839",
                "cdnDownLoadUrl": None,
                "presentURL": None,
            },
        ],
    },
}


class _FakeClient:
    """只提供分享接口需要的账号字段。"""

    account = ""


def _build_service(root_response=None, sub_response=None):
    """构造 share 服务，并把 ``_share_post`` 换成按 pCaID 分发的桩。"""
    service = Yun139ShareService(_FakeClient(), MagicMock(), MagicMock())
    root = ROOT_RESPONSE if root_response is None else root_response
    sub = SUB_RESPONSE if sub_response is None else sub_response

    def fake_post(path, body):
        request = (body or {}).get("getOutLinkInfoReq") or {}
        node_id = str(request.get("pCaID") or "")
        if node_id == ROOT_NODE_ID:
            return root
        if node_id == ROOT_FOLDER_ID:
            return sub
        return {"resultCode": 0, "desc": "", "data": {"caLst": None, "coLst": None}}

    service._share_post = fake_post
    return service


class TestShareNodeKeyCase(unittest.TestCase):
    """线上接口用 ``caID``/``coID``，条目必须能正常映射成文件节点。"""

    def setUp(self):
        self.service = _build_service()

    def test_root_folder_is_visible(self):
        items = self.service.list_share_directory(SHARE_URL)
        self.assertEqual(len(items), 1, items)
        folder = items[0]
        self.assertEqual(folder["name"], ROOT_FOLDER_NAME)
        self.assertTrue(folder["is_dir"])
        # 节点 ID 是转存与下钻的关键，必须从 caID 落到节点信息里
        self.assertEqual(
            self.service._node_info[folder["id"]]["node_id"], ROOT_FOLDER_ID
        )
        node = self.service._share_node(
            LINK_ID, "", ROOT_RESPONSE["data"]["caLst"][0], True
        )
        self.assertEqual(node.native["node_id"], ROOT_FOLDER_ID)
        self.assertEqual(node.native["modified_at"], "2026-10-12T23:56:04")

    def test_folder_node_id_round_trip(self):
        folder = self.service.list_share_directory(SHARE_URL)[0]
        link_id, password, node_id = self.service.decode_node_id(folder["id"])
        self.assertEqual((link_id, password, node_id), (LINK_ID, "", ROOT_FOLDER_ID))

    def test_sub_folder_files_are_visible(self):
        folder = self.service.list_share_directory(SHARE_URL)[0]
        items = self.service.list_share_directory(SHARE_URL, parent_id=folder["id"])
        names = [item["name"] for item in items]
        self.assertEqual(names, [SUB_FOLDER_NAME, "第2集.iso", "第1集.iso"], names)
        files = [item for item in items if not item["is_dir"]]
        self.assertEqual([item["size"] for item in files], [801308672, 944439296])
        self.assertEqual(
            self.service._node_info[files[0]["id"]]["node_id"], FILE_2_ID
        )
        link_id, _, node_id = self.service.decode_node_id(files[1]["id"])
        self.assertEqual((link_id, node_id), (LINK_ID, FILE_1_ID))

    def test_list_share_files_recurses(self):
        files = self.service.list_share_files(SHARE_URL)
        names = sorted(item["name"] for item in files)
        self.assertEqual(names, ["第1集.iso", "第2集.iso"], names)

    def test_check_share_status_reports_title_and_count(self):
        status = self.service.check_share_status(SHARE_URL)
        self.assertTrue(status.is_valid, status.error_message)
        self.assertEqual(status.share_info.get("share_title"), ROOT_FOLDER_NAME)
        self.assertEqual(status.file_count, 1)

    def test_legacy_lowercase_keys_still_supported(self):
        """历史版本接口用小写 d（caId/coId），兼容分支不能丢。"""
        legacy_root = {
            "resultCode": 0,
            "data": {
                "lkName": "旧接口分享",
                "caLst": [{"caId": ROOT_FOLDER_ID, "caName": ROOT_FOLDER_NAME}],
                "coLst": [{"coId": FILE_2_ID, "coName": "第2集.iso", "coSize": 801308672}],
            },
        }
        service = _build_service(root_response=legacy_root)
        items = service.list_share_directory(SHARE_URL)
        self.assertEqual([item["name"] for item in items], [ROOT_FOLDER_NAME, "第2集.iso"])
        self.assertEqual(
            service._node_info[items[0]["id"]]["node_id"], ROOT_FOLDER_ID
        )

    def test_entry_without_id_is_dropped(self):
        dirty_root = {
            "resultCode": 0,
            "data": {
                "lkName": "脏数据",
                "caLst": [{"caName": "没有 ID 的目录"}],
                "coLst": [{"coName": "没有 ID 的文件"}],
            },
        }
        service = _build_service(root_response=dirty_root)
        self.assertEqual(service.list_share_directory(SHARE_URL), [])

    def test_share_meta_still_reachable(self):
        """根目录无文件时，分享名要能从信封（data.lkName）取到。"""
        self.assertEqual(self.service._share_title, "")
        self.service.list_share_directory(SHARE_URL)
        self.assertEqual(self.service._share_title, ROOT_FOLDER_NAME)


if __name__ == "__main__":
    unittest.main()
