#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
autofish.py —— 三角洲行动 · 独立自动钓鱼

设计要点（都是刻意的设计，见 docs/adr/）：
  1. 不做任何图像识别 / 不读游戏内存 / 不注入游戏进程（ADR-0001）。
     输入只有系统音频回环，输出只有鼠标左键注入。
  2. 归一分只负责回答「有没有出现咬钩音」（存在判据）；
     回答「是不是我的鱼」的是**电平**与**声像差**（归属判据）——
     因为归一分是归一化的，对音量与方位都免疫（ADR-0002）。
  3. 不做延迟仲裁，每次命中当下判定（ADR-0003）。
  4. 默认用「瘦身模板」（只取咬钩声本体），而不是整段 1.5 秒。
     这同时降低了检测延迟：整段模板要等 1.5 秒缓冲填满才能出峰（约 720 ms 延迟），
     瘦身模板在咬钩声播完的瞬间就能出峰（约 210 ms），快出约半秒。

用法：
    python autofish.py                    图形界面（默认）
    python autofish.py --nogui            纯控制台
    python autofish.py --calibrate 120    标定：只听不点，跑 120 秒后给建议门限
    python autofish.py --list             列出回环设备
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import json
import os
import queue
import shutil
import sys
import threading
import time
import warnings
import wave
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import soundcard as sc

try:
    from soundcard.mediafoundation import SoundcardRuntimeWarning

    warnings.simplefilter("ignore", SoundcardRuntimeWarning)
except Exception:
    pass

# ---------------------------------------------------------------- 常量

APP_DIR = Path(os.environ.get("LOCALAPPDATA", ".")) / "AutoFish"
HITS_DIR = APP_DIR / "hits"
CONFIG_PATH = APP_DIR / "autofish.json"
TEMPLATE_PATH = APP_DIR / "template.wav"
CSV_PATH = APP_DIR / "hits.csv"
LOG_PATH = APP_DIR / "autofish.log"
CRASH_PATH = APP_DIR / "crash.log"

SR = 48000           # 采样率，用端点原生值，避免重采样
BLOCK = 960          # 每次读 20 ms
CH = 2               # 原生立体声
CALIB_FLOOR = 0.50   # 标定期间放宽记录门限：否则"差一点命中"的样本会被丢掉，
                     # 而 score_min 的建议值正是要从这些样本的分布里算出来的

VK = {f"F{i}": 0x6F + i for i in range(1, 13)}
VK.update({"F13": 0x7C})

MOUSEEVENTF_LEFTDOWN = 2
MOUSEEVENTF_LEFTUP = 4
GA_ROOT = 2

CSV_FIELDS = ["t", "verdict", "score", "level_db", "pan_db", "rms_l_db", "rms_r_db",
              "coherence", "centroid_hz", "hf_ratio", "lag_ms", "note"]


def db(v: float) -> float:
    return 20.0 * np.log10(max(v, 1e-12))


def now_hms() -> str:
    return time.strftime("%H:%M:%S")


# ---------------------------------------------------------------- 冻结运行（exe）支持
#
# 用 PyInstaller 的 --noconsole 打包后，sys.stdout / sys.stderr 都是 None，
# 这时任何一个 print() 都会抛 RuntimeError: lost sys.stdout —— 界面还没起来就崩了，
# 而且因为是窗口程序，连崩溃信息都看不见。下面三件事都是为了堵这个坑。


_STDIO_SINK = None          # 必须持有引用，否则被 GC 回收会连带关掉


def ensure_std_streams() -> str:
    """无控制台运行时给 stdout/stderr 找个去处。返回一行说明供日志用。"""
    global _STDIO_SINK
    if sys.stdout is not None and sys.stderr is not None:
        return ""
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    _STDIO_SINK = open(os.devnull, "w", encoding="utf-8", errors="replace")
    if sys.stdout is None:
        sys.stdout = _STDIO_SINK
    if sys.stderr is None:
        sys.stderr = _STDIO_SINK
    return "无控制台运行（exe 模式）：标准输出已接到空设备"


def _write_crash(text: str) -> str:
    """把 traceback 追加进 crash.log，返回文件路径（失败则给说明）。"""
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        with CRASH_PATH.open("a", encoding="utf-8") as f:
            f.write("\n=== %s ===\n%s"
                    % (time.strftime("%Y-%m-%d %H:%M:%S"), text))
        return str(CRASH_PATH)
    except Exception:
        return "(crash.log 写入失败)"


def _last_line(text: str) -> str:
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    return lines[-1] if lines else text.strip()


def install_crash_handler(popup: bool = True) -> None:
    """未捕获异常 → 写 crash.log；能弹窗就弹一次，避免"双击没反应"无从下手。"""
    import traceback

    def hook(exc_type, exc, tb):
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        where = _write_crash(text)
        try:
            sys.stderr.write(text)
        except Exception:
            pass
        if not popup:
            return
        try:
            import tkinter as tk
            from tkinter import messagebox
            r = tk.Tk()
            r.withdraw()
            messagebox.showerror(
                "AutoFish 出错了",
                "程序遇到未处理的错误，已记录到：\n%s\n\n%s" % (where, _last_line(text)))
            r.destroy()
        except Exception:
            pass

    sys.excepthook = hook

    def thread_hook(args):
        # 工作线程里的异常不弹窗（会刷屏），但一定要留痕
        text = "".join(traceback.format_exception(
            args.exc_type, args.exc_value, args.exc_traceback))
        line = "[%s] 后台线程 %s 异常：\n%s" % (
            now_hms(), getattr(args.thread, "name", "?"), text)
        _write_crash(line)
        try:
            APP_DIR.mkdir(parents=True, exist_ok=True)
            with LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    threading.excepthook = thread_hook


# ---------------------------------------------------------------- 配置


@dataclass
class Config:
    # 存在判据
    score_min: float = 0.80
    # 归属判据
    level_min_db: float = -28.0
    # 电平上限：0 表示「不设上限」。设成非 0（如 -22）时，电平必须落在
    # [level_min_db, level_max_db] 区间内才判为自己的鱼 —— 太响（贴脸的敌方、
    # 爆炸、枪声）也一并拦掉。默认关，向后兼容。
    level_max_db: float = 0.0
    pan_max_db: float = 5.0
    # 节奏
    cooldown_s: float = 1.5
    interrupt_delay_s: float = 2.0
    recast_delay_s: float = 4.0
    fallback_s: float = 20.0
    # 环境
    device: str = ""
    foreground_title: str = "三角洲行动"
    # 进程名兜底：万一窗口标题为空或被改，靠 exe 文件名也能认出游戏。
    # 实测：三角洲行动 = DeltaForceClient-Win64-Shipping.exe（子串、不分大小写）。
    foreground_process: str = "deltaforce"
    require_cursor_in_game: bool = True
    # 模板
    wav: str = ""
    template_mode: str = "slim"          # slim | full
    template_floor_db: float = -30.0     # slim 模式下判定「咬钩声本体」的门限
    # 热键
    hotkey_toggle: str = "F9"
    hotkey_quit: str = "F10"
    # 留档
    save_hits: bool = True
    save_rejects: bool = True
    hits_max: int = 200
    # 其它
    notify_sound: bool = False
    window_on_top: bool = True
    # 停靠位置：窗口右下角距屏幕右/下边缘的距离（逻辑像素，会自动按 DPI 缩放）。
    # 默认值是按实测截图标定的 —— 下边距留出游戏右下角 HUD（弹药/快捷栏）的高度。
    snap_margin_right: int = 5
    snap_margin_bottom: int = 152

    @staticmethod
    def load() -> "Config":
        c = Config()
        if CONFIG_PATH.exists():
            try:
                raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                for k, v in raw.items():
                    if hasattr(c, k):
                        setattr(c, k, v)
            except Exception as e:
                print("[!] 配置读取失败，用默认值：%s" % e)
        return c

    def save(self) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- WAV / 模板


def read_wav_mono(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        nch, sw, rate, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        raw = w.readframes(n)
    if sw == 1:
        a = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif sw == 2:
        a = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif sw == 4:
        a = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    elif sw == 3:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        v = np.where(v >= 8388608, v - 16777216, v)
        a = v.astype(np.float32) / 8388608.0
    else:
        raise ValueError("不支持的位宽: %d" % sw)
    if nch > 1:
        a = a.reshape(-1, nch).mean(axis=1)
    return a.astype(np.float32), rate


def write_wav_mono(path: Path, x: np.ndarray, rate: int = SR) -> None:
    y = np.clip(x, -1.0, 1.0)
    pcm = (y * 32767.0).astype("<i2").tobytes()
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


def resample_linear(a: np.ndarray, src: int, dst: int) -> np.ndarray:
    if src == dst:
        return a
    n = int(round(len(a) * dst / src))
    x = np.linspace(0, len(a) - 1, n)
    return np.interp(x, np.arange(len(a)), a).astype(np.float32)


def trim_silence(a: np.ndarray, rate: int, floor_db: float = -45.0, pad_ms: int = 20) -> np.ndarray:
    thr = 10 ** (floor_db / 20.0)
    idx = np.where(np.abs(a) > thr)[0]
    if idx.size == 0:
        return a
    pad = rate * pad_ms // 1000
    return a[max(int(idx[0]) - pad, 0): min(int(idx[-1]) + pad, len(a))]


def active_span(a: np.ndarray, rate: int, floor_db: float, frame_ms: int = 10,
                pad_ms: int = 20) -> tuple[int, int]:
    """找「咬钩声本体」：10 ms 分帧能量超门限的首尾，前后各留 pad_ms。"""
    hop = max(1, rate * frame_ms // 1000)
    nf = len(a) // hop
    if nf < 2:
        return 0, len(a)
    e = (a[:nf * hop].reshape(nf, hop) ** 2).mean(axis=1)
    loud = np.where(10 * np.log10(e + 1e-12) > floor_db)[0]
    if loud.size == 0:
        return 0, len(a)
    pad = rate * pad_ms // 1000
    return max(0, int(loud[0]) * hop - pad), min(len(a), (int(loud[-1]) + 1) * hop + pad)


def bootstrap_template(cfg: Config) -> tuple[np.ndarray, int]:
    """确定模板来源并预处理。返回 (模板, 采样率)。"""
    APP_DIR.mkdir(parents=True, exist_ok=True)
    src = None
    if cfg.wav:
        p = Path(cfg.wav)
        if not p.exists():
            raise SystemExit("找不到指定的模板文件：%s" % p)
        src = p
    elif TEMPLATE_PATH.exists():
        src = TEMPLATE_PATH
    else:
        raise SystemExit(
            "找不到模板。请用 --wav 指定一个咬钩音 wav（仓库 测试音频/01_原始音效_1秒5.wav 即可），"
            "或把该文件复制为 %s" % TEMPLATE_PATH)

    a, rate = read_wav_mono(src)
    if rate != SR:
        a = resample_linear(a, rate, SR)
        rate = SR
    if cfg.template_mode == "slim":
        lo, hi = active_span(a, rate, cfg.template_floor_db)
        a = a[lo:hi]
    else:
        a = trim_silence(a, rate)
    return a.astype(np.float32), rate


# ---------------------------------------------------------------- 打分器


class Detector:
    """归一化互相关（匹配滤波）。只回答「音频里有没有咬钩音」。"""

    def __init__(self, template: np.ndarray):
        self.t = template.astype(np.float32)
        self.n = len(self.t)
        fft_len = 1
        need = self.n + SR // 5
        while fft_len < need:
            fft_len <<= 1
        self.fft_len = fft_len
        pad = np.zeros(fft_len, dtype=np.float32)
        pad[: self.n] = self.t
        self.tspec = np.conj(np.fft.rfft(pad))
        self.t_energy = float(np.dot(self.t, self.t)) or 1.0
        self._buf = np.zeros(fft_len, dtype=np.float32)

    def score(self, buf: np.ndarray) -> tuple[float, int]:
        if len(buf) > self.fft_len:          # 缓冲比 FFT 还长时只取最新的一段
            buf = buf[-self.fft_len:]
        count = len(buf)
        if count < self.n:
            return 0.0, -1
        b = self._buf
        b[:count] = buf
        if count < self.fft_len:
            b[count:] = 0.0
        corr = np.fft.irfft(np.fft.rfft(b) * self.tspec, n=self.fft_len)
        cum = np.concatenate(([0.0], np.cumsum(np.square(buf, dtype=np.float64))))
        nw = count - self.n + 1
        ew = cum[self.n: self.n + nw] - cum[0:nw]
        good = ew > 1e-12
        if not good.any():
            return 0.0, -1
        s = np.zeros(nw)
        s[good] = np.abs(corr[:nw][good]) / np.sqrt(ew[good] * self.t_energy)
        k = int(np.argmax(s))
        return float(s[k]), k


class RollingBuffer:
    def __init__(self, capacity: int):
        self.cap = capacity
        self.buf = np.zeros((capacity, CH), dtype=np.float32)
        self.count = 0

    def push(self, block: np.ndarray) -> None:
        n = len(block)
        if n >= self.cap:
            self.buf[:] = block[-self.cap:]
            self.count = self.cap
            return
        if self.count + n > self.cap:
            keep = self.cap - n
            self.buf[:keep] = self.buf[self.count - keep:self.count]
            self.count = keep
        self.buf[self.count:self.count + n] = block
        self.count += n

    def clear(self) -> None:
        self.count = 0


# ---------------------------------------------------------------- Windows API

_u32 = ctypes.windll.user32


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.c_void_p)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("mi", _MOUSEINPUT)]


INPUT_MOUSE = 0
_u32.SendInput.argtypes = [ctypes.c_uint, ctypes.POINTER(_INPUT), ctypes.c_int]
_u32.SendInput.restype = ctypes.c_uint


def mouse_click() -> bool:
    """注入一次左键点击。

    返回 False 表示**系统没有接受这个事件** —— 按 Microsoft 的说明，被 UIPI 拦下时
    SendInput 会返回 0。所以这个返回值就是"点了但没生效"的硬证据，
    比推断"游戏是不是管理员"可靠得多。（这两个 API 都返回 0，错误码不带原因。）
    """
    down = _INPUT(INPUT_MOUSE, _MOUSEINPUT(0, 0, 0, MOUSEEVENTF_LEFTDOWN, 0, None))
    up = _INPUT(INPUT_MOUSE, _MOUSEINPUT(0, 0, 0, MOUSEEVENTF_LEFTUP, 0, None))
    n1 = _u32.SendInput(1, ctypes.byref(down), ctypes.sizeof(_INPUT))
    time.sleep(0.01)
    n2 = _u32.SendInput(1, ctypes.byref(up), ctypes.sizeof(_INPUT))
    return bool(n1 and n2)


def window_text(hwnd: int) -> str:
    """窗口标题；hwnd 无效或无标题返回 ""。"""
    if not hwnd:
        return ""
    n = _u32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    _u32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value or ""


def foreground_window() -> int:
    """前台窗口句柄，取不到返回 0。"""
    return _u32.GetForegroundWindow() or 0


def foreground_title() -> str:
    return window_text(foreground_window())


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def cursor_root_window() -> tuple[int, str]:
    """光标所在的最顶层窗口，返回 (hwnd, 标题)。"""
    p = POINT()
    if not _u32.GetCursorPos(ctypes.byref(p)):
        return 0, ""
    h = _u32.WindowFromPoint(p)
    if not h:
        return 0, ""
    root = _u32.GetAncestor(h, GA_ROOT) or h
    return root, window_text(root)


def cursor_root_title() -> str:
    return cursor_root_window()[1]


def key_down(name: str) -> bool:
    vk = VK.get(name.upper())
    if vk is None:
        return False
    return bool(_u32.GetAsyncKeyState(vk) & 0x8000)


# --- 权限/提权检查 -----------------------------------------------------------
# mouse_event 是把事件注入到「桌面输入队列」，不需要任何特殊权限就能对
# 别的进程的窗口生效。唯一的硬门槛是 Windows 的 UIPI：
# 低完整性级别的进程**不能**向高完整性级别（以管理员运行）的窗口注入输入 ——
# 而且是被**静默丢弃**，没有任何报错。所以必须自己查、自己喊。

_k32 = ctypes.windll.kernel32
_adv = ctypes.windll.advapi32
_k32.GetCurrentProcess.restype = ctypes.c_void_p
_k32.OpenProcess.restype = ctypes.c_void_p
_k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
_k32.CloseHandle.argtypes = [ctypes.c_void_p]
_k32.QueryFullProcessImageNameW.restype = ctypes.c_int
_k32.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                            ctypes.c_wchar_p,
                                            ctypes.POINTER(ctypes.c_ulong)]
_adv.OpenProcessToken.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                  ctypes.POINTER(ctypes.c_void_p)]
_adv.GetTokenInformation.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
                                     ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]
TOKEN_QUERY = 0x0008
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_TOKEN_ELEVATION = 20


class _ELEVATION(ctypes.Structure):
    _fields_ = [("value", ctypes.c_uint32)]


def _token_elevated(hprocess) -> bool | None:
    try:
        tok = ctypes.c_void_p()
        if not _adv.OpenProcessToken(hprocess, TOKEN_QUERY, ctypes.byref(tok)):
            return None
        try:
            e, sz = _ELEVATION(), ctypes.c_uint32()
            if not _adv.GetTokenInformation(tok, _TOKEN_ELEVATION, ctypes.byref(e),
                                            ctypes.sizeof(e), ctypes.byref(sz)):
                return None
            return bool(e.value)
        finally:
            _k32.CloseHandle(tok)
    except Exception:
        return None


def process_elevated(hwnd: int | None = None) -> bool | None:
    """hwnd 为空时查自己；返回 True/False，查不到返回 None。"""
    try:
        if hwnd:
            pid = ctypes.c_ulong()
            _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if not pid.value:
                return None
            h = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
            if not h:
                return None
            try:
                return _token_elevated(h)
            finally:
                _k32.CloseHandle(h)
        return _token_elevated(_k32.GetCurrentProcess())
    except Exception:
        return None


def window_process_name(hwnd: int) -> str:
    """窗口所属进程的 exe 文件名（小写、不含路径）。查不到返回 ""。

    用 PROCESS_QUERY_LIMITED_INFORMATION：权限要求低，对提升权限的
    进程一般也放行 —— 比读窗口标题更稳（标题可能为空、被改、或被
    更高权限的窗口挡住读不到）。
    """
    if not hwnd:
        return ""
    pid = ctypes.c_ulong()
    _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return ""
    h = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not h:
        return ""
    try:
        size = ctypes.c_ulong(1024)
        buf = ctypes.create_unicode_buffer(1024)
        if not _k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return ""
        return (buf.value or "").rsplit("\\", 1)[-1].lower()
    finally:
        _k32.CloseHandle(h)


def window_gate_ok(fg_title: str, fg_proc: str, hwnd: int, title: str) -> bool:
    """前台/光标窗口是否算「在游戏里」：标题含 fg_title **或** 进程名含 fg_proc。

    两个都为空 = 不设限。之前只匹配标题，2026-09-19 发现配置被测试
    污染成 __NO_SUCH__ 后永远判不中 —— 顺手把进程名兜底加上：
    游戏改标题、标题为空这类情况也认得出（实测进程是
    DeltaForceClient-Win64-Shipping.exe，标题「三角洲行动 」结尾带空格）。
    """
    if not fg_title and not fg_proc:
        return True
    if fg_title and fg_title in (title or "").lower():
        return True
    return bool(fg_proc) and fg_proc in window_process_name(hwnd)


# ---------------------------------------------------------------- 引擎

IDLE, RUNNING, BUSY = "已停止", "挂机中", "收鱼中"


class Engine:
    def __init__(self, cfg: Config, events: "queue.Queue | None" = None):
        self.cfg = cfg
        self.events = events or queue.Queue()
        self.detector: Detector | None = None
        self.template: np.ndarray | None = None
        self.act_lo, self.act_hi = 0, 0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._seq_busy = threading.Event()
        self._lock = threading.Lock()
        self.metrics = {"score": 0.0, "level_db": -99.0, "pan_db": 0.0,
                        "state": IDLE, "count": 0, "last": "—", "verdict": "—",
                        "fg_ok": False, "cursor_ok": False, "device": "", "error": ""}
        self._last_trigger = time.monotonic()
        self._last_cand_abs = -(10 ** 9)   # 同一次咬钩音只判定一次
        self._hits_written: list[Path] = []
        self._calib: list[dict] | None = None
        self.dry = False              # 标定期间为 True：只记录，绝不点击
        self._self_elevated = process_elevated()
        self._fg_hwnd = None
        self._fg_elev: bool | None = None
        self._fg_title_last = None
        self._uipi_warned = False

    # -------------------------------------------------- 生命周期

    def prepare(self) -> None:
        self.template, rate = bootstrap_template(self.cfg)
        self.detector = Detector(self.template)
        self.act_lo, self.act_hi = 0, len(self.template)
        msg = ("模板 mode=%s  %d 采样 (%.0f ms)  FFT=%d  检测延迟约 %.0f ms"
               % (self.cfg.template_mode, len(self.template),
                  len(self.template) * 1000.0 / rate, self.detector.fft_len,
                  len(self.template) * 1000.0 / rate + BLOCK * 1000.0 / SR))
        self.emit("log", msg)
        self.emit("log", "权限自检：本进程%s管理员权限。鼠标注入本身不需要任何特殊权限，"
                         "但如果游戏是以管理员身份运行的，Windows 的 UIPI 会把点击静默丢弃。"
                  % ("已获得" if self._self_elevated else "未获得"))

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        if self.detector is None:
            self.prepare()
        self._stop.clear()
        self._last_trigger = time.monotonic()
        self._last_cand_abs = -(10 ** 9)
        with self._lock:
            self.metrics["count"] = 0
            self.metrics["error"] = ""
        self._thread = threading.Thread(target=self._loop, name="AutoFish", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def join(self, timeout: float = 2.0) -> None:
        if self._thread:
            self._thread.join(timeout)

    def emit(self, kind: str, msg: str = "", **kw) -> None:
        kw["kind"] = kind
        if msg:
            kw["msg"] = msg
        self.events.put(kw)
        if kind == "log":
            line = "[%s] %s" % (now_hms(), kw.get("msg", ""))
            print(line, flush=True)
            try:
                APP_DIR.mkdir(parents=True, exist_ok=True)
                with LOG_PATH.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception:
                pass

    # -------------------------------------------------- 主循环

    def _pick_device(self):
        loops = [m for m in sc.all_microphones(include_loopback=True)
                 if getattr(m, "isloopback", False)]
        if not loops:
            loops = list(sc.all_microphones(include_loopback=True))
        hint = (self.cfg.device or "").lower()
        if hint:
            for m in loops:
                if hint in m.name.lower():
                    return m
        spk = sc.default_speaker()
        if spk:
            for m in loops:
                if spk.name in m.name:
                    return m
        return loops[0]

    def _loop(self) -> None:
        try:
            mic = self._pick_device()
        except Exception as e:
            self.emit("error", msg="找不到可用的音频设备：%s" % e)
            with self._lock:
                self.metrics["state"] = IDLE
                self.metrics["error"] = str(e)
            return
        self.emit("log", "采集设备：%s" % mic.name)
        with self._lock:
            self.metrics["device"] = mic.name
            self.metrics["state"] = RUNNING
        self._capture_loop(mic)

    def _capture_loop(self, mic) -> None:
        win = self.detector.n + SR // 5
        roll = RollingBuffer(win)
        abs_total = 0
        last_fg = 0.0
        try:
            with mic.recorder(samplerate=SR, channels=CH, blocksize=BLOCK) as rec:
                while not self._stop.is_set():
                    data = rec.record(numframes=BLOCK)
                    if data is None or len(data) == 0:
                        continue
                    if data.ndim == 1:
                        data = np.stack([data, data], axis=1)
                    if data.shape[1] == 1:
                        data = np.repeat(data, 2, axis=1)
                    roll.push(data[:, :2].astype(np.float32))
                    abs_total += len(data)
                    if roll.count < win:
                        continue
                    mono = roll.buf[:, 0] * 0.5 + roll.buf[:, 1] * 0.5
                    t = time.monotonic()

                    if t - last_fg > 0.2:
                        last_fg = t
                        fg = (getattr(self.cfg, "foreground_title", "") or "").lower()
                        fp = (getattr(self.cfg, "foreground_process", "") or "").lower()
                        hwnd = foreground_window()
                        title = window_text(hwnd)
                        f_ok = window_gate_ok(fg, fp, hwnd, title)
                        if self.cfg.require_cursor_in_game:
                            c_hwnd, c_title = cursor_root_window()
                        else:
                            c_hwnd, c_title = hwnd, title
                        c_ok = window_gate_ok(fg, fp, c_hwnd, c_title)
                        with self._lock:
                            self.metrics["fg_ok"] = f_ok
                            self.metrics["cursor_ok"] = c_ok
                        if hwnd != self._fg_hwnd:
                            self._fg_hwnd = hwnd
                            self._fg_elev = process_elevated(hwnd)
                        # 窗口不匹配时把"实际是谁"说出来，否则用户只看到一句 REJECT_WINDOW
                        if title != self._fg_title_last:
                            self._fg_title_last = title
                            if (fg or fp) and not f_ok:
                                self.emit("log",
                                          "前台窗口「%s」（进程 %s）不是游戏"
                                          "（要含「%s」或进程「%s」），因此不会点击。"
                                          "（测试音频时看到这句是正常的：说明电平和声像两道闸门都已通过）"
                                          % (title or "无标题",
                                             window_process_name(hwnd) or "?",
                                             self.cfg.foreground_title,
                                             getattr(self.cfg, "foreground_process", "")))
                            elif (fg or fp) and f_ok and not c_ok:
                                self.emit("log",
                                          "前台窗口没问题，但光标停在「%s」上，因此不会点击"
                                          % (c_title or "(无标题)"))
                        if self._fg_elev and self._self_elevated is False \
                                and not self._uipi_warned:
                            self._uipi_warned = True
                            with self._lock:
                                self.metrics["error"] = "游戏是管理员、脚本不是 → 点击会被丢弃"
                            self.emit("log",
                                      "⚠ 游戏进程以管理员身份运行，而本脚本没有。"
                                      "Windows 的 UIPI 会静默丢弃注入的点击（不报错、不生效）。"
                                      "请改用管理员身份运行 autofish.bat。")

                    fb = self.cfg.fallback_s
                    if fb > 0 and not self.dry and not self._seq_busy.is_set() \
                            and t - self._last_trigger >= fb:
                        if self._action_ok():
                            self._last_trigger = t
                            ok = mouse_click()
                            self.emit("log", "兜底补杆：%.0f 秒无触发，点击一次%s"
                                      % (fb, "" if ok else "（但没生效）"))
                            if not ok:
                                self._inject_failed()

                    score, lag = self.detector.score(mono)
                    with self._lock:
                        self.metrics["score"] = round(score, 3)
                    # 标定期间用更低的记录门限，才能看到"差一点命中"的分布
                    if score < (CALIB_FLOOR if self.dry else self.cfg.score_min):
                        continue
                    if t - self._last_trigger < self.cfg.cooldown_s:
                        continue
                    if self._seq_busy.is_set():
                        continue

                    # 同一段咬钩音在缓冲里滑过时，匹配位置的绝对下标是不变的；
                    # 据此去重，避免一声咬钩被反复判定。
                    abs_pos = abs_total - roll.count + lag
                    if abs_pos - self._last_cand_abs < self.detector.n // 2:
                        continue
                    self._last_cand_abs = abs_pos

                    a, b = lag + self.act_lo, lag + self.act_hi
                    seg = mono[a:b]
                    segL = roll.buf[a:b, 0]
                    segR = roll.buf[a:b, 1]
                    rl = float(np.sqrt(np.mean(segL ** 2))) if segL.size else 0.0
                    rr = float(np.sqrt(np.mean(segR ** 2))) if segR.size else 0.0
                    level = float(np.sqrt(np.mean(seg ** 2))) if seg.size else 0.0
                    pan = db(rl) - db(rr)
                    coh = self._coh(segL, segR)
                    cen, hf = self._spec(seg)

                    row = {
                        "t": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "score": round(score, 4),
                        "level_db": round(db(level), 2),
                        "pan_db": round(pan, 2),
                        "rms_l_db": round(db(rl), 2),
                        "rms_r_db": round(db(rr), 2),
                        "coherence": round(coh, 4),
                        "centroid_hz": round(cen, 1),
                        "hf_ratio": round(hf, 5),
                        "lag_ms": round(lag * 1000.0 / SR, 1),
                    }
                    with self._lock:
                        self.metrics["level_db"] = row["level_db"]
                        self.metrics["pan_db"] = row["pan_db"]

                    if self._calib is not None:
                        self._calib.append(row)

                    verdict, note = self._judge(row)
                    row["verdict"] = verdict
                    row["note"] = note
                    accept = (verdict == "ACCEPT" and not self.dry)

                    if accept:
                        # 先把鼠标点出去，再做写盘。_record 要写 wav + csv，
                        # 磁盘 I/O（还可能被杀软拦一下）能让关键路径平白多出几十毫秒，
                        # 而"收杆"这个窗口很紧。
                        self._last_trigger = t
                        with self._lock:
                            self.metrics["count"] += 1
                            self.metrics["last"] = now_hms()
                            self.metrics["state"] = BUSY
                        self._seq_busy.set()
                        threading.Thread(target=self._sequence,
                                         name="AutoFishSeq", daemon=True).start()

                    self._record(row, roll, lag)   # 需要用 clear() 之前的缓冲
                    with self._lock:
                        self.metrics["verdict"] = "%s %s" % (verdict, note)
                    self.emit("hit", row=row)

                    if accept:
                        roll.clear()
        except Exception as e:
            self.emit("error", msg="采集异常：%s" % e)
            with self._lock:
                self.metrics["error"] = str(e)
        finally:
            with self._lock:
                self.metrics["state"] = IDLE
            self.emit("log", "采集已停止")

    # -------------------------------------------------- 判定

    def _judge(self, row: dict) -> tuple[str, str]:
        if row["score"] < self.cfg.score_min:
            return "BELOW", "低于存在判据门限（%.3f < %.2f，只有标定期会记）" % (
                row["score"], self.cfg.score_min)
        if row["level_db"] < self.cfg.level_min_db:
            return "REJECT_LEVEL", "电平 %.1f < %.1f（判为他人：离得远）" % (
                row["level_db"], self.cfg.level_min_db)
        if self.cfg.level_max_db > self.cfg.level_min_db \
                and row["level_db"] > self.cfg.level_max_db:
            return "REJECT_LEVEL_HI", "电平 %.1f > %.1f（判为他人：太响，像贴脸的枪/爆炸）" % (
                row["level_db"], self.cfg.level_max_db)
        if abs(row["pan_db"]) > self.cfg.pan_max_db:
            return "REJECT_PAN", "声像差 %+.1f 超过 ±%.1f（判为他人：不在正前方）" % (
                row["pan_db"], self.cfg.pan_max_db)
        if not self._action_ok():
            return "REJECT_WINDOW", "前台窗口或光标不在游戏内"
        return "ACCEPT", ("通过（标定中：只记录，不点击）" if self.dry
                          else "通过 → 执行收鱼序列")

    def _action_ok(self) -> bool:
        fg = (getattr(self.cfg, "foreground_title", "") or "").lower()
        fp = (getattr(self.cfg, "foreground_process", "") or "").lower()
        if not fg and not fp:
            return True
        hwnd = foreground_window()
        if not window_gate_ok(fg, fp, hwnd, window_text(hwnd)):
            return False
        if self.cfg.require_cursor_in_game:
            c_hwnd, c_title = cursor_root_window()
            if not window_gate_ok(fg, fp, c_hwnd, c_title):
                return False
        return True

    @staticmethod
    def _coh(x: np.ndarray, y: np.ndarray) -> float:
        if len(x) < 16:
            return 0.0
        x = x - x.mean()
        y = y - y.mean()
        d = float(np.sqrt(np.dot(x, x) * np.dot(y, y)))
        return float(np.dot(x, y) / d) if d > 1e-12 else 0.0

    @staticmethod
    def _spec(x: np.ndarray) -> tuple[float, float]:
        if len(x) < 64:
            return 0.0, 0.0
        sp = np.abs(np.fft.rfft(x * np.hanning(len(x)))) ** 2
        fr = np.fft.rfftfreq(len(x), 1.0 / SR)
        tot = float(sp.sum())
        if tot <= 1e-20:
            return 0.0, 0.0
        return float((sp * fr).sum() / tot), float(sp[fr >= 4000].sum() / tot)

    # -------------------------------------------------- 留档

    def _record(self, row: dict, roll: RollingBuffer, lag: int) -> None:
        try:
            APP_DIR.mkdir(parents=True, exist_ok=True)
            new = not CSV_PATH.exists()
            with CSV_PATH.open("a", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
                if new:
                    w.writeheader()
                w.writerow({k: row.get(k, "") for k in CSV_FIELDS})
        except Exception:
            pass
        if not self.cfg.save_hits:
            return
        if row["verdict"] != "ACCEPT" and not self.cfg.save_rejects:
            return
        try:
            HITS_DIR.mkdir(parents=True, exist_ok=True)
            pre = int(0.25 * SR)
            post = int(len(self.template) + 0.25 * SR)
            lo = max(0, lag - pre)
            hi = min(roll.count, lag + post)
            mono = roll.buf[lo:hi, 0] * 0.5 + roll.buf[lo:hi, 1] * 0.5
            ts = time.strftime("%Y%m%d_%H%M%S")
            name = "h_%s_%06d_%s.wav" % (ts, int(row["score"] * 1000), row["verdict"])
            p = HITS_DIR / name
            write_wav_mono(p, mono)
            self._hits_written.append(p)
            lim = max(10, int(self.cfg.hits_max))
            while len(self._hits_written) > lim:
                old = self._hits_written.pop(0)
                try:
                    old.unlink(missing_ok=True)
                except Exception:
                    pass
            row["wav"] = str(p)
        except Exception:
            pass

    # -------------------------------------------------- 收鱼序列

    def _sequence(self) -> None:
        cfg = self.cfg
        steps: list[str] = []

        def step(name: str) -> bool:
            if not self._action_ok():
                steps.append("窗口不符，取消")
                return False
            if not mouse_click():
                steps.append(name + "✗未生效")
                self._inject_failed()
                return False
            steps.append(name)
            return True

        try:
            if not step("抬杆"):
                return
            if not self._sleep(cfg.interrupt_delay_s):
                return
            if not step("打断检视"):
                return
            if not self._sleep(cfg.recast_delay_s):
                return
            if not step("抛竿"):
                return
        finally:
            self._seq_busy.clear()
            self._last_trigger = time.monotonic()
            with self._lock:
                # 只有引擎仍在跑，才恢复"挂机中"。
                # 收鱼序列要跑满 6 秒，用户在这个窗口里按 F9 停掉引擎后，
                # 主循环已经退出并把状态置成"已停止"；若这里无条件写回 RUNNING，
                # 界面就会被锁死在"挂机中"——这正是之前的 bug。
                self.metrics["state"] = IDLE if self._stop.is_set() else RUNNING
            self.emit("log", "收鱼序列：" + (" → ".join(steps) if steps else "无动作"))

    def _inject_failed(self) -> None:
        with self._lock:
            self.metrics["error"] = "点击未生效（多半是 UIPI）"
        self.emit("log",
                  "⚠ 点击没有生效 —— 系统没有接受注入的事件。"
                  "最常见的原因：游戏以管理员身份运行，而本脚本没有，"
                  "会被 Windows 的 UIPI 静默丢弃。请用管理员身份运行 autofish.bat。")

    def _sleep(self, s: float) -> bool:
        end = time.monotonic() + max(0.0, s)
        while time.monotonic() < end:
            if self._stop.is_set():
                return False
            time.sleep(0.05)
        return True

    # -------------------------------------------------- 标定

    def calibrate(self, seconds: float) -> list[dict]:
        """只听不点地跑一段时间，收集每次候选命中的特征。

        期间 self.dry = True：判定照常做（必须做，否则不知道门限会把谁拦下），
        但既不发起点鼠标，也不触发兜底补杆。
        """
        self._calib = []
        was_dry = self.dry
        self.dry = True
        self.start()
        try:
            t0 = time.monotonic()
            while time.monotonic() - t0 < seconds and self.running:
                time.sleep(0.1)
        finally:
            self.stop()
            self.join(3.0)
            self.dry = was_dry
        rows = self._calib or []
        self._calib = None
        return rows


def suggest(rows: list[dict], cfg: Config) -> dict:
    """按「自己的鱼最响、最居中」这一假设给出建议门限。真实裁判是 hits 目录里的录音。

    电平这道闸门建议成**区间**：用自己的鱼的电平中位数当中心、上下各留几 dB。
    旧逻辑「最高 − 12」有个前提假设——最响的候选 = 自己的鱼——一旦标定时混进
    枪声/爆炸，会把上限顶到很响，建议出来的下限也跟着松，反而漏掉太响的干扰。
    改成中位数中心 + 双向留余量更贴合实际（用户实测自己的鱼稳定在 -24 附近）。
    """
    if not rows:
        return {}
    lv = np.array([r["level_db"] for r in rows])
    pn = np.abs(np.array([r["pan_db"] for r in rows]))
    sc = np.array([r["score"] for r in rows])
    med = float(np.percentile(lv, 50))
    lo = round(float(np.clip(med - 6.0, -60.0, -6.0)), 1)
    hi = round(float(np.clip(med + 6.0, lo + 2.0, 0.0)), 1)
    out = {
        "score_min": round(float(np.clip(np.percentile(sc, 5) - 0.03, 0.6, 0.95)), 2),
        "level_min_db": lo,
        "level_max_db": hi,
        "pan_max_db": round(float(np.clip(np.percentile(pn, 25) + 2.0, 2.0, 12.0)), 1),
        "_n": len(rows),
        "_level_max": round(float(lv.max()), 1),
        "_level_p50": round(float(np.percentile(lv, 50)), 1),
        "_pan_p50": round(float(np.percentile(pn, 50)), 1),
    }
    return out


def print_calibration(rows: list[dict], cfg: Config) -> None:
    print("\n" + "=" * 62)
    print("标定结果：共 %d 次候选命中（score ≥ %.2f）" % (len(rows), cfg.score_min))
    if not rows:
        print("一次候选都没有。检查设备、音量，或把 score_min 临时调低再跑。")
        print("=" * 62)
        return
    lv = np.array([r["level_db"] for r in rows])
    pn = np.abs(np.array([r["pan_db"] for r in rows]))
    sc = np.array([r["score"] for r in rows])
    print("\n%-10s %8s %8s %8s %8s" % ("", "最小", "中位", "最大", "P90"))
    for name, arr in (("归一分", sc), ("电平dB", lv), ("|声像差|dB", pn)):
        print("%-10s %8.2f %8.2f %8.2f %8.2f" % (name, arr.min(), np.median(arr),
                                                 arr.max(), np.percentile(arr, 90)))
    s = suggest(rows, cfg)
    print("\n建议门限（假设：自己的鱼最响、最居中）")
    print("  score_min    = %.2f" % s["score_min"])
    print("  level_min_db = %.1f   level_max_db = %.1f   （电平中位 %.1f ± 6 dB）"
          % (s["level_min_db"], s["level_max_db"], s["_level_p50"]))
    print("  pan_max_db   = %.1f   （|声像差|中位 %.1f + 2）"
          % (s["pan_max_db"], s["_pan_p50"]))
    print("\n真正的裁判是 %s 里的录音：听一遍就知道哪次是自己的。" % HITS_DIR)
    print("确认后把上面三行填进 %s" % CONFIG_PATH)
    print("=" * 62 + "\n")


# ---------------------------------------------------------------- 控制台前端


def console_run(engine: Engine) -> None:
    cfg = engine.cfg
    stop = threading.Event()
    print("\n控制台模式。热键：%s 启停 / %s 退出（Ctrl+C 亦可）"
          % (cfg.hotkey_toggle, cfg.hotkey_quit))
    engine.start()
    last_toggle = last_quit = False
    try:
        while not stop.is_set():
            td = key_down(cfg.hotkey_toggle)
            qd = key_down(cfg.hotkey_quit)
            if td and not last_toggle:
                if engine.running:
                    engine.stop(); engine.join(2.0); print("[%s] 已停止" % now_hms())
                else:
                    engine.start(); print("[%s] 已启动" % now_hms())
            if qd and not last_quit:
                break
            last_toggle, last_quit = td, qd
            while True:
                try:
                    ev = engine.events.get_nowait()
                except queue.Empty:
                    break
                if ev.get("kind") == "hit":
                    r = ev["row"]
                    print("[%s] %-13s 分 %.3f  电平 %6.1f  声像 %+5.1f  %s"
                          % (now_hms(), r["verdict"], r["score"], r["level_db"],
                             r["pan_db"], r["note"]))
            m = dict(engine.metrics)
            sys.stdout.write("\r  实时 分 %.3f  电平 %6.1f dBFS  声像 %+5.1f dB  "
                             "触发 %d 次  %s   "
                             % (m["score"], m["level_db"], m["pan_db"], m["count"],
                                "前台OK" if m["fg_ok"] else "前台不符"))
            sys.stdout.flush()
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    finally:
        engine.stop()
        engine.join(3.0)
        print("\n已退出。")


# ---------------------------------------------------------------- 图形界面


def enable_dpi_awareness() -> float:
    """把进程标记为 DPI-aware，返回系统缩放系数。

    不做这件事的话，在 125%/150% 缩放的显示器上，Windows 会先把整个窗口按 96 DPI
    画成位图、再整体放大 —— 结果就是所有文字都发虚。
    """
    u32 = ctypes.windll.user32
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)      # PER_MONITOR_DPI_AWARE
    except Exception:
        try:
            u32.SetProcessDPIAware()
        except Exception:
            pass
    try:
        scale = ctypes.windll.shcore.GetScaleFactorForDevice(0) / 100.0
    except Exception:
        try:
            scale = u32.GetDpiForSystem() / 96.0
        except Exception:
            scale = 1.0
    return scale if scale > 0 else 1.0


def _pick_family(root, prefs, fallback):
    import tkinter.font as tkfont
    try:
        fams = set(tkfont.families(root))
    except Exception:
        return fallback
    for p in prefs:
        if p in fams:
            return p
    return fallback


TICK_COLOR = "#b45309"      # 门限刻度线；条超出门限时也用这个色


class BipolarMeter:
    """以 0 为中心的双向指示条。

    声像差是有正负、且「0 = 正中间」才算自己的量，用普通进度条会读错 ——
    所以在中点画一条基准线，条从中间向左右生长，两侧刻度标出门限位置。
    门限是**可变**的（改完参数点「应用门限」），所以要能重画：见 set_gate()。
    """

    def __init__(self, parent, width: int, height: int, span: float, bg: str, gate: float):
        import tkinter as tk
        self.w, self.h, self.span, self.gate = width, height, span, gate
        self._last = 0.0
        self.cv = tk.Canvas(parent, width=width, height=height, bg=bg,
                            highlightthickness=0, bd=0)
        self.cv.create_rectangle(1, 1, width - 1, height - 1, outline="#d1d5db", fill="")
        # 四条短竖线：上下各两条，标出 ±门限 的位置
        self.ticks = [
            self.cv.create_line(0, 1, 0, 5, fill=TICK_COLOR),
            self.cv.create_line(0, height - 5, 0, height - 1, fill=TICK_COLOR),
            self.cv.create_line(0, 1, 0, 5, fill=TICK_COLOR),
            self.cv.create_line(0, height - 5, 0, height - 1, fill=TICK_COLOR),
        ]
        self.bar = self.cv.create_rectangle(width // 2, 3, width // 2, height - 3,
                                            fill="#15803d", outline="")
        self.mid = self.cv.create_line(width // 2, 1, width // 2, height - 1,
                                       fill="#9ca3af")
        self.set_gate(gate)

    def tick_x(self) -> tuple[float, float]:
        half = self.w / 2.0 - 2
        g = min(self.gate, self.span) / self.span * half
        return self.w / 2.0 - g, self.w / 2.0 + g

    def set_gate(self, gate: float) -> None:
        """门限变了就重画刻度线。

        刻度是构造时按当时的门限画死的。如果只改 cfg 不重画，刻度会停在旧位置，
        和旁边写着的门限对不上 —— 纯误导。填充条的绿/橙判断用的也是 self.gate，
        所以顺带按新门限重刷一次颜色。
        """
        self.gate = float(gate)
        xl, xr = self.tick_x()
        for item, x in zip(self.ticks, (xl, xl, xr, xr)):
            _, y0, _, y1 = self.cv.coords(item)
            self.cv.coords(item, x, y0, x, y1)
        self.set(self._last)

    def pack(self, **kw):
        self.cv.pack(**kw)

    def set(self, v: float) -> None:
        self._last = float(v)
        v = max(-self.span, min(self.span, float(v)))
        cx = self.w / 2.0
        half = cx - 2
        x = cx + half * (v / self.span)
        self.cv.coords(self.bar, min(cx, x), 3, max(cx, x), self.h - 3)
        self.cv.itemconfig(self.bar,
                           fill="#15803d" if abs(v) <= self.gate else TICK_COLOR)
        self.cv.tag_raise(self.mid)


def gui_run(engine: Engine) -> None:
    import tkinter as tk
    import tkinter.font as tkfont
    from tkinter import messagebox, ttk

    cfg = engine.cfg
    scale = enable_dpi_awareness()

    def px(v: float) -> int:
        return int(round(v * scale))

    root = tk.Tk()
    root.title("AutoFish · %s" % cfg.foreground_title)

    # 无控制台运行时 sys.stderr 是 None，Tk 的默认处理只能丢到空设备里。
    # 官方文档明说这种情况应当覆盖这个钩子 —— 于是把它接到统一的崩溃处理器上。
    def _tk_callback_error(exc, val, tb):
        sys.excepthook(exc, val, tb)

    root.report_callback_exception = _tk_callback_error

    root.geometry("%dx%d" % (px(800), px(700)))
    root.minsize(px(560), px(240))
    root.tk.call("tk", "scaling", 96.0 * scale / 72.0)
    try:
        root.attributes("-topmost", bool(cfg.window_on_top))
    except Exception:
        pass

    # 字体一律用「负数字号」＝像素尺寸，绕开 point→pixel 换算，缩放完全可控
    fam = _pick_family(root, ["Microsoft YaHei UI", "Microsoft YaHei", "微软雅黑", "Segoe UI"],
                       "TkDefaultFont")
    monofam = _pick_family(root, ["Consolas", "Cascadia Mono", "Courier New"], "TkFixedFont")
    for nm in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont",
               "TkTooltipFont", "TkIconFont"):
        try:
            tkfont.nametofont(nm).configure(family=fam, size=-px(14))
        except Exception:
            pass
    F_H1 = tkfont.Font(root=root, family=fam, size=-px(19), weight="bold")
    F_H2 = tkfont.Font(root=root, family=fam, size=-px(15), weight="bold")
    F_TXT = tkfont.Font(root=root, family=fam, size=-px(14))
    F_SUB = tkfont.Font(root=root, family=fam, size=-px(12))
    F_MONO = tkfont.Font(root=root, family=monofam, size=-px(12))

    style = ttk.Style()
    BG = style.lookup("TFrame", "background") or root.cget("background")
    GREY = "#6b7280"
    pad = {"padx": px(12), "pady": px(5)}
    # 窗口宽度（逻辑像素）。两种形态都用它 —— 560 是"内容刚好放得下"的最小值：
    # 再窄的话右侧门限提示会被裁掉（以前拖到最窄时就是这样）。
    # 展开态要在这个宽度里放下参数区和全部按钮，所以参数排 3 列、按钮分两行。
    WIN_W = 560

    state_var = tk.StringVar(value="● " + IDLE)
    count_var = tk.StringVar(value="0")
    last_var = tk.StringVar(value="—")
    verdict_var = tk.StringVar(value="等待咬钩音…")
    info_var = tk.StringVar(value="")
    score_txt = tk.StringVar(value="0.000")
    level_txt = tk.StringVar(value="— dBFS")
    pan_txt = tk.StringVar(value="— dB")
    gate_txt = {k: tk.StringVar() for k in ("score", "level", "pan")}
    calib_done = {"v": False}

    top = ttk.Frame(root)
    top.pack(fill="x", padx=px(14), pady=(px(12), px(2)))
    state_lbl = tk.Label(top, textvariable=state_var, font=F_H1, fg=GREY)
    state_lbl.pack(side="left")
    ttk.Label(top, text="触发", font=F_SUB, foreground=GREY).pack(side="left", padx=(px(20), px(5)))
    ttk.Label(top, textvariable=count_var, font=F_H2).pack(side="left")
    ttk.Label(top, text="上次", font=F_SUB, foreground=GREY).pack(side="left", padx=(px(20), px(5)))
    ttk.Label(top, textvariable=last_var, font=F_TXT).pack(side="left")

    info = ttk.Label(root, textvariable=info_var, font=F_SUB, foreground=GREY,
                     anchor="w", justify="left")
    info.pack(fill="x", padx=px(14), pady=(0, px(6)))

    meters = ttk.LabelFrame(root, text=" 实时指标（存=存在判据 属=归属判据） ")
    meters.pack(fill="x", **pad)
    setters: dict = {}
    gate_setters: dict = {}      # 门限变化时需要重画的控件（目前只有声像差的刻度线）
    for key, label, txt, lo, hi in (
        ("score", "归一分", score_txt, 0.0, 1.0),
        ("level", "电平", level_txt, -60.0, 0.0),
        ("pan", "声像差", pan_txt, -20.0, 20.0),
    ):
        row = ttk.Frame(meters)
        row.pack(fill="x", padx=px(12), pady=px(4))
        ttk.Label(row, text=label, font=F_TXT, width=6, anchor="w").pack(side="left")
        if key == "pan":
            bm = BipolarMeter(row, px(250), px(16), 20.0, BG, cfg.pan_max_db)
            bm.pack(side="left", padx=px(10))
            setters[key] = bm.set
            gate_setters[key] = bm.set_gate
        else:
            pb = ttk.Progressbar(row, maximum=(hi - lo), length=px(250), value=0)
            pb.pack(side="left", padx=px(10))

            def _mk(vbar=pb, vlo=lo, vhi=hi):
                return lambda v: vbar.configure(
                    value=max(vlo, min(vhi, float(v))) - vlo)
            setters[key] = _mk()
        ttk.Label(row, textvariable=txt, font=F_H2, width=10, anchor="e").pack(side="left")
        ttk.Label(row, textvariable=gate_txt[key], font=F_SUB,
                  foreground=GREY).pack(side="left", padx=(px(16), 0))

    verdict = tk.Label(root, textvariable=verdict_var, font=F_TXT, anchor="w",
                       justify="left", padx=px(12), pady=px(7), bg=BG, fg=GREY)
    verdict.pack(fill="x", padx=px(12), pady=(px(2), px(4)))

    gates = ttk.LabelFrame(root, text=" 参数（改完点「应用门限」） ")
    gates.pack(fill="x", **pad)
    gvars = {}
    items = (
        ("score_min", "归一分 ≥", ""),
        ("level_min_db", "电平 ≥", "dBFS"),
        ("level_max_db", "电平 ≤", "dBFS"),
        ("pan_max_db", "声像差 ≤", "dB"),
        ("cooldown_s", "冷却", "s"),
        ("interrupt_delay_s", "打断延时", "s"),
        ("recast_delay_s", "重抛延时", "s"),
        ("fallback_s", "兜底", "s"),
    )
    for i, (key, label, unit) in enumerate(items):
        r, c = divmod(i, 3)          # 3 列：4 列放不进 560 逻辑像素
        cell = ttk.Frame(gates)
        cell.grid(row=r, column=c, sticky="w", padx=(px(12), px(16)), pady=px(6))
        ttk.Label(cell, text=label, font=F_SUB).pack(side="left")
        v = tk.StringVar(value=str(getattr(cfg, key)))
        ttk.Entry(cell, textvariable=v, width=7, font=F_TXT).pack(side="left", padx=px(4))
        if unit:
            ttk.Label(cell, text=unit, font=F_SUB, foreground=GREY).pack(side="left")
        gvars[key] = v
    for c in range(3):
        gates.columnconfigure(c, weight=1)

    # 三种按钮行：展开态占两行（560 宽度放不下 7 个按钮），挂机中只留一行
    btns_full = ttk.Frame(root)      # 第 1 行：主操作 + 两个开关
    btns_full2 = ttk.Frame(root)     # 第 2 行：不常用的工具
    btns_mini = ttk.Frame(root)
    ui = {"collapsed": False, "log": False,     # 日志默认收起，需要时手动展开
          "log_before": False, "log_forced": False}
    log_btn_var = tk.StringVar(value="日志 ▾")
    toggle_btn_var = tk.StringVar(value="开始挂机 (%s)" % cfg.hotkey_toggle)

    def apply_cfg():
        news = {}
        for key, var in gvars.items():
            try:
                news[key] = float(var.get())
            except ValueError:
                messagebox.showerror("输入有误", "所有参数都必须是数字")
                return
        for key, val in news.items():
            setattr(cfg, key, val)
        cfg.score_min = min(0.98, max(0.30, cfg.score_min))
        cfg.cooldown_s = max(0.2, cfg.cooldown_s)
        cfg.fallback_s = max(0.0, cfg.fallback_s)
        # 电平上限 0 表示「关上限」，负值（如 -22）是真实上限，**必须原样保留**。
        # 不要在这里夹取 —— 之前 `max(0.0, ...)` 把负上限全夹成 0，等于一应用就关掉上限。
        # 「区间搞反（上限 ≤ 下限）」的兜底在 _judge 里做：此时只保留下限那道检查。
        cfg.save()
        # 门限改了，声像差那条的刻度线必须跟着重画，否则刻度停在旧位置，
        # 和右边写的门限对不上（填充条的绿/橙判断也依赖它）。
        if "pan" in gate_setters:
            gate_setters["pan"](cfg.pan_max_db)
        for key in gvars:
            gvars[key].set(str(getattr(cfg, key)))
        if cfg.level_max_db > cfg.level_min_db:
            log("参数已保存：分 ≥ %.2f ｜ 电平 %.1f~%.1f ｜ 声像 ≤ %.1f ｜ 冷却 %.1f ｜ 兜底 %.0f"
                % (cfg.score_min, cfg.level_min_db, cfg.level_max_db,
                   cfg.pan_max_db, cfg.cooldown_s, cfg.fallback_s))
        else:
            log("参数已保存：分 ≥ %.2f ｜ 电平 ≥ %.1f（无上限） ｜ 声像 ≤ %.1f ｜ 冷却 %.1f ｜ 兜底 %.0f"
                % (cfg.score_min, cfg.level_min_db, cfg.pan_max_db,
                   cfg.cooldown_s, cfg.fallback_s))

    def toggle():
        if engine.running:
            engine.stop(); engine.join(2.0)
            log("已停止")
            set_collapsed(False)      # 停止后自动恢复完整面板
        else:
            engine.start()
            log("已启动")
            set_collapsed(True)       # 开始后自动收起，方便把窗口贴到屏幕角落

    ttk.Button(btns_full, textvariable=toggle_btn_var,
               command=toggle).pack(side="left", padx=(0, px(6)))

    def do_calib():
        dlg = tk.Toplevel(root)
        dlg.title("标定")
        dlg.transient(root); dlg.grab_set()
        dlg.resizable(False, False)
        ttk.Label(dlg, text="标定时长（秒）", font=F_TXT).pack(padx=px(20), pady=(px(16), px(4)))
        sv = tk.StringVar(value="120")
        ent = ttk.Entry(dlg, textvariable=sv, width=10, font=F_TXT)
        ent.pack()
        ent.focus_set()
        ttk.Label(dlg, text="这段时间内只听不点，可以正常钓鱼。", font=F_SUB,
                  foreground=GREY).pack(padx=px(20), pady=px(10))
        out = {}

        def go():
            try:
                out["n"] = max(10.0, float(sv.get()))
            except ValueError:
                out["n"] = 120.0
            dlg.destroy()

        ttk.Button(dlg, text="开始", command=go).pack(pady=(0, px(16)))
        dlg.update_idletasks()
        x = root.winfo_rootx() + (root.winfo_width() - dlg.winfo_width()) // 2
        y = root.winfo_rooty() + (root.winfo_height() - dlg.winfo_height()) // 3
        dlg.geometry("+%d+%d" % (max(0, x), max(0, y)))
        root.wait_window(dlg)
        if "n" not in out:
            return
        was = engine.running
        if was:
            engine.stop(); engine.join(2.0)
        log("标定开始：%.0f 秒，只听不点。可以正常钓鱼，要收杆请自己点。" % out["n"])

        def work():
            rows = engine.calibrate(out["n"])
            s = suggest(rows, cfg)
            engine.events.put({"kind": "calib", "rows": len(rows), "s": s})
            if was:
                engine.start()
                log("已恢复挂机")
        threading.Thread(target=work, daemon=True).start()

    def quit_app():
        engine.stop(); engine.join(2.0)
        root.destroy()

    def snap_dock() -> None:
        """把窗口停靠到屏幕右下角。

        锚点取**客户区**的右下角（= 你肉眼看到的那个边），而不是外框 ——
        外框还含标题栏和一圈不可见的拖拽边框，按外框对齐会差出一条标题栏。
        改尺寸或开关日志后重新贴一次，窗口就朝左上方向生长，右下角钉在原地。

        边距来自配置 snap_margin_right / snap_margin_bottom（逻辑像素）。
        """
        root.update_idletasks()
        # winfo_x/y 是外框左上角，winfo_rootx/rooty 是客户区左上角，差值就是边框/标题栏
        dx = max(0, root.winfo_rootx() - root.winfo_x())
        dy = max(0, root.winfo_rooty() - root.winfo_y())
        x = root.winfo_screenwidth() - root.winfo_width() - dx - px(cfg.snap_margin_right)
        y = root.winfo_screenheight() - root.winfo_height() - dy - px(cfg.snap_margin_bottom)
        root.geometry("+%d+%d" % (max(0, x), max(0, y)))

    # 第一行：主操作 + 两个开关
    ttk.Button(btns_full, text="▾ 收起面板",
               command=lambda: set_collapsed(True)).pack(side="left", padx=(0, px(6)))
    ttk.Button(btns_full, textvariable=log_btn_var,
               command=lambda: toggle_log()).pack(side="left", padx=(0, px(6)))

    # 第二行：偶尔才用的工具
    ttk.Button(btns_full2, text="标定…", command=do_calib).pack(side="left", padx=(0, px(6)))
    ttk.Button(btns_full2, text="应用门限", command=apply_cfg).pack(side="left", padx=(0, px(6)))
    ttk.Button(btns_full2, text="数据目录",
               command=lambda: os.startfile(APP_DIR)).pack(side="left", padx=(0, px(6)))
    ttk.Button(btns_full2, text="退出 (%s)" % cfg.hotkey_quit,
               command=quit_app).pack(side="left")

    # 挂机中的精简行：只保留停止、展开、日志、贴角
    ttk.Button(btns_mini, textvariable=toggle_btn_var,
               command=toggle).pack(side="left", padx=(0, px(6)))
    ttk.Button(btns_mini, text="▴ 展开面板",
               command=lambda: set_collapsed(False)).pack(side="left", padx=(0, px(6)))
    ttk.Button(btns_mini, textvariable=log_btn_var,
               command=lambda: toggle_log()).pack(side="left", padx=(0, px(6)))
    ttk.Button(btns_mini, text="↘ 贴右下角",
               command=snap_dock).pack(side="left")

    logbox = ttk.LabelFrame(root, text=" 日志 ")
    txt = tk.Text(logbox, height=12, width=50, wrap="none", font=F_MONO, relief="flat",
                  highlightthickness=0, background=BG, foreground="#374151",
                  padx=px(6), pady=px(4))
    sb = ttk.Scrollbar(logbox, command=txt.yview)
    txt.configure(yscrollcommand=sb.set)
    # 窗口窄，而日志是 wrap="none"（保住等宽对齐），所以横向得给个滚动条，
    # 否则长行（判定那些带说明的行）右侧就看不到了。
    sbx = ttk.Scrollbar(logbox, orient="horizontal", command=txt.xview)
    txt.configure(xscrollcommand=sbx.set)
    sbx.pack(side="bottom", fill="x")     # 先占底边，再放右侧和内容
    sb.pack(side="right", fill="y")
    txt.pack(side="left", fill="both", expand=True)

    def log(msg: str) -> None:
        txt.insert("end", "[%s] %s\n" % (now_hms(), msg))
        txt.see("end")

    # ---------------------------------------------------------- 折叠布局
    #
    # 常态（未挂机）：顶部监测 → 指标 → 判定 → 参数 → 完整按钮行 →（可选）日志
    # 折叠态（挂机中）：只留顶部监测 → 指标 → 判定，加一行精简按钮
    # 日志区默认收起，始终由用户手动控制（挂机中会被强制收起，避免挡视野）

    def fit_window() -> None:
        """把窗口收缩到内容刚好放得下，不浪费一个像素。

        宽度**定死**成 WIN_W（展开态和收起态一样宽），不跟着当前宽度走 ——
        否则拖窄一次就再也回不来，而且拖动后右下角的停靠位置也会错位。
        """
        root.update_idletasks()
        w = px(WIN_W)
        h = root.winfo_reqheight()
        root.minsize(px(WIN_W), h)
        root.geometry("%dx%d" % (w, h))

    def relayout() -> None:
        for w in (top, info, meters, verdict, gates,
                  btns_full, btns_full2, btns_mini, logbox):
            w.pack_forget()
        top.pack(fill="x", padx=px(14), pady=(px(12), px(2)))
        info.pack(fill="x", padx=px(14), pady=(0, px(6)))
        meters.pack(fill="x", **pad)
        verdict.pack(fill="x", padx=px(12), pady=(px(2), px(4)))
        if not ui["collapsed"]:
            gates.pack(fill="x", **pad)
            btns_full.pack(fill="x", **pad)
            btns_full2.pack(fill="x", **pad)
        else:
            btns_mini.pack(fill="x", **pad)
        if ui["log"]:
            logbox.pack(fill="both", expand=True, padx=px(12), pady=(px(6), px(12)))
        log_btn_var.set("日志 ▴" if ui["log"] else "日志 ▾")
        txt.configure(height=8 if ui["collapsed"] else 12)
        # 窗口只有 560 逻辑像素宽：状态行和判定行内容长了要换行，而不是被裁掉
        info.configure(wraplength=px(WIN_W) - px(30))
        verdict.configure(wraplength=px(WIN_W) - px(26))
        fit_window()
        # 尺寸变了就重新贴角：右下角钉住不动，窗口朝左上生长。
        # 不加这一步的话，收起态下展开日志会让窗口直接长到屏幕外面去。
        snap_dock()

    def set_collapsed(on: bool) -> None:
        on = bool(on)
        if on == ui["collapsed"]:
            return
        if on:
            # 挂机中强制收起日志，否则照样挡视野；但记住用户原本的选择，停止后还回去
            ui["log_before"] = ui["log"]
            ui["log"] = False
            ui["log_forced"] = False
        elif not ui["log_forced"]:
            # 只在用户"没有自己动过日志"时才还原，否则会把他在挂机中的手动展开抹掉
            ui["log"] = ui.get("log_before", False)
        ui["collapsed"] = on
        relayout()          # relayout 里会重新贴角

    def toggle_log() -> None:
        if ui["collapsed"]:
            ui["log_forced"] = True      # 挂机中用户自己开合的日志，展开面板时要尊重
        ui["log"] = not ui["log"]
        relayout()

    engine.prepare()
    # 用 emit 而不是本地的 log()：log() 只写界面文本框，emit 会同时落进
    # autofish.log —— 排查故障时"这次到底是 exe 在跑还是脚本在跑"很关键。
    engine.emit("log", "运行方式：%s（%s）"
                % ("独立 exe" if getattr(sys, "frozen", False) else "Python 脚本",
                   sys.executable))
    log("模板就绪，检测延迟约 %.0f ms" % (len(engine.template) * 1000.0 / SR + 20))

    def poll():
        try:
            while True:
                ev = engine.events.get_nowait()
                k = ev.get("kind")
                if k == "log":
                    log(ev["msg"])
                elif k == "error":
                    log("错误：" + ev["msg"])
                elif k == "hit":
                    r = ev["row"]
                    log("%-13s 分 %.3f  电平 %6.1f  声像 %+5.1f  %s"
                        % (r["verdict"], r["score"], r["level_db"], r["pan_db"], r["note"]))
                elif k == "calib":
                    n, s = ev["rows"], ev["s"]
                    if not s:
                        log("标定结束：没有候选命中")
                    else:
                        log("标定结束：%d 次候选；建议 分 ≥ %.2f / 电平 %.1f~%.1f / 声像 ≤ %.1f"
                            % (n, s["score_min"], s["level_min_db"], s["level_max_db"],
                               s["pan_max_db"]))
                        gvars["score_min"].set(str(s["score_min"]))
                        gvars["level_min_db"].set(str(s["level_min_db"]))
                        gvars["level_max_db"].set(str(s["level_max_db"]))
                        gvars["pan_max_db"].set(str(s["pan_max_db"]))
                        log("建议值已填进输入框，确认后点「应用门限」")
                    calib_done["v"] = True
        except queue.Empty:
            pass

        m = engine.metrics
        st = m["state"]
        # 以引擎的真实存活状态为准：线程已退出就一定是"已停止"，
        # 这样即使某条后台路径残留了状态，界面也不会显示"挂机中"。
        if not engine.running and st != IDLE:
            st = IDLE
        state_var.set("● " + st)
        state_lbl.configure(fg={"挂机中": "#15803d", "收鱼中": "#b45309"}.get(st, GREY))
        toggle_btn_var.set(("■ 停止 (%s)" if engine.running else "开始挂机 (%s)")
                           % cfg.hotkey_toggle)
        count_var.set(str(m["count"]))
        last_var.set(m["last"])

        v = m["verdict"]
        if v.startswith("ACCEPT"):
            verdict_var.set("✔ " + v.split(" ", 1)[-1])
            verdict.configure(bg="#E7F6EC", fg="#10693a")
        elif v.startswith("REJECT"):
            verdict_var.set("✘ " + v.split(" ", 1)[-1])
            verdict.configure(bg="#FDF3E7", fg="#9a5b00")
        else:
            verdict_var.set(v)
            verdict.configure(bg=BG, fg=GREY)

        info_var.set("设备 %s      门限 %s      前台 %s      光标 %s%s"
                     % (m["device"] or "—",
                        "已标定" if calib_done["v"] else "默认值",
                        "OK" if m["fg_ok"] else "不符",
                        "OK" if m["cursor_ok"] else "不在游戏内",
                        ("      ⚠ " + m["error"]) if m["error"] else ""))

        # 门限提示：窗口只有 560 逻辑像素宽，写全 "归属判据 · 门限 ≥ -35.0 dBFS"
        # 会被右侧裁掉。所以统一用 "存/属 + 门限" 的紧凑写法，
        # 「存=存在判据、属=归属判据」的图例放在指标框标题里。
        gate_txt["score"].set("存 ≥ %.2f" % cfg.score_min)
        if cfg.level_max_db > cfg.level_min_db:
            gate_txt["level"].set("属 %.0f~%.0f" % (cfg.level_min_db, cfg.level_max_db))
        else:
            gate_txt["level"].set("属 ≥ %.0f" % cfg.level_min_db)
        gate_txt["pan"].set("属 ≤ %.0f" % cfg.pan_max_db)

        score_txt.set("%.3f" % m["score"])
        level_txt.set("%.1f dBFS" % m["level_db"])
        pan_txt.set("%+.1f dB" % m["pan_db"])
        setters["score"](m["score"])
        setters["level"](m["level_db"])
        setters["pan"](m["pan_db"])
        root.after(100, poll)

    last_toggle = {"v": False}
    last_quit = {"v": False}

    def hotkeys():
        td = key_down(cfg.hotkey_toggle)
        qd = key_down(cfg.hotkey_quit)
        if td and not last_toggle["v"]:
            toggle()
        if qd and not last_quit["v"]:
            quit_app()
            return
        last_toggle["v"], last_quit["v"] = td, qd
        root.after(60, hotkeys)

    log("就绪。点「开始挂机」或按 %s。运行期间请勿移动鼠标——点击打在光标当前位置。"
        % cfg.hotkey_toggle)
    log("挂机开始后参数区会自动收起、窗口缩到最小并停靠到右下角；"
        "日志用「日志 ▾」按钮随时展开。")
    relayout()
    # 启动就停靠到右下角。放在 after 里是因为窗口还没映射时
    # 拿不到标题栏/边框的实际厚度，会差出一条标题栏。
    root.after(30, snap_dock)
    root.after(100, poll)
    root.after(60, hotkeys)
    root.mainloop()


# ---------------------------------------------------------------- 入口


def main() -> None:
    ap = argparse.ArgumentParser(description="三角洲行动 · 独立自动钓鱼")
    ap.add_argument("--nogui", action="store_true", help="纯控制台模式")
    ap.add_argument("--calibrate", type=float, metavar="SEC", help="标定：只听不点，跑 SEC 秒")
    ap.add_argument("--list", action="store_true", help="列出回环设备")
    ap.add_argument("--wav", help="指定咬钩音模板 wav")
    ap.add_argument("--device", help="回环设备名关键字，如 FxSound")
    ap.add_argument("--template-mode", choices=["slim", "full"], help="slim=只取咬钩声本体（默认）")
    args = ap.parse_args()
    scale = enable_dpi_awareness()

    if args.list:
        for m in sc.all_microphones(include_loopback=True):
            print("  %-46s%s" % (m.name, "  [loopback]" if getattr(m, "isloopback", False) else ""))
        print("  默认输出：%s" % sc.default_speaker())
        return

    APP_DIR.mkdir(parents=True, exist_ok=True)
    cfg = Config.load()
    if args.wav:
        cfg.wav = args.wav
    if args.device:
        cfg.device = args.device
    if args.template_mode:
        cfg.template_mode = args.template_mode
    if not CONFIG_PATH.exists():
        cfg.save()

    engine = Engine(cfg)

    if args.calibrate:
        engine.prepare()
        print("标定模式：%.0f 秒，只听不点。开始钓鱼吧。" % args.calibrate)
        rows = engine.calibrate(args.calibrate)
        print_calibration(rows, cfg)
        return

    if args.nogui:
        console_run(engine)
    else:
        try:
            gui_run(engine)
        except ImportError as e:
            print("[!] 无法加载图形界面（%s），退回控制台模式。" % e)
            console_run(engine)


if __name__ == "__main__":
    ensure_std_streams()
    install_crash_handler()
    try:
        main()
    except SystemExit:
        raise
    except BaseException:
        # 走到这里说明是"启动阶段就崩了"。控制台下 excepthook 会照常打 traceback；
        # 无控制台（exe）时会写 crash.log 并弹窗，不让它变成"双击没反应"。
        sys.excepthook(*sys.exc_info())
        sys.exit(1)
