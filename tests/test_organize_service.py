"""整理台执行逻辑测试：同名策略、同目录改名、视频失败时字幕跟随跳过。"""

import sys
import types

from tests.plugin_env import OwnerDelegator, load_module, register_mock, set_media_type

register_mock("organize_svc_test")
register_mock(
    "organize_svc_test.core",
    OwnerDelegator=OwnerDelegator,
    CloudDriveCapability=types.SimpleNamespace(
        DIRECTORY_READ="directory_read",
        FILE_QUERY="file_query",
        FILE_MUTATION="file_mutation",
        BATCH_FILE_MUTATION="batch_file_mutation",
    ),
)
register_mock("organize_svc_test.core.services")
register_mock("organize_svc_test.utils")
register_mock("app")
register_mock("app.log", logger=types.SimpleNamespace(info=lambda *a, **k: None,
                                                       warning=lambda *a, **k: None,
                                                       error=lambda *a, **k: None,
                                                       debug=lambda *a, **k: None))
register_mock("app.core.metainfo", MetaInfo=object)
set_media_type()

plan = load_module(
    "organize_svc_test.utils.organize_plan",
    "plugins.v2/cloudsubscribex/utils/organize_plan.py",
)
sys.modules["organize_svc_test.utils"].organize_plan = plan
service_module = load_module(
    "organize_svc_test.core.services.organize",
    "plugins.v2/cloudsubscribex/core/services/organize.py",
)
OrganizeService = service_module.OrganizeService
TargetFile = service_module.TargetFile
GroupPlan = service_module.GroupPlan


class FakeCloudFile:
    def __init__(self, name):
        self.name = name


class FakeBatch:
    def __init__(self, fail_move=False):
        self.renamed = []
        self.moved = []
        self.fail_move = fail_move

    def rename_files(self, path, items):
        self.renamed.append((path, items))
        return {
            key: FakeCloudFile(value["target_name"])
            for key, value in items.items()
        }

    def move_files(self, items, target_dir):
        self.moved.append((target_dir, items))
        if self.fail_move:
            return {}
        return {
            key: FakeCloudFile(next(iter(items.values())).name)
            for key in items
        }


class FakeDrive:
    def __init__(self, batch):
        self.batch = batch

    def supports(self, capability):
        return capability == "batch_file_mutation"

    def require(self, capability):
        return self.batch


def make_service(batch):
    service = OrganizeService(types.SimpleNamespace())
    service._cloud_drive = FakeDrive(batch)
    return service


def test_target_exists_skips_without_touching_cloud():
    batch = FakeBatch()
    service = make_service(batch)
    object.__setattr__(service, "_find_entry", lambda target: FakeCloudFile(target.source_name))
    object.__setattr__(service, "_directory_names", lambda path: {target.target_name})
    target = TargetFile(
        file_id="1", source_name="Show.S01E01.mkv", source_dir="/待整理/Show",
        target_dir="/剧集/Show/Season 01", target_name="Show - S01E01.mkv",
    )
    success, actual, reason = service._move_target(target, "skip")
    assert success is False
    assert reason.startswith("skip:")
    assert batch.renamed == []
    assert batch.moved == []


def test_conflict_suffix_renames_and_moves():
    batch = FakeBatch()
    service = make_service(batch)
    object.__setattr__(service, "_find_entry", lambda target: FakeCloudFile(target.source_name))
    object.__setattr__(service, "_directory_names", lambda path: {"Show - S01E01.mkv"})
    target = TargetFile(
        file_id="1", source_name="Show.S01E01.mkv", source_dir="/待整理/Show",
        target_dir="/剧集/Show/Season 01", target_name="Show - S01E01.mkv",
    )
    success, actual, reason = service._move_target(target, "suffix")
    assert success is True
    assert actual == "Show - S01E01 (2).mkv"
    assert batch.renamed[0][1]["1"]["target_name"] == "Show - S01E01 (2).mkv"
    assert batch.moved[0][0] == "/剧集/Show/Season 01"


def test_same_directory_only_renames():
    batch = FakeBatch()
    service = make_service(batch)
    object.__setattr__(service, "_find_entry", lambda target: FakeCloudFile(target.source_name))
    object.__setattr__(service, "_directory_names", lambda path: set())
    target = TargetFile(
        file_id="1", source_name="old.mkv", source_dir="/剧集/Show/Season 01",
        target_dir="/剧集/Show/Season 01", target_name="new.mkv",
    )
    success, actual, reason = service._move_target(target, "skip")
    assert success is True
    assert actual == "new.mkv"
    assert len(batch.renamed) == 1
    assert batch.moved == []


def test_unreadable_target_fails_safe():
    batch = FakeBatch()
    service = make_service(batch)
    object.__setattr__(service, "_find_entry", lambda target: FakeCloudFile(target.source_name))
    object.__setattr__(service, "_directory_names", lambda path: None)
    target = TargetFile(
        file_id="1", source_name="a.mkv", source_dir="/待整理",
        target_dir="/剧集", target_name="a.mkv",
    )
    success, actual, reason = service._move_target(target, "skip")
    assert success is False
    assert "无法读取目标目录" in reason
    assert batch.renamed == []


def test_subtitle_skipped_when_video_skipped():
    service = make_service(FakeBatch())
    service._stop_event = __import__("threading").Event()
    object.__setattr__(service, "_generate_strm_for_target", lambda target, name: None)

    def fake_move(target, policy):
        if target.is_subtitle:
            return True, target.target_name, ""
        return False, target.target_name, "skip:目标同名文件已存在"

    object.__setattr__(service, "_move_target", fake_move)
    video = TargetFile(
        file_id="1", source_name="Show.S01E01.mkv", source_dir="/源",
        target_dir="/库", target_name="Show - S01E01.mkv", episode=1,
    )
    subtitle = TargetFile(
        file_id="2", source_name="Show.S01E01.chs.srt", source_dir="/源",
        target_dir="/库", target_name="Show - S01E01.chs.srt", episode=1, is_subtitle=True,
    )
    package = types.SimpleNamespace(videos=[], duplicates=[], ignored=[])
    plan = GroupPlan(
        key="Show", group={"title": "Show", "status": "ready"},
        package=package, media=None, targets=[video], subtitles=[subtitle],
    )
    result = service._execute_plan(plan)
    assert result["skipped"] == 2
    assert result["done"] == 0
    assert any("字幕跳过" in item["reason"] for item in result["items"])
