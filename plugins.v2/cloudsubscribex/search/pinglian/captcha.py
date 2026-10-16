"""盘链验证码识别。 """

from __future__ import annotations

import threading
from io import BytesIO
from pathlib import Path
from typing import List, Optional, Tuple, Union

try:
    import numpy as np
    from PIL import Image
except ImportError:
    np = None
    Image = None

from app.log import logger

# 图像尺寸与插槽切分参数
IMAGE_WIDTH = 150
IMAGE_HEIGHT = 50
SLOT_BOUNDS = [
    (5, 40),
    (40, 75),
    (75, 110),
    (110, 145),
]
SLOT_Y_BOUNDS = (4, 46)
GLYPH_SIZE = (32, 32)

# HOG 参数：4x4 单元格，8方向，2x2 块归一化 (288 维)
HOG_CELL_SIZE = 8
HOG_ORIENTATIONS = 8
HOG_CELLS_Y = GLYPH_SIZE[1] // HOG_CELL_SIZE  # 4
HOG_CELLS_X = GLYPH_SIZE[0] // HOG_CELL_SIZE  # 4
HOG_BLOCK_SIZE = 2
HOG_BLOCKS_Y = HOG_CELLS_Y - HOG_BLOCK_SIZE + 1  # 3
HOG_BLOCKS_X = HOG_CELLS_X - HOG_BLOCK_SIZE + 1  # 3


class PinglianCaptchaError(RuntimeError):
    """盘链验证码模型或识别异常。"""

    def __init__(self, message: str, code: str = "captcha_error"):
        super().__init__(message)
        self.code = code


def _check_dependencies():
    if np is None or Image is None:
        raise PinglianCaptchaError(
            "缺少 NumPy 或 Pillow 依赖，无法运行验证码识别器",
            code="captcha_dependency_missing",
        )


def _preprocess_image(image_input: Union[bytes, Image.Image]) -> np.ndarray:
    """从验证码图像提取前景文字密度 (0.0~1.0)。"""
    if isinstance(image_input, bytes):
        pil_im = Image.open(BytesIO(image_input)).convert("RGB")
    elif isinstance(image_input, Image.Image):
        pil_im = image_input.convert("RGB")
    else:
        raise TypeError(f"不支持的图像输入类型: {type(image_input)}")

    arr = np.asarray(pil_im, dtype=np.float32)
    # 盘链基准底色淡蓝白 #F3F7FF -> (243, 247, 255)
    bg_ref = np.array([243.0, 247.0, 255.0], dtype=np.float32)
    dist = np.sqrt(np.sum((arr - bg_ref) ** 2, axis=2))
    foreground = np.clip((dist - 15.0) / 100.0, 0.0, 1.0)
    return foreground


def _extract_slots(foreground: np.ndarray) -> List[np.ndarray]:
    """切分 4 个字符槽位并归一化为 32x32 单字符网格。"""
    slots = []
    y_min, y_max = SLOT_Y_BOUNDS
    for x_min, x_max in SLOT_BOUNDS:
        slot_raw = foreground[y_min:y_max, x_min:x_max]
        slot_im = Image.fromarray((slot_raw * 255.0).astype(np.uint8))
        resized = slot_im.resize(GLYPH_SIZE, Image.Resampling.BILINEAR)
        slots.append(np.asarray(resized, dtype=np.float32) / 255.0)
    return slots


def _compute_hog(image: np.ndarray) -> np.ndarray:
    """纯 NumPy 计算单字符 32x32 的 288 维 HOG 局部梯度直方图。"""
    gy, gx = np.gradient(image)
    mag = np.hypot(gx, gy)
    ang = (np.arctan2(gy, gx) % np.pi) * (HOG_ORIENTATIONS / np.pi)

    histograms = np.zeros((HOG_CELLS_Y, HOG_CELLS_X, HOG_ORIENTATIONS), dtype=np.float32)
    for cy in range(HOG_CELLS_Y):
        for cx in range(HOG_CELLS_X):
            cell_mag = mag[
                cy * HOG_CELL_SIZE: (cy + 1) * HOG_CELL_SIZE, cx * HOG_CELL_SIZE: (cx + 1) * HOG_CELL_SIZE].ravel()
            cell_ang = ang[
                cy * HOG_CELL_SIZE: (cy + 1) * HOG_CELL_SIZE, cx * HOG_CELL_SIZE: (cx + 1) * HOG_CELL_SIZE].ravel()
            bin_idx = cell_ang.astype(np.int32) % HOG_ORIENTATIONS
            bin_frac = cell_ang - bin_idx
            np.add.at(histograms[cy, cx], bin_idx, cell_mag * (1.0 - bin_frac))
            np.add.at(histograms[cy, cx], (bin_idx + 1) % HOG_ORIENTATIONS, cell_mag * bin_frac)

    blocks = []
    eps = 1e-5
    for by in range(HOG_BLOCKS_Y):
        for bx in range(HOG_BLOCKS_X):
            block = histograms[by: by + HOG_BLOCK_SIZE, bx: bx + HOG_BLOCK_SIZE].ravel()
            norm = np.sqrt(np.sum(block ** 2) + eps)
            block_normed = block / norm
            block_normed = np.clip(block_normed, 0.0, 0.2)
            block_normed /= np.sqrt(np.sum(block_normed ** 2) + eps)
            blocks.append(block_normed)

    return np.concatenate(blocks)


class PinglianCaptchaRecognizer:
    """盘链验证码识别器"""

    def __init__(self, model_path: Optional[Path] = None):
        self._model_path = model_path or Path(__file__).with_name("captcha.bin")
        self._bundle: Optional[dict[str, np.ndarray]] = None
        self._lock = threading.RLock()

    def _ensure_loaded(self) -> dict[str, np.ndarray]:
        if self._bundle is not None:
            return self._bundle

        with self._lock:
            if self._bundle is not None:
                return self._bundle

            _check_dependencies()
            if not self._model_path.is_file():
                raise PinglianCaptchaError(
                    f"盘链验证码模型不存在：{self._model_path}",
                    code="captcha_model_missing",
                )

            try:
                with np.load(self._model_path, allow_pickle=False) as data:
                    version = int(data["format_version"].item())
                    if version != 1:
                        raise ValueError(f"不支持的验证码模型格式版本：{version}")

                    bundle = {
                        "classes": data["classes"].astype("<U1"),
                        "coef": data["coef"].astype(np.float32),
                        "intercept": data["intercept"].astype(np.float32),
                        "mean": data["mean"].astype(np.float32),
                        "scale": data["scale"].astype(np.float32),
                    }
                self._bundle = bundle
                logger.debug(
                    f"盘链验证码模型加载成功：类别数 {len(bundle['classes'])}，"
                    f"体积 {self._model_path.stat().st_size / 1024:.1f} KB"
                )
            except Exception as error:
                raise PinglianCaptchaError(
                    f"盘链验证码模型加载失败：{error}",
                    code="captcha_model_invalid",
                ) from error

            return self._bundle

    @property
    def is_ready(self) -> bool:
        try:
            self._ensure_loaded()
            return True
        except Exception:
            return False

    def recognize(self, image_bytes: bytes) -> Tuple[str, float]:
        """识别 4 字符验证码，返回 (预测文本, 置信度评分)。"""
        bundle = self._ensure_loaded()
        foreground = _preprocess_image(image_bytes)
        slots = _extract_slots(foreground)

        features = np.stack([_compute_hog(slot) for slot in slots])
        normalized = (features - bundle["mean"]) / bundle["scale"]
        scores = normalized @ bundle["coef"].T + bundle["intercept"]

        best_indices = np.argmax(scores, axis=1)
        classes = bundle["classes"]
        predicted = "".join([str(classes[idx]) for idx in best_indices])

        sorted_scores = np.sort(scores, axis=1)
        margins = sorted_scores[:, -1] - sorted_scores[:, -2]
        confidence = float(np.mean(margins))

        return predicted, confidence
