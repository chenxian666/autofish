#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fish_probe.py —— 三角洲行动「自动钓鱼」误触发诊断探针

目的：量化「自己的鱼上钩」与「别人的鱼上钩」在音频特征上是否可分。
基于经典算法（WASAPI 回环 + 归一化互相关），但做了三件关键改动：

  1. 不把左右声道平均成单声道 —— 保留方位信息（自己的鱼多半居中/近场，
     别人的鱼带 3D 定位，会偏向一侧）。
  2. 额外记录每个命中的绝对电平（RMS）—— 原来的算法做了能量归一化，
     把「响度」这个距离线索完全抹掉了。
  3. 记录频谱质心 / 高频占比 —— 远处的音源通常被低通 + 混响。

用法：
  python fish_probe.py --list                       列出回环设备
  python fish_probe.py --selftest                   自检：播放参考音效并验证能被检出
  python fish_probe.py --record mine  --seconds 300 采「只有我钓鱼」的样本
  python fish_probe.py --record other --seconds 300 采「我旁边有人钓鱼」的样本
  python fish_probe.py --report mine.csv other.csv  对比两个样本的分布

产出：每次命中一行 CSV，另有每 0.5 秒一条的环境底噪行（kind=bg）用于对比。
"""

import argparse
import csv
import os
import sys
import time
import threading
import math

import numpy as np

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import soundcard as sc

# ---------------------------------------------------------------- 参考音效

# 默认参考音效：仓库自带的原始咬钩音（也可用 --wav 指定任意 wav）
DEF_WAV = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "测试音频", "01_原始音效_1秒5.wav")


def load_wav_mono(path):
    """读 WAV（8/16/24/32bit、PCM/float），多声道求平均降单声道，返回 float32[-1,1] 与采样率。"""
    import wave
    with wave.open(path, "rb") as w:
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


def trim_silence(a, rate, floor_db=-45.0, pad_ms=20):
    """复刻原程序的 TrimSilence：按门限掐头去尾，前后各留 pad_ms。"""
    thr = 10 ** (floor_db / 20.0)
    idx = np.where(np.abs(a) > thr)[0]
    if idx.size == 0:
        return a
    pad = rate * pad_ms // 1000
    lo = max(int(idx[0]) - pad, 0)
    hi = min(int(idx[-1]) + pad, len(a))
    return a[lo:hi]


def resample_linear(a, src_rate, dst_rate):
    if src_rate == dst_rate:
        return a
    n = int(round(len(a) * dst_rate / src_rate))
    x = np.linspace(0, len(a) - 1, n)
    return np.interp(x, np.arange(len(a)), a).astype(np.float32)


def template_active_span(a, rate, floor_db=-30.0, frame_ms=10):
    """找出模板里「真正的咬钩声」那一段（10ms 分帧能量超门限的首尾），
    用于把特征计算限定在有信息量的区间，避免被前导底噪稀释。"""
    hop = max(1, rate * frame_ms // 1000)
    nf = len(a) // hop
    if nf < 2:
        return 0, len(a)
    e = (a[:nf * hop].reshape(nf, hop) ** 2).mean(axis=1)
    loud = np.where(10 * np.log10(e + 1e-12) > floor_db)[0]
    if loud.size == 0:
        return 0, len(a)
    return int(loud[0]) * hop, min(len(a), (int(loud[-1]) + 1) * hop)


# ---------------------------------------------------------------- 打分器

class Detector:
    """归一化互相关（匹配滤波）。与原程序数学等价，但用 rfft 且全程向量化。"""

    def __init__(self, template, fft_len):
        self.t = template.astype(np.float32)
        self.n = len(self.t)
        self.fft_len = fft_len
        pad = np.zeros(fft_len, dtype=np.float32)
        pad[: self.n] = self.t
        self.tspec = np.conj(np.fft.rfft(pad))
        self.t_energy = float(np.dot(self.t, self.t)) or 1.0
        self._buf = np.zeros(fft_len, dtype=np.float32)
        self._cum = np.zeros(1, dtype=np.float64)

    def score(self, buf):
        """buf: float32，长度 >= 模板长度。返回 (最高分, 命中位置, 各位置分数数组用不到)"""
        count = len(buf)
        if count < self.n:
            return 0.0, -1
        b = self._buf
        b[:count] = buf
        if count < self.fft_len:
            b[count:] = 0.0
        spec = np.fft.rfft(b)
        corr = np.fft.irfft(spec * self.tspec, n=self.fft_len)
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


def corr_coeff(x, y):
    """两个等长信号的归一化互相关系数，衡量双声道相干性。"""
    if len(x) < 16:
        return 0.0
    x = x - x.mean()
    y = y - y.mean()
    d = math.sqrt(float(np.dot(x, x)) * float(np.dot(y, y)))
    return float(np.dot(x, y) / d) if d > 1e-12 else 0.0


def spectral_features(x, rate):
    """返回 (频谱质心 Hz, 4kHz 以上能量占比)。"""
    if len(x) < 64:
        return 0.0, 0.0
    w = x * np.hanning(len(x))
    sp = np.abs(np.fft.rfft(w)) ** 2
    freqs = np.fft.rfftfreq(len(x), 1.0 / rate)
    tot = float(sp.sum())
    if tot <= 1e-20:
        return 0.0, 0.0
    centroid = float((sp * freqs).sum() / tot)
    hf = float(sp[freqs >= 4000.0].sum() / tot)
    return centroid, hf


def db(v):
    return 20.0 * math.log10(v + 1e-12)


# ---------------------------------------------------------------- 设备

def pick_loopback(device_hint=None):
    mics = [m for m in sc.all_microphones(include_loopback=True)]
    loops = [m for m in mics if getattr(m, "isloopback", False)]
    if not loops:
        loops = mics
    if device_hint:
        for m in loops:
            if device_hint.lower() in m.name.lower():
                return m
        print("[!] 没找到名字含 %r 的回环设备，改用默认输出。" % device_hint)
    spk = sc.default_speaker()
    for m in loops:
        if spk and spk.name in m.name:
            return m
    return loops[0]


def list_devices():
    print("--- 回环采集设备（可作为 --device 的取值）---")
    for m in sc.all_microphones(include_loopback=True):
        flag = " [loopback]" if getattr(m, "isloopback", False) else ""
        print("   %s%s" % (m.name, flag))
    print("--- 默认输出 ---")
    print("   %s" % (sc.default_speaker() or "无").__str__())


# ---------------------------------------------------------------- 主流程

def build(device_hint, wav):
    tpl, rate = load_wav_mono(wav)
    tpl = trim_silence(tpl, rate)
    if rate != 48000:
        tpl = resample_linear(tpl, rate, 48000)
        rate = 48000
    fft_len = 1
    need = len(tpl) + rate // 5
    while fft_len < need:
        fft_len <<= 1
    act_lo, act_hi = template_active_span(tpl, rate)
    det = Detector(tpl, fft_len)
    print("模板: %d 采样 (%.0f ms) @ %d Hz；有效段 %.0f–%.0f ms；FFT 长度 %d"
          % (len(tpl), len(tpl) * 1000.0 / rate, rate,
             act_lo * 1000.0 / rate, act_hi * 1000.0 / rate, fft_len))
    return tpl, rate, det, fft_len, act_lo, act_hi


FIELDS = ["kind", "t_iso", "seq", "score_mono", "score_l", "score_r",
          "rms_l_db", "rms_r_db", "pan_db", "coherence", "centroid_hz", "hf_ratio",
          "level_db", "lag_ms"]


def make_row(kind, seq, t0, score_m, score_l, score_r, blk_l, blk_r, rate, seg_lo, seg_hi):
    """seg_lo/seg_hi 是缓冲区内的绝对下标：命中时指向匹配到的咬钩声，环境行指向最近一段。"""
    lo = max(0, int(seg_lo))
    hi = min(len(blk_l), int(seg_hi))
    seg_l = blk_l[lo:hi]
    seg_r = blk_r[lo:hi]
    rl = float(np.sqrt(np.mean(seg_l ** 2))) if seg_l.size else 0.0
    rr = float(np.sqrt(np.mean(seg_r ** 2))) if seg_r.size else 0.0
    cen, hf = spectral_features((seg_l + seg_r) * 0.5, rate) if seg_l.size else (0.0, 0.0)
    return {
        "kind": kind,
        "t_iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t0)),
        "seq": seq,
        "score_mono": round(score_m, 4),
        "score_l": round(score_l, 4),
        "score_r": round(score_r, 4),
        "rms_l_db": round(db(rl), 2),
        "rms_r_db": round(db(rr), 2),
        "pan_db": round(db(rl) - db(rr), 2),
        "coherence": round(corr_coeff(seg_l, seg_r), 4),
        "centroid_hz": round(cen, 1),
        "hf_ratio": round(hf, 5),
    }


def record(args):
    tpl, rate, det, fft_len, act_lo, act_hi = build(args.device, args.wav)
    mic = pick_loopback(args.device)
    print("采集设备: %s" % mic.name)
    win = len(tpl) + rate // 5
    rolling = np.zeros((win, 2), dtype=np.float32)
    filled = 0
    seq = 0
    last_hit = -99.0
    last_bg = 0.0
    t_start = time.time()
    out = open(args.out, "w", newline="", encoding="utf-8-sig")
    wr = csv.DictWriter(out, fieldnames=FIELDS)
    wr.writeheader()
    print("开始采集 -> %s（Ctrl+C 停止）" % args.out)
    try:
        with mic.recorder(samplerate=rate, channels=2, blocksize=args.block) as rec:
            while True:
                if args.seconds and time.time() - t_start >= args.seconds:
                    break
                data = rec.record(numframes=args.block)
                if data is None or len(data) == 0:
                    continue
                if data.ndim == 1:
                    data = np.stack([data, data], axis=1)
                if data.shape[1] == 1:
                    data = np.repeat(data, 2, axis=1)
                blk = data[:, :2].astype(np.float32)
                # 推入环形缓冲
                n = len(blk)
                if n >= win:
                    rolling[:] = blk[-win:]
                    filled = win
                else:
                    if filled + n > win:
                        keep = win - n
                        rolling[:keep] = rolling[filled - keep:filled]
                        filled = keep
                    rolling[filled:filled + n] = blk
                    filled += n
                if filled < win:
                    continue
                now = time.time()
                mono = rolling.mean(axis=1)
                # 近期能量太低就跳过打分，省 CPU
                tail_db = db(float(np.sqrt(np.mean(mono[-rate // 10:] ** 2))))
                if tail_db < args.noise_floor:
                    if now - last_bg >= 0.5:
                        last_bg = now
                        seq += 1
                        row = make_row("bg", seq, now, 0.0, 0.0, 0.0,
                                       rolling[:, 0], rolling[:, 1], rate,
                                       win - rate // 5, win)
                        row["level_db"] = round(tail_db, 2)
                        wr.writerow(row)
                        out.flush()
                    continue
                sm, lag = det.score(mono)
                if now - last_bg >= 0.5:
                    last_bg = now
                    seq += 1
                    row = make_row("bg", seq, now, round(sm, 4), 0.0, 0.0,
                                   rolling[:, 0], rolling[:, 1], rate,
                                   win - rate // 5, win)
                    row["level_db"] = round(tail_db, 2)
                    row["lag_ms"] = round(lag * 1000.0 / rate, 1)
                    wr.writerow(row)
                    out.flush()
                sys.stdout.write("\r实时最高分 %.3f  电平 %.1f dBFS  已记录命中 %d 次   "
                                 % (sm, tail_db, seq))
                sys.stdout.flush()
                if sm >= args.floor and now - last_hit >= args.refractory:
                    sl, _ = det.score(rolling[:, 0].copy())
                    sr, _ = det.score(rolling[:, 1].copy())
                    seq += 1
                    row = make_row("hit", seq, now, sm, sl, sr,
                                   rolling[:, 0], rolling[:, 1], rate,
                                   lag + act_lo, lag + act_hi)
                    row["level_db"] = round(tail_db, 2)
                    row["lag_ms"] = round(lag * 1000.0 / rate, 1)
                    wr.writerow(row)
                    out.flush()
                    last_hit = now
                    sys.stdout.write("\n  [命中] %.3f (L %.3f / R %.3f)  声像 %+.1f dB  相干 %.2f  质心 %.0f Hz\n"
                                     % (sm, sl, sr, row["pan_db"], row["coherence"], row["centroid_hz"]))
    except KeyboardInterrupt:
        pass
    finally:
        out.close()
    print("\n采集结束，已写入 %s" % args.out)


def selftest(args):
    tpl, rate, det, fft_len, act_lo, act_hi = build(args.device, args.wav)
    mic = pick_loopback(args.device)
    spk = sc.default_speaker()
    print("采集设备: %s" % mic.name)
    print("播放设备: %s" % (spk.name if spk else "无"))
    win = len(tpl) + rate // 5
    peak = {"v": 0.0}
    stop = threading.Event()

    def play():
        time.sleep(0.6)
        x = (tpl * args.gain).astype(np.float32)
        try:
            spk.play(x, samplerate=rate)
        except Exception as e:
            print("播放失败:", e)

    threading.Thread(target=play, daemon=True).start()
    rolling = np.zeros(win, dtype=np.float32)
    filled = 0
    best = 0.0
    with mic.recorder(samplerate=rate, channels=2, blocksize=args.block) as rec:
        t0 = time.time()
        while time.time() - t0 < args.seconds:
            data = rec.record(numframes=args.block)
            if data.ndim == 1:
                data = np.stack([data, data], axis=1)
            mono = data[:, :2].mean(axis=1).astype(np.float32)
            n = len(mono)
            if n >= win:
                rolling[:] = mono[-win:]
                filled = win
            else:
                if filled + n > win:
                    keep = win - n
                    rolling[:keep] = rolling[filled - keep:filled]
                    filled = keep
                rolling[filled:filled + n] = mono
                filled += n
            if filled < win:
                continue
            s, lag = det.score(rolling)
            best = max(best, s)
            seg_l = rolling[lag:lag + len(tpl)][act_lo:act_hi] if lag >= 0 else np.zeros(1)
            sys.stdout.write("\r自检中 最高分 %.3f   " % best)
            sys.stdout.flush()
    print("\n自检完成：最高分 = %.3f" % best)
    print("结论：%s" % ("可以通过阈值 0.35，链路正常。" if best >= 0.35
                        else "未能过阈值 —— 检查设备选择、音量，或用 --device 指定。"))
    return best


def report(paths):
    """对比多个 CSV 的命中特征分布，判断哪些特征是可分的。"""
    data = {}
    for p in paths:
        rows = []
        with open(p, newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                if r.get("kind") != "hit":
                    continue
                if float(r["score_mono"]) < float(0.2):
                    continue
                rows.append(r)
        data[p] = rows
    if not data:
        print("没有数据")
        return
    feats = ["score_mono", "score_l", "score_r", "rms_l_db", "rms_r_db",
             "pan_db", "coherence", "centroid_hz", "hf_ratio"]
    print("%-16s %10s %10s %10s %10s" % ("特征", "样本A均值", "样本B均值", "差异", "可分性"))
    keys = list(data.keys())
    for f in feats:
        vals = []
        for k in keys:
            v = [float(r[f]) for r in data[k] if r.get(f) not in ("", None)]
            vals.append((float(np.mean(v)), float(np.std(v)), len(v)) if v else (float("nan"), 0.0, 0))
        if len(vals) < 2:
            continue
        (m1, s1, n1), (m2, s2, n2) = vals[0], vals[1]
        if math.isnan(m1) or math.isnan(m2):
            continue
        pooled = math.sqrt((s1 ** 2 + s2 ** 2) / 2.0) or 1e-9
        d = abs(m1 - m2) / pooled
        verdict = "★ 强可分" if d > 1.5 else ("可分" if d > 0.8 else "不可分")
        print("%-16s %10.3f %10.3f %10.3f %10s" % (f, m1, m2, m1 - m2, verdict))
    print("\n样本量: " + ", ".join("%s=%d" % (os.path.basename(k), len(v)) for k, v in data.items()))
    print("提示：差异一栏的绝对值越大、方差越小，越适合做判据。d>1.5 基本可以直接用。")


def main():
    ap = argparse.ArgumentParser(description="三角洲行动自动钓鱼误触发诊断探针")
    ap.add_argument("--wav", default=DEF_WAV, help="参考音效（默认用仓库 测试音频/ 里的原始咬钩音）")
    ap.add_argument("--device", default=None, help="回环设备名关键字，如 FxSound")
    ap.add_argument("--out", default="fish_probe.csv", help="输出 CSV")
    ap.add_argument("--seconds", type=float, default=0, help="采集时长，0 表示直到 Ctrl+C")
    ap.add_argument("--block", type=int, default=960, help="每次读取的采样数（20ms@48k）")
    ap.add_argument("--floor", type=float, default=0.20, help="记录命中的最低分数（低于阈值也记，便于看分布）")
    ap.add_argument("--refractory", type=float, default=1.0, help="同一事件的抑制秒数")
    ap.add_argument("--noise-floor", type=float, default=-65.0, help="低于此电平不打分")
    ap.add_argument("--gain", type=float, default=1.0, help="自检播放时的增益（默认原音量，避免吓人）")
    ap.add_argument("--list", action="store_true", help="列出设备")
    ap.add_argument("--selftest", action="store_true", help="播放参考音效并验证能检出")
    ap.add_argument("--record", metavar="TAG", help="采集模式，TAG 仅用于提示（如 mine / other）")
    ap.add_argument("--report", nargs="+", metavar="CSV", help="对比多个 CSV 的特征分布")
    args = ap.parse_args()

    if args.list:
        list_devices()
    elif args.report:
        report(args.report)
    elif args.selftest:
        selftest(args)
    elif args.record:
        args.out = args.out if args.out != "fish_probe.csv" else ("%s.csv" % args.record)
        record(args)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
