# 内置 aria2c 二进制说明

本目录放的是插件**随包分发**的 aria2c 可执行文件，用于"内置下载引擎"（1.8.0 起，默认关闭）。

## 文件

| 文件 | 说明 |
| --- | --- |
| `aria2c-linux-x86_64.xz` | aria2 1.37.0，Linux x86_64 **静态链接**构建，xz 压缩（3.4 MB，解开 9.2 MB） |
| `COPYING` | aria2 的 GPL-2.0 许可证原文（上游随包提供） |
| `LICENSE.OpenSSL` | 静态链接的 OpenSSL 许可证原文 |

## 校验

解压后二进制的 sha256：

```
36f66dab69edcc44255d0dba90c93f5aa4a304ec60c7136d8c279dfc89c23e1d
```

运行时插件会**先解压到 `/config/plugins/CloudSubscribeX/bin/aria2c`，再校验 sha256，最后用 `aria2c --version` 探针确认可执行**；任一环节失败即关闭内置引擎并回落插件自带下载器。

## 来源与构建

- 上游项目：https://github.com/aria2/aria2 （GPL-2.0）
- 二进制来源：https://github.com/dmesg00/aria2-static-builds/releases/tag/v1.37.0
  （资产 `aria2-1.37.0-linux-gnu-64bit-build1.tar.bz2`，静态构建，内含 OpenSSL）
- 本地处理：仅做 `strip --strip-all`（无符号裁剪）后 `xz -9` 压缩，未修改代码。上游该仓库公开其构建脚本，源码可从上游 tag `release-1.37.0` 取得。

## 许可义务（重要）

插件以**独立进程**方式调用该二进制（不链接、不派生），插件自身代码许可证不变；但**分发该二进制时须同时提供对应源码**。满足方式：

1. 本目录保留 `COPYING`；
2. 本文件给出上游 tag 与构建仓库地址，任何用户可据此取得源码；
3. 不修改二进制代码逻辑，仅 strip + 压缩。

停止分发即停止该义务；插件卸载时本目录随插件目录一并删除。
