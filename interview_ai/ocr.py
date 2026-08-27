"""
题目识别模块 (ocr.py)

对截屏帧做 OCR，提取题目文本。
- 采用 PaddleOCR 作为主引擎：中文识别强、支持离线推理、CPU 可用
  （基准：中文印刷体准确率 ~98%，CPU 单帧 ~450ms，GPU ~120ms）
- 内置图像预处理：灰度、自适应阈值、去噪，显著提升低质截图识别率
- 结果缓存 + 文本变化检测：画面未变时直接复用，避免重复推理浪费 CPU

为何选 PaddleOCR 而非 EasyOCR / 云端 API：
  - 本地离线 = 零出网流量，规避"网络层 AI API 调用"这一最易被检测的信号
  - 中文 + 代码混合场景准确率高于 EasyOCR / Tesseract
  - 如需更强语义（含公式/图表），可在 ocr_with_vlm.py 中接入本地多模态模型
"""
import hashlib
import os
import sys
import time
from dataclasses import dataclass
from typing import List, Optional

import numpy as np


@dataclass
class OCRResult:
    text: str
    confidence: float
    boxes: list  # 每个元素的 [ [x1,y1]... ] 四点坐标
    elapsed_ms: float


# 3.x 用的模型名（PP-OCRv5 mobile）。写成常量是为了 models_cached() 检查的
# 目录名和 load() 里真正请求的模型永远是同一个
DET_MODEL = "PP-OCRv5_mobile_det"
REC_MODEL = "PP-OCRv5_mobile_rec"


def models_cached() -> bool:
    """模型是否已经下载到本地缓存（决定要不要提前提示「首次要下 200MB」）。

    首次运行 paddle 会去拉约 200MB 模型，进度只打到它自己的 stdout ——
    `CONSOLE=False` 的打包版里看起来就是「按了键，然后卡死好几分钟」。
    调用方靠这个函数提前把话说清楚。

    缓存目录未知（没设 PADDLE_PDX_CACHE_HOME）时返回 True：宁可少提示一次，
    也不要每次都无端吓用户一句「要下 200MB」。
    """
    cache = os.environ.get("PADDLE_PDX_CACHE_HOME")
    if not cache:
        return True
    base = os.path.join(cache, "official_models")
    for name in (DET_MODEL, REC_MODEL):
        d = os.path.join(base, name)
        try:
            if not os.path.isdir(d) or not os.listdir(d):
                return False
        except OSError:
            return False
    return True


class OCR:
    """
    题目识别器（PaddleOCR 封装）。

    用法:
        rec = OCR()
        rec.load()                          # 预热模型（首次较慢）
        result = rec.recognize(frame)       # frame: BGR numpy
        print(result.text)
    """

    def __init__(self, lang: str = "ch", use_gpu: bool = False,
                 cache_ttl: float = 2.0, cpu_threads: int = 4):
        """
        :param lang: 语言，ch=中英文混合
        :param use_gpu: 是否有 CUDA 可用
        :param cache_ttl: 相同画面结果缓存有效期（秒），0=禁用
        :param cpu_threads: CPU 推理线程数。Paddle 默认吃满全部核心，
            OCR 期间会抢占系统输入线程导致鼠标卡顿；限制到 4 可保持
            系统流畅（代价是推理变慢，可按机器核数在 config 调整）
        """
        self.lang = lang
        self.use_gpu = use_gpu
        self.cache_ttl = cache_ttl
        self.cpu_threads = max(1, int(cpu_threads))
        self._ocr = None
        self._last_hash: Optional[str] = None
        self._last_result: Optional[OCRResult] = None
        self._last_time: float = 0.0

    @property
    def loaded(self) -> bool:
        """模型是否已加载。首次 recognize() 才会真正加载（可能先下载）。"""
        return self._ocr is not None

    # -------- 生命周期 --------
    @staticmethod
    def _patch_paddlex_deps_for_frozen():
        """
        PyInstaller 打包环境下 paddlex 的依赖自检基于 importlib.metadata，
        部分 dist-info 元数据缺失会误报 DependencyError（实际模块都在 exe
        里，build.spec 已用 copy_metadata 收集主要包的元数据，这里兜底
        跳过剩余的 raise 检查）。
        """
        if not getattr(sys, "frozen", False):
            return
        try:
            import paddlex.utils.deps as _deps
            _deps.require_extra = lambda *a, **k: None
            _deps.require_deps = lambda *a, **k: None
            _deps.is_extra_available = lambda *a, **k: True
            _deps.is_dep_available = lambda *a, **k: True
        except Exception:
            pass

    def load(self):
        """加载并预热模型。首次调用会下载/初始化，需几秒。"""
        self._patch_paddlex_deps_for_frozen()
        # 限制底层线程池（OpenMP/MKL/paddle intra-op）大小：
        # 在 import paddle 之前设置才生效。避免 OCR 时 CPU 满载卡鼠标。
        for var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS",
                    "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
            os.environ.setdefault(var, str(self.cpu_threads))
        import paddleocr
        from paddleocr import PaddleOCR

        # 兼容 PaddleOCR 2.x / 3.x（3.x 移除了 use_angle_cls/use_gpu/show_log）
        ver = getattr(paddleocr, "__version__", "2")
        major = int(ver.split(".")[0]) if ver[:1].isdigit() else 2
        self._paddle_major = major
        if major >= 3:
            # PP-OCRv5 mobile 模型：比 medium 快 3-4 倍，识别精度对
            # 屏幕文本（笔试页面）足够。
            # enable_mkldnn=False：PaddlePaddle 新版 oneDNN/PIR 的
            # ConvertPirAttribute2RuntimeAttribute NotImplementedError
            # （已试 FLAGS_enable_pir_api=0 无效，指令层实现缺陷）
            self._ocr = PaddleOCR(
                lang=self.lang,
                text_detection_model_name=DET_MODEL,
                text_recognition_model_name=REC_MODEL,
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
                enable_mkldnn=False,
                cpu_threads=self.cpu_threads,
            )
        else:
            self._ocr = PaddleOCR(use_angle_cls=True, lang=self.lang,
                                  use_gpu=self.use_gpu, show_log=False)
        # 预热：跑一张空白图，避免首帧推理延迟过高
        self._run_paddle(np.zeros((64, 64, 3), dtype=np.uint8))

    # -------- 主入口 --------
    def recognize(self, frame: np.ndarray) -> OCRResult:
        """
        对一帧图像做 OCR。
        1) 画面指纹未变且缓存未过期 → 直接返回上次结果（节省推理）
        2) 否则走预处理 → PaddleOCR → 组装 OCRResult
        """
        if frame is None:
            return OCRResult("", 0.0, [], 0.0)

        h = self._frame_hash(frame)
        now = time.time()
        if (self.cache_ttl > 0 and h == self._last_hash
                and (now - self._last_time) < self.cache_ttl and self._last_result):
            return self._last_result

        t0 = time.time()
        preprocessed = self._preprocess(frame)
        text, boxes, conf = self._run_paddle(preprocessed)
        elapsed = (time.time() - t0) * 1000

        result = OCRResult(text=text, confidence=conf, boxes=boxes, elapsed_ms=elapsed)
        self._last_hash = h
        self._last_result = result
        self._last_time = time.time()  # 完成时刻（而非开始时刻，否则推理耗时>ttl 时缓存必失效）
        return result

    # -------- 内部 --------
    def _preprocess(self, frame: np.ndarray) -> np.ndarray:
        """图像预处理：只做缩放（限制长边）。

        不做二值化/灰度化：浏览器截屏的文字本身就是清晰渲染的，
        adaptiveThreshold 会破坏深色模式页面和代码高亮颜色，且拖慢速度。
        """
        import cv2
        h, w = frame.shape[:2]
        max_side = 1600
        if max(h, w) > max_side:
            scale = max_side / max(h, w)
            img = cv2.resize(frame, (int(w * scale), int(h * scale)))
        else:
            img = frame
        # PaddleOCR 接受 BGR
        return img

    def _run_paddle(self, img: np.ndarray) -> tuple:
        """调用 PaddleOCR，返回 (拼接文本, 文本框列表, 平均置信度)。"""
        if self._ocr is None:
            self.load()

        if getattr(self, "_paddle_major", 2) >= 3:
            return self._run_paddle_v3(img)
        return self._run_paddle_v2(img)

    def _run_paddle_v3(self, img: np.ndarray) -> tuple:
        """PaddleOCR 3.x：predict() 返回 OCRResult 列表，字段化访问。"""
        results = self._ocr.predict(img)
        lines, boxes, confs = [], [], []
        for r in results:
            texts = r.get("rec_texts") if hasattr(r, "get") else None
            if texts is None and hasattr(r, "json"):
                texts = r.json.get("res", {}).get("rec_texts", [])
            if not texts:
                continue
            scores = r.get("rec_scores") if hasattr(r, "get") else None
            polys = r.get("rec_polys") if hasattr(r, "get") else None
            for i, text in enumerate(texts):
                lines.append(text)
                if polys is not None and i < len(polys):
                    boxes.append(polys[i].tolist()
                                 if hasattr(polys[i], "tolist") else polys[i])
                if scores is not None and i < len(scores):
                    confs.append(float(scores[i]))
        full_text = "\n".join(lines)
        avg_conf = sum(confs) / len(confs) if confs else 0.0
        return full_text, boxes, avg_conf

    def _run_paddle_v2(self, img: np.ndarray) -> tuple:
        """PaddleOCR 2.x：ocr() 返回 [[ [box, (text, conf)], ... ]]。"""
        raw = self._ocr.ocr(img, cls=True)
        lines, boxes, confs = [], [], []
        if raw and isinstance(raw, list):
            for page in raw:
                if page is None:
                    continue
                for item in page:
                    box, (text, conf) = item[0], item[1]
                    lines.append(text)
                    boxes.append(box)
                    confs.append(float(conf))
        full_text = "\n".join(lines)
        avg_conf = sum(confs) / len(confs) if confs else 0.0
        return full_text, boxes, avg_conf

    @staticmethod
    def _frame_hash(frame: np.ndarray) -> str:
        """用低分辨率灰度图的 md5 作为画面指纹，检测是否发生实质变化。"""
        import cv2
        small = cv2.resize(frame, (160, 90))
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        return hashlib.md5(gray.tobytes()).hexdigest()


# 向后兼容别名
QuestionRecognizer = OCR


if __name__ == "__main__":
    # 快速自测：对一张截图做 OCR
    import cv2
    img = cv2.imread("test_capture.png")
    if img is not None:
        rec = OCR()
        rec.load()
        r = rec.recognize(img)
        print(f"[OCR] conf={r.confidence:.2f} time={r.elapsed_ms:.0f}ms")
        print(r.text)
    else:
        print("请先运行 capture.py 生成 test_capture.png")
