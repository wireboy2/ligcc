"""
主流程：截屏 → OCR → 作答 → 隐形浮层（主）/ 剪贴板（辅）
==================================================================

架构
----
识别线程（capture + ocr）持续扫描屏幕上的题目；
答案生成后，默认投递到 StealthOverlay（你照着抄）；
可通过 --delivery clipboard 切换到剪贴板模式。

热键（默认，可在 config.yaml 改）
---------------------------------
  Ctrl+Alt+Q   触发一次识别+解答
  Ctrl+Alt+A   追加识别并合并解答（长题一屏截不完时用）
  Ctrl+Alt+V   显示 / 隐藏浮层
  Ctrl+Alt+C   清空浮层内容
  Ctrl+Alt+M   切换截图显示器（多屏循环，浮层跟随）
  Ctrl+Alt+W   浮层停靠：右上→右下→左下→左上→居中（拖丢了用它拉回来）
  Ctrl+Alt+= / -   字号 +2 / -2（长行立刻按新字号重折）
  Ctrl+Alt+[ / ]   背板更透 / 更实（每次 15/255）
  Ctrl+Alt+Shift+方向键   浮层尺寸 ±60px（←→ 宽、↑↓ 高）
  Ctrl+Alt+方向键   浮层位置 ±20px（被 Intel 转屏占用时回退到 Ctrl+Alt+Shift+HJKL）
  Ctrl+Alt+X   退出

浮层怎么移动
------------
  浮层是"点击穿透"窗口，收不到普通鼠标消息，所以拖动走全局鼠标钩子：
  在浮层上 **按住 Ctrl 拖动**（或 **按住鼠标中键拖动**）即可移动，
  松手后位置写入 history/overlay_state.json，下次启动沿用
  （热键调过的字号/透明度/尺寸也记在同一个文件里）；
  不按 Ctrl 的普通点击照旧穿透到下层 IDE。也可在 config.yaml 里用
  overlay: { pos: [x, y] } 钉死位置、remember_pos: false 关掉记忆。

隐蔽要点
--------
  1. 浮层用 WDA_EXCLUDEFROMCAPTURE → 屏幕共享/录屏中不可见
  2. WS_EX_TOOLWINDOW → 不出现在 Alt-Tab / 任务栏
  3. WS_EX_NOACTIVATE + SW_SHOWNOACTIVATE → 弹出时不抢焦点，
     避免浏览器/会议窗口失去焦点触发 blur 日志
  4. OCR 本地推理（PaddleOCR）；解答走 API（配置见 aiKey.txt 或 config.yaml）
  5. 进程名/窗口标题伪装（build.bat 打包为 msbuild.exe 等）

API 配置
--------
  自动读取项目根目录 aiKey.txt（apiKey= / url= / 模型：），也可在
  config.yaml 中用 api: { key: ..., url: ..., model: ... } 覆盖。

运行
----
  pip install -r requirements.txt
  python main.py --once                      # 验证：截屏+OCR+解答 一次
  python main.py --once --duration 30        # 同上，看 30 秒自动关（不用 Ctrl+C）
  python main.py                             # 完整热键循环（推荐）
  python main.py --delivery clipboard        # 答案写剪贴板（默认仍是 overlay）

仅 Windows 可用。
"""
import argparse
import os
import sys
import time
import threading
from typing import Callable

# PaddleOCR 模型缓存重定向：
# - 打包成 exe 后：放 exe 同目录 .paddle_cache（模型下载一次后复用）
# - 源码运行：放项目根目录 .paddle_cache
if getattr(sys, "frozen", False):
    _ROOT = os.path.dirname(sys.executable)
else:
    _ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("PADDLE_PDX_CACHE_HOME", os.path.join(_ROOT, ".paddle_cache"))
os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")

# Windows 下把输出编码设为 utf-8，避免中文乱码
if sys.platform == "win32":
    try:
        import io
        # 已经是 utf-8 就别再包一层：新 wrapper 会顶掉旧的，旧的被回收时
        # 会连底下的 buffer 一起关掉 —— 后面所有 print 都会 ValueError
        # （测试里先包过一层再 import main 就正好踩到）
        if (getattr(sys.stdout, "encoding", "") or "").lower().replace("-", "") != "utf8":
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                          line_buffering=True)
    except Exception:
        pass

from capture import ScreenCapturer, CaptureRegion
from ocr import OCR, models_cached
from overlay import (StealthOverlay, check_system, load_saved_pos,
                     load_saved_size, load_state)
from config import load_config, DeliveryMode, Config as _CfgDefaults
from history import QALog

import json
import urllib.request
import urllib.error


# ---------------------------------------------------------------------------
# 答案生成：调用 Claude API (兼容 Anthropic Messages 格式)
# ---------------------------------------------------------------------------
class AnswerProvider:
    """
    把 OCR 出的题目文本 → 答案文本。
    通过配置的 Claude API 接口完成解答。
    """

    def __init__(self, mode: str = "api", api_key: str = "", api_url: str = "", model: str = ""):
        self.mode = mode
        self.api_key = api_key
        self.api_url = api_url
        self.model = model
        # 重试时的进度回调（App 会接到浮层上）。代理抖一下要等 2s+4s，
        # 这十几秒里不说话，用户只会以为程序死了
        self.status_cb: Callable[[str], None] | None = None

    def _say(self, msg: str):
        if self.status_cb:
            try:
                self.status_cb(msg)
            except Exception:
                pass

    def answer(self, question: str) -> str:
        """单次识别文本 → 答案。"""
        return self._answer_with_retry(question)

    def answer_from_passes(self, passes: list[str]) -> str:
        """
        多次 OCR 识别结果 → 答案（视为同一道题）。

        场景：题目很长/分屏展示，第一次只识别到一部分；滚动或翻页后
        再识别一次，把多段拼起来让 AI 合并去重后作答。
        """
        valid = [p.strip() for p in passes if p and p.strip()]
        if not valid:
            return ""
        if len(valid) == 1:
            return self._answer_with_retry(valid[0])

        sections = "\n\n".join(
            f"【第 {i} 次识别】\n{p}" for i, p in enumerate(valid, 1)
        )
        merged = (
            "你是一个面试/编程题解答助手。下面给出的是【同一道题目】的"
            "多次屏幕 OCR 识别结果：题目较长，分了几次截取，各段之间"
            "可能有内容重叠、顺序交错或个别字识别错误。请先把这几段"
            "合并还原成一道完整的题目（在心中去重对齐，不要输出合并过程），"
            "然后直接给出简洁准确的答案；如果是代码题请给出完整可运行的代码。\n\n"
            + sections
        )
        return self._answer_with_retry(merged)

    def _answer_with_retry(self, question: str) -> str:
        if not question.strip():
            return ""
        if self.mode == "none":
            return ""
        if not self.api_key or not self.api_url:
            return f"【识别到的题目】\n{question}\n\n【错误】未配置 API 密钥或地址"

        # 代理网络不稳定（间歇性 SSL 中断/403），带重试
        last_err = None
        for attempt in range(3):
            try:
                return self._call_claude(question)
            except Exception as e:
                last_err = e
                print(f"[api] 第 {attempt + 1} 次调用失败: {e}")
                if attempt < 2:
                    wait = 2 * (attempt + 1)
                    self._say(f"模型调用失败，{wait}s 后重试"
                              f"（第 {attempt + 2}/3 次）…\n\n{e}")
                    time.sleep(wait)
        return f"【识别到的题目】\n{question}\n\n【API 调用失败】{last_err}"

    def refine_for_history(self, question: str, answer: str) -> tuple[str, str] | None:
        """
        调用 AI 把原始题目/答案整理成规范存档格式（复盘用）。

        :return: (整理后的题目, 整理后的答案)；失败/未配置返回 None
                 （调用方保留原始记录，不丢数据）
        """
        if self.mode == "none" or not self.api_key or not self.api_url:
            return None
        if not (question.strip() and answer.strip()):
            return None
        prompt = (
            "请把下面这道面试/编程题的题目和答案整理成规范的复盘存档格式。\n"
            "题目只保留题目本身，必须剔除以下界面噪音（OCR 从网页截屏带进来的）：\n"
            "- 窗口标题、地址栏 URL、导航栏、登录/会员提示、通过率/在线人数等网站统计\n"
            "- 编辑器/代码区的行号（单独成行的数字）\n"
            "- 状态栏信息（如「行7，列3」「已存储」）、系统时间（如 22:51、2026/8/26）\n"
            "- OCR 产生的孤立符号与碎字：单独成行、与题目无关的 X × □ ☆ ♡ ∈ ● c T 之类无意义字符\n"
            "保留：题号与题名、完整题目描述、示例（输入/输出/解释）、数据范围与约束、进阶提问。\n"
            "答案保留核心解题思路与完整代码，去掉口语化开场白，层次清晰。\n"
            "严格只输出一个 JSON 对象，不要任何其它文字或代码块标记：\n"
            '{"question": "整理后的题目", "answer": "整理后的答案"}\n\n'
            f"【原始题目（多次 OCR 识别拼接）】\n{question}\n\n"
            f"【原始答案】\n{answer}"
        )
        last_err = None
        for attempt in range(3):
            try:
                # 整理输出含完整代码块，给更大输出预算防截断
                text = self._call_api(prompt, max_tokens=8192)
                return self._parse_qa_json(text)
            except Exception as e:
                last_err = e
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
        print(f"[history] AI 整理失败: {last_err}")
        return None

    @staticmethod
    def _parse_qa_json(text: str) -> tuple[str, str] | None:
        """
        从模型输出中稳健提取 {"question":..,"answer":..}。

        注意：整理后的答案内部常含 ```python 等代码块围栏，
        所以只能剥「最外层」围栏，绝不能对全文 split("```")。
        """
        s = text.strip()
        if not s:
            return None
        # 只剥最外层围栏：首行 ```/```json + 末尾 ```
        if s.startswith("```"):
            first_nl = s.find("\n")
            if first_nl != -1:
                s = s[first_nl + 1:]
            stripped = s.rstrip()
            if stripped.endswith("```"):
                s = stripped[:-3]
            s = s.strip()
        candidates = [s]
        # 前后有多余文字时：取第一个 { 到最后一个 }
        i, j = s.find("{"), s.rfind("}")
        if 0 <= i < j:
            candidates.append(s[i:j + 1])
        for c in candidates:
            try:
                obj = json.loads(c)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(obj, dict):
                q = str(obj.get("question", "")).strip()
                a = str(obj.get("answer", "")).strip()
                if q and a:
                    return q, a
        return None

    def _call_claude(self, question: str) -> str:
        """解答入口：拼解答提示词后调用 API。"""
        content = (
            "你是一个面试/编程题解答助手。请直接给出简洁准确的答案，"
            "如果是代码题请给出完整可运行的代码。\n\n"
            f"题目：\n{question}"
        )
        return self._call_api(content)

    def _call_api(self, content: str, max_tokens: int = 4096) -> str:
        """裸调用 Claude Messages API（content 即完整 user 消息）。"""
        payload = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": content}],
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "anthropic-version": "2023-06-01",
            # 该代理屏蔽 Python 默认 UA，必须带常规浏览器 UA
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.api_url, data=data, headers=headers, method="POST")

        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read().decode("utf-8"))

        # Anthropic Messages 格式：content 是块列表（可能含 thinking 块），
        # 真正的答案在所有 type=="text" 的块里
        content = body.get("content", [])
        if content and isinstance(content, list):
            texts = [b.get("text", "") for b in content
                     if isinstance(b, dict) and b.get("type") == "text"]
            return "\n".join(t for t in texts if t)
        return str(body)


# ---------------------------------------------------------------------------
# 主应用
# ---------------------------------------------------------------------------
class App:
    # 键盘微调浮层位置的步长（像素）：20px 一次，按十几下就能横跨半屏，
    # 又足够细，摆到「刚好不挡住题目」的位置
    NUDGE = 20

    def __init__(self, args):
        self.cfg = load_config(args.config)
        if args.delivery:
            self.cfg.delivery = DeliveryMode(args.delivery)
        if args.answer_mode:
            self.cfg.answer_mode = args.answer_mode
        if args.monitor is not None:
            self.cfg.monitor = args.monitor

        # 显示器列表（启动即打印，方便选择 monitor 编号）
        mons = ScreenCapturer.list_monitors()
        for i, m in enumerate(mons):
            tag = "全部合并" if i == 0 else ("主屏" if i == 1 else f"屏{i}")
            print(f"[monitor] {i}: {m['width']}x{m['height']} @({m['left']},{m['top']})  <- {tag}")
        sel = mons[self.cfg.monitor] if 0 <= self.cfg.monitor < len(mons) else mons[1]
        print(f"[monitor] 当前截图屏: {self.cfg.monitor} ({sel['width']}x{sel['height']})")

        self.capturer = ScreenCapturer(
            region=self._build_region(self.cfg.region),
            backend=self.cfg.capture_backend,
            monitor=self.cfg.monitor,
        )
        self.ocr = OCR(lang=self.cfg.ocr_lang, cpu_threads=self.cfg.ocr_cpu_threads)
        self.answerer = AnswerProvider(
            mode=self.cfg.answer_mode,
            api_key=self.cfg.api_key,
            api_url=self.cfg.api_url,
            model=self.cfg.api_model,
        )
        # 重试等待期间的提示也投到浮层（回调在浮层建好之后才可能被调用）
        self.answerer.status_cb = lambda m: self._status(m, "api")

        # 浮层（仅 overlay 模式需要）
        self.overlay: StealthOverlay | None = None
        if self.cfg.delivery == DeliveryMode.OVERLAY:
            info = check_system()
            print(f"[系统检查] {info}")
            if not info["supported"]:
                print("  ⚠ 将 fallback 到 WDA_MONITOR（捕获侧留纯黑块，仍不泄露内容）")
            # 位置优先级：config 里钉死的 pos > 上次记住的位置 >
            # 截图屏右上角（跟着 monitor 走，答题时不用扭头）
            ov_w, ov_h = self.cfg.overlay_size
            # 尺寸同理：上次用热键调过就沿用，config 显式写了 size 的以 config 为准
            if self.cfg.overlay_remember_pos:
                saved_size = load_saved_size()
                if saved_size and self.cfg.overlay_size == _CfgDefaults().overlay_size:
                    ov_w, ov_h = saved_size
                    print(f"[overlay] 沿用上次记住的尺寸 {ov_w}x{ov_h}")
            ox = sel["left"] + max(40, sel["width"] - ov_w - 40)
            oy = sel["top"] + 40
            if self.cfg.overlay_pos:
                ox, oy = self.cfg.overlay_pos
                print(f"[overlay] 使用 config 指定位置 ({ox},{oy})")
            elif self.cfg.overlay_remember_pos:
                saved = load_saved_pos()
                if saved:
                    ox, oy = saved
                    print(f"[overlay] 沿用上次记住的位置 ({ox},{oy})")
            # 字号/透明度同理：上次用热键调过就沿用，但 config 里显式写了的以
            # config 为准（写了就是要钉死，不该被上次的临时调整盖掉）
            font_size, bg_alpha = self.cfg.overlay_font_size, self.cfg.overlay_bg_alpha
            if self.cfg.overlay_remember_pos:
                look = load_state()
                dflt = _CfgDefaults()
                if font_size == dflt.overlay_font_size and isinstance(look.get("font_size"), int):
                    font_size = look["font_size"]
                if bg_alpha == dflt.overlay_bg_alpha and isinstance(look.get("bg_alpha"), int):
                    bg_alpha = look["bg_alpha"]
                if (font_size, bg_alpha) != (self.cfg.overlay_font_size, self.cfg.overlay_bg_alpha):
                    print(f"[overlay] 沿用上次记住的字号 {font_size}px / 不透明度 {bg_alpha}")
            self.overlay = StealthOverlay(
                title=self.cfg.window_fake_title,
                width=ov_w,
                height=ov_h,
                x=ox,
                y=oy,
                bg_color=self.cfg.overlay_bg_color,
                bg_alpha=bg_alpha,
                text_color=self.cfg.overlay_text_color,
                font_size=font_size,
                line_height=self.cfg.overlay_line_height,
                font_name=self.cfg.overlay_font_name,
                wrap=self.cfg.overlay_wrap,
                # 钉死位置时不再回写，避免覆盖 config 的意图
                remember_pos=self.cfg.overlay_remember_pos and not self.cfg.overlay_pos,
            )
            print(f"[overlay] 亲和性模式: {self.overlay.affinity_mode}")
            print(f"[overlay] 字体 {self.overlay.font_name} {self.overlay.font_size}px"
                  f" / 行高 {self.overlay.line_height}px"
                  f" / 一屏 {self.overlay.visible_lines()} 行"
                  f" / 尺寸 {self.overlay.width}x{self.overlay.height}"
                  f" / 长行{'软换行' if self.overlay.wrap else '裁切'}")

        self._running = True
        self._answering = False
        self._last_answer = ""
        # 实际生效的热键名（_register_hotkeys 填；--once 模式不注册，保持空）
        self._hotkey_labels: dict[str, str] = {}
        # 多次识别合并：累积的各次 OCR 文本（同一道题的不同部分）
        self._passes: list[str] = []
        # 复盘存储：每道题一条记录；Q=新题新记录，A=更新当前记录
        self.qa_log = QALog()
        self._entry_id = self.qa_log.next_id()
        # 整理版本号：A 更新同一记录时递增，防旧的后台整理结果覆盖新内容
        self._refine_gen: dict[int, int] = {}
        # 版本号检查与写库必须原子（与 run_once 的 递增+落盘 互斥），
        # 否则旧整理的 检查→写入 可能插在新内容的 递增→落盘 之间
        self._refine_lock = threading.Lock()
        if self.qa_log.count:
            print(f"[history] 已载入 {self.qa_log.count} 条历史记录（{self.qa_log.path}）")

    # ------------------------------------------------------------------ 工具
    @staticmethod
    def _build_region(region) -> CaptureRegion | None:
        """config 里的 (left, top, right, bottom) → CaptureRegion(left, top, w, h)。"""
        if not region:
            return None
        left, top, right, bottom = region
        return CaptureRegion(left, top, right - left, bottom - top)

    # ------------------------------------------------------------------ 状态反馈
    def _key(self, name: str) -> str:
        """某个动作**实际生效**的热键名，用于提示语。

        默认键被占用时会自动换键（见 _register_hotkeys），提示里硬写
        `Ctrl+Alt+M` 就可能是错的 —— 照着按没反应比不提示更糟。
        还没注册过（--once 模式）就退回配置值。
        """
        return (getattr(self, "_hotkey_labels", {}).get(name)
                or getattr(self.cfg, f"hotkey_{name}", name))

    def _status(self, msg: str, tag: str = "status"):
        """把进度/错误同时打到控制台**和浮层**。

        为什么非要上浮层：OCR + 一次 API 要几秒到十几秒，这期间浮层内容
        一动不动，只有控制台在滚 —— 而 `CONSOLE=False` 的打包版根本没有
        控制台，用户面对的是一块「毫无反应」的背板，分不清是热键没生效、
        截屏失败了，还是模型还在写。失败原因同理：只 print 等于没说。

        clipboard 模式没有浮层，退化成只打印。
        """
        print(f"[{tag}] {msg}")
        if self.cfg.delivery == DeliveryMode.OVERLAY and getattr(self, "overlay", None):
            self.overlay.set_text(msg)
            self.overlay.show()

    # ------------------------------------------------------------------ 单次流程
    def run_once(self, append: bool = False):
        """
        识别 + 解答一次。

        :param append: False=按 Q，清空之前的累积、只按本次识别作答；
                       True=按追加键，把本次识别并入之前的累积，
                       让 AI 把多次识别当同一道题合并作答。
        """
        self._status("识别中…", "ocr")
        frame = self.capturer.grab()
        if frame is None:
            self._status(f"截屏失败。多屏的话按 {self._key('monitor')} 换一块屏再试。",
                         "capture")
            return ""
        if not self.ocr.loaded:
            # 模型是懒加载的：真正的下载/初始化就发生在下面这次 recognize 里。
            # 首次可能要拉 200MB，paddle 的进度条只进它自己的 stdout，
            # 无控制台版看起来就是卡死 —— 所以先把话说在前头
            if models_cached():
                self._status("首次识别：正在加载 OCR 模型，约十几秒…", "ocr")
            else:
                self._status("首次运行：正在下载 OCR 模型（约 200MB）…\n\n"
                             "存到 .paddle_cache/ 里，视网速要几分钟。"
                             "下完这一次，以后启动就不用等了。", "ocr")
        try:
            result = self.ocr.recognize(frame)
        except Exception as e:
            self._status(f"OCR 失败：{type(e).__name__}: {e}\n\n"
                         "首次运行时多半是模型没下完（网络问题），再按一次重试。", "ocr")
            return ""
        print(f"---- OCR 识别结果 (conf={result.confidence:.2f} "
              f"{result.elapsed_ms:.0f}ms) ----")
        print(result.text[:1000])
        if not result.text.strip():
            # 最常见的原因就是截错屏（题目在另一块显示器上），所以直接把
            # 换屏热键写在提示里，不用去翻文档
            self._status(f"这一屏没识别到文字。题目可能在另一块显示器上"
                         f"（{self._key('monitor')} 换屏），或者字太小/对比度太低。", "ocr")
            return ""

        if append:
            if self._passes and result.text.strip() == self._passes[-1].strip():
                print("[merge] 识别内容与上一段相同（画面没变化），不重复追加")
            else:
                self._passes.append(result.text)
        else:
            self._passes = [result.text]
            self._entry_id = self.qa_log.next_id()  # 新题 → 新记录

        chars = sum(len(p) for p in self._passes)
        print(f"[merge] 当前共 {len(self._passes)} 段识别结果，总字数 {chars}")
        if self.cfg.answer_mode == "none":
            # 只识别不解答：把识别到的原文投上去。否则浮层会永远停在
            # 「AI 作答中…」——它其实早就干完了，只是本来就不解答
            self._status(f"（answer_mode=none，只识别不解答 / 共 {chars} 字）\n\n"
                         + "\n\n".join(self._passes), "ocr")
        else:
            self._status(f"AI 作答中…（已识别 {chars} 字）", "answer")
        answer = self.answerer.answer_from_passes(self._passes)
        self._last_answer = answer
        self._deliver(answer)

        # 复盘存储：Q/A 均落盘（A 更新同一条记录）。
        # 原始内容先落盘（AI 整理失败也不丢数据）；递增版本号与落盘
        # 原子执行，使在途的旧整理线程可见地作废。
        try:
            with self._refine_lock:
                self.qa_log.upsert(
                    self._entry_id,
                    question="\n\n".join(self._passes),
                    answer=answer,
                    passes=len(self._passes),
                    ocr_conf=result.confidence,
                )
                gen = self._refine_gen.get(self._entry_id, 0) + 1
                self._refine_gen[self._entry_id] = gen
        except Exception as e:
            print(f"[history] 记录保存失败: {e}")
            gen = self._refine_gen.get(self._entry_id, 0) + 1
            self._refine_gen[self._entry_id] = gen

        # 后台把原始记录丢给 AI 整理（去界面噪音/规范格式）后覆盖：
        # 不阻塞解答流程；期间若按 A 更新了内容，旧整理作废不覆盖
        self._spawn_refine(self._entry_id, gen,
                           "\n\n".join(self._passes), answer)
        return answer

    def _spawn_refine(self, entry_id: int, gen: int, raw_q: str, raw_a: str):
        """后台线程：AI 整理题目/答案 → 更新 history 记录。"""
        def _task():
            try:
                refined = self.answerer.refine_for_history(raw_q, raw_a)
                if not refined:
                    return  # 保留原始记录
                # 版本号检查与写库原子：期间记录被 A 更新过则作废
                with self._refine_lock:
                    if self._refine_gen.get(entry_id) != gen:
                        return
                    if self.qa_log.refine(entry_id, refined[0], refined[1]):
                        print(f"[history] 第 {entry_id} 题已由 AI 整理入库")
            except Exception as e:
                print(f"[history] AI 整理异常（保留原始记录）: {e}")

        threading.Thread(target=_task, daemon=True).start()

    def _deliver(self, answer: str):
        if not answer.strip():
            return
        if self.cfg.delivery == DeliveryMode.OVERLAY and self.overlay:
            self.overlay.set_text(answer)
            self.overlay.show()
            print("[deliver] 答案已送到隐形浮层（照着抄）")
        else:
            try:
                import pyperclip
                pyperclip.copy(answer)
                print("[deliver] 答案已写入剪贴板 (Ctrl+V 粘贴)")
            except Exception as e:
                # 剪贴板模式没有浮层可用，只能打印；但顺带说清怎么补救
                print(f"[deliver] 剪贴板写入失败: {e}"
                      f"（可以改用 --delivery overlay）")

    # ------------------------------------------------------------------ 热键
    def _register_hotkeys(self):
        """注册全局热键（Windows RegisterHotKey）。

        关键：RegisterHotKey(NULL) 把 WM_HOTKEY 投递到「注册线程」的消息
        队列，因此注册与 GetMessage 循环必须在同一线程（这里都在主线程）。

        组合键来自 config 的 `hotkeys:` 段（解析见 config.parse_hotkey），
        写错或解析不了就退回内置默认值并打印提示。

        回退链（见 config.hotkey_candidates）只对「还在用默认键」的项生效：
        M/A/W 常被微信、QQ、网易云占用，Ctrl+Alt+方向键 被 Intel 显卡驱动拿去
        转屏幕，占用时自动换到 N/B、F/G、E/T、Ctrl+Alt+Shift+HJKL。用户显式配了
        键就不再擅自更换 —— 写什么注册什么，占用了直说，免得「我配的键怎么变了」。
        """
        import ctypes
        import ctypes.wintypes as wt
        from config import format_hotkey, hotkey_candidates, parse_hotkey
        _Defaults = _CfgDefaults
        u = ctypes.windll.user32
        u.RegisterHotKey.argtypes = [wt.HWND, ctypes.c_int, wt.UINT, wt.UINT]
        u.RegisterHotKey.restype = wt.BOOL

        defaults = _Defaults()
        # (RegisterHotKey id, 配置项名, 默认键被占时按顺序试的替代键, 说明, 动作)
        specs = [
            (1, "solve", (), "识别并解答", self._action_answer),
            (2, "toggle", (), "显示/隐藏浮层",
             lambda: self.overlay.toggle() if self.overlay else None),
            (3, "clear", (), "清空浮层",
             lambda: self.overlay.set_text("") if self.overlay else None),
            (4, "quit", (), "退出", self._quit),
            (5, "monitor", ("Ctrl+Alt+N", "Ctrl+Alt+B"), "切换截图显示器",
             self._cycle_monitor),
            (6, "append", ("Ctrl+Alt+F", "Ctrl+Alt+G"), "追加识别并合并解答",
             lambda: self._action_answer(append=True)),
            (7, "dock", ("Ctrl+Alt+E", "Ctrl+Alt+T"), "浮层停靠", self._dock_overlay),
            (8, "font_up", (), "字号 +2", lambda: self._bump_font(+2)),
            (9, "font_down", (), "字号 -2", lambda: self._bump_font(-2)),
            (10, "alpha_down", (), "背板更透", lambda: self._bump_alpha(-15)),
            (11, "alpha_up", (), "背板更实", lambda: self._bump_alpha(+15)),
            (12, "width_down", (), "浮层变窄", lambda: self._bump_size(-60, 0)),
            (13, "width_up", (), "浮层变宽", lambda: self._bump_size(+60, 0)),
            (14, "height_down", (), "浮层变矮", lambda: self._bump_size(0, -60)),
            (15, "height_up", (), "浮层变高", lambda: self._bump_size(0, +60)),
            # 方向键常被 Intel 显卡驱动（屏幕旋转）/ IDE 占用，回退到 vim 式 HJKL
            (16, "move_left", ("Ctrl+Alt+Shift+H",), "浮层左移",
             lambda: self._nudge(-self.NUDGE, 0)),
            (17, "move_right", ("Ctrl+Alt+Shift+L",), "浮层右移",
             lambda: self._nudge(+self.NUDGE, 0)),
            (18, "move_up", ("Ctrl+Alt+Shift+K",), "浮层上移",
             lambda: self._nudge(0, -self.NUDGE)),
            (19, "move_down", ("Ctrl+Alt+Shift+J",), "浮层下移",
             lambda: self._nudge(0, +self.NUDGE)),
        ]

        mapping: dict[int, tuple[int, int, object]] = {}
        # name → 实际注册成功的键名（启动时打印、README 让用户以此为准）
        self._hotkey_labels: dict[str, str] = {}
        for _id, name, fallbacks, desc, fn in specs:
            raw = getattr(self.cfg, f"hotkey_{name}")
            default = getattr(defaults, f"hotkey_{name}")
            if parse_hotkey(raw) is None:
                print(f"[hotkey] hotkeys.{name} = {raw!r} 解析失败，改用默认 {default}")
            candidates = hotkey_candidates(raw, default, fallbacks)
            for m, v in candidates:
                if u.RegisterHotKey(None, _id, m, v):
                    mapping[_id] = (m, v, fn)
                    self._hotkey_labels[name] = format_hotkey(m, v)
                    break
            else:
                tried = " / ".join(format_hotkey(m, v) for m, v in candidates)
                sfx = "均被占用" if len(candidates) > 1 else "被占用"
                print(f"[hotkey] {desc} 注册失败（{tried} {sfx}），该功能本次不可用")
        if not mapping:
            raise RuntimeError(
                "所有热键注册失败。请关闭占用这些组合键的程序，"
                "或在 config.yaml 的 hotkeys: 段换一组键后重试"
            )
        return mapping

    def _action_answer(self, append: bool = False):
        """热键 Q/追加键：OCR+API 耗时较长，放独立线程执行，避免阻塞消息循环。"""
        if self._answering:
            # 这条只打控制台：浮层此刻正显示「识别中…/AI 作答中…」，
            # 那就是用户要的答复，用「忽略本次」把它盖掉反而丢信息
            print("[hotkey] 上一次解答还在进行，忽略本次")
            return
        self._answering = True

        def _task():
            try:
                # 工作线程降到 BELOW_NORMAL：OCR 是 CPU 密集型，正常
                # 优先级会抢占系统输入线程（鼠标卡顿的根源）；
                # Paddle 内部线程池在本线程首次推理时创建，
                # Windows 上子线程继承创建者优先级，一并降级。
                try:
                    import ctypes
                    import ctypes.wintypes as wt
                    k = ctypes.windll.kernel32
                    k.GetCurrentThread.restype = wt.HANDLE
                    k.SetThreadPriority.argtypes = [wt.HANDLE, ctypes.c_int]
                    k.SetThreadPriority(k.GetCurrentThread(), -1)  # BELOW_NORMAL
                except Exception:
                    pass
                self.run_once(append=append)
            except Exception as e:
                # 兜底：任何没预料到的异常也要落到浮层，不然无控制台版
                # 就是「按了键什么都没发生」
                self._status(f"解答过程出错：{type(e).__name__}: {e}", "answer")
            finally:
                self._answering = False

        threading.Thread(target=_task, daemon=True).start()

    def _dock_overlay(self):
        """热键 W：把浮层循环停靠到 右上→右下→左下→左上→居中。"""
        if not self.overlay:
            return
        where = self.overlay.dock_next()
        print(f"[overlay] 已停靠到{where} ({self.overlay.x},{self.overlay.y})")

    def _bump_font(self, delta: int):
        """热键 +/-：现场调字号（一屏放几行也跟着变，长行会重新折）。"""
        if not self.overlay:
            return
        size = self.overlay.bump_font(delta)
        print(f"[overlay] 字号 {size}px / 行高 {self.overlay.line_height}px"
              f" / 一屏 {self.overlay.visible_lines()} 行")

    def _bump_alpha(self, delta: int):
        """热键 [/]：现场调背板不透明度（越透越不挡下层，但字也越难读）。"""
        if not self.overlay:
            return
        a = self.overlay.bump_alpha(delta)
        print(f"[overlay] 背板不透明度 {a}/255")

    def _bump_size(self, dw: int, dh: int):
        """热键 Ctrl+Alt+Shift+方向键：现场改浮层大小。

        宽窄影响折行、高矮影响一屏行数，所以两个数都打出来，
        免得「按了没反应」其实是已经夹到显示器边界了。
        """
        if not self.overlay:
            return
        w, h = self.overlay.bump_size(dw, dh)
        print(f"[overlay] 尺寸 {w}x{h} / 一屏 {self.overlay.visible_lines()} 行")

    def _nudge(self, dx: int, dy: int):
        """热键 Ctrl+Alt+方向键：键盘微调浮层位置（每次 NUDGE 像素）。

        鼠标拖动要按住 Ctrl 再瞄准浮层，摆最后几像素时不如按键。落盘走
        move() 的限流（长按方向键会自动重复，每次都写文件没必要），
        退出时 destroy() 会补写最后一次。
        """
        if not self.overlay:
            return
        ov = self.overlay
        ov.move(ov.x + dx, ov.y + dy)
        print(f"[overlay] 位置 ({ov.x},{ov.y})")

    def _cycle_monitor(self):
        """热键 M：循环切换截图显示器 1→2→…→n→1，浮层跟随移动。"""
        mons = ScreenCapturer.list_monitors()
        n = len(mons) - 1  # 实际显示器数（[0] 是合并虚拟屏，跳过）
        if n <= 1:
            print("[monitor] 只检测到一块显示器，无需切换")
            return
        old = self.capturer.monitor if self.capturer.monitor >= 1 else 1
        new = old % n + 1  # 1→2→…→n→1
        self.capturer.monitor = new
        self.cfg.monitor = new
        m = mons[new]
        print(f"[monitor] 截图屏 {old} → {new}: {m['width']}x{m['height']} @({m['left']},{m['top']})")
        if self.overlay:
            x = m["left"] + max(40, m["width"] - self.overlay.width - 40)
            y = m["top"] + 40
            self.overlay.move(x, y, save=True)
            print(f"[monitor] 浮层已移到屏 {new} 右上角")

    def _hotkey_loop(self, mapping):
        """消息循环（主线程）：接收并分发 WM_HOTKEY。"""
        import ctypes
        import ctypes.wintypes as wt
        u = ctypes.windll.user32
        msg = wt.MSG()
        while True:
            ret = u.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret <= 0:  # WM_QUIT 或出错
                break
            if msg.message == 0x0312:  # WM_HOTKEY，wParam 即注册时的 id
                entry = mapping.get(msg.wParam)
                if entry:
                    _, _, fn = entry
                    try:
                        fn()
                    except Exception as e:
                        print(f"[hotkey] 执行异常: {e}")
            u.TranslateMessage(ctypes.byref(msg))
            u.DispatchMessageW(ctypes.byref(msg))

    def _quit(self):
        print("退出中...")
        self._running = False
        # GetMessageW 处于阻塞态，需向主线程队列投递 WM_QUIT 让其返回 0
        import ctypes
        import ctypes.wintypes as wt
        u = ctypes.windll.user32
        u.PostThreadMessageW.argtypes = [wt.DWORD, wt.UINT, ctypes.c_size_t, ctypes.c_size_t]
        u.PostThreadMessageW(threading.main_thread().ident, 0x0012, 0, 0)

    def _arm_auto_quit(self, duration: float | None):
        """到点自动退出（--duration）。

        守护线程 + _quit()：_quit 是往主线程队列投 WM_QUIT，消息循环因此
        正常返回，浮层照常 destroy、位置照常落盘 —— 比 os._exit 干净。
        """
        if not duration or duration <= 0:
            return
        print(f"[timer] {duration:g}s 后自动退出（--duration）")

        def _fire():
            time.sleep(duration)
            if self._running:
                print(f"[timer] 到点了（{duration:g}s），自动退出")
                self._quit()

        threading.Thread(target=_fire, daemon=True).start()

    # ------------------------------------------------------------------ 生命周期
    def run(self, duration: float | None = None):
        mapping = self._register_hotkeys()
        print("已启动。热键（以下是本次实际生效的键）：")
        for name, desc in (
            ("solve", "识别并解答（清空之前的累积，单次识别作答）"),
            ("append", "追加识别并合并解答（长题分几次识别，AI 会当同一道题合并）"),
            ("toggle", "显示/隐藏浮层"),
            ("clear", "清空浮层"),
            ("monitor", "切换截图显示器"),
            ("dock", "浮层停靠：右上→右下→左下→左上→居中"),
            ("font_up", "字号 +2"),
            ("font_down", "字号 -2"),
            ("alpha_down", "背板更透（更隐蔽、更不挡下层）"),
            ("alpha_up", "背板更实（字更清楚）"),
            ("width_down", "浮层变窄 60px（长行会按新宽度重折）"),
            ("width_up", "浮层变宽 60px"),
            ("height_down", "浮层变矮 60px"),
            ("height_up", "浮层变高 60px（一屏能放更多行）"),
            ("move_left", "浮层左移 20px"),
            ("move_right", "浮层右移 20px"),
            ("move_up", "浮层上移 20px"),
            ("move_down", "浮层下移 20px"),
            ("quit", "退出"),
        ):
            label = self._hotkey_labels.get(name)
            if label:
                print(f"  {label}  {desc}")
        if self.cfg.delivery == DeliveryMode.OVERLAY:
            print("（答案通过隐形浮层投递，屏幕共享中不可见）")
            print("  · 光标移到浮层上滚动滚轮 → 翻页")
            print("  · 在浮层上按住 Ctrl 拖动（或按住鼠标中键拖动）→ 移动浮层"
                  + ("，位置会被记住" if self.overlay and self.overlay.remember_pos else ""))
            print("  · 不按 Ctrl 的普通点击照旧穿透到下层 IDE/浏览器")

        # 鼠标钩子（滚轮翻页 + 拖动移动）：必须在主线程装
        # （回调经主线程的 GetMessage 循环分发）
        if self.overlay and self.overlay.install_mouse_hook():
            print("[overlay] 滚轮翻页 / Ctrl 拖动移动 已启用")

        # 主线程直接跑消息循环：热键在主线程注册，WM_HOTKEY 也投到主线程
        # 队列；同时泵消息可让工作线程对浮层窗口的调用正常送达。
        self._arm_auto_quit(duration)
        try:
            self._hotkey_loop(mapping)
        except KeyboardInterrupt:
            print("\n[Ctrl+C] 退出中…")
        finally:
            if self.overlay:
                self.overlay.destroy()
            sys.exit(0)

    def run_once_cli(self, duration: float | None = None):
        """`--once`：截屏+OCR+解答一次。

        浮层模式下不能跑完就返回 —— 窗口一销毁答案就没了，得停下来等人看完。
        以前这里是 `while True: sleep(1)`，只能 Ctrl+C，而且有两个隐患：
          · 热键循环没起来，说明书上写的退出键（Ctrl+Alt+X）根本不管用；
          · 没有消息泵，滚轮翻页 / Ctrl 拖动全是死的 —— 长答案翻不到第二页，
            浮层挡住题目也挪不开，等于「验证链路」只能验证第一屏。
        所以改成走正常那套：注册热键 + 装鼠标钩子 + 跑消息循环。
        `--duration N` 到点自动退，挂在脚本/CI 里跑就不会卡住。
        """
        self.run_once()
        if not self.overlay:
            return                      # 剪贴板模式：答案已在剪贴板，没什么要等的
        mapping = self._register_hotkeys()
        quit_key = self._hotkey_labels.get("quit") or "Ctrl+C"
        print(f"（--once 已跑完一次。浮层留着给你看：{quit_key} 退出，"
              f"滚轮翻页，Ctrl+拖动挪位置）")
        self.overlay.install_mouse_hook()
        self._arm_auto_quit(duration)
        try:
            self._hotkey_loop(mapping)
        except KeyboardInterrupt:
            print("\n[Ctrl+C] 退出中…")
        finally:
            self.overlay.destroy()


def parse_args():
    p = argparse.ArgumentParser(description="面试辅助工具（Windows）")
    p.add_argument("--once", action="store_true", help="只做一次 截屏+OCR+解答 后退出")
    p.add_argument("--config", default="config.yaml", help="配置文件路径")
    p.add_argument("--delivery", choices=["overlay", "clipboard"], help="答案投递方式")
    p.add_argument("--answer-mode", choices=["api", "none"], help="解答模式（api=调用配置的模型）")
    p.add_argument("--monitor", type=int, choices=[0, 1, 2, 3, 4],
                   help="截图显示器编号：1=主屏 2=副屏… 0=全部合并（默认取 config）")
    p.add_argument("--duration", type=float, metavar="秒",
                   help="跑够这么多秒自动退出（--once 时用来限时看答案；挂在脚本里跑不会卡住）")
    p.add_argument("--export-md", action="store_true",
                   help="把已存的问答记录导出为可读的复盘 Markdown（复盘记录.md）后退出")
    p.add_argument("--refine", action="store_true",
                   help="把历史中未经 AI 整理的记录批量整理入库并更新复盘文档（先备份 .bak）")
    return p.parse_args()


def cmd_export_md(args) -> int:
    """导出复盘 Markdown。"""
    qa = QALog()
    if qa.count == 0:
        print("还没有任何问答记录")
        return 0
    out = qa.export_markdown()
    print(f"已导出 {qa.count} 题到 {out}")
    return 0


def cmd_refine(args) -> int:
    """对历史中未经 AI 整理的旧记录批量补整理（旧版本/旧 exe 产生的记录）。"""
    import shutil
    cfg = load_config(args.config)
    if cfg.answer_mode == "none" or not (cfg.api_key and cfg.api_url):
        print("未配置 API（aiKey.txt / config.yaml），无法整理")
        return 1
    qa = QALog()
    if qa.count == 0:
        print("还没有任何问答记录")
        return 0
    ids = qa.uncleaned()
    if not ids:
        print(f"共 {qa.count} 条记录均已整理过，无需处理")
        return 0

    # 批量改写前备份（失败可从 .bak 恢复）
    bak = qa.path + ".bak"
    try:
        shutil.copy2(qa.path, bak)
        print(f"已备份：{bak}")
    except OSError as e:
        print(f"备份失败（继续整理）：{e}")

    provider = AnswerProvider(mode="api", api_key=cfg.api_key,
                              api_url=cfg.api_url, model=cfg.api_model)
    ok = fail = 0
    for i, eid in enumerate(ids, 1):
        rec = qa.get(eid)
        if not rec:
            continue
        print(f"[refine] ({i}/{len(ids)}) 整理第 {eid} 题 ...")
        refined = provider.refine_for_history(rec.get("question", ""),
                                              rec.get("answer", ""))
        if refined:
            qa.refine(eid, refined[0], refined[1])
            ok += 1
        else:
            fail += 1
    print(f"整理完成：成功 {ok}、失败 {fail}（失败记录保留原文）")
    out = qa.export_markdown()
    print(f"复盘文档已更新：{out}")
    return 0


def main() -> int:
    args = parse_args()
    if args.refine:
        return cmd_refine(args)
    if args.export_md:
        return cmd_export_md(args)
    app = App(args)
    if args.once:
        app.run_once_cli(duration=args.duration)
    else:
        app.run(duration=args.duration)
    return 0


if __name__ == "__main__":
    sys.exit(main())
