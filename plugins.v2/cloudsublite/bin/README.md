# 内置 aria2c 二进制说明

本目录放的是插件**随包分发**的 aria2c 可执行文件，用于"内置下载引擎"（1.8.0 起，默认关闭）。

## 文件

| 文件 | 说明 |
| --- | --- |
| `aria2c-linux-x86_64.xz` | aria2 **1.36.0 + Aria2-Pro 补丁**，Linux x86_64 静态链接，xz 压缩（3.3 MB，解开 9.9 MB） |
| `COPYING` | aria2 的 GPL-2.0 许可证原文 |
| `LICENSE.OpenSSL` | 静态链接的 OpenSSL 许可证原文 |

## 校验

解压后二进制的 sha256：

```
c7944f5e27212e0bd37b761f888039eea6e5cf29342e2f86d3f0075118586432
```

运行时插件会**先解压到 `/config/plugins/CloudSubscribeX/bin/aria2c`，再校验 sha256，最后用 `aria2c --version` 探针确认可执行**；任一环节失败即关闭内置引擎并回落插件自带下载器。

## 为什么用补丁版（而不是上游 1.37.0）

上游 aria2 把 `--max-connection-per-server` 的合法区间写死为 `1..16`
（`src/OptionHandlerFactory.cc:441-443`），传 64 会被 JSON-RPC 直接拒绝（HTTP 400）。
跨盘转存要 64+ 连接才有速度，所以改用 [P3TERX/Aria2-Pro-Core](https://github.com/P3TERX/Aria2-Pro-Core)
的静态构建（1.36.0，2021-08-22 release），它的补丁把该上限改为无上限、`min-split-size` 下限改为 1K。

实测（本机，限速服务端，单文件单服务器）：

| 配置 | aria2 自报连接 | 服务端并发 |
| --- | --- | --- |
| `x=16` | 16 | 16 |
| `x=64` | 64 | 44 |
| `x=96` | 96 | 98 |

## 来源与构建

- 上游项目：https://github.com/aria2/aria2 （GPL-2.0），tag `release-1.36.0`
- 补丁与构建：https://github.com/P3TERX/Aria2-Pro-Core
  - 二进制资产：`1.36.0_2021.08.22/aria2-1.36.0-static-linux-amd64.tar.gz`
  - 补丁清单：`patch/0001-options-unlock-connection-per-server-limit.patch`、
    `0002-download-retry-on-slow-speed-and-reset.patch`、
    `0003-option-add-option-to-retry-on-http-4xx.patch`、
    `0004-option-add-option-to-disable-want-digest-header.patch`
  - 构建脚本：仓库内 `aria2-gnu-linux-build.sh`
- 本地处理：仅 `xz -9` 压缩，未做任何修改（二进制自带 strip）。

## 许可义务

插件以**独立进程**方式调用该二进制（不链接、不派生），插件自身代码许可证不变；但**分发该二进制时须同时提供对应源码**。满足方式：

1. 本目录保留 `COPYING`；
2. 本文件给出上游 tag、补丁仓库与补丁文件名，任何用户可据此取得完整源码；
3. 未修改二进制本身。

停止分发即停止该义务；插件卸载时本目录随插件目录一并删除。
