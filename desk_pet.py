#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""透明动画桌宠 + DeepSeek 余额/缓存命中率显示。

渲染采用 Windows 逐像素透明窗口（UpdateLayeredWindow）：
- 文字边缘保留真实抗锯齿渐变，不再有锯齿/色边
- 背景完全透明，可叠加任意桌面内容
tkinter 仅用于事件（拖动/菜单/取色器）。

用法：
    python desk_pet.py            # 正常启动
    python desk_pet.py --smoke    # 自检：加载并播放几帧后自动退出
"""

from __future__ import annotations

import argparse
import ctypes
import glob
import json
import os
import sys
import threading
import time
import tkinter as tk
import tkinter.colorchooser as colorchooser
import urllib.error
import urllib.request
from ctypes import wintypes
from pathlib import Path
from tkinter import messagebox

import numpy as np
from PIL import Image, ImageDraw, ImageFont

# 自定义 GIF 目录：留空则使用内置 gifs/ 文件夹；
# 可通过环境变量 DESK_PET_GIF_DIR 覆盖（例如指定你自己的动画目录）
DEFAULT_GIF_DIR = os.environ.get("DESK_PET_GIF_DIR", "")
DEFAULT_TEXT_COLOR = "#000000"
UP_COLOR = (94, 230, 155)
DOWN_COLOR = (255, 122, 122)

BALANCE_URL = "https://api.deepseek.com/user/balance"
CODEX_PROCESSES = ("chatgpt.exe", "codex.exe")

PROBE_TEXT = (
    "你是一个深海主题的可爱助手。请记住以下背景设定并准备回答关于它的问题："
    "在深蓝色的大海深处，住着一只名为小鲸的鲸鱼娘，她穿着深蓝色女仆装，"
    "头上戴着鲸鱼造型的发饰，身后有一条巨大的鲸鱼尾巴。她负责守护海中的宝藏，"
    "每天都会巡视珊瑚礁、帮助迷路的小鱼找到回家的路、照料受伤的海豚，"
    "还会在月圆之夜带领海象合唱团表演。她的性格温柔又活泼，最喜欢收集闪闪发光的贝壳，"
    "把漂亮的贝壳串成项链送给来访的客人。如果海面刮起暴风雨，她会用尾巴卷起保护罩，"
    "守护海底小镇里的每一户人家。她有一个小秘密：她其实不会游泳，"
    "只是用尾巴在水里划来划去假装会游，但大家都心照不宣地替她保守秘密。"
    "每当有新的访客到来，她都会热情地介绍海底的每一处风景，"
    "从五彩斑斓的珊瑚花园到会发光的深海鱼群。"
) * 5


# ---------------- Win32 逐像素透明窗口 ----------------

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TOPMOST = 0x00000008
ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_byte),
        ("BlendFlags", ctypes.c_byte),
        ("SourceConstantAlpha", ctypes.c_byte),
        ("AlphaFormat", ctypes.c_byte),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class SIZE(ctypes.Structure):
    _fields_ = [("cx", wintypes.LONG), ("cy", wintypes.LONG)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER)]


def make_premultiplied_bgra(img: Image.Image) -> np.ndarray:
    # 整数运算预乘（uint16 避免溢出），比 float32 快约 3 倍
    arr = np.asarray(img, dtype=np.uint16)
    a = arr[..., 3:4]
    premul = (arr[..., 0:3] * a) >> 8  # (r*a)/255 ≈ (r*a)>>8
    out = np.empty(arr.shape[:2] + (4,), dtype=np.uint8)
    out[..., 0] = premul[..., 2]  # B
    out[..., 1] = premul[..., 1]  # G
    out[..., 2] = premul[..., 0]  # R
    out[..., 3] = arr[..., 3]     # A
    return out


class LayeredWindow:
    """用 UpdateLayeredWindow 实现逐像素 alpha 透明窗口。"""

    def __init__(self, hwnd: int):
        self.hwnd = hwnd
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED | WS_EX_TOPMOST)
        user32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE
        self._screen_dc = user32.GetDC(0)
        self._mem_dc = gdi32.CreateCompatibleDC(self._screen_dc)
        self._bitmap = None
        self._old_bitmap = None
        self._cap_w = 0
        self._cap_h = 0
        self._bits = None

    def update(self, img: Image.Image, x: int, y: int) -> None:
        w, h = img.size
        # 预分配大容量 DIB：拖动缩放时窗口尺寸频繁变化，
        # 只要没超过容量就不重建位图，只更新左上角区域。
        if self._bitmap is None or w > self._cap_w or h > self._cap_h:
            if self._old_bitmap:
                gdi32.SelectObject(self._mem_dc, self._old_bitmap)
            if self._bitmap:
                gdi32.DeleteObject(self._bitmap)
            self._cap_w = max(w, self._cap_w)
            self._cap_h = max(h, self._cap_h)
            bmi = BITMAPINFO()
            bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.bmiHeader.biWidth = self._cap_w
            bmi.bmiHeader.biHeight = -self._cap_h  # 自顶向下
            bmi.bmiHeader.biPlanes = 1
            bmi.bmiHeader.biBitCount = 32
            bmi.bmiHeader.biCompression = 0
            bmi.bmiHeader.biSizeImage = self._cap_w * self._cap_h * 4
            bits = ctypes.c_void_p()
            self._bitmap = gdi32.CreateDIBSection(
                self._mem_dc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0
            )
            if not self._bitmap:
                raise OSError("CreateDIBSection failed")
            self._old_bitmap = gdi32.SelectObject(self._mem_dc, self._bitmap)
            self._bits = bits

        data = make_premultiplied_bgra(img)  # (h, w, 4)
        # DIB 行宽是容量 cap_w，必须按行写入，否则行错位撕裂
        # 先清空整个缓冲，避免上次更大尺寸的旧数据残留在右侧/底部
        ctypes.memset(self._bits, 0, self._cap_w * self._cap_h * 4)
        row_bytes = w * 4
        src = data.tobytes()
        stride = self._cap_w * 4
        for row in range(h):
            dst = ctypes.c_void_p(self._bits.value + row * stride)
            ctypes.memmove(dst, src[row * row_bytes : (row + 1) * row_bytes], row_bytes)

        blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        pt_src = POINT(0, 0)
        pt_dst = POINT(x, y)
        size = SIZE(w, h)
        ok = user32.UpdateLayeredWindow(
            self.hwnd,
            self._screen_dc,
            ctypes.byref(pt_dst),
            ctypes.byref(size),
            self._mem_dc,
            ctypes.byref(pt_src),
            0,
            ctypes.byref(blend),
            ULW_ALPHA,
        )
        if not ok:
            raise OSError(f"UpdateLayeredWindow failed: {ctypes.get_last_error()}")

    def close(self) -> None:
        if self._old_bitmap:
            gdi32.SelectObject(self._mem_dc, self._old_bitmap)
        if self._bitmap:
            gdi32.DeleteObject(self._bitmap)
        if self._mem_dc:
            gdi32.DeleteDC(self._mem_dc)
        if self._screen_dc:
            user32.ReleaseDC(0, self._screen_dc)


# ---------------- 工具函数 ----------------


def app_dir() -> Path:
    """返回应用数据目录：打包成 exe 后为 exe 所在目录，否则为脚本目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def log(msg: str) -> None:
    try:
        log_path = app_dir() / "widget.log"
        with open(log_path, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def excepthook(tp, val, tb):
    import traceback

    log("EXCEPTION: " + " | ".join(traceback.format_exception(tp, val, tb)).replace("\n", " "))


def _default_config() -> dict:
    return {
        "api_key": "",
        "check_interval_seconds": 60,
        "cache_probe_enabled": True,
        "cache_probe_minutes": 30,
        "cache_probe_model": "deepseek-chat",
        "follow_codex": False,
        "text_color": DEFAULT_TEXT_COLOR,
        "pos": None,
    }


def load_config() -> dict:
    cfg = _default_config()
    base = app_dir()
    candidates = [base / "balance_config.json"]
    # 仅在开发模式（未打包）读取旧版 DeepSeek 气泡的配置，避免 exe 泄露本机 key
    if not getattr(sys, "frozen", False):
        legacy = Path(os.environ.get("LEGACY_BALANCE_CONFIG", ""))
        if not str(legacy).strip():
            legacy = Path("")
        candidates.append(legacy)
    for path in candidates:
        if path and path.is_file():
            try:
                cfg.update(json.loads(path.read_text(encoding="utf-8")))
                cfg["_source"] = str(path)
                break
            except Exception:
                continue
    return cfg


def save_config(cfg: dict) -> None:
    try:
        path = app_dir() / "balance_config.json"
        payload = {k: v for k, v in cfg.items() if not k.startswith("_")}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def parse_hex_color(value: str | None, default: tuple[int, int, int]) -> tuple[int, int, int]:
    s = (value or "").strip()
    if s.startswith("#") and len(s) == 7:
        try:
            return tuple(int(s[i : i + 2], 16) for i in (1, 3, 5))  # type: ignore[return-value]
        except ValueError:
            pass
    return default


def get_key(cfg: dict) -> str:
    key = (cfg.get("api_key") or "").strip()
    if not key:
        key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    return key


def fetch_balance(api_key: str) -> dict:
    req = urllib.request.Request(
        BALANCE_URL,
        headers={"Authorization": "Bearer " + api_key, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def format_balance(data: dict) -> tuple[str, float, str]:
    infos = data.get("balance_infos") or []
    if not infos:
        return "¥0.00", 0.0, "无余额数据"
    info = infos[0]
    cur = (info.get("currency") or "CNY").upper()
    total = info.get("total_balance", "0")
    granted = info.get("granted_balance", "0")
    topped = info.get("topped_up_balance", "0")
    symbol = "$" if cur == "USD" else "¥"
    try:
        value = float(total)
    except Exception:
        value = 0.0
    detail = (
        "货币: {cur}\n"
        "总余额: {s}{total}\n"
        "赠送余额: {s}{granted}\n"
        "充值余额: {s}{topped}\n"
    ).format(cur=cur, s=symbol, total=total, granted=granted, topped=topped)
    return "{s}{total}".format(s=symbol, total=total), value, detail


def probe_cache(api_key: str, model: str = "deepseek-chat") -> tuple[float, int, int] | None:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": PROBE_TEXT},
            {"role": "user", "content": "ping"},
        ],
        "max_tokens": 1,
        "stream": False,
        "temperature": 0,
    }

    def call() -> dict:
        req = urllib.request.Request(
            "https://api.deepseek.com/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": "Bearer " + api_key,
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))

    call()  # warm-up: establishes the disk cache
    u = call()["usage"]
    hit = int(u.get("prompt_cache_hit_tokens", 0))
    miss = int(u.get("prompt_cache_miss_tokens", 0))
    total = hit + miss
    if total <= 0:
        return None
    return round(hit / total * 100, 1), hit, miss


def codex_running() -> bool:
    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    kernel32 = ctypes.windll.kernel32
    snap = kernel32.CreateToolhelp32Snapshot(2, 0)
    if snap in (-1, 0):
        return True
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.lower() in CODEX_PROCESSES:
                return True
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
        return False
    finally:
        kernel32.CloseHandle(snap)


# ---------------- 桌宠主体 ----------------


class ModernMenu:
    """简约现代风格的自定义右键菜单。

    用 PIL 渲染 + UpdateLayeredWindow 逐像素透明显示：
    圆角边缘和文字都保留真实抗锯齿，不再有色键锯齿。
    """

    PAD = 10
    ITEM_H = 30
    WIDTH = 200
    RADIUS = 10
    BG = (255, 255, 255)
    FG = (31, 41, 55)
    HOVER = (238, 242, 247)
    BORDER = (229, 233, 240)
    SEP = (239, 242, 246)
    ACCENT = (76, 125, 240)
    GRAY = (198, 205, 216)

    def __init__(self, master: tk.Tk):
        self.master = master
        self._wins: list[dict] = []
        self._watch_id = None
        self._open_time = 0.0
        self._active_sub = None  # (id(win_info), item_index)
        self._font = ImageFont.truetype(
            r"C:\Windows\Fonts\msyhbd.ttc", 15
        )
        self._font_normal = ImageFont.truetype(
            r"C:\Windows\Fonts\msyh.ttc", 15
        )
        self._arrow_font = ImageFont.truetype(
            r"C:\Windows\Fonts\seguiemj.ttf", 14
        ) if os.path.isfile(r"C:\Windows\Fonts\seguiemj.ttf") else self._font_normal

    # ---------- 构建菜单项 ----------
    def show(self, x: int, y: int, items: list) -> None:
        self.close()
        self._create_window(items, x, y)
        self._open_time = time.time()
        self._start_watch()

    def close(self) -> None:
        for win in self._wins:
            try:
                win["layered"].close()
                win["top"].destroy()
            except Exception:
                pass
        self._wins = []
        self._active_sub = None
        if self._watch_id is not None:
            self.master.after_cancel(self._watch_id)
            self._watch_id = None

    def _create_window(self, items: list, x: int, y: int) -> dict:
        height = self._menu_height(items)
        top = tk.Toplevel(self.master)
        top.overrideredirect(True)
        top.attributes("-topmost", True)
        top.geometry(f"{self.WIDTH}x{height}+{x}+{y}")
        top.update_idletasks()
        top.update()
        hwnd = int(top.wm_frame(), 16)
        layered = LayeredWindow(hwnd)
        info = {
            "top": top,
            "layered": layered,
            "items": items,
            "hover": None,
            "x": x,
            "y": y,
            "w": self.WIDTH,
            "h": height,
        }
        top.bind("<Motion>", lambda e, wi=info: self._on_motion(wi, e))
        top.bind("<Leave>", lambda _e, wi=info: self._on_leave(wi))
        top.bind("<Button-1>", lambda e, wi=info: self._on_click(wi, e))
        top.bind("<Button-3>", lambda _e: self.close())
        self._wins.append(info)
        self._redraw(info)
        return info

    def _menu_height(self, items: list) -> int:
        h = self.PAD
        for item in items:
            if item[0] == "sep":
                h += 9
            else:
                h += self.ITEM_H
        return h + self.PAD

    # ---------- 渲染 ----------
    def _redraw(self, info: dict) -> None:
        img = self._render_menu(info["items"], info["hover"])
        info["layered"].update(img, info["x"], info["y"])

    def _render_menu(self, items: list, hover_idx: int | None) -> Image.Image:
        height = self._menu_height(items)
        img = Image.new("RGBA", (self.WIDTH, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle(
            [0, 0, self.WIDTH - 1, height - 1],
            radius=self.RADIUS,
            fill=self.BG,
            outline=self.BORDER,
            width=1,
        )
        y = self.PAD
        for idx, item in enumerate(items):
            kind = item[0]
            if kind == "sep":
                draw.line(
                    [self.PAD + 4, y + 4, self.WIDTH - self.PAD - 4, y + 4],
                    fill=self.SEP,
                    width=1,
                )
                y += 9
                continue
            x0, y0 = self.PAD, y
            x1, y1 = self.WIDTH - self.PAD, y + self.ITEM_H
            if idx == hover_idx:
                draw.rounded_rectangle(
                    [x0 + 3, y0 + 3, x1 - 3, y1 - 3],
                    radius=6,
                    fill=self.HOVER,
                )
            tx = x0 + 12
            if kind == "check":
                color = self.ACCENT if item[3] else self.GRAY
                r = 4
                cx, cy = tx + r, y0 + self.ITEM_H // 2
                draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)
                tx += 16
            label = item[1]
            font = self._font_normal
            draw.text(
                (tx, y0 + (self.ITEM_H - 17) / 2),
                label,
                font=font,
                fill=self.FG,
            )
            if kind == "sub":
                draw.text(
                    (x1 - 10, y0 + (self.ITEM_H - 13) / 2),
                    "›",
                    font=self._arrow_font,
                    fill=self.GRAY,
                )
            y += self.ITEM_H
        return img

    # ---------- 命中检测 ----------
    def _hit_test(self, items: list, rel_y: int) -> int | None:
        y = self.PAD
        for idx, item in enumerate(items):
            if item[0] == "sep":
                y += 9
                continue
            if y <= rel_y < y + self.ITEM_H:
                return idx
            y += self.ITEM_H
        return None

    # ---------- 事件 ----------
    def _on_motion(self, info: dict, event: tk.Event) -> None:
        idx = self._hit_test(info["items"], event.y)
        if idx != info["hover"]:
            info["hover"] = idx
            self._redraw(info)
        item = info["items"][idx] if idx is not None else None
        if item and item[0] == "sub":
            key = (id(info), idx)
            if key != self._active_sub:
                self._active_sub = key
                self._open_sub(info, item, idx)
        elif self._active_sub is not None:
            self._active_sub = None
            self._close_subs()

    def _on_leave(self, info: dict) -> None:
        info["hover"] = None
        self._redraw(info)
        if self._active_sub is not None:
            self._active_sub = None
            self._close_subs()

    def _on_click(self, info: dict, event: tk.Event) -> None:
        idx = self._hit_test(info["items"], event.y)
        if idx is None:
            return
        item = info["items"][idx]
        kind = item[0]
        if kind == "sub":
            self._open_sub(info, item, idx)
            return
        command = item[2]
        self.close()
        if command:
            command()

    def _open_sub(self, info: dict, item: tuple, idx: int) -> None:
        subitems = item[2]
        self._close_subs()
        item_y = self.PAD
        for i in range(idx):
            if info["items"][i][0] == "sep":
                item_y += 9
            else:
                item_y += self.ITEM_H
        sx = info["x"] + info["w"] - 6
        sy = info["y"] + item_y
        self._create_window(subitems, sx, sy)
        self._open_time = time.time()

    def _close_subs(self) -> None:
        for win in self._wins[1:]:
            try:
                win["layered"].close()
                win["top"].destroy()
            except Exception:
                pass
        self._wins = self._wins[:1]

    def _start_watch(self) -> None:
        self._watch()

    def _watch(self) -> None:
        # 菜单刚打开/子菜单刚弹出时给 400ms 缓冲，避免误关
        if time.time() - self._open_time < 0.4:
            self._watch_id = self.master.after(120, self._watch)
            return
        x, y = self.master.winfo_pointerxy()
        inside = False
        for win in self._wins:
            try:
                # 窗口未就绪（宽高过小）时不作为关闭依据
                ww, wh = win["w"], win["h"]
                if ww < 20 or wh < 20:
                    inside = True
                    break
                margin = 24
                if (
                    win["x"] - margin <= x <= win["x"] + ww + margin
                    and win["y"] - margin <= y <= win["y"] + wh + margin
                ):
                    inside = True
                    break
            except Exception:
                continue
        if not inside:
            self.close()
            return
        self._watch_id = self.master.after(120, self._watch)


class DeskPet:
    def __init__(self, root: tk.Tk, gif_dir: str) -> None:
        self.root = root
        self.gif_paths = self._find_gifs(gif_dir)
        if not self.gif_paths:
            raise RuntimeError(
                "没有找到 GIF 文件。请检查目录：\n" + gif_dir + "\n或脚本同目录的 gifs\\ 文件夹。"
            )

        self.cfg = load_config()
        self.text_color = parse_hex_color(self.cfg.get("text_color"), (0, 0, 0))
        self.gif_names = [Path(p).stem for p in self.gif_paths]
        self.current = 0
        self.frame_index = 0
        self.scale = 0.35
        self.topmost = True

        # 帧缓存
        self.raw_frames: dict[int, list[Image.Image]] = {}
        self.small_frames: dict[int, list[Image.Image]] = {}
        self.content_right: dict[int, int] = {}
        self.gif_delays: dict[int, int] = {}
        self.scaled_frames: dict[int, list[Image.Image]] = {}
        self.frame_scale: dict[int, float] = {}
        self._pending_preview = None
        self._pending_rebuild = None
        self._preview_frame: Image.Image | None = None
        self._text_cache: dict[tuple, Image.Image] = {}

        self._drag_offset = None
        self._press_pos = None
        self._after_id = None
        self._flash_job = None

        # 余额/缓存状态
        self.balance_text = ""
        self.balance_value = None
        self.balance_detail = "尚未查询"
        self.cache_text = "缓存 --"
        self.cache_detail = ""
        self.last_update = ""
        self.seen_codex = codex_running()
        self.balance_color = self.text_color

        # 初始位置
        pos = self.cfg.get("pos")
        if pos and len(pos) == 2:
            self.x, self.y = int(pos[0]), int(pos[1])
        else:
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            self.x, self.y = sw - 400, sh - 420

        # 窗口
        self.root.overrideredirect(True)
        self.root.geometry("200x200+%d+%d" % (self.x, self.y))
        self.root.update_idletasks()
        self.root.update()
        # 必须用顶层窗口句柄（wm_frame），winfo_id 返回的是子窗口，
        # UpdateLayeredWindow 作用于子窗口会失效。
        hwnd = int(self.root.wm_frame(), 16) if isinstance(self.root.wm_frame(), str) else self.root.wm_frame()
        self.layered = LayeredWindow(hwnd)
        self.root.bind("<Button-1>", self._on_press)
        self.root.bind("<B1-Motion>", self._on_drag)
        self.root.bind("<ButtonRelease-1>", self._on_release)
        self.root.bind("<Button-3>", self._on_menu)
        self.root.bind("<Control-MouseWheel>", self._on_wheel)
        self.root.bind("<Escape>", lambda _e: self.quit())

        self.menu = ModernMenu(self.root)

        self._load_raw(self.current)
        self._ensure_frames(self.current, self.scale)
        self._refresh()
        self._play()
        self.refresh_balance()
        self.probe_cache_now()
        # 首次运行：没有 API key 时自动弹出配置窗口，方便收件人使用
        if not get_key(self.cfg):
            self.root.after(500, self._configure_api_key)

    # ---------- GIF 加载与缩放 ----------
    def _find_gifs(self, gif_dir: str) -> list[str]:
        if gif_dir and os.path.isdir(gif_dir):
            paths = sorted(glob.glob(os.path.join(gif_dir, "*.gif")))
            if paths:
                return paths
        # 打包成 exe 后：内置资源解压到 sys._MEIPASS，脚本旁目录其次
        candidates = []
        if hasattr(sys, "_MEIPASS"):
            candidates.append(Path(sys._MEIPASS) / "gifs")
        candidates.append(app_dir() / "gifs")
        for local in candidates:
            if local.is_dir():
                paths = sorted(str(p) for p in local.glob("*.gif"))
                if paths:
                    return paths
        return []

    def _load_raw(self, index: int) -> None:
        if index in self.raw_frames:
            return
        im = Image.open(self.gif_paths[index])
        count = getattr(im, "n_frames", 1)
        delay = 250
        frames: list[Image.Image] = []
        for i in range(count):
            im.seek(i)
            delay = int(im.info.get("duration", delay)) or delay
            frames.append(im.convert("RGBA"))
        self.raw_frames[index] = frames
        self.gif_delays[index] = delay
        # 预缓存 512 中间帧，供拖动缩放时快速预览（避免每次 LANCZOS 大图）
        self.small_frames[index] = [
            f.resize((512, 512), Image.Resampling.BILINEAR) for f in frames
        ]
        max_right = 0
        for f in frames:
            arr = np.asarray(f, dtype=np.uint8)
            ys, xs = np.where(arr[..., 3] > 0)
            if len(xs):
                max_right = max(max_right, int(xs.max()))
        self.content_right[index] = max_right

    def _scale_frame(self, frame: Image.Image, scale: float) -> Image.Image:
        w, h = frame.size
        target_w = max(1, round(w * scale))
        target_h = max(1, round(h * scale))
        arr = np.asarray(frame, dtype=np.float32)
        alpha = arr[..., 3:4] / 255.0
        premul = arr.copy()
        premul[..., 0:3] *= alpha
        premul_img = Image.fromarray(premul.astype(np.uint8), "RGBA")
        return premul_img.resize((target_w, target_h), Image.Resampling.LANCZOS)

    def _scale_frame_fast(self, frame: Image.Image, scale: float, index: int) -> Image.Image:
        # 用预缓存的 512 中间帧做快速预览，只做一次轻量 BILINEAR
        smalls = self.small_frames[self.current]
        small = smalls[index % len(smalls)]
        w, h = frame.size
        target_w = max(1, round(w * scale))
        target_h = max(1, round(h * scale))
        return small.resize((target_w, target_h), Image.Resampling.BILINEAR)

    def _ensure_frames(self, index: int, scale: float) -> None:
        if index not in self.raw_frames:
            self._load_raw(index)
        if abs(self.frame_scale.get(index, -1) - scale) < 1e-6 and self.scaled_frames.get(index):
            return
        self.scaled_frames[index] = [self._scale_frame(f, scale) for f in self.raw_frames[index]]
        self.frame_scale[index] = scale

    # ---------- 文字渲染 ----------
    def _render_text_image(
        self, text: str, size: int, color: tuple[int, int, int]
    ) -> Image.Image:
        key = (text, size, color)
        cached = self._text_cache.get(key)
        if cached is not None:
            return cached
        font_path = r"C:\Windows\Fonts\msyhbd.ttc"
        if not os.path.isfile(font_path):
            font_path = r"C:\Windows\Fonts\msyh.ttc"
        ss = 6
        font = ImageFont.truetype(font_path, size * ss)
        tmp = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
        bbox = ImageDraw.Draw(tmp).textbbox((0, 0), text, font=font)
        w = max(1, bbox[2] - bbox[0] + 6 * ss)
        h = max(1, bbox[3] - bbox[1] + 6 * ss)
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.text((3 * ss - bbox[0], 3 * ss - bbox[1]), text, font=font, fill=color + (255,))
        img = img.resize(
            (max(1, round(w / ss)), max(1, round(h / ss))), Image.Resampling.LANCZOS
        )
        self._text_cache[key] = img
        return img

    def _text_sizes(self) -> tuple[int, int]:
        base_balance, base_cache = 17, 12
        ratio = self.scale / 0.35
        return max(13, round(base_balance * ratio)), max(11, round(base_cache * ratio))

    def _content_right_scaled(self) -> int:
        raw = self.content_right.get(self.current, 0)
        return max(0, round(raw * self.scale))

    def _text_x(self) -> int:
        return self._content_right_scaled() + 14

    # ---------- 合成与显示 ----------
    def _compose(self) -> Image.Image:
        frames = self.scaled_frames[self.current]
        frame = self._preview_frame or frames[self.frame_index % len(frames)]
        fw, fh = frame.size
        balance_size, cache_size = self._text_sizes()
        balance_img = self._render_text_image(
            self.balance_text or "查询中…", balance_size, self.balance_color
        )
        cache_img = self._render_text_image(self.cache_text, cache_size, self.text_color)
        tw = max(balance_img.width, cache_img.width)
        total_w = self._text_x() + tw + 10
        total_h = max(fh, balance_img.height + cache_img.height + 24)
        canvas = Image.new("RGBA", (total_w, total_h), (0, 0, 0, 0))
        canvas.alpha_composite(frame, (0, 0))
        tx = self._text_x()
        text_y = max(0, (fh - balance_img.height) // 2 - 10)
        cache_y = max(0, (fh - cache_img.height) // 2 + 16)
        canvas.alpha_composite(balance_img, (tx, text_y))
        canvas.alpha_composite(cache_img, (tx, cache_y))
        return canvas

    def _refresh(self) -> None:
        img = self._compose()
        self.layered.update(img, self.x, self.y)

    # ---------- 拖动 ----------
    def _on_press(self, event: tk.Event) -> None:
        self._drag_offset = (event.x_root - self.x, event.y_root - self.y)
        self._press_pos = (event.x_root, event.y_root)

    def _on_drag(self, event: tk.Event) -> None:
        if self._drag_offset:
            self.x = event.x_root - self._drag_offset[0]
            self.y = event.y_root - self._drag_offset[1]
            self._refresh()

    def _on_release(self, event: tk.Event) -> None:
        moved = self._press_pos and (
            abs(event.x_root - self._press_pos[0]) + abs(event.y_root - self._press_pos[1]) > 6
        )
        self._drag_offset = None
        self._press_pos = None
        self.cfg["pos"] = [self.x, self.y]
        save_config(self.cfg)
        self._finish_scale()
        if not moved:
            self.refresh_balance()

    # ---------- 菜单 ----------
    def _menu_items(self) -> list:
        gif_items = [
            ("cmd", name, lambda idx=i: self._switch_gif(idx))
            for i, name in enumerate(self.gif_names)
        ]
        size_items = [
            ("cmd", "小", lambda: self._set_scale(0.28)),
            ("cmd", "中", lambda: self._set_scale(0.35)),
            ("cmd", "大", lambda: self._set_scale(0.45)),
            ("sep",),
            ("cmd", "调节大小…", self._open_size_dialog),
        ]
        return [
            ("sub", "切换动画", gif_items),
            ("sub", "大小", size_items),
            ("sep",),
            ("cmd", "配置 API Key…", self._configure_api_key),
            ("cmd", "字体颜色…", self._pick_text_color),
            ("cmd", "立即刷新余额", self.refresh_balance),
            ("cmd", "余额详情", self.show_balance_detail),
            ("cmd", "缓存命中率探测", self.probe_cache_now),
            ("check", "跟随 Codex", self._toggle_follow_codex, self.cfg.get("follow_codex", False)),
            ("check", "置顶", self._toggle_topmost, self.topmost),
            ("sep",),
            ("cmd", "退出", self.quit),
        ]

    def _on_menu(self, event: tk.Event) -> None:
        self.menu.show(event.x_root, event.y_root, self._menu_items())

    def _switch_gif(self, index: int) -> None:
        if index == self.current or index < 0 or index >= len(self.gif_paths):
            return
        if self._pending_preview is not None:
            self.root.after_cancel(self._pending_preview)
            self._pending_preview = None
        if self._pending_rebuild is not None:
            self.root.after_cancel(self._pending_rebuild)
            self._pending_rebuild = None
        self._preview_frame = None
        self.current = index
        self.frame_index = 0
        self._load_raw(index)
        self._ensure_frames(index, self.scale)
        self._refresh()
        self._play()

    # ---------- 缩放 ----------
    def _set_scale(self, scale: float, *, quick: bool = False) -> None:
        self.scale = max(0.08, min(1.0, scale))
        if not quick:
            self._finish_scale()
            return
        self._pause_play()
        if self._pending_preview is not None:
            self.root.after_cancel(self._pending_preview)
        self._pending_preview = self.root.after(16, self._preview_scale)

    def _preview_scale(self) -> None:
        self._pending_preview = None
        frames = self.raw_frames.get(self.current)
        if not frames:
            self._finish_scale()
            return
        idx = self.frame_index % len(frames)
        self._preview_frame = self._scale_frame_fast(frames[idx], self.scale, idx)
        self.frame_index = idx
        self._refresh()
        if self._pending_rebuild is not None:
            self.root.after_cancel(self._pending_rebuild)
        self._pending_rebuild = self.root.after(200, self._finish_scale)

    def _finish_scale(self) -> None:
        if self._pending_preview is not None:
            self.root.after_cancel(self._pending_preview)
            self._pending_preview = None
        if self._pending_rebuild is not None:
            self.root.after_cancel(self._pending_rebuild)
            self._pending_rebuild = None
        self._preview_frame = None
        self._load_raw(self.current)
        self._ensure_frames(self.current, self.scale)
        self.frame_index = self.frame_index % len(self.scaled_frames[self.current])
        self._refresh()
        self._play()

    def _open_size_dialog(self) -> None:
        win = tk.Toplevel(self.root)
        win.title("大小")
        win.attributes("-topmost", True)
        win.resizable(False, False)
        win.transient(self.root)
        scale_var = tk.IntVar(value=round(self.scale * 100))
        tk.Label(win, text="大小（%）").pack(padx=12, pady=(10, 2))
        slider = tk.Scale(
            win,
            from_=10,
            to=100,
            orient="horizontal",
            variable=scale_var,
            length=220,
            command=lambda value: self._set_scale(int(value) / 100, quick=True),
        )
        slider.pack(padx=12)
        slider.bind(
            "<ButtonRelease-1>",
            lambda _event: self._set_scale(scale_var.get() / 100),
        )
        tk.Button(win, text="关闭", command=win.destroy).pack(pady=(4, 10))

    def _on_wheel(self, event: tk.Event) -> str:
        step = 0.03 if event.delta > 0 else -0.03
        self._set_scale(self.scale + step, quick=True)
        return "break"

    # ---------- 颜色 ----------
    def _configure_api_key(self) -> None:
        win = tk.Toplevel(self.root)
        win.title("配置 DeepSeek API Key")
        win.attributes("-topmost", True)
        win.resizable(False, False)
        win.transient(self.root)
        # 居中显示，避免出现在屏幕角落被遮挡
        win.update_idletasks()
        ww, wh = 360, 160
        sw = win.winfo_screenwidth()
        sh = win.winfo_screenheight()
        win.geometry(f"{ww}x{wh}+{(sw - ww) // 2}+{(sh - wh) // 2}")
        win.lift()
        win.focus_force()
        tk.Label(win, text="DeepSeek API Key（仅保存在本机配置文件中）").pack(
            padx=12, pady=(12, 4)
        )
        entry = tk.Entry(win, width=44, show="*")
        entry.insert(0, get_key(self.cfg))
        entry.pack(padx=12, pady=4)

        def save() -> None:
            key = entry.get().strip()
            self.cfg["api_key"] = key
            save_config(self.cfg)
            win.destroy()
            self.refresh_balance()

        tk.Button(win, text="保存", command=save).pack(pady=(4, 12))

    def _pick_text_color(self) -> None:
        rgb = colorchooser.askcolor(color="#%02X%02X%02X" % self.text_color, parent=self.root)
        if rgb and rgb[1]:
            self.text_color = tuple(int(rgb[1][i : i + 2], 16) for i in (1, 3, 5))
            self.balance_color = self.text_color
            self.cfg["text_color"] = rgb[1]
            save_config(self.cfg)
            self._refresh()

    def _toggle_topmost(self) -> None:
        self.topmost = not self.topmost
        style = user32.GetWindowLongW(self.layered.hwnd, GWL_EXSTYLE)
        if self.topmost:
            style |= WS_EX_TOPMOST
        else:
            style &= ~WS_EX_TOPMOST
        user32.SetWindowLongW(self.layered.hwnd, GWL_EXSTYLE, style)
        user32.SetWindowPos(
            self.layered.hwnd,
            -1 if self.topmost else -2,
            0,
            0,
            0,
            0,
            0x0001 | 0x0002,
        )

    def _toggle_follow_codex(self) -> None:
        self.cfg["follow_codex"] = not self.cfg.get("follow_codex", False)
        save_config(self.cfg)
        self.seen_codex = codex_running()

    # ---------- 余额 / 缓存 ----------
    def refresh_balance(self) -> None:
        key = get_key(self.cfg)
        if not key:
            self.balance_text = "未配置 Key"
            self.balance_detail = (
                "尚未配置 DeepSeek API Key。\n\n"
                "两种方式任选其一：\n"
                "1) 编辑 balance_config.json 中的 api_key\n"
                "2) 设置系统环境变量 DEEPSEEK_API_KEY\n\n"
                "Key 获取: platform.deepseek.com → API Keys"
            )
            self._refresh()
            return
        self.balance_text = "查询中…"
        self._refresh()
        threading.Thread(target=self._fetch_balance, args=(key,), daemon=True).start()

    def _fetch_balance(self, api_key: str) -> None:
        try:
            data = fetch_balance(api_key)
            text, value, detail = format_balance(data)
            ok = True
        except urllib.error.HTTPError as e:
            text, value, detail = ("Key 无效" if e.code == 401 else "查询失败"), None, "HTTP %s: %s" % (e.code, e.reason)
            ok = False
        except Exception as e:
            text, value, detail = "网络错误", None, str(e)
            ok = False
        self.root.after(0, lambda: self._apply_balance(text, value, detail, ok))

    def _apply_balance(self, text: str, value: float | None, detail: str, ok: bool) -> None:
        self.balance_detail = detail
        self.last_update = time.strftime("%H:%M:%S")
        prev = self.balance_value
        if ok and value is not None:
            self.balance_value = value
        self.balance_text = text
        self.balance_color = self.text_color
        self._refresh()
        if ok and prev is not None and value is not None and abs(value - prev) > 1e-9:
            self.flash(value > prev)

    def flash(self, up: bool) -> None:
        self.balance_color = UP_COLOR if up else DOWN_COLOR
        self._refresh()
        if self._flash_job:
            self.root.after_cancel(self._flash_job)
        self._flash_job = self.root.after(1800, self._unflash)

    def _unflash(self) -> None:
        self._flash_job = None
        self.balance_color = self.text_color
        self._refresh()

    def probe_cache_now(self) -> None:
        key = get_key(self.cfg)
        if not key:
            return
        if not self.cfg.get("cache_probe_enabled", True):
            self.cache_text = "缓存 关"
            self.cache_detail = "缓存命中率监测已关闭（可在 balance_config.json 开启）"
            self._refresh()
            return
        threading.Thread(target=self._probe_cache, args=(key,), daemon=True).start()

    def _probe_cache(self, api_key: str) -> None:
        try:
            rate, hit, miss = probe_cache(api_key, self.cfg.get("cache_probe_model", "deepseek-chat"))
            ok = True
            err = ""
        except urllib.error.HTTPError as e:
            rate, hit, miss, ok = None, 0, 0, False
            err = "HTTP %s" % e.code
        except Exception as e:
            rate, hit, miss, ok = None, 0, 0, False
            err = str(e)[:120]
        if ok and rate is not None:
            line = "缓存命中 %.1f%%" % rate
            log("cache probe OK rate=%s hit=%s miss=%s" % (rate, hit, miss))
            detail = (
                "缓存命中率(探测): %.1f%%\n命中 tokens: %s\n未命中 tokens: %s\n"
                "探测方式: 发送固定提示词两次,取第二次的命中比例\n"
                "说明: 这是模拟探测值,实际命中率取决于你请求的提示词复用程度"
            ) % (rate, hit, miss)
        else:
            line = "缓存 --"
            log("cache probe FAILED: " + (err if not ok else "no data"))
            detail = "缓存探测失败: " + (err if not ok else "无数据")
        self.root.after(0, lambda: self._apply_cache(line, detail))

    def _apply_cache(self, line: str, detail: str) -> None:
        self.cache_text = line
        self.cache_detail = detail
        self._refresh()

    def show_balance_detail(self) -> None:
        head = "DeepSeek 余额\n"
        body = self.balance_detail or self.balance_text or "尚未查询"
        cache = "\n\n" + (self.cache_detail or self.cache_text) if (self.cache_detail or self.cache_text) else ""
        tail = ("\n更新时间: " + self.last_update) if self.last_update else ""
        messagebox.showinfo("DeepSeek 余额", head + body + cache + tail)

    # ---------- 轮询 / 跟随 ----------
    def _poll(self) -> None:
        self.refresh_balance()
        interval = max(10, int(self.cfg.get("check_interval_seconds", 60))) * 1000
        self.root.after(interval, self._poll)

    def _cache_poll(self) -> None:
        self.probe_cache_now()
        interval = max(5, int(self.cfg.get("cache_probe_minutes", 30))) * 60000
        self.root.after(interval, self._cache_poll)

    def _monitor(self) -> None:
        if not self.cfg.get("follow_codex", False):
            self.root.after(10000, self._monitor)
            return
        running = codex_running()
        if running:
            if not self.seen_codex:
                self.seen_codex = True
            if self.root.state() == "withdrawn":
                log("codex detected open -> show pet")
                self.root.deiconify()
                self.layered.update(self._compose(), self.x, self.y)
                self.refresh_balance()
        elif self.seen_codex:
            if self.root.state() != "withdrawn":
                log("codex closed -> hide pet")
                self.root.withdraw()
        self.root.after(10000, self._monitor)

    # ---------- 播放 ----------
    def _pause_play(self) -> None:
        if self._after_id is not None:
            self.root.after_cancel(self._after_id)
            self._after_id = None

    def _play(self) -> None:
        self._pause_play()
        self._tick()

    def _tick(self) -> None:
        frames = self.scaled_frames.get(self.current)
        if frames:
            if len(frames) > 1:
                self.frame_index = (self.frame_index + 1) % len(frames)
            self._refresh()
        self._after_id = self.root.after(self.gif_delays.get(self.current, 250), self._tick)

    def quit(self) -> None:
        try:
            self.cfg["pos"] = [self.x, self.y]
        except Exception:
            pass
        save_config(self.cfg)
        self.layered.close()
        self.root.destroy()


def main() -> int:
    parser = argparse.ArgumentParser(description="透明动画桌宠 + DeepSeek 余额")
    parser.add_argument("--smoke", action="store_true", help="自检模式")
    args = parser.parse_args()

    root = tk.Tk()
    root.report_callback_exception = excepthook
    try:
        pet = DeskPet(root, DEFAULT_GIF_DIR)
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1

    root.after(10000, pet._monitor)
    interval = max(10, int(pet.cfg.get("check_interval_seconds", 60))) * 1000
    root.after(interval, pet._poll)
    cache_interval = max(5, int(pet.cfg.get("cache_probe_minutes", 30))) * 60000
    root.after(cache_interval, pet._cache_poll)

    if args.smoke:
        root.after(1500, root.destroy)

    root.mainloop()
    print("桌宠已关闭。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
