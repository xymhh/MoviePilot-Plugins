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


def test_group_plan_uses_sync_handler_for_platform_target():
    """整理台必须走同步服务的命名实现，不能假设插件自身有该方法。"""
    service = make_service(FakeBatch())
    object.__setattr__(service, "_recognize_package", lambda package: types.SimpleNamespace(
        title="Show", year=2020, tmdb_id=1,
    ))

    class FakeNaming:
        called = False
        root_seen = None
        _CLOUD_MEDIA_ROOT = "/媒体库"

        def _platform_target(self, root, subscribe, media, filename, season=None, episode=None):
            self.called = True
            self.root_seen = root
            return "/媒体库/剧集/Show (2020)/Season 01", "Show - S01E01.mkv"

    naming = FakeNaming()
    service._sync_handler = naming

    entry = plan.OrganizeEntry(
        file_id="1", name="Show.S01E01.mkv", size=100,
        parent_path="/待整理/Show/Season 1", is_video=True,
    )
    package = plan.PackagePlan(
        key="Show", pack_name="Show", source_dirs=["/待整理/Show"],
        videos=[plan.VideoCandidate(entry=entry, season=1, episode=1, subtitles=[])],
        duplicates=[], ignored=[], is_tv=True,
    )
    result = service._build_group_plan(package, 0)
    assert naming.called is True
    assert naming.root_seen == "/媒体库"
    assert result.group["status"] == "ready"
    assert result.targets[0].target_dir == "/媒体库/剧集/Show (2020)/Season 01"
    assert result.targets[0].target_name == "Show - S01E01.mkv"


def test_missing_target_suffix_is_restored_before_rename():
    """115 改名接口会截断缺失扩展名的目标名，执行前必须补回源后缀。"""
    batch = FakeBatch()
    service = make_service(batch)
    object.__setattr__(service, "_find_entry", lambda target: FakeCloudFile(target.source_name))
    object.__setattr__(service, "_directory_names", lambda path: set())
    target = TargetFile(
        file_id="1", source_name="Show.S01E01.mkv", source_dir="/待整理/Show",
        target_dir="/剧集/Show/Season 01", target_name="Show - S01E01",
    )
    success, actual, reason = service._move_target(target, "skip")
    assert success is True
    assert actual == "Show - S01E01.mkv"
    assert batch.renamed[0][1]["1"]["target_name"] == "Show - S01E01.mkv"


class FakeListing:
    def __init__(self, files=()):
        self.checked = True
        self.files = list(files)


class FakeLookup:
    def __init__(self, directory_id=None):
        self.checked = True
        self.directory_id = directory_id


class FakeNode:
    def __init__(self, name, is_directory, directory_id=""):
        self.name = name
        self.is_directory = is_directory
        self.id = directory_id


class FakeDirectories:
    """用 path -> 子项 的字典模拟 115 目录树。"""

    def __init__(self, tree):
        self.tree = {
            path: {"id": f"cid{index}", "children": dict(children)}
            for index, (path, children) in enumerate(tree.items())
        }
        self.deleted = []

    def _path_by_id(self, directory_id):
        for path, node in self.tree.items():
            if node["id"] == directory_id:
                return path
        return ""

    def resolve_directory(self, path):
        node = self.tree.get(path)
        return FakeLookup(node["id"] if node else None)

    def list_directory(self, directory_id):
        path = self._path_by_id(directory_id)
        if not path:
            return FakeListing()
        return FakeListing(
            FakeNode(
                name, is_directory,
                self.tree.get(f"{path.rstrip('/')}/{name}", {}).get("id", ""),
            )
            for name, is_directory in self.tree[path]["children"].items()
        )

    def delete_directory(self, directory_id):
        path = self._path_by_id(directory_id)
        if not path:
            return False
        parent = path.rsplit("/", 1)[0] or "/"
        self.tree[parent]["children"].pop(path.rsplit("/", 1)[-1], None)
        self.tree.pop(path, None)
        self.deleted.append(path)
        return True

    def refresh_directories(self):
        return None


class FakePruneDrive:
    def __init__(self, directories, batch=False):
        self.directories = directories
        self.batch = batch

    def supports(self, capability):
        if self.batch:
            return capability in {"directory_read", "batch_file_mutation"}
        return capability in {"directory_read", "file_mutation"}

    def require(self, capability):
        if capability == "directory_read":
            return self.directories
        if capability == "batch_file_mutation":
            return types.SimpleNamespace(
                delete_files=lambda ids: {
                    item for item in ids if self.directories.delete_directory(item)
                }
            )
        return types.SimpleNamespace(
            delete_file=lambda item: self.directories.delete_directory(item)
        )


def test_prune_empty_dirs_removes_emptied_chain_but_not_source_root():
    directories = FakeDirectories({
        "/待整理": {"Show": True, "keep.mkv": False},
        "/待整理/Show": {"Season 1": True},
        "/待整理/Show/Season 1": {},
    })
    service = make_service(FakeBatch())
    service._cloud_drive = FakePruneDrive(directories)
    removed = service._prune_empty_dirs(["/待整理/Show/Season 1"], "/待整理")
    assert removed == 2
    assert "/待整理" in directories.tree
    assert "/待整理/Show" not in directories.tree
    assert "/待整理/Show/Season 1" not in directories.tree


def test_prune_empty_dirs_stops_at_non_empty_parent():
    directories = FakeDirectories({
        "/待整理": {"Show": True},
        "/待整理/Show": {"Season 1": True, "extra.mkv": False},
        "/待整理/Show/Season 1": {},
    })
    service = make_service(FakeBatch())
    service._cloud_drive = FakePruneDrive(directories, batch=True)
    removed = service._prune_empty_dirs(["/待整理/Show/Season 1"], "/待整理")
    assert removed == 1
    assert "/待整理/Show/Season 1" not in directories.tree
    assert "/待整理/Show" in directories.tree


def test_prune_empty_dirs_never_removes_source_root():
    directories = FakeDirectories({
        "/待整理": {"Show": True},
        "/待整理/Show": {"Season 1": True},
        "/待整理/Show/Season 1": {},
    })
    service = make_service(FakeBatch())
    service._cloud_drive = FakePruneDrive(directories)
    removed = service._prune_empty_dirs(["/待整理/Show/Season 1"], "/待整理")
    assert removed == 2
    assert "/待整理" in directories.tree
    assert directories.tree["/待整理"]["children"] == {}


def test_cleanup_empty_dirs_scans_source_and_removes_all_empty_dirs():
    directories = FakeDirectories({
        "/待整理": {"A": True, "B": True, "keep.mkv": False},
        "/待整理/A": {"Season 1": True},
        "/待整理/A/Season 1": {},
        "/待整理/B": {"Season 1": True, "note.txt": False},
        "/待整理/B/Season 1": {},
    })
    service = make_service(FakeBatch())
    service._cloud_drive = FakePruneDrive(directories)
    result = service.cleanup_empty_dirs("/待整理")
    assert result["success"] is True
    assert result["data"]["removed"] == 3
    assert "/待整理/A" not in directories.tree
    assert "/待整理/B/Season 1" not in directories.tree
    assert "/待整理/B" in directories.tree
    assert "/待整理" in directories.tree
