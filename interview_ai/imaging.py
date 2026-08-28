"""
截图编码模块 (imaging.py)

把 capture 抓到的 BGR 帧编成可以直接塞进 Anthropic Messages 请求的图片块
（base64 + media_type），供 `input_mode: image` 使用。

- 编码前限制长边：端点自己会把长边 >1568px 的图缩到 1568，本地先缩等于
  少传一大截上行流量（首字更快），画质一分不亏
- 格式退化链 webp → png → jpeg：webp 对「大片纯色背景 + 细小文字」的
  截屏压得最狠（同画质约为 png 的 1/5），但 OpenCV 是否带 webp 编码器
  取决于发行版，编不出来就自动换下一个，而不是在按下热键时才报错
- 画面指纹：与 ocr 共用一套（低分辨率灰度 md5），用于「画面没变就别重复追加」

为什么截图不会把浮层自己拍进去：浮层设了 WDA_EXCLUDEFROMCAPTURE，mss 的
BitBlt 同样看不到它 —— 否则上一轮答案会被当成题目再喂一遍。
"""
import base64
import hashlib
from dataclasses import dataclass
from typing import Optional

import numpy as np

# 端点会把长边超过这个值的图片自己缩下来，所以本地缩到同一个尺寸即可：
# 传更大的只是白费上行带宽和等待，模型并不会看得更清。
MAX_SIDE_DEFAULT = 1568

# (OpenCV 扩展名, HTTP media type)。Anthropic 认 jpeg/png/gif/webp。
FORMATS = {
    "webp": (".webp", "image/webp"),
    "png": (".png", "image/png"),
    "jpeg": (".jpg", "image/jpeg"),
    "jpg": (".jpg", "image/jpeg"),
}
# 编不出来时按这个顺序退（webp 编码器不是所有 opencv 发行版都带）
FALLBACK_ORDER = ("webp", "png", "jpeg")


@dataclass
class Shot:
    """一张已经编好、可直接发给模型的截图。"""
    b64: str            # base64 编码后的图片数据
    media_type: str     # image/webp 之类
    width: int          # 编码时的实际像素宽（缩放之后）
    height: int
    nbytes: int         # 编码后的字节数（未 base64，base64 后约 ×4/3）
    fingerprint: str    # 画面指纹，用于去重
    scaled: bool        # 是否因为超过 max_side 被缩过

    @property
    def kb(self) -> float:
        return self.nbytes / 1024

    def to_block(self) -> dict:
        """转成 Messages API 的 image content block。"""
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": self.media_type, "data": self.b64},
        }


def frame_fingerprint(frame: np.ndarray) -> str:
    """低分辨率灰度图的 md5，作为「画面有没有实质变化」的指纹。

    ocr.OCR._frame_hash 也用这一份实现：两处各写一遍，早晚会出现
    「OCR 认为画面变了、图片模式认为没变」这种对不上的怪事。
    """
    import cv2
    small = cv2.resize(frame, (160, 90))
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    return hashlib.md5(gray.tobytes()).hexdigest()


def _encode_params(fmt: str, quality: int) -> list:
    """OpenCV imencode 的参数。png 没有「质量」，只有压缩级别。"""
    import cv2
    if fmt == "png":
        return [int(cv2.IMWRITE_PNG_COMPRESSION), 6]
    if fmt in ("jpeg", "jpg"):
        return [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)]
    return [int(cv2.IMWRITE_WEBP_QUALITY), int(quality)]


def encode_frame(frame: Optional[np.ndarray],
                 max_side: int = MAX_SIDE_DEFAULT,
                 fmt: str = "webp",
                 quality: int = 80) -> Optional[Shot]:
    """一帧 BGR → Shot；帧为空或所有格式都编不出来时返回 None。

    :param max_side: 长边上限（像素），超了按比例缩。INTER_AREA 缩小文字
                     比默认的双线性清楚，这里的图是要给模型读字的
    :param fmt: 首选格式（webp | png | jpeg），编不出来按 FALLBACK_ORDER 退
    :param quality: webp/jpeg 的质量 1-100；png 忽略此项
    """
    if frame is None or getattr(frame, "size", 0) == 0:
        return None
    import cv2

    fingerprint = frame_fingerprint(frame)
    h, w = frame.shape[:2]
    scaled = False
    if max_side > 0 and max(h, w) > max_side:
        scale = max_side / max(h, w)
        frame = cv2.resize(frame, (max(1, int(w * scale)), max(1, int(h * scale))),
                           interpolation=cv2.INTER_AREA)
        scaled = True
        h, w = frame.shape[:2]

    fmt = (fmt or "webp").lower().lstrip(".")
    order = [fmt] + [f for f in FALLBACK_ORDER if f != fmt]
    for i, name in enumerate(order):
        ext, media_type = FORMATS.get(name, (None, None))
        if ext is None:
            continue
        try:
            ok, buf = cv2.imencode(ext, frame, _encode_params(name, quality))
        except Exception as e:
            ok, buf = False, None
            print(f"[imaging] {name} 编码异常（{type(e).__name__}: {e}）")
        if ok and buf is not None and len(buf):
            if i:
                print(f"[imaging] {order[0]} 编不出来，本次改用 {name}"
                      f"（把 config.yaml 的 image.format 直接写成 {name} 可少试一次）")
            data = buf.tobytes()
            return Shot(b64=base64.b64encode(data).decode("ascii"),
                        media_type=media_type, width=w, height=h,
                        nbytes=len(data), fingerprint=fingerprint, scaled=scaled)
    return None


def describe(shots: list) -> str:
    """一串 Shot 的一句话摘要（打印/浮层提示用）。"""
    if not shots:
        return "0 张截图"
    kb = sum(s.kb for s in shots)
    size = f"{shots[-1].width}x{shots[-1].height}"
    return (f"{len(shots)} 张截图（{size}，共 {kb:.0f}KB）" if len(shots) > 1
            else f"1 张截图（{size}，{kb:.0f}KB）")


if __name__ == "__main__":
    # 快速自测：抓一帧当前屏并报告各格式的体积（不出网）
    from capture import ScreenCapturer
    f = ScreenCapturer().grab()
    if f is None:
        print("抓帧失败")
    else:
        print(f"原始帧 {f.shape[1]}x{f.shape[0]}")
        for name in FALLBACK_ORDER:
            s = encode_frame(f, fmt=name)
            print(f"  {name:5} -> " + (f"{s.width}x{s.height} {s.kb:7.1f}KB "
                                       f"base64 {len(s.b64) / 1024:7.1f}KB"
                                       if s else "编码失败"))
