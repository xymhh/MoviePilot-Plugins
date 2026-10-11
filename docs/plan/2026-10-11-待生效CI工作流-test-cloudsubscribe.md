# 待生效：CI 工作流 `test-cloudsubscribe.yml`（网盘订阅助手单测）

- 日期：2026-10-11
- 状态：**尚未推送到仓库**（文件内容见文末，本地已备好）
- 原因：推送用 GitHub token 只具备 `repo` 权限，GitHub 拒绝「用无 `workflow` 权限的 PAT 创建/更新 `.github/workflows/*`」：

  ```
  ! [remote rejected] main -> main (refusing to allow a Personal Access Token to create or update
    workflow `.github/workflows/test-cloudsubscribe.yml` without `workflow` scope)
  ```

- 生效方式（二选一）：
  1. 给 token 勾上 `workflow` 权限（GitHub → Settings → Developer settings → Personal access tokens → 该 token → `workflow`），
     回复 agent 一句即可，agent 会补推这个文件；
  2. 自己在网页上新建 `.github/workflows/test-cloudsubscribe.yml`，内容照抄下方 YAML。
- 落地后效果：改动 `plugins.v2/cloudsubscribe/**` 或顶层 `tests/**` 时自动跑 95 个单测（Python 3.12，对应该插件 V2 运行环境）。
- 注：本次合并不依赖该工作流 —— 插件功能、发布流程（`plugins-release.yml` 已存在）都不受影响。

````yaml
name: Test cloudsubscribe (V2)

on:
  push:
    paths:
      - 'plugins.v2/cloudsubscribe/**'
      - 'tests/**'
      - '.github/workflows/test-cloudsubscribe.yml'
  pull_request:
    paths:
      - 'plugins.v2/cloudsubscribe/**'
      - 'tests/**'
      - '.github/workflows/test-cloudsubscribe.yml'
  workflow_dispatch:

jobs:
  test:
    name: test (ubuntu-latest)
    runs-on: ubuntu-latest
    steps:
      - name: Checkout plugin code
        uses: actions/checkout@v7

      - name: Set up Python 3.12
        uses: actions/setup-python@v5
        with:
          # 网盘订阅助手运行在 MoviePilot V2（system_version >= 2.14.6），
          # 其容器使用 Python 3.12，测试在这条版本线上跑才有意义。
          python-version: '3.12'
          cache: 'pip'
          cache-dependency-path: plugins.v2/cloudsubscribe/requirements.txt

      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install -r plugins.v2/cloudsubscribe/requirements.txt

      - name: Run tests
        run: |
          python -m unittest discover -s tests -p "test_*.py" -v
````
