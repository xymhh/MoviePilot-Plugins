"""导出目录树增量补齐单测：差异计算 + 只对差异目录取列表。"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from plugin_env import ensure_package, load_module

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
plugins_v2_path = os.path.join(project_root, "plugins.v2")
if plugins_v2_path not in sys.path:
    sys.path.insert(0, plugins_v2_path)

ensure_package("app")
ensure_package("app.log")

package = ensure_package("cloudsubscribex")
package.__path__ = [os.path.join(plugins_v2_path, "cloudsubscribex")]

cloud_module = load_module(
    "cloudsubscribex.core.cloud", "plugins.v2/cloudsubscribex/core/cloud.py",
)
core_module = ensure_package("cloudsubscribex.core")
core_module.__path__ = [os.path.join(plugins_v2_path, "cloudsubscribex", "core")]
core_module.CloudDriveCapability = cloud_module.CloudDriveCapability

utils_package = ensure_package("cloudsubscribex.utils")
utils_package.__path__ = [
    os.path.join(plugins_v2_path, "cloudsubscribex", "utils")
]

export_module = load_module(
    "cloudsubscribex.utils.strm_export",
    "plugins.v2/cloudsubscribex/utils/strm_export.py",
)
rebuild_strm_by_export_tree = export_module.rebuild_strm_by_export_tree
CloudDriveCapability = cloud_module.CloudDriveCapability


class _Generator:
    """按 cloud_dir + file_name 映射到本地 .strm 路径。"""

    def __init__(self, local_root: Path):
        self.local_root = local_root
        self.written = []

    def local_path(self, local_root, cloud_root, cloud_dir, file_name):
        relative = cloud_dir[len(cloud_root):].strip("/")
        name = Path(file_name).stem + ".strm"
        return self.local_root / relative / name if relative else self.local_root / name

    def write(self, *, local_root, cloud_root, cloud_dir, file_name, template_values):
        path = self.local_path(local_root, cloud_root, cloud_dir, file_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(template_values.get("pickcode", ""), encoding="utf-8")
        self.written.append((cloud_dir, file_name))
        return path, "content"


class _Exporter:
    def __init__(self, paths):
        self.paths = paths
        self.calls = []

    def export_directory_tree(self, directory_id, *, timeout_seconds=600.0):
        self.calls.append(directory_id)
        yield from self.paths


class _Provider:
    def __init__(self, exporter=None, dirs=None, listings=None):
        self.exporter = exporter
        self.dirs = dirs or {}
        self.listings = listings or {}
        self.list_calls = []
        self.resolve_calls = []

    def supports(self, capability):
        return capability == CloudDriveCapability.STRM_EXPORT and self.exporter is not None

    def require(self, capability):
        if capability == CloudDriveCapability.STRM_EXPORT:
            if self.exporter is None:
                raise RuntimeError("no export")
            return self.exporter
        raise RuntimeError(f"unsupported {capability}")

    def resolve_directory(self, path):
        self.resolve_calls.append(path)
        cid = self.dirs.get(path)
        return SimpleNamespace(checked=cid is not None, directory_id=cid)

    def list_directory(self, cid):
        self.list_calls.append(cid)
        files = self.listings.get(cid)
        if files is None:
            return SimpleNamespace(checked=False, files=[])
        return SimpleNamespace(checked=True, files=files)


def _file(name, pickcode):
    return SimpleNamespace(
        name=name, is_directory=False, playback_values={"pickcode": pickcode},
    )


class ExportTreeIncrementTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.local_root = Path(self._tmp.name)
        self.cloud_root = "/媒体库"
        self.dirs = {
            "/媒体库": "100",
            "/媒体库/剧集": "200",
            "/媒体库/剧集/某剧/Season 1": "300",
        }
        self.listings = {
            "300": [_file("某剧.S01E01.mkv", "pc000000000001"),
                    _file("某剧.S01E02.mkv", "pc000000000002")],
        }

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, exporter, provider=None):
        generator = _Generator(self.local_root)
        provider = provider or _Provider(
            exporter, self.dirs, self.listings,
        )
        stats = rebuild_strm_by_export_tree(
            provider=provider,
            generator=generator,
            local_root=str(self.local_root),
            cloud_root=self.cloud_root,
            video_extensions={".mkv", ".mp4"},
            reference_values=lambda item: dict(item.playback_values),
        )
        return stats, generator, provider

    def test_unsupported_provider_reports_error(self):
        provider = _Provider(None, self.dirs, self.listings)
        stats = rebuild_strm_by_export_tree(
            provider=provider,
            generator=_Generator(self.local_root),
            local_root=str(self.local_root),
            cloud_root=self.cloud_root,
            video_extensions={".mkv"},
            reference_values=lambda item: dict(item.playback_values),
        )
        self.assertEqual(stats["errors"], 1)
        self.assertEqual(provider.list_calls, [])

    def test_diff_only_creates_missing(self):
        exporter = _Exporter([
            "/", "/剧集/", "/剧集/某剧/", "/剧集/某剧/Season 1/",
            "/剧集/某剧/Season 1/某剧.S01E01.mkv",
            "/剧集/某剧/Season 1/某剧.S01E02.mkv",
            "/剧集/某剧/Season 1/cover.jpg",
        ])
        stats, generator, provider = self._run(exporter)
        self.assertEqual(stats["files"], 2)
        self.assertEqual(stats["created"], 2)
        self.assertEqual(stats["errors"], 0)
        self.assertEqual(provider.list_calls, ["300"])
        self.assertEqual(len(generator.written), 2)
        created = sorted(p.name for p in self.local_root.rglob("*.strm"))
        self.assertEqual(created, ["某剧.S01E01.strm", "某剧.S01E02.strm"])

    def test_existing_strm_skipped_without_extra_list(self):
        target_dir = self.local_root / "剧集" / "某剧" / "Season 1"
        target_dir.mkdir(parents=True)
        (target_dir / "某剧.S01E01.strm").write_text("old", encoding="utf-8")
        exporter = _Exporter([
            "/剧集/某剧/Season 1/某剧.S01E01.mkv",
            "/剧集/某剧/Season 1/某剧.S01E02.mkv",
        ])
        stats, generator, provider = self._run(exporter)
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(stats["created"], 1)
        self.assertEqual(generator.written, [("/媒体库/剧集/某剧/Season 1", "某剧.S01E02.mkv")])

    def test_no_diff_makes_no_list_calls(self):
        target_dir = self.local_root / "剧集" / "某剧" / "Season 1"
        target_dir.mkdir(parents=True)
        (target_dir / "某剧.S01E01.strm").write_text("x", encoding="utf-8")
        exporter = _Exporter(["/剧集/某剧/Season 1/某剧.S01E01.mkv"])
        stats, generator, provider = self._run(exporter)
        self.assertEqual(provider.list_calls, [])
        self.assertEqual(stats["created"], 0)
        self.assertEqual(stats["skipped"], 1)

    def test_export_failure_returns_error_for_fallback(self):
        class _Boom:
            def export_directory_tree(self, directory_id, *, timeout_seconds=600.0):
                raise RuntimeError("115 导出失败")

        stats, _generator, provider = self._run(_Boom())
        self.assertEqual(stats["errors"], 1)
        self.assertEqual(provider.list_calls, [])


if __name__ == "__main__":
    unittest.main()
