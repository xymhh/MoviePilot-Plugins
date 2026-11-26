"""本地 .npz 线性分类器验证码模型的懒加载基类。

HDHive（动态 GIF，5 字符）与 Pinglian（静态图，4 字符）各写了一份
几乎相同的模型加载：线程安全懒加载 + ``np.load`` + ``format_version``
校验 + 公共键类型转换 + 失败包装。只有这一段抽出来；HOG 特征提取、
GIF 运动恢复、置信度聚合、输出校验、``HDHiveCaptchaSolver`` 全部留在
各源文件里——那两部分的维度和算法都不同，不能共用。

两个文件里都没有 "Turnstile" 这个词：这里抽的是本地 npz 线性模型
（HOG 特征 + coef/intercept）的加载，不是 Turnstile 求解。

差异点全部参数化，不在基类里写死任一源的值：
* 错误类与三段文案（缺失/版本/加载失败）由调用方注入；
* HDHive 的 bundle 多 ``threshold`` / ``min_component`` 两个键，
  由 ``extra_keys`` 注入；
* 依赖检查的时机不同：Pinglian 在查文件之前，HDHive 在查文件之后
  （且先打一条 debug 日志），由 ``check_dependencies_first`` +
  ``log_loading`` 控制；numpy 本身也是懒导入，基类不顶层 import，
  由 ``load_numpy`` 钩子在检查通过后返回；
* 成功日志级别与内容不同（debug+KB / info+阈值），由 ``log_loaded`` 注入。

默认模型路径不能由基类定：两处都是
``Path(__file__).with_name("captcha.bin")``，即各文件同目录下的
``captcha.bin``（两个文件的键集合不同，不可互换），必须由子类显式传。
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence, Tuple


class NpzLinearCaptchaModel:
    """本地 .npz 线性模型：懒加载 + 线性评分。

    子类只传参数，不覆写加载流程；``recognize`` 的特征提取与置信度
    聚合仍由子类自己实现。
    """

    def __init__(
        self,
        model_path,
        *,
        error_factory: Callable[..., Exception],
        load_numpy: Callable[[], Any],
        extra_keys: Sequence[Tuple[str, Callable[[Any], Any]]] = (),
        check_dependencies_first: bool = True,
        log_loading: Optional[Callable[[Path], None]] = None,
        log_loaded: Optional[Callable[[Dict[str, Any], Path], None]] = None,
        missing_message: Callable[[Path], str],
        version_message: Callable[[int], str],
        invalid_message: Callable[[Exception], str],
    ):
        self._model_path = Path(model_path)
        self._bundle: Optional[Dict[str, Any]] = None
        self._lock = threading.RLock()
        self._error_factory = error_factory
        self._load_numpy = load_numpy
        self._extra_keys = tuple(extra_keys)
        self._check_dependencies_first = bool(check_dependencies_first)
        self._log_loading = log_loading
        self._log_loaded = log_loaded
        self._missing_message = missing_message
        self._version_message = version_message
        self._invalid_message = invalid_message

    def _load(self) -> Dict[str, Any]:
        """线程安全懒加载，返回 bundle（已缓存则直接返回同一对象）。"""
        if self._bundle is not None:
            return self._bundle
        with self._lock:
            if self._bundle is not None:
                return self._bundle
            numpy_module = None
            if self._check_dependencies_first:
                numpy_module = self._load_numpy()
            if not self._model_path.is_file():
                raise self._error_factory(
                    self._missing_message(self._model_path),
                    code="captcha_model_missing",
                )
            if self._log_loading is not None:
                self._log_loading(self._model_path)
            if not self._check_dependencies_first:
                numpy_module = self._load_numpy()
            np = numpy_module
            try:
                with np.load(self._model_path, allow_pickle=False) as data:
                    version = int(data["format_version"].item())
                    if version != 1:
                        raise ValueError(self._version_message(version))
                    bundle = {
                        "classes": data["classes"].astype("<U1"),
                        "coef": data["coef"].astype(np.float32),
                        "intercept": data["intercept"].astype(np.float32),
                        "mean": data["mean"].astype(np.float32),
                        "scale": data["scale"].astype(np.float32),
                    }
                    for key, caster in self._extra_keys:
                        bundle[key] = caster(data[key])
                self._bundle = bundle
                if self._log_loaded is not None:
                    self._log_loaded(bundle, self._model_path)
            except Exception as error:
                raise self._error_factory(
                    self._invalid_message(error),
                    code="captcha_model_invalid",
                ) from error
            return self._bundle

    def ensure_ready(self) -> None:
        """完成模型存在性与格式检查（失败向外抛）。"""
        self._load()

    @property
    def is_ready(self) -> bool:
        """模型能否加载（失败吞掉异常返回 False）。"""
        try:
            self._load()
            return True
        except Exception:
            return False

    def classify(self, features):
        """线性评分：``(features-mean)/scale @ coef.T + intercept``。

        返回 ``(decisions, bundle)``；置信度聚合与输出校验由子类做。
        """
        bundle = self._load()
        normalized = (features - bundle["mean"]) / bundle["scale"]
        return normalized @ bundle["coef"].T + bundle["intercept"], bundle
