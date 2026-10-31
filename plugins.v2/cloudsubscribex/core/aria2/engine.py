"""aria2 daemon 生命周期：惰性启动、探活、失败重启与退出清理。

单进程独占，生命周期跟随插件：第一次有下载任务时才拉起，MP 退出后由
``--stop-with-process`` 兜底自灭。secret 只在内存，不落盘、不写日志。
"""

from __future__ import annotations

import os
import secrets
import socket
import subprocess
import time
from pathlib import Path
from threading import Lock
from typing import Optional

from app.log import logger

from .binary import ensure_binary
from .rpc import Aria2Error, Aria2Rpc


class Aria2Engine:
    """单个 aria2 daemon 的进程与 RPC 句柄。"""

    READY_TIMEOUT = 10.0
    READY_INTERVAL = 0.2

    def __init__(self, settings):
        self._settings = settings
        self._lock = Lock()
        self._process: Optional[subprocess.Popen] = None
        self._rpc_client: Optional[Aria2Rpc] = None
        self._ready = False
        self._restart_budget = 2
        self._warned = False

    @property
    def ready(self) -> bool:
        process = self._process
        return bool(
            self._ready
            and process is not None
            and process.poll() is None
        )

    def ensure_started(self) -> bool:
        """幂等启动；失败返回 False 并最多记一次 warning。"""
        with self._lock:
            if self.ready:
                return True
            if self._restart_budget <= 0:
                return False

            self._stop_locked()
            ok = self._start_locked()
            if not ok:
                self._restart_budget -= 1
                self._stop_locked()
                if not self._warned:
                    logger.warning(
                        "aria2 下载引擎启动失败，后续将回落到内置下载器"
                    )
                    self._warned = True
            return ok

    def rpc(self) -> Aria2Rpc:
        if not self.ensure_started() or self._rpc_client is None:
            raise Aria2Error(-1, "engine-not-ready")
        return self._rpc_client

    def stop(self) -> None:
        """幂等停止；terminate → wait(3) → kill。"""
        with self._lock:
            self._stop_locked()

    def _start_locked(self) -> bool:
        data_dir = Path(self._settings.data_dir)
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            logger.warning(f"创建 aria2 数据目录失败：{error}")
            return False

        binary, reason = ensure_binary(data_dir / "bin")
        if binary is None:
            logger.warning(f"aria2 二进制不可用（{reason}），回落内置下载器")
            return False

        # 端口冲突概率极低，允许换端口重试一次。
        for _attempt in range(2):
            port = self._free_port()
            secret = secrets.token_hex(16)
            command = self._build_command(binary, port, secret, data_dir)
            try:
                self._process = subprocess.Popen(
                    command,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except Exception as error:  # noqa: BLE001 - 启动失败由上层回落
                logger.warning(f"拉起 aria2 进程失败：{error}")
                self._process = None
                continue
            self._rpc_client = Aria2Rpc(port, secret)
            if self._probe_ready():
                self._ready = True
                logger.info(
                    f"aria2 下载引擎已就绪：端口 {port}，"
                    f"并发 {self._settings.max_concurrent}，"
                    f"连接 {self._settings.connections}"
                )
                return True
            self._stop_locked()
        return False

    def _probe_ready(self) -> bool:
        deadline = time.monotonic() + self.READY_TIMEOUT
        last_error = None
        while time.monotonic() < deadline:
            process = self._process
            if process is None or process.poll() is not None:
                return False
            rpc = self._rpc_client
            if rpc is not None:
                try:
                    rpc.get_version()
                    return True
                except Aria2Error as error:
                    last_error = error
            time.sleep(self.READY_INTERVAL)
        if last_error is not None:
            logger.debug(
                f"aria2 RPC 探活超时，最后一次错误：{last_error}", exc_info=True
            )
        return False

    def _stop_locked(self) -> None:
        rpc = self._rpc_client
        self._rpc_client = None
        self._ready = False
        if rpc is not None:
            try:
                rpc.shutdown()
            except Exception as error:  # noqa: BLE001 - 进程可能已退出
                logger.debug(f"aria2 shutdown 调用失败：{error}", exc_info=True)
        process = self._process
        self._process = None
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=3)
        except Exception:  # noqa: BLE001 - terminate 失败时强杀
            try:
                process.kill()
                process.wait(timeout=3)
            except Exception as error:  # noqa: BLE001 - 进程可能已在退出
                logger.debug(f"aria2 强杀失败：{error}", exc_info=True)

    @staticmethod
    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def _build_command(
            self, binary: Path, port: int, secret: str, data_dir: Path,
    ) -> list[str]:
        settings = self._settings
        return [
            str(binary),
            "--enable-rpc",
            "--rpc-listen-all=false",
            f"--rpc-listen-port={port}",
            f"--rpc-secret={secret}",
            f"--dir={data_dir}",
            "--file-allocation=none",
            "--continue=true",
            f"--max-concurrent-downloads={settings.max_concurrent}",
            "--max-connection-per-server=16",
            "--split=16",
            f"--min-split-size={settings.min_split_size}",
            f"--disk-cache={settings.disk_cache}",
            "--auto-file-renaming=false",
            "--allow-overwrite=true",
            "--check-certificate=true",
            "--follow-torrent=false",
            "--enable-dht=false",
            "--bt-enable-lpd=false",
            "--console-log-level=warn",
            "--summary-interval=0",
            "--quiet=true",
            f"--log={data_dir / 'aria2.log'}",
            f"--save-session={data_dir / 'aria2.session'}",
            "--save-session-interval=30",
            f"--stop-with-process={os.getpid()}",
        ]
