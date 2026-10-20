# P2b TargetWriter 单测与静态检查

> 日期：2026-10-20
> 分支：`feat/p2b-target-writer`，最终提交 `e544500`
> 环境：本机 Python 3.11，未连接真实网盘

## 定向测试

```bash
python -m pytest tests/test_target_writer.py tests/test_cross_transfer_relay.py tests/test_rapid_miss_abort.py -q
```

结果：

```text
51 passed, 2 warnings in 3.67s
```

覆盖：

- `TargetWriter`：不存在/同大小同哈希复用/同大小不同哈希冲突/写后查回/写后未找到/并发只写一次/115 改名回填/查询异常。
- relay：目标已存在不提交中继、目标不存在提交一次、写后查不到失败、冲突不回落。
- local/progressive：目标已存在跳过 transfer、写入后真实名回填、源文件 sha1 冲突不调用上传。
- rapid abort：目标探测只在 fake 上传后出现，保留原 abort 语义。

## 全量测试

```bash
python -m pytest tests -q
```

结果：

```text
2 failed, 677 passed, 2 warnings, 3 subtests passed in 12.20s
```

失败两项均为既有环境问题，与本次改动无关：

- `tests/test_checkin_days.py::TestP115ClientCheckin::test_p115_checkin_already_checked_in_accumulates_points`
- `tests/test_checkin_days.py::TestP115ClientCheckin::test_p115_checkin_success_accumulates_points`

## Lint

```bash
python scripts/lint.py --all
```

结果：

```text
Checked 639 file(s), 0 error(s), 333 warning(s)
```

## 未覆盖

- 未做真实 115 e2e（删除目标后重传、真实自动改名、并发同名）。
- `tests/test_transfer_organize_behavior.py` 在本机 pytest 下有既有 collection 问题，未计入上述全量数字。
