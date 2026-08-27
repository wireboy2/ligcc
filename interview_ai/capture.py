"""
屏幕采集模块 (capture.py)

负责从 Windows 桌面抓取题目区域图像。
- 默认使用 mss（GDI 帧缓冲，跨版本兼容，~5-15ms/帧）
- 可选 WGC (Windows.Graphics.Capture) 后端，通过 wgc_python 获得 180+FPS 与"后台/被遮挡窗口"捕获能力
- 支持指定区域捕获（只抓题目所在区域，减少 OCR 压力）
- 独立采集线程 + 最新帧缓存（生产者-消费者，无锁取最新帧）

关键隐蔽性说明：
  截屏本身在 Windows 上属于普通用户权限操作，mss/BitBlt 不会向系统注册
  "截屏行为"事件，腾讯会议等会议软件无法据此感知被截屏。真正需要规避的是
  "PrintScreen 键事件监听"与"明显的截图工具窗口"，本工具均不涉及。
"""
import time
from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np


@dataclass
class CaptureRegion:
    """捕获区域（屏幕坐标，左上角为原点）"""
    left: int
    top: int
    width: int
    height: int

    def to_mss(self) -> dict:
        return {"left": self.left, "top": self.top,
                "width": self.width, "height": self.height}


class ScreenCapturer:
    """
    屏幕采集器。

    用法:
        cap = ScreenCapturer(region=CaptureRegion(100, 100, 1200, 800))
        cap.start()              # 启动后台采集线程
        frame = cap.get_latest() # 取最新帧 (numpy BGR)
        cap.stop()
    """

    SUPPORTED_BACKENDS = ("mss",)

    def __init__(self, region: Optional["CaptureRegion"] = None, backend: str = "mss",
                 monitor: int = 1):
        """
        :param region: 捕获区域（绝对屏幕坐标），None 表示整个所选显示器
        :param backend: 目前只有 "mss"。传别的（包括尚未实现的 "wgc"）会打印
                        一行提示并降级到 mss —— 配错了还能继续用，
                        而不是在按下热键时才抛 NotImplementedError
        :param monitor: 显示器编号（mss 约定）：
                        1=主显示器，2=第二块，…，0=所有显示器合并虚拟屏
        """
        self.region = region
        self.backend = backend if backend in self.SUPPORTED_BACKENDS else "mss"
        if self.backend != backend:
            if backend == "wgc":
                print("[capture] capture_backend: wgc 尚未实现，本次使用 mss")
            else:
                print(f"[capture] 未知 capture_backend: {backend!r}，本次使用 mss")
        self.monitor = monitor
        self._latest: Optional[np.ndarray] = None
        self._running = False
        self._thread = None

    # -------- 单次抓取（供手动调用 / 测试） --------
    def grab(self) -> Optional[np.ndarray]:
        """抓取一帧，返回 BGR 格式的 numpy 数组，失败返回 None。"""
        return self._grab_mss()

    def _pick_monitor(self, sct) -> dict:
        """按 self.monitor 选显示器（越界回退主屏）。region 优先于 monitor。"""
        if self.region:
            return self.region.to_mss()
        mons = sct.monitors  # [0]=全部合并, [1..]=各显示器
        idx = self.monitor if 0 <= self.monitor < len(mons) else 1
        return mons[idx]

    def _grab_mss(self) -> Optional[np.ndarray]:
        import mss
        with mss.mss() as sct:
            mon = self._pick_monitor(sct)
            raw = sct.grab(mon)
            # mss 返回 BGRA，转成 BGR（OpenCV / OCR 通用）
            frame = np.array(raw)[:, :, :3]  # 丢弃 alpha
            return frame

    def _grab_wgc(self) -> Optional[np.ndarray]:
        """
        WGC 后端（**尚未实现**，`__init__` 会把 backend="wgc" 降级成 mss，
        所以正常流程走不到这里）。

        真要做的话：Windows.Graphics.Capture 能抓被其它窗口遮挡的目标窗口，
        对「题目在被遮住的窗口里」的场景有实际价值，代价是要引入 WinRT 绑定
        （winrt / wgc_python 之类）和一小段 D3D 互操作，属于独立的一块工作。
        """
        raise NotImplementedError(
            "WGC 后端尚未实现；capture_backend 目前只支持 mss"
        )

    # -------- 后台连续采集 --------
    def start(self, poll_interval: float = 0.0):
        """启动采集线程，持续刷新最新帧。poll_interval=0 时全力采集。"""
        import threading
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, args=(poll_interval,), daemon=True)
        self._thread.start()

    def _loop(self, poll_interval: float):
        import mss
        with mss.mss() as sct:
            while self._running:
                # 每帧重新选取（支持运行时切换显示器/区域）
                mon = self._pick_monitor(sct)
                t0 = time.time()
                raw = sct.grab(mon)
                self._latest = np.array(raw)[:, :, :3]
                if poll_interval > 0:
                    dt = time.time() - t0
                    time.sleep(max(0, poll_interval - dt))

    def get_latest(self) -> Optional[np.ndarray]:
        """获取最新一帧的拷贝（避免外部修改缓存）。"""
        return None if self._latest is None else self._latest.copy()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)

    # -------- 工具 --------
    @staticmethod
    def list_monitors() -> list:
        """枚举所有显示器，用于配置 region。"""
        import mss
        with mss.mss() as sct:
            return sct.monitors  # monitors[0]=全部合并, monitors[1..]=各显示器


if __name__ == "__main__":
    # 快速自测：抓取全屏并保存
    cap = ScreenCapturer()
    frame = cap.grab()
    if frame is not None:
        from cv2 import imwrite
        imwrite("test_capture.png", frame)
        print(f"[OK] captured {frame.shape}")
    print("monitors:", ScreenCapturer.list_monitors())
