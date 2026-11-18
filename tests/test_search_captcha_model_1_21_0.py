"""搜索源瘦身 v1.21.0：第 8 项——npz 验证码模型加载抽取。

覆盖：Pinglian / HDHive 两家识别器经共用基类
``NpzLinearCaptchaModel`` 加载真实 .npz 模型文件时的行为与旧代码一致：
公共键类型转换、HDHive 额外键、缺失/版本错误/损坏模型的错误码、
双重检查锁缓存、``is_ready``/``ensure_ready`` 语义、``classify`` 线性评分
数学、默认模型路径仍指向各源自己的 captcha.bin。
HOG 特征、GIF 运动恢复、Solver 不在抽取范围，不测。
"""

import sys
import tempfile
import types
import unittest
from pathlib import Path

import numpy as np

from plugin_env import (
    ensure_package,
    install_app_mocks,
    load_module,
)

install_app_mocks(
    "app.log",
    "cloudsubscribex",
    "cloudsubscribex.search",
)

_SAVED_MODULES = {}
for _name in (
    "cloudsubscribex.search.captcha_model",
    "cloudsubscribex.search.pinglian.captcha",
    "cloudsubscribex.search.hdhive.web.captcha",
    "cloudsubscribex.search.hdhive.web.action",
    "cloudsubscribex.search.hdhive.web.parser",
):
    _SAVED_MODULES[_name] = sys.modules.get(_name)


def tearDownModule():
    for _name, _mod in _SAVED_MODULES.items():
        if _mod is None:
            sys.modules.pop(_name, None)
        else:
            sys.modules[_name] = _mod


load_module(
    "cloudsubscribex.search.captcha_model",
    "plugins.v2/cloudsubscribex/search/captcha_model.py",
)
ensure_package("cloudsubscribex.search.pinglian")
pinglian_module = load_module(
    "cloudsubscribex.search.pinglian.captcha",
    "plugins.v2/cloudsubscribex/search/pinglian/captcha.py",
)
PinglianCaptchaRecognizer = pinglian_module.PinglianCaptchaRecognizer
PinglianCaptchaError = pinglian_module.PinglianCaptchaError

ensure_package("cloudsubscribex.search.hdhive")
ensure_package("cloudsubscribex.search.hdhive.web")


def _mock_submodule(name, **attrs):
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod
    return mod


# captcha.py 只在类型层面引用 action/parser；识别器测试不需要真实实现。
_mock_submodule(
    "cloudsubscribex.search.hdhive.web.action",
    ServerActionProtocol=object,
    ServerActionResponse=object,
)
_mock_submodule(
    "cloudsubscribex.search.hdhive.web.parser",
    decode_embedded_text=lambda *args, **kwargs: "",
    response_body=lambda *args, **kwargs: b"",
    response_text=lambda *args, **kwargs: "",
)
hdhive_module = load_module(
    "cloudsubscribex.search.hdhive.web.captcha",
    "plugins.v2/cloudsubscribex/search/hdhive/web/captcha.py",
)
HDHiveCaptchaRecognizer = hdhive_module.HDHiveCaptchaRecognizer
HDHiveCaptchaError = hdhive_module.HDHiveCaptchaError

CLASSES = np.array([str(i) for i in range(10)] + ["A", "B"])
FEATURE_DIM = 288


def _write_model(path, *, version=1, drop=(), extra=False):
    data = {
        "format_version": np.array([version]),
        "classes": CLASSES,
        "coef": np.arange(len(CLASSES) * FEATURE_DIM, dtype=np.float64).reshape(
            len(CLASSES), FEATURE_DIM
        ) / 1000.0,
        "intercept": np.arange(len(CLASSES), dtype=np.float64),
        "mean": np.full(FEATURE_DIM, 0.5),
        "scale": np.full(FEATURE_DIM, 2.0),
    }
    if extra:
        data["threshold"] = np.array([0.025])
        data["min_component"] = np.array([18])
    for key in drop:
        data.pop(key, None)
    # 传文件对象：np.savez 对字符串路径会自动补 .npz 后缀。
    with open(path, "wb") as handle:
        np.savez(handle, **data)


class CaptchaModelTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_pinglian_loads_and_caches_bundle(self):
        model = self.dir / "captcha.bin"
        _write_model(model)
        recognizer = PinglianCaptchaRecognizer(model)
        bundle = recognizer._ensure_loaded()
        self.assertEqual(
            set(bundle), {"classes", "coef", "intercept", "mean", "scale"}
        )
        self.assertEqual(bundle["classes"].dtype, np.dtype("<U1"))
        self.assertEqual(bundle["coef"].dtype, np.float32)
        self.assertTrue(recognizer.is_ready)
        self.assertIs(recognizer._ensure_loaded(), bundle)

    def test_hdhive_loads_extra_keys(self):
        model = self.dir / "captcha.bin"
        _write_model(model, extra=True)
        recognizer = HDHiveCaptchaRecognizer(model)
        self.assertIsNone(recognizer.ensure_ready())
        bundle = recognizer._model()
        self.assertEqual(bundle["threshold"], 0.025)
        self.assertEqual(bundle["min_component"], 18)
        self.assertIs(recognizer._model(), bundle)

    def test_missing_model_error_code(self):
        missing = self.dir / "nope.bin"
        with self.assertRaises(PinglianCaptchaError) as ctx:
            PinglianCaptchaRecognizer(missing)._ensure_loaded()
        self.assertEqual(ctx.exception.code, "captcha_model_missing")
        self.assertFalse(PinglianCaptchaRecognizer(missing).is_ready)
        with self.assertRaises(HDHiveCaptchaError) as ctx:
            HDHiveCaptchaRecognizer(missing)._model()
        self.assertEqual(ctx.exception.code, "captcha_model_missing")

    def test_bad_version_wrapped_as_invalid(self):
        model = self.dir / "captcha.bin"
        _write_model(model, version=2)
        with self.assertRaises(PinglianCaptchaError) as ctx:
            PinglianCaptchaRecognizer(model)._ensure_loaded()
        self.assertEqual(ctx.exception.code, "captcha_model_invalid")
        self.assertIn("2", str(ctx.exception))
        with self.assertRaises(HDHiveCaptchaError) as ctx:
            HDHiveCaptchaRecognizer(model)._model()
        self.assertEqual(ctx.exception.code, "captcha_model_invalid")
        self.assertIn("不支持的模型格式", str(ctx.exception))

    def test_missing_key_wrapped_as_invalid(self):
        model = self.dir / "captcha.bin"
        _write_model(model, drop=("scale",))
        with self.assertRaises(PinglianCaptchaError) as ctx:
            PinglianCaptchaRecognizer(model)._ensure_loaded()
        self.assertEqual(ctx.exception.code, "captcha_model_invalid")

    def test_classify_matches_manual_scoring(self):
        model = self.dir / "captcha.bin"
        _write_model(model)
        recognizer = PinglianCaptchaRecognizer(model)
        features = np.random.default_rng(7).normal(
            size=(3, FEATURE_DIM)
        ).astype(np.float32)
        decisions, bundle = recognizer.classify(features)
        expected = (
            (features - bundle["mean"]) / bundle["scale"]
        ) @ bundle["coef"].T + bundle["intercept"]
        np.testing.assert_allclose(decisions, expected, rtol=1e-5)
        self.assertEqual(decisions.shape, (3, len(CLASSES)))

    def test_default_model_path_stays_per_source(self):
        pinglian = PinglianCaptchaRecognizer()
        self.assertEqual(pinglian._model_path.name, "captcha.bin")
        self.assertEqual(pinglian._model_path.parent.name, "pinglian")
        hdhive = HDHiveCaptchaRecognizer()
        self.assertEqual(hdhive._model_path.name, "captcha.bin")
        self.assertEqual(hdhive._model_path.parent.name, "web")

    def test_recognize_with_missing_model_fails_before_image(self):
        # 模型缺失时 recognize 应在图片处理之前就报模型缺失。
        missing = self.dir / "nope.bin"
        with self.assertRaises(PinglianCaptchaError) as ctx:
            PinglianCaptchaRecognizer(missing).recognize(b"not-an-image")
        self.assertEqual(ctx.exception.code, "captcha_model_missing")


if __name__ == "__main__":
    unittest.main()
