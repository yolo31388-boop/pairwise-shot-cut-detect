"""镜头切换检测核心模块。

修复的问题：
1. 双阈值判定   —— 高阈值确认切镜、低阈值识别扰动区，闪光灯/快速摇镜不再误判为切镜
2. 显示序检测   —— 按 PTS 重排后再做帧差，切点落在显示序，时间码不再偏移
3. 渐变单独处理 —— 持续低帧差的淡入淡出/叠化识别为渐变过渡，不并入硬切也不漏掉
4. 跨文件时间码 —— 多机位按内嵌时间码对齐到全局时间轴，文件边界黑场不算切镜
5. 切点后取帧   —— 关键帧取切点之后（新镜头首帧），缩略图与内容一致
6. 任务隔离     —— 每个分析任务使用独立工作目录，批量并发互不覆盖

仅依赖标准库。帧用数值序列（灰度像素）或 {"pixels": [...], "pts": ...} 字典表示。
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile

# ---------------------------------------------------------------- 常量

T_HIGH = 0.55        # 高阈值：超过才可能是硬切
T_LOW = 0.20         # 低阈值：超过即进入"扰动区"（闪光/摇镜/渐变）
FLASH_MAX_LEN = 2    # 高帧差持续不超过该帧数且前后平稳 → 闪光灯，不是切镜
FADE_MIN_LEN = 4     # 帧差持续处于 [T_LOW, T_HIGH) 至少该帧数 → 渐变过渡
DEFAULT_FPS = 25.0


# ---------------------------------------------------------------- 工具

def _frame_pixels(frame):
    """取出帧的像素序列；支持裸序列或字典形式。"""
    if isinstance(frame, dict):
        px = frame.get("pixels", frame.get("data"))
        return list(px) if px is not None else []
    if isinstance(frame, (list, tuple)):
        return list(frame)
    return []


def _frame_pts(frame, default):
    if isinstance(frame, dict):
        for key in ("pts", "timestamp", "timecode"):
            if key in frame:
                return frame[key]
    return default


def frame_diff(a, b):
    """相邻帧归一化平均绝对差，取值约 [0, 1]。"""
    pa, pb = _frame_pixels(a), _frame_pixels(b)
    n = min(len(pa), len(pb))
    if n == 0:
        return 0.0
    total = sum(abs(float(pa[i]) - float(pb[i])) for i in range(n))
    peak = max(
        [abs(float(v)) for v in pa[:n]] + [abs(float(v)) for v in pb[:n]] + [1.0]
    )
    scale = 255.0 if peak > 1.5 else 1.0
    return total / n / scale


def _as_list(x):
    return list(x) if isinstance(x, (list, tuple)) else []


# ---------------------------------------------------------------- 1. 双阈值判定

def dual_threshold_cut(diffs, t_high=T_HIGH, t_low=T_LOW, flash_max=FLASH_MAX_LEN):
    """双阈值硬切判定。

    规则：diffs[i] >= t_high 且其前后 flash_max 帧内没有其它 >= t_low 的帧，
    才判定为硬切。闪光灯（亮-灭两个连续大帧差）与快速摇镜（连续多帧中等
    帧差）落在扰动区内，被排除。

    返回切点列表，切点为新镜头首帧下标（diffs[i] 对应切点 i+1）。
    """
    ds = [float(d) for d in _as_list(diffs)]
    cuts = []
    n = len(ds)
    for i, d in enumerate(ds):
        if d < t_high:
            continue
        lo = max(0, i - flash_max)
        hi = min(n, i + flash_max + 1)
        neighbors_hot = any(ds[j] >= t_low for j in range(lo, hi) if j != i)
        if not neighbors_hot:
            cuts.append(i + 1)
    return cuts


# ---------------------------------------------------------------- 2. 显示序检测

def display_order_detect(frames, fps=DEFAULT_FPS, t_high=T_HIGH, t_low=T_LOW):
    """按显示序（PTS）重排后检测切镜。

    输入可以是解码序的帧列表（字典帧带 pts），先按 PTS 排序再算帧差，
    切点与时间码都落在显示序上，B 帧重排序不再造成偏移。
    """
    fs = _as_list(frames)
    if not fs:
        return {"cuts": [], "timecodes": [], "order": []}
    indexed = list(enumerate(fs))
    indexed.sort(key=lambda kv: _frame_pts(kv[1], kv[0]))
    ordered = [f for _, f in indexed]
    order = [i for i, _ in indexed]
    diffs = scan_diff(ordered)
    cuts = dual_threshold_cut(diffs, t_high, t_low)
    timecodes = [c / float(fps) for c in cuts]
    return {"cuts": cuts, "timecodes": timecodes, "order": order, "diffs": diffs}


# ---------------------------------------------------------------- 3. 渐变单独处理

def fade_separate(diffs, t_low=T_LOW, t_high=T_HIGH, min_len=FADE_MIN_LEN):
    """把渐变过渡（淡入淡出/叠化）从帧差序列中单独识别出来。

    帧差持续处于 [t_low, t_high) 至少 min_len 帧的区段判定为渐变，
    与硬切分开返回，整段转场不再被算成一个镜头、也不会漏检。
    """
    ds = [float(d) for d in _as_list(diffs)]
    fades = []
    start = None
    for i, d in enumerate(ds + [0.0]):  # 末尾补 0 收尾
        in_band = t_low <= d < t_high
        if in_band and start is None:
            start = i
        elif not in_band and start is not None:
            if i - start >= min_len:
                fades.append({
                    "type": "fade",
                    "start": start,          # 渐变首帧（显示序）
                    "end": i,                # 渐变结束后的首帧
                    "length": i - start,
                })
            start = None
    return fades


# ---------------------------------------------------------------- 4. 跨文件时间码

def cross_file_timecode(files, fps=DEFAULT_FPS):
    """多机位/多文件按内嵌时间码拼到全局时间轴。

    不按文件名排序；每个文件用其起始时间码定位。文件交界处（片尾黑场、
    时间码跳变）标记为 boundary，不作为切镜。返回全局时间轴与需排除的
    边界位置（全局帧号）。
    """
    fs = [f for f in _as_list(files) if isinstance(f, dict)]
    if not fs:
        return {"timeline": [], "boundaries": [], "total_frames": 0}

    def _tc(f):
        return float(f.get("start_tc", f.get("start_timecode", 0.0)))

    def _nframes(f):
        if "frames" in f and isinstance(f["frames"], (list, tuple)):
            return len(f["frames"])
        return int(f.get("num_frames", round(float(f.get("duration", 0.0)) * fps)))

    ordered = sorted(fs, key=lambda f: (_tc(f), str(f.get("name", ""))))
    origin = _tc(ordered[0])
    timeline = []
    boundaries = []
    for f in ordered:
        g_start = int(round((_tc(f) - origin) * fps))
        n = _nframes(f)
        if timeline and g_start > 0:
            boundaries.append(g_start)  # 文件交界：黑场/跳变不算切镜
        timeline.append({
            "name": f.get("name"),
            "global_start": g_start,
            "global_end": g_start + n,
            "start_tc": _tc(f),
        })
    total = max(t["global_end"] for t in timeline)
    return {"timeline": timeline, "boundaries": boundaries, "total_frames": total}


# ---------------------------------------------------------------- 5. 切点后取帧

def frame_after_cut(cut, num_frames=None, offset=1):
    """关键帧取切点之后第 offset 帧（新镜头的画面），而非切点前一帧。

    cut 为新镜头首帧下标；默认取 cut+1 以避开可能的过渡残留帧，
    并钳制在合法范围内。缩略图因此与镜头内容一致。
    """
    idx = int(cut) + int(offset)
    if num_frames is not None:
        idx = min(idx, int(num_frames) - 1)
    return max(0, idx)


# ---------------------------------------------------------------- 6. 任务隔离

def task_isolation(task_id, base_dir=None):
    """为每个分析任务返回独立工作目录并确保存在。

    目录名含任务标识，批量并发时中间文件互不覆盖，结果可复现。
    """
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(task_id)) or "task"
    root = base_dir or os.path.join(tempfile.gettempdir(), "shot_cut_tasks")
    path = os.path.join(root, safe)
    os.makedirs(path, exist_ok=True)
    return path


# ---------------------------------------------------------------- 管线函数

def load_clip(spec, fps=DEFAULT_FPS):
    """加载/规范化一个片段为 {"frames", "fps", "start_tc", "name"}。"""
    if isinstance(spec, dict):
        return {
            "frames": _as_list(spec.get("frames")),
            "fps": float(spec.get("fps", fps)),
            "start_tc": float(spec.get("start_tc", spec.get("start_timecode", 0.0))),
            "name": spec.get("name"),
        }
    return {"frames": [], "fps": float(fps), "start_tc": 0.0, "name": None}


def scan_diff(frames):
    """按给定顺序计算相邻帧差序列（调用方负责先排成显示序）。"""
    fs = _as_list(frames)
    return [frame_diff(fs[i], fs[i + 1]) for i in range(len(fs) - 1)]


def segment(cuts, total=None):
    """由切点列表生成镜头段 [{"start", "end"}]，end 为开区间。"""
    cs = sorted(int(c) for c in _as_list(cuts))
    if total is None:
        total = (cs[-1] + 1) if cs else 0
    total = int(total)
    bounds = [0] + [c for c in cs if 0 < c < total] + [total]
    return [
        {"start": bounds[i], "end": bounds[i + 1]}
        for i in range(len(bounds) - 1)
        if bounds[i + 1] > bounds[i]
    ]


def present(result, fps=DEFAULT_FPS):
    """把检测结果整理为展示层结构：镜头段 + 时间码 + 切点后关键帧。"""
    if not isinstance(result, dict):
        return {"shots": []}
    cuts = result.get("cuts", [])
    total = result.get("total") or result.get("total_frames")
    shots = []
    for seg in segment(cuts, total):
        shots.append({
            "start": seg["start"],
            "end": seg["end"],
            "start_tc": seg["start"] / float(fps),
            "end_tc": seg["end"] / float(fps),
            "keyframe": frame_after_cut(seg["start"], total, offset=0)
            if seg["start"] == 0 else frame_after_cut(seg["start"], total),
        })
    return {"shots": shots, "fades": result.get("fades", [])}


def validate_cut(cut, diffs=None, t_high=T_HIGH, t_low=T_LOW):
    """校验候选切点：孤立超过高阈值才有效，扰动区内的判无效。"""
    ds = _as_list(diffs)
    i = int(cut) - 1  # 切点 cut 对应 diffs[cut-1]
    if not (0 <= i < len(ds)):
        return {"valid": False, "reason": "no evidence"}
    if ds[i] < t_high:
        return {"valid": False, "reason": "below high threshold"}
    lo, hi = max(0, i - FLASH_MAX_LEN), min(len(ds), i + FLASH_MAX_LEN + 1)
    if any(ds[j] >= t_low for j in range(lo, hi) if j != i):
        return {"valid": False, "reason": "inside disturbance (flash/pan/fade)"}
    return {"valid": True, "reason": "isolated spike above high threshold"}


def metrics(result, ground_truth=None, tolerance=1):
    """统计检测指标；给出真值时计算精确率/召回率/F1。"""
    cuts = sorted(_as_list(result.get("cuts", [])) if isinstance(result, dict) else [])
    out = {"cuts": len(cuts)}
    if isinstance(result, dict) and "fades" in result:
        out["fades"] = len(result["fades"])
    gt = sorted(_as_list(ground_truth))
    if gt or cuts:
        matched = set()
        tp = 0
        for c in cuts:
            for j, g in enumerate(gt):
                if j not in matched and abs(c - g) <= tolerance:
                    matched.add(j)
                    tp += 1
                    break
        p = tp / len(cuts) if cuts else 0.0
        r = tp / len(gt) if gt else 0.0
        out.update({
            "tp": tp,
            "precision": p,
            "recall": r,
            "f1": (2 * p * r / (p + r)) if (p + r) else 0.0,
        })
    return out


def audit(result):
    """生成确定性的审计记录：数量、切点、内容哈希，便于复核与回归。"""
    if not isinstance(result, dict):
        result = {}
    cuts = sorted(int(c) for c in _as_list(result.get("cuts")))
    fades = _as_list(result.get("fades"))
    digest = hashlib.sha256(repr((cuts, fades)).encode("utf-8")).hexdigest()
    return {"cuts": cuts, "num_cuts": len(cuts), "num_fades": len(fades), "sha256": digest}


def fallback(reason=None, total=None, interval=DEFAULT_FPS):
    """主检测不可用时的降级策略：按固定间隔分段，保证有确定输出。"""
    total = int(total) if total else 0
    step = max(1, int(interval))
    cuts = list(range(step, total, step)) if total else []
    return {"mode": "fallback", "reason": str(reason) if reason else None, "cuts": cuts}


def determinism(frames=None, fps=DEFAULT_FPS):
    """确定性校验：同一输入跑两遍完整检测，结果必须逐位一致。"""
    fs = _as_list(frames)
    r1 = display_order_detect(fs, fps)
    r2 = display_order_detect(fs, fps)
    same = audit(r1) == audit(r2) and r1.get("order") == r2.get("order")
    return {"deterministic": same, "checks": 2, "cuts": r1["cuts"]}
