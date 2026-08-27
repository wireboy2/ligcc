"""
问答记录存储（面试后复盘用）。

存储格式：JSONL，每行一道题的完整记录
  {"id": 1, "ts": "2026-08-26 14:30:05", "question": "...", "answer": "...",
   "passes": 2, "ocr_conf": 0.93}

- 同一道题多次追加识别（热键 A）→ 更新同一条记录（question 合并、answer 刷新）
- 按新题识别（热键 Q）→ 新增一条
- 写入方式：整文件原子重写（题量小，代价可忽略），避免追加识别产生重复条目
- 文件位置：exe 同目录 / 项目根目录下 history/qa_log.jsonl
"""
import json
import os
import sys
import tempfile
import threading
import time
from datetime import datetime

if getattr(sys, "frozen", False):
    _ROOT = os.path.dirname(sys.executable)
else:
    _ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULT_FILE = os.path.join(_ROOT, "history", "qa_log.jsonl")


class QALog:
    """线程安全的问答记录器。"""

    def __init__(self, path: str = DEFAULT_FILE):
        self.path = path
        self._lock = threading.Lock()
        self._records: list[dict] = []
        self._load()

    # ------------------------------------------------------------ 内部
    def _load(self):
        """启动时载入已有记录（文件不存在/损坏则从空开始）。"""
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        if isinstance(rec, dict) and "id" in rec:
                            self._records.append(rec)
                    except json.JSONDecodeError:
                        continue  # 跳过损坏行
        except OSError:
            pass

    def _flush(self):
        """
        整文件原子重写（先写临时文件再替换，中断不损坏原文件）。

        Windows 上目标文件被并发读取（--export-md / 用户查看）时
        os.replace 会报 PermissionError，短暂重试等读侧释放句柄；
        重试仍失败则回退直接写（宁可牺牲原子性也不丢内存中的记录）。
        """
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=os.path.dirname(self.path), suffix=".tmp",
            prefix="qa_log_", text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                for rec in self._records:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            for attempt in range(6):
                try:
                    os.replace(tmp, self.path)
                    return
                except PermissionError:
                    time.sleep(0.05 * (attempt + 1))
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        # 读侧一直没释放：回退直接覆写目标文件
        with open(self.path, "w", encoding="utf-8") as f:
            for rec in self._records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # ------------------------------------------------------------ 对外
    def next_id(self) -> int:
        """下一个记录 id（沿用已有文件的最大 id 递增）。"""
        with self._lock:
            return (max(r["id"] for r in self._records) + 1) if self._records else 1

    def upsert(self, entry_id: int, question: str, answer: str,
               passes: int = 1, ocr_conf: float | None = None) -> dict:
        """
        新增或更新一条记录。

        :param entry_id: 记录 id。热键 Q 分配新 id（新增），
                         热键 A 沿用当前 id（更新同一条）。
        :param question: 题目文本（多次识别时为多段拼接后的全文）
        :param passes: 识别次数
        """
        rec = {
            "id": entry_id,
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "question": question,
            "answer": answer,
            "passes": passes,
        }
        if ocr_conf is not None:
            rec["ocr_conf"] = round(float(ocr_conf), 4)
        with self._lock:
            # 已存在同 id → 原位更新；否则按 id 顺序插入
            for i, r in enumerate(self._records):
                if r["id"] == entry_id:
                    self._records[i] = rec
                    break
            else:
                self._records.append(rec)
                self._records.sort(key=lambda r: r["id"])
            self._flush()
        return rec

    def refine(self, entry_id: int, question: str, answer: str) -> bool:
        """
        用 AI 整理后的题目/答案更新已有记录（保留原 id/ts/统计字段，
        打上 cleaned 标记）。记录不存在返回 False。
        """
        with self._lock:
            for r in self._records:
                if r["id"] == entry_id:
                    r["question"] = question
                    r["answer"] = answer
                    r["cleaned"] = True
                    self._flush()
                    return True
            return False

    def get(self, entry_id: int) -> dict | None:
        """按 id 取一条记录的副本（无则 None）。"""
        with self._lock:
            for r in self._records:
                if r["id"] == entry_id:
                    return dict(r)
        return None

    def uncleaned(self) -> list[int]:
        """尚未经过 AI 整理（无 cleaned 标记）的记录 id 列表。"""
        with self._lock:
            return [r["id"] for r in self._records if not r.get("cleaned")]

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._records)

    # ------------------------------------------------------------ 导出
    def export_markdown(self, md_path: str | None = None) -> str:
        """
        导出可读的复盘文档（Markdown）。

        :param md_path: 输出路径，缺省为 JSONL 同目录的 复盘记录.md
        :return: 实际输出路径
        """
        if md_path is None:
            md_path = os.path.join(os.path.dirname(self.path), "复盘记录.md")
        with self._lock:
            records = list(self._records)
        lines = [f"# 面试复盘记录（共 {len(records)} 题）",
                 "",
                 f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                 f"> 源数据：{os.path.basename(self.path)}",
                 ""]
        for rec in records:
            lines.append(f"## {rec['id']}. {rec.get('ts', '')}")
            meta = []
            if rec.get("cleaned"):
                meta.append("AI整理版")
            if rec.get("passes", 1) > 1:
                meta.append(f"{rec['passes']} 次识别合并")
            if "ocr_conf" in rec:
                meta.append(f"OCR置信 {rec['ocr_conf']}")
            if meta:
                lines.append(f"（{'，'.join(meta)}）")
            lines += ["", "### 题目", "", rec.get("question", "").strip() or "（无）",
                      "", "### 答案", "", rec.get("answer", "").strip() or "（无）", ""]
        os.makedirs(os.path.dirname(md_path), exist_ok=True)
        with open(md_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        return md_path
