# 任务书：P2b 统一目标写入器 TargetWriter

> 项目：`moviepilot-plugins-cs`（MoviePilot 插件 CloudSubscribeX）
> 基线：`1d70252`（P2a 已完成，线上验证通过）
> 分支：从 `1d70252` 新建 `feat/p2b-target-writer`，不要直接改 `main`
> 预估：2–3 天；先完成 relay 接线，再接 local upload
> 状态：待实施（2026-10-20）

## 1. 背景

当前同步链路已经有持久化 `sync_jobs` 账本（P1）和 done 目标回查（P2a）：

- `handlers/sync/service.py::_transfer_file` 负责认领账本、调用 `_transfer_file_impl`、成功后记账。
- `core/transfer.py::CrossTransferTaskManager` 负责跨盘传输；内部有多条最终写入路径：
  - `_relay_transfer`：深圳中继上传；
  - `_progressive_transfer`：边下载边上传；
  - `CrossDriveTransfer().transfer`：本地缓存下载后普通上传/秒传；
  - 另有 `upload_from_link` Range 直传路径。
- `_RELAY_TARGET_DONE` 是进程内 30 分钟缓存，只保护中继路径；进程重启、多进程、本地上传路径都不受保护。当前 `_relay_transfer` 在缓存命中时直接复用，甚至不查目标文件，存在“目标已被人删除但仍跳过上传”的正确性风险。

P2b 的目标：把所有**同步链路的最终目标写入**收敛到一个 `TargetWriter`，由它统一做「查目标 → 冲突决策 → 串行化 → 写入 → 写后核验 → 返回真实结果」。`_RELAY_TARGET_DONE` 不再参与正确性判断。

## 2. 范围

### 必须做

1. 新增 `plugins.v2/cloudsubscribex/core/target_writer.py`：
   - `TargetPlan`、`TargetResult`、`TargetConflictError`、`TargetUnknownError`、`TargetWriter`。
   - 进程级目标锁（同一 `provider + path + name` 串行）。
   - 写入前查目标；写入后按真实 `CloudFile` 核验大小/哈希。
   - 默认冲突策略 `skip`：同名不同内容不覆盖、不自动加 `(1)`，返回 `conflict`。
   - 不依赖任何进程内“已完成”缓存。
2. 中继路径 `_relay_transfer` 改为走 `TargetWriter.commit`：
   - 删除 `_RELAY_TARGET_DONE` 的使用；如无其它引用，删除该缓存及辅助函数。
   - 目标已存在且内容匹配 → 直接复用，不提交中继任务。
   - 目标不存在 → 提交中继；成功后由 `TargetWriter` 查回真实文件并核验。
   - 中继失败且允许回落 → 仍返回 `False`，保持现有本地回退行为。
   - 同名不同内容 / 查目录失败 → 抛 `TargetConflictError` / `TargetUnknownError`，由 `_run` 标记失败，**不得回退本地再上传一份**。
3. local upload 路径接入：
   - `_progressive_transfer` 整体包在 `TargetWriter.commit` 的 `write` 回调里；目标已存在则跳过下载/上传。
   - 普通本地路径中 `CrossDriveTransfer().transfer(...)` 包在 `TargetWriter.commit` 里；目标已存在则跳过真实上传。
   - `upload_from_link` Range 直传路径至少在写入前用 `TargetWriter.probe` 查目标；若目标已存在且匹配，直接复用。
4. 结果回填：
   - `TargetWriter` 返回的 `actual_name`/`file.id`/`size`/`sha1`/`md5` 必须用于任务 `result_*` 字段，不能再假设“目标名 == 请求名”。
5. 日志：
   - INFO 记录 `reused` / `written` / `conflict` / `unknown`，包含目标路径、请求名、真实名、job/任务 id；不得打印 cookie/token/分享链接。
6. 测试与文档：
   - 新增 `tests/test_target_writer.py`，至少覆盖第 5 节 8 条。
   - 更新 `docs/summary/` 下新增 P2b 总结（做了什么、没做什么、已知边界、测试数字）。
   - 不更新版本号、不打 tag、不发 Release；版本与发布由主 agent 复核后统一做。

### 不做（本阶段）

- 不改 `sync_jobs` 表结构/状态机；账本 claim 仍由 `service._transfer_file` 负责。
- 不改整理台改名/移动执行器。
- 不改重命名模板、UI、通知、订阅扫描逻辑。
- 不重构 `CrossTransferTaskManager` 的下载/缓存/进度体系。
- 不改 `_run` 的取消、缓存引用计数、中继超时语义（除新增目标冲突异常）。
- 不引入新依赖、不访问真实网盘、不改运行中的 MoviePilot 实例。

## 3. 接口契约

```python
@dataclass(frozen=True)
class TargetPlan:
    provider_key: str          # 目标网盘 key，如 "115"
    path: str                  # 目标目录，规范化后不带尾部 /
    name: str                  # 请求的目标文件名
    size: int = 0              # 期望大小；0 表示未知
    sha1: str = ""             # 期望 SHA1；空表示未知
    md5: str = ""              # 期望 MD5；空表示未知
    conflict_policy: str = "skip"  # 本阶段只实现 skip；replace/suffix 保留字段


@dataclass
class TargetResult:
    status: str                # reused | written | not_written | conflict | unknown
    file: CloudFile | None = None
    actual_name: str = ""
    actual_size: int = 0
    reason: str = ""


class TargetWriter:
    def __init__(self, provider_resolver, *, lock_registry=None): ...

    def probe(self, plan: TargetPlan) -> TargetResult:
        """只查目标，不写。exists+match→reused；exists+mismatch→conflict；
        查目录异常→unknown；不存在→not_written。"""

    def commit(self, plan: TargetPlan,
               write: Callable[[], CloudFile | bool | None]) -> TargetResult:
        """持目标锁：probe → 需要写则调用 write() → 写后核验。
        write 返回 False/None 表示未写入（允许上层回退），返回 True 或
        CloudFile 表示已写，随后必须查回真实文件。"""
```

### 匹配规则

- 目标存在时：
  - `plan.size > 0` 且 `file.size != plan.size` → 不匹配；
  - `plan.sha1` 和 `file.sha1` 都非空且不相等 → 不匹配；
  - `plan.md5` 和 `file.md5` 都非空且不相等 → 不匹配；
  - 其余情况（至少名称相同，大小未知或相等）→ 匹配。
- 写后核验：
  - `find_file(plan.path, plan.name)` 查回文件；
  - 若精确名查不到，允许查父目录一次，按 `size` + 非空哈希匹配真实名（解决 115 自动改名）；仍查不到 → `unknown`；
  - 查回文件大小与 `plan.size` 不一致 → `unknown`，不得标成功。
- 锁：
  - 进程级 `dict[str, Lock]`，key = `provider_key|normalized_path|name`；
  - 锁只包住 `probe + write + verify`，不包下载前的准备；
  - 同 key 并发第二个请求在拿到锁后必须重新 probe，不能复用第一个请求的旧判断。
- 异常：
  - `TargetConflictError` / `TargetUnknownError` 继承 `RuntimeError`；
  - `TargetWriter` 不吞 `write()` 抛出的异常；写后核验失败抛 `TargetUnknownError`；
  - `probe` 查目录异常返回 `unknown`，不抛，方便调用方决定是否重试。

## 4. 集成点

### 4.1 `_relay_transfer`

现状：`lock → _relay_target_recently_done → _relay_transfer_locked → _mark_relay_target_done`。

改为：

```python
plan = TargetPlan(
    provider_key=task["target_provider"],
    path=target_path,
    name=target_name,
    size=int(total or 0),
    sha1=str(checksum or ""),
)
result = self._target_writer.commit(
    plan,
    write=lambda: self._relay_transfer_locked(...),
)
```

- `reused`：把任务标 success，`method="relay-reuse"`，`result_*` 用 `result.file` 的真实字段。
- `written`：`_relay_transfer_locked` 仍负责中继提交/轮询；写后核验由 `TargetWriter` 做。避免两边重复更新；建议让 `_relay_transfer_locked` 只返回 `CloudFile`/`False`，任务状态更新统一在 `_relay_transfer` 完成。
- `not_written`：返回 `False`，保持 `_run` 的本地回退。
- `conflict`/`unknown`：抛异常，`_run` 现有 `except Exception` 会标记 failed；不要回退本地。

### 4.2 `_progressive_transfer`

在 `_run` 调用处包一层：

```python
progressive_result = self._target_writer.commit(
    plan,
    write=lambda: self._progressive_transfer(...),
)
```

- `reused`：跳过 `_progressive_transfer`，直接标 success，`method="reuse"`。
- `not_written`：继续原有本地缓存/普通上传分支。
- `written`：使用 `result.file` 回填；`_progressive_transfer` 内部仍可做进度更新。
- 注意：`commit` 的锁会覆盖下载+上传；同目标并发第二个任务会等待，拿到锁后 probe 到文件并复用，不会重复下载。

### 4.3 普通本地路径

把 `CrossDriveTransfer().transfer(...)` 调用包进 `commit`：

- `reused`：跳过上传，直接 success。
- `written`：用 `result.file` 回填，`method` 由 `write` 返回值/`TargetResult` 保留 `rapid`/`fallback` 信息。
- `not_written`：沿用现有“秒传未命中且普通上传失败”的错误路径。

### 4.4 Range 直传 `upload_from_link`

在调用前 `probe`：

- `reused`：跳过直传，标 success。
- `conflict`/`unknown`：抛异常或回退到本地缓存（按现有语义，优先不回退，避免重复）。
- `not_written`：继续直传。

## 5. 测试要求

新增 `tests/test_target_writer.py`，用 fake provider/`find_file`，不访问网络：

1. `probe` 目标不存在 → `not_written`。
2. 同名同大小 → `reused`，不调用 `write`。
3. 同名同大小 + sha1 一致 → `reused`。
4. 同名不同大小 → `conflict`，不调用 `write`。
5. 同名同大小但 sha1 不同 → `conflict`。
6. `write` 返回 True 且写后可查回 → `written`，`actual_name`/`file.id` 正确。
7. `write` 返回 False → `not_written`，允许上层回退。
8. 写后查不到 → `unknown`（抛 `TargetUnknownError` 或返回 unknown，按契约固定一种）。
9. 两个线程同目标并发：第一个写、第二个复用，`write` 只调用一次。
10. 写后 115 自动改名为 `name (2).ext`：父目录按大小匹配，`actual_name` 返回真实名。
11. `probe` 查目录抛异常 → `unknown`，不写。
12. relay 集成：目标已存在时 `FakeRelay.submit` 不被调用；目标不存在时提交一次。
13. relay 写后找不到文件 → 任务 failed，不标记 success。
14. 本地路径集成：目标已存在时 `CrossDriveTransfer.transfer` 不被调用。
15. 旧 `_RELAY_TARGET_DONE` 不再影响行为：清空/填充缓存都不改变结果（若已删除，测试改为断言无该符号或行为不依赖）。

同时更新既有测试：
- `tests/test_cross_transfer_relay.py` 中 `_RELAY_TARGET_DONE.clear()` 若符号删除，改成新接口准备。
- 不要削弱现有 relay 成功/失败/回落断言。

## 6. 验收命令

```bash
cd /app/working/workspaces/default/moviepilot-plugins-cs
python -m pytest tests/test_target_writer.py -q
python -m pytest tests/test_cross_transfer_relay.py tests/test_sync_job_ledger.py tests/test_sync_transfer_ledger_wiring.py -q
python -m pytest tests -q          # 期望 653+ 通过，仅允许既有 2 例 p115client 环境失败
python scripts/lint.py --all       # 0 error
```

## 7. 交付物

- `feat/p2b-target-writer` 分支上的代码 + 测试 + `docs/summary/2026-10-20-TargetWriterP2b.md`。
- 每个里程碑一个中文 commit：
  1. `feat: 新增 TargetWriter 目标写入器`
  2. `refactor: 中继路径接入 TargetWriter`
  3. `refactor: 本地上传路径接入 TargetWriter`
- 最终汇报必须包含：改了哪些文件、测试命令与真实输出、未覆盖边界、是否有行为变化。
- 不要 push、不要发 Release、不要改版本号；主 agent 复核后统一处理。

## 8. 禁止事项

- 不要读取/打印真实 cookie、token、分享链接、115 用户 ID。
- 不要调用真实网盘 API；测试全部用 fake。
- 不要为了让测试过而放宽断言、跳过测试或删除既有用例。
- 不要改无关文件、不要全文件格式化、不要引入新依赖。
- 不要把 `_RELAY_TARGET_DONE` 仅改名为持久化缓存却继续作为正确性依据；正确性必须以目标实际查询为准。
- 不要在 handler 里新增 `upload_file` / `upload_progressive` / `relay.submit` 的最终目标直调；新写入必须经过 `TargetWriter`。

## 9. 里程碑

1. **M1（约 1 天）**：`TargetWriter` + 单测 1–11；不接线。
2. **M2（约 1 天）**：relay 接线 + 测试 12–13；删除 `_RELAY_TARGET_DONE` 依赖。
3. **M3（约 1 天）**：local/progressive/Range 接线 + 测试 14–15；全量 + lint。

如果 M2 发现 relay 状态更新耦合过深，允许先提交 M1+M2，M3 单独评估；不要为了赶工把 local 路径改坏。
