#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""透明动画桌宠 + DeepSeek 余额/缓存命中率显示 —— macOS 移植版。

原版 desk_pet.py 依赖 Win32 的 UpdateLayeredWindow 做逐像素透明窗口，
macOS 上不存在该 API。本移植用 AppKit 的等价物重建渲染层：

- 无边框 NSWindow + clearColor 背景 + setOpaque_(False) → 真正的逐像素 alpha，
  边缘保留完整抗锯齿渐变，效果等同（甚至优于）Windows 版
- 自绘 NSView 显示 PIL 合成好的帧；按屏幕 backingScaleFactor 渲染 → Retina 高清
- 透明像素点击穿透（hitTest_ 查 alpha），不会用一个隐形方块挡住桌面图标
- 原生 NSMenu 右键菜单 / NSColorPanel 取色器 / NSAlert 对话框
- 「跟随 Codex」用 pgrep 替代 Win32 的 CreateToolhelp32Snapshot

配置文件 balance_config.json 与 Windows 版完全兼容（含窗口位置，均按
屏幕左上角为原点存储）。

用法：
    python desk_pet_mac.py                  # 正常启动
    python desk_pet_mac.py --smoke          # 自检：跑几秒后自动退出
    python desk_pet_mac.py --dump-frame out.png   # 离屏合成一帧后退出（无需 GUI）
"""

from __future__ import annotations

import argparse
import glob
import io
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

# ---------------- 常量（与 Windows 版保持一致） ----------------

DEFAULT_GIF_DIR = os.environ.get("DESK_PET_GIF_DIR", "")
DEBUG = os.environ.get("DESK_PET_DEBUG") == "1"
DEFAULT_TEXT_COLOR = "#000000"
UP_COLOR = (94, 230, 155)
DOWN_COLOR = (255, 122, 122)

BALANCE_URL = "https://api.deepseek.com/user/balance"
# macOS 上的对应进程名：ChatGPT 桌面端 / codex CLI
CODEX_PROCESSES = ("ChatGPT", "codex")

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

# macOS 中文字体候选（路径, ttc 索引）；越靠前越优先。
# 注意：新版 macOS 已不再以独立文件提供 PingFang，故以冬青黑体 GB 为首选，
# 它是与微软雅黑最接近的无衬线中文字体。
FONT_CANDIDATES_BOLD = [
    ("/System/Library/Fonts/Hiragino Sans GB.ttc", 2),   # W6 粗
    ("/System/Library/Fonts/PingFang.ttc", 4),           # 旧版 macOS 才有
    ("/System/Library/Fonts/STHeiti Medium.ttc", 1),     # 黑体-简 Medium
    ("/System/Library/Fonts/Supplemental/Songti.ttc", 1),
    ("/System/Library/Fonts/Supplemental/Arial Unicode.ttf", 0),
]
FONT_CANDIDATES_NORMAL = [
    ("/System/Library/Fonts/Hiragino Sans GB.ttc", 0),   # W3 常规
    ("/System/Library/Fonts/PingFang.ttc", 2),
    ("/System/Library/Fonts/STHeiti Medium.ttc", 1),
    ("/System/Library/Fonts/Supplemental/Songti.ttc", 6),
    ("/System/Library/Fonts/Supplemental/Arial Unicode.ttf", 0),
]


# ---------------- 工具函数（与 Windows 版逻辑一致） ----------------


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resource_root() -> Path:
    """打包后的 Resources 根目录（py2app 设的 RESOURCEPATH；pyinstaller 用 _MEIPASS）。"""
    res = os.environ.get("RESOURCEPATH")
    if res:
        return Path(res)
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return app_dir()


def log(msg: str) -> None:
    try:
        with open(app_dir() / "widget.log", "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def _default_config() -> dict:
    return {
        "api_key": "",
        "check_interval_seconds": 60,
        "cache_probe_enabled": True,
        "cache_probe_minutes": 30,
        "cache_probe_model": "deepseek-chat",
        "show_balance_info": True,
        "follow_codex": False,
        "text_color": DEFAULT_TEXT_COLOR,
        "pos": None,
    }


def load_config() -> dict:
    cfg = _default_config()
    path = app_dir() / "balance_config.json"
    if path.is_file():
        try:
            cfg.update(json.loads(path.read_text(encoding="utf-8")))
            cfg["_source"] = str(path)
        except Exception:
            pass
    return cfg


def save_config(cfg: dict) -> None:
    try:
        path = app_dir() / "balance_config.json"
        payload = {k: v for k, v in cfg.items() if not k.startswith("_")}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def parse_hex_color(value, default):
    s = (value or "").strip()
    if s.startswith("#") and len(s) == 7:
        try:
            return tuple(int(s[i : i + 2], 16) for i in (1, 3, 5))
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


def format_balance(data: dict):
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
        "货币: {cur}\n总余额: {s}{total}\n赠送余额: {s}{granted}\n充值余额: {s}{topped}\n"
    ).format(cur=cur, s=symbol, total=total, granted=granted, topped=topped)
    return "{s}{total}".format(s=symbol, total=total), value, detail


def probe_cache(api_key: str, model: str = "deepseek-chat"):
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

    call()  # warm-up
    u = call()["usage"]
    hit = int(u.get("prompt_cache_hit_tokens", 0))
    miss = int(u.get("prompt_cache_miss_tokens", 0))
    total = hit + miss
    if total <= 0:
        return None
    return round(hit / total * 100, 1), hit, miss


def codex_running() -> bool:
    """macOS 版进程探测：pgrep -x 精确匹配进程名。"""
    for name in CODEX_PROCESSES:
        try:
            res = subprocess.run(
                ["pgrep", "-x", name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
            )
            if res.returncode == 0:
                return True
        except Exception:
            continue
    return False


_font_cache = {}


def load_font(size: int, bold: bool = True):
    key = (size, bold)
    cached = _font_cache.get(key)
    if cached is not None:
        return cached
    candidates = FONT_CANDIDATES_BOLD if bold else FONT_CANDIDATES_NORMAL
    for path, index in candidates:
        if not os.path.isfile(path):
            continue
        try:
            font = ImageFont.truetype(path, size, index=index)
            _font_cache[key] = font
            return font
        except Exception:
            continue
    font = ImageFont.load_default()
    _font_cache[key] = font
    return font


# ---------------- 与 GUI 无关的渲染核心 ----------------


class PetRenderer:
    """GIF 加载、缩放、文字合成。纯 PIL/numpy，无 GUI 依赖，可离屏自检。"""

    def __init__(self, gif_dir: str, cfg: dict):
        self.cfg = cfg
        self.gif_paths = self._find_gifs(gif_dir)
        if not self.gif_paths:
            raise RuntimeError(
                "没有找到 GIF 文件。请检查目录：\n" + (gif_dir or "(未设置)") + "\n或脚本同目录的 gifs/ 文件夹。"
            )
        self.gif_names = [Path(p).stem for p in self.gif_paths]
        self.text_color = parse_hex_color(cfg.get("text_color"), (0, 0, 0))
        self.balance_color = self.text_color
        self.current = 0
        self.frame_index = 0
        self.scale = 0.35
        self.pixel_ratio = 1.0  # Retina backingScaleFactor

        self.raw_frames = {}
        self.small_frames = {}
        self.content_right = {}
        self.gif_delays = {}
        self.scaled_frames = {}
        self.frame_scale = {}
        self._text_cache = {}

        self.balance_text = ""
        self.balance_value = None
        self.balance_detail = "尚未查询"
        self.cache_text = "缓存 --"
        self.cache_detail = ""
        self.last_update = ""

    # ---------- GIF ----------
    def _find_gifs(self, gif_dir: str):
        if gif_dir and os.path.isdir(gif_dir):
            paths = sorted(glob.glob(os.path.join(gif_dir, "*.gif")))
            if paths:
                return paths
        candidates = []
        # 打包后查找顺序：py2app Resources/、pyinstaller _MEIPASS/、脚本旁
        if os.environ.get("RESOURCEPATH"):
            candidates.append(Path(os.environ["RESOURCEPATH"]) / "gifs")
        if hasattr(sys, "_MEIPASS"):
            candidates.append(Path(sys._MEIPASS) / "gifs")
        candidates.append(app_dir() / "gifs")
        for local in candidates:
            if local.is_dir():
                paths = sorted(str(p) for p in local.glob("*.gif"))
                if paths:
                    return paths
        return []

    def load_raw(self, index: int) -> None:
        if index in self.raw_frames:
            return
        im = Image.open(self.gif_paths[index])
        count = getattr(im, "n_frames", 1)
        delay = 250
        frames = []
        for i in range(count):
            im.seek(i)
            delay = int(im.info.get("duration", delay)) or delay
            frames.append(im.convert("RGBA"))
        self.raw_frames[index] = frames
        self.gif_delays[index] = delay
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
        return frame.resize((target_w, target_h), Image.Resampling.LANCZOS)

    def _scale_frame_fast(self, index_in_gif: int, scale: float) -> Image.Image:
        smalls = self.small_frames[self.current]
        small = smalls[index_in_gif % len(smalls)]
        raw = self.raw_frames[self.current][index_in_gif % len(smalls)]
        w, h = raw.size
        return small.resize(
            (max(1, round(w * scale)), max(1, round(h * scale))),
            Image.Resampling.BILINEAR,
        )

    def _render_scale(self) -> float:
        """实际像素缩放 = 逻辑缩放 × Retina 倍率。"""
        return self.scale * self.pixel_ratio

    def ensure_frames(self, index: int) -> None:
        self.load_raw(index)
        scale = self._render_scale()
        if abs(self.frame_scale.get(index, -1) - scale) < 1e-6 and self.scaled_frames.get(index):
            return
        self.scaled_frames[index] = [self._scale_frame(f, scale) for f in self.raw_frames[index]]
        self.frame_scale[index] = scale

    # ---------- 文字 ----------
    def _render_text_image(self, text: str, size: int, color) -> Image.Image:
        key = (text, size, color)
        cached = self._text_cache.get(key)
        if cached is not None:
            return cached
        ss = 4  # 超采样，保证文字边缘平滑
        font = load_font(max(1, size * ss), bold=True)
        tmp = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
        bbox = ImageDraw.Draw(tmp).textbbox((0, 0), text, font=font)
        w = max(1, bbox[2] - bbox[0] + 6 * ss)
        h = max(1, bbox[3] - bbox[1] + 6 * ss)
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.text((3 * ss - bbox[0], 3 * ss - bbox[1]), text, font=font, fill=tuple(color) + (255,))
        img = img.resize(
            (max(1, round(w / ss)), max(1, round(h / ss))), Image.Resampling.LANCZOS
        )
        self._text_cache[key] = img
        return img

    def _text_sizes(self):
        base_balance, base_cache = 17, 12
        ratio = self.scale / 0.35
        px = self.pixel_ratio
        return (
            max(13, round(base_balance * ratio * px)),
            max(11, round(base_cache * ratio * px)),
        )

    def _text_x(self) -> int:
        raw = self.content_right.get(self.current, 0)
        return max(0, round(raw * self._render_scale())) + round(14 * self.pixel_ratio)

    # ---------- 合成 ----------
    def compose(self, preview_frame: Image.Image = None) -> Image.Image:
        """返回像素分辨率的合成图（RGBA）。"""
        frames = self.scaled_frames[self.current]
        frame = preview_frame if preview_frame is not None else frames[self.frame_index % len(frames)]
        if not self.cfg.get("show_balance_info", True):
            return frame
        fw, fh = frame.size
        balance_size, cache_size = self._text_sizes()
        balance_img = self._render_text_image(
            self.balance_text or "查询中…", balance_size, self.balance_color
        )
        cache_img = self._render_text_image(self.cache_text, cache_size, self.text_color)
        tw = max(balance_img.width, cache_img.width)
        pad = round(10 * self.pixel_ratio)
        total_w = self._text_x() + tw + pad
        total_h = max(fh, balance_img.height + cache_img.height + round(24 * self.pixel_ratio))
        canvas = Image.new("RGBA", (total_w, total_h), (0, 0, 0, 0))
        canvas.alpha_composite(frame, (0, 0))
        tx = self._text_x()
        off = round(10 * self.pixel_ratio)
        text_y = max(0, (fh - balance_img.height) // 2 - off)
        cache_y = max(0, (fh - cache_img.height) // 2 + round(16 * self.pixel_ratio))
        canvas.alpha_composite(balance_img, (tx, text_y))
        canvas.alpha_composite(cache_img, (tx, cache_y))
        return canvas

    def canvas_size(self) -> tuple:
        """当前动画所有帧共享的合成画布尺寸（像素）。"""
        img = self.compose()
        return img.size


# ---------------- macOS GUI 层 ----------------


def build_gui(renderer: PetRenderer, smoke: bool = False, setup: bool = False):
    import objc
    from AppKit import (
        NSAlert,
        NSApplication,
        NSApplicationActivationPolicyAccessory,
        NSBackingStoreBuffered,
        NSColor,
        NSColorPanel,
        NSCompositingOperationSourceOver,
        NSCompositingOperationCopy,
        NSRectFillUsingOperation,
        NSEvent,
        NSEventModifierFlagCommand,
        NSEventModifierFlagControl,
        NSFloatingWindowLevel,
        NSImage,
        NSMakeRect,
        NSMakeSize,
        NSMenu,
        NSMenuItem,
        NSNormalWindowLevel,
        NSScreen,
        NSSecureTextField,
        NSSlider,
        NSStatusWindowLevel,
        NSView,
        NSWindow,
        NSWindowCollectionBehaviorCanJoinAllSpaces,
        NSWindowCollectionBehaviorStationary,
        NSWindowStyleMaskBorderless,
    )
    from Foundation import NSData, NSObject, NSOperationQueue, NSTimer, NSZeroRect
    from PyObjCTools import AppHelper

    def pil_to_nsimage(img: Image.Image, point_size) -> "NSImage":
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        raw = buf.getvalue()
        data = NSData.dataWithBytes_length_(raw, len(raw))
        nsimg = NSImage.alloc().initWithData_(data)
        nsimg.setSize_(NSMakeSize(point_size[0], point_size[1]))
        return nsimg

    def on_main(fn):
        NSOperationQueue.mainQueue().addOperationWithBlock_(fn)

    # ---------- 无边框可拖动窗口 ----------
    class PetWindow(NSWindow):
        def canBecomeKeyWindow(self):
            return True

    # ---------- 自绘视图：逐像素 alpha + 透明穿透 ----------
    class PetView(NSView):
        def initWithFrame_(self, frame):
            self = objc.super(PetView, self).initWithFrame_(frame)
            if self is None:
                return None
            self._nsimage = None
            self._alpha = None  # numpy alpha（像素分辨率）
            self._ratio = 1.0
            self._pet = None
            self._serial = 0
            return self

        def isFlipped(self):
            return True

        def acceptsFirstMouse_(self, event):
            return True

        def setPayload_alpha_ratio_(self, nsimage, alpha, ratio):
            self._nsimage = nsimage
            self._alpha = alpha
            self._ratio = ratio
            self._serial += 1
            self.setNeedsDisplay_(True)

        def drawRect_(self, rect):
            # 透明窗口必须先把脏区清成全透明，否则上一帧会残留形成拖影
            NSColor.clearColor().set()
            NSRectFillUsingOperation(rect, NSCompositingOperationCopy)
            if self._nsimage is None:
                return
            if DEBUG:
                log("drawRect serial=%s" % self._serial)
            bounds = self.bounds()
            self._nsimage.drawInRect_fromRect_operation_fraction_respectFlipped_hints_(
                bounds, NSZeroRect, NSCompositingOperationSourceOver, 1.0, True, None
            )

        def hitTest_(self, point):
            """透明像素让点击穿透到桌面。"""
            if self._alpha is None:
                return objc.super(PetView, self).hitTest_(point)
            p = self.convertPoint_fromView_(point, self.superview())
            if not self.mouse_inRect_(p, self.bounds()):
                return None
            px = int(p.x * self._ratio)
            py = int(p.y * self._ratio)
            h, w = self._alpha.shape
            if 0 <= px < w and 0 <= py < h:
                if int(self._alpha[py, px]) > 12:
                    return self
            return None

        # ---------- 鼠标 ----------
        def mouseDown_(self, event):
            if self._pet:
                self._pet.on_press()

        def mouseDragged_(self, event):
            if self._pet:
                self._pet.on_drag()

        def mouseUp_(self, event):
            if self._pet:
                self._pet.on_release()

        def rightMouseDown_(self, event):
            if self._pet:
                self._pet.on_menu(event, self)

        def scrollWheel_(self, event):
            if not self._pet:
                return
            flags = event.modifierFlags()
            if not (flags & (NSEventModifierFlagControl | NSEventModifierFlagCommand)):
                return
            delta = event.scrollingDeltaY()
            if abs(delta) < 1e-6:
                return
            self._pet.on_wheel(0.03 if delta > 0 else -0.03)

    # ---------- 菜单 / 定时器 / 取色回调 ----------
    class Bridge(NSObject):
        def initWithPet_(self, pet):
            self = objc.super(Bridge, self).init()
            if self is None:
                return None
            self._pet = pet
            return self

        def invoke_(self, sender):
            cb = self._pet.action_for_tag(sender.tag())
            if cb:
                try:
                    cb()
                except Exception as exc:
                    log("menu action failed: %r" % (exc,))

        def onTick_(self, timer):
            self._pet.tick()

        def onBalancePoll_(self, timer):
            self._pet.poll_balance()

        def onCachePoll_(self, timer):
            self._pet.poll_cache()

        def onMonitor_(self, timer):
            self._pet.monitor()

        def onUnflash_(self, timer):
            self._pet.unflash()

        def onRebuild_(self, timer):
            self._pet.finish_scale()

        def onFirstRun_(self, timer):
            self._pet.configure_api_key()

        def colorChanged_(self, sender):
            self._pet.apply_color_from_panel(sender.color())

        def sliderChanged_(self, sender):
            self._pet.set_scale(sender.floatValue() / 100.0, quick=True)

        def sliderDone_(self, sender):
            self._pet.set_scale(sender.floatValue() / 100.0)

        def quitNow_(self, sender):
            self._pet.quit()

    # ---------- 主控制器 ----------
    class PetApp:
        def __init__(self, renderer: PetRenderer):
            self.r = renderer
            self.cfg = renderer.cfg
            self.topmost = True
            self.seen_codex = codex_running()
            self._drag_origin = None
            self._press_point = None
            self._moved = False
            self._tick_timer = None
            self._flash_timer = None
            self._preview_frame = None
            self._rebuild_timer = None
            self._nsimage_cache = {}
            self._actions = {}
            self._next_tag = 1

            screen = NSScreen.screens()[0]
            self.screen_h = screen.frame().size.height
            self.r.pixel_ratio = float(screen.backingScaleFactor() or 1.0)

            sw = int(screen.frame().size.width)
            sh = int(self.screen_h)
            pos = self.cfg.get("pos")
            if pos and len(pos) == 2:
                self.x, self.y = int(pos[0]), int(pos[1])
            else:
                self.x, self.y = sw - 400, sh - 420

            self.r.load_raw(self.r.current)
            self.r.ensure_frames(self.r.current)

            size = self.r.canvas_size()
            pw = size[0] / self.r.pixel_ratio
            ph = size[1] / self.r.pixel_ratio
            rect = NSMakeRect(self.x, self.screen_h - self.y - ph, pw, ph)

            self.window = PetWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                rect, NSWindowStyleMaskBorderless, NSBackingStoreBuffered, False
            )
            self.window.setOpaque_(False)
            self.window.setBackgroundColor_(NSColor.clearColor())
            self.window.setHasShadow_(False)
            self.window.setLevel_(NSStatusWindowLevel)
            self.window.setIgnoresMouseEvents_(False)
            self.window.setCollectionBehavior_(
                NSWindowCollectionBehaviorCanJoinAllSpaces
                | NSWindowCollectionBehaviorStationary
            )

            self.view = PetView.alloc().initWithFrame_(
                NSMakeRect(0, 0, pw, ph)
            )
            self.view._pet = self
            self.window.setContentView_(self.view)
            self.window.orderFrontRegardless()

            self.bridge = Bridge.alloc().initWithPet_(self)
            self.refresh()

        # ---------- 渲染 ----------
        def _compose_image(self):
            return self.r.compose(self._preview_frame)

        def refresh(self) -> None:
            img = self._compose_image()
            ratio = self.r.pixel_ratio
            pw = img.size[0] / ratio
            ph = img.size[1] / ratio
            nsimg = pil_to_nsimage(img, (pw, ph))
            alpha = np.asarray(img, dtype=np.uint8)[..., 3]

            frame = NSMakeRect(self.x, self.screen_h - self.y - ph, pw, ph)
            self.window.setFrame_display_(frame, False)
            self.view.setFrame_(NSMakeRect(0, 0, pw, ph))
            self.view.setPayload_alpha_ratio_(nsimg, alpha, ratio)

        # ---------- 播放 ----------
        def start(self) -> None:
            self._schedule_tick()
            self.balance_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                max(10, int(self.cfg.get("check_interval_seconds", 60))),
                self.bridge, "onBalancePoll:", None, True,
            )
            self.cache_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                max(5, int(self.cfg.get("cache_probe_minutes", 30))) * 60,
                self.bridge, "onCachePoll:", None, True,
            )
            self.monitor_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                10.0, self.bridge, "onMonitor:", None, True
            )
            if self.cfg.get("show_balance_info", True):
                self.refresh_balance()
                self.probe_cache_now()
            # 刻意不自动弹出 API Key 配置框：模态框会抢走键盘焦点，
            # 可能把用户正在别处敲的内容（甚至密码）当成 Key 捕获，
            # 写入明文配置并作为 Bearer token 发送出去。
            # 未配置时只在桌宠旁显示「未配置 Key」，由用户主动从右键菜单
            # 或 `--setup` 参数配置。

        def _schedule_tick(self) -> None:
            self._cancel_tick()
            delay = max(30, int(self.r.gif_delays.get(self.r.current, 250))) / 1000.0
            self._tick_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                delay, self.bridge, "onTick:", None, True
            )

        def _cancel_tick(self) -> None:
            if self._tick_timer is not None:
                self._tick_timer.invalidate()
                self._tick_timer = None

        def tick(self) -> None:
            frames = self.r.scaled_frames.get(self.r.current)
            if frames:
                if len(frames) > 1:
                    self.r.frame_index = (self.r.frame_index + 1) % len(frames)
                self.refresh()
            if DEBUG:
                log("tick frame=%s" % self.r.frame_index)

        # ---------- 拖动 ----------
        def on_press(self) -> None:
            loc = NSEvent.mouseLocation()
            self._drag_origin = (loc.x - self.x, loc.y + self.y)
            self._press_point = (loc.x, loc.y)
            self._moved = False

        def on_drag(self) -> None:
            if not self._drag_origin:
                return
            loc = NSEvent.mouseLocation()
            self.x = int(loc.x - self._drag_origin[0])
            self.y = int(self._drag_origin[1] - loc.y)
            self._moved = True
            self.refresh()

        def on_release(self) -> None:
            moved = self._moved
            if self._press_point:
                loc = NSEvent.mouseLocation()
                moved = moved or (
                    abs(loc.x - self._press_point[0]) + abs(loc.y - self._press_point[1]) > 6
                )
            self._drag_origin = None
            self._press_point = None
            self.cfg["pos"] = [self.x, self.y]
            save_config(self.cfg)
            if not moved:
                self.refresh_balance()

        def on_wheel(self, step: float) -> None:
            self.set_scale(self.r.scale + step, quick=True)

        # ---------- 缩放 ----------
        def set_scale(self, scale: float, quick: bool = False) -> None:
            self.r.scale = max(0.08, min(1.0, scale))
            if not quick:
                self.finish_scale()
                return
            self._cancel_tick()
            frames = self.r.raw_frames.get(self.r.current)
            if frames:
                idx = self.r.frame_index % len(frames)
                self._preview_frame = self.r._scale_frame_fast(idx, self.r._render_scale())
                self.refresh()
            if self._rebuild_timer is not None:
                self._rebuild_timer.invalidate()
            self._rebuild_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                0.25, self.bridge, "onRebuild:", None, False
            )

        def finish_scale(self) -> None:
            self._rebuild_timer = None
            self._preview_frame = None
            self.r.ensure_frames(self.r.current)
            frames = self.r.scaled_frames[self.r.current]
            self.r.frame_index = self.r.frame_index % len(frames)
            self.refresh()
            self._schedule_tick()

        # ---------- 菜单 ----------
        def action_for_tag(self, tag):
            return self._actions.get(int(tag))

        def _item(self, menu, title, callback, state=None):
            tag = self._next_tag
            self._next_tag += 1
            self._actions[tag] = callback
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                title, "invoke:", ""
            )
            item.setTarget_(self.bridge)
            item.setTag_(tag)
            if state is not None:
                item.setState_(1 if state else 0)
            menu.addItem_(item)
            return item

        def on_menu(self, event, view) -> None:
            menu = NSMenu.alloc().init()
            menu.setAutoenablesItems_(False)

            gif_menu = NSMenu.alloc().init()
            for i, name in enumerate(self.r.gif_names):
                self._item(
                    gif_menu, name, (lambda idx: lambda: self.switch_gif(idx))(i),
                    state=(i == self.r.current),
                )
            gif_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("切换动画", None, "")
            menu.addItem_(gif_item)
            menu.setSubmenu_forItem_(gif_menu, gif_item)

            size_menu = NSMenu.alloc().init()
            self._item(size_menu, "小", lambda: self.set_scale(0.28))
            self._item(size_menu, "中", lambda: self.set_scale(0.35))
            self._item(size_menu, "大", lambda: self.set_scale(0.45))
            size_menu.addItem_(NSMenuItem.separatorItem())
            slider_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("", None, "")
            slider = NSSlider.alloc().initWithFrame_(NSMakeRect(12, 4, 180, 24))
            slider.setMinValue_(10.0)
            slider.setMaxValue_(100.0)
            slider.setFloatValue_(self.r.scale * 100)
            slider.setTarget_(self.bridge)
            slider.setAction_("sliderChanged:")
            holder = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 204, 32))
            holder.addSubview_(slider)
            slider_item.setView_(holder)
            size_menu.addItem_(slider_item)
            size_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("大小", None, "")
            menu.addItem_(size_item)
            menu.setSubmenu_forItem_(size_menu, size_item)

            menu.addItem_(NSMenuItem.separatorItem())
            self._item(menu, "配置 API Key…", self.configure_api_key)
            self._item(menu, "字体颜色…", self.pick_text_color)
            self._item(menu, "立即刷新余额", self.refresh_balance)
            self._item(menu, "余额详情", self.show_balance_detail)
            self._item(menu, "缓存命中率探测", self.probe_cache_now)
            self._item(
                menu, "显示余额/缓存", self.toggle_balance_info,
                state=self.cfg.get("show_balance_info", True),
            )
            self._item(
                menu, "跟随 Codex", self.toggle_follow_codex,
                state=self.cfg.get("follow_codex", False),
            )
            self._item(menu, "置顶", self.toggle_topmost, state=self.topmost)
            menu.addItem_(NSMenuItem.separatorItem())
            self._item(menu, "退出", self.quit)

            NSMenu.popUpContextMenu_withEvent_forView_(menu, event, view)

        def switch_gif(self, index: int) -> None:
            if index == self.r.current or not (0 <= index < len(self.r.gif_paths)):
                return
            self._preview_frame = None
            self.r.current = index
            self.r.frame_index = 0
            self.r.load_raw(index)
            self.r.ensure_frames(index)
            self.refresh()
            self._schedule_tick()

        # ---------- 开关 ----------
        def toggle_topmost(self) -> None:
            self.topmost = not self.topmost
            self.window.setLevel_(NSStatusWindowLevel if self.topmost else NSNormalWindowLevel)

        def toggle_follow_codex(self) -> None:
            self.cfg["follow_codex"] = not self.cfg.get("follow_codex", False)
            save_config(self.cfg)
            self.seen_codex = codex_running()

        def toggle_balance_info(self) -> None:
            self.cfg["show_balance_info"] = not self.cfg.get("show_balance_info", True)
            save_config(self.cfg)
            if self.cfg["show_balance_info"]:
                self.refresh_balance()
                self.probe_cache_now()
            self.refresh()

        # ---------- 对话框 ----------
        def _activate(self):
            NSApplication.sharedApplication().activateIgnoringOtherApps_(True)

        def configure_api_key(self) -> None:
            self._activate()
            alert = NSAlert.alloc().init()
            alert.setMessageText_("配置 DeepSeek API Key")
            alert.setInformativeText_(
                "只接受 DeepSeek 格式的 Key（以 sk- 开头）。\n"
                "Key 以明文保存在本机 balance_config.json 中。\n"
                "获取：platform.deepseek.com → API Keys"
            )
            field = NSSecureTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 320, 24))
            field.setStringValue_(self.cfg.get("api_key") or "")
            alert.setAccessoryView_(field)
            alert.addButtonWithTitle_("保存")
            alert.addButtonWithTitle_("取消")
            if alert.runModal() != 1000:  # 非「保存」一律不落盘
                return
            value = str(field.stringValue()).strip()
            if value and not value.startswith("sk-"):
                # 防呆：拒绝把明显不是 API Key 的输入（例如误敲的密码）写入明文文件
                warn = NSAlert.alloc().init()
                warn.setMessageText_("这看起来不是 DeepSeek API Key")
                warn.setInformativeText_(
                    "DeepSeek 的 Key 以 sk- 开头。为避免把密码等敏感内容\n"
                    "误存进明文配置文件，本次输入已丢弃，未保存也未发送。"
                )
                warn.addButtonWithTitle_("好")
                warn.runModal()
                return
            self.cfg["api_key"] = value
            save_config(self.cfg)
            self.refresh_balance()

        def show_balance_detail(self) -> None:
            self._activate()
            body = self.r.balance_detail or self.r.balance_text or "尚未查询"
            extra = self.r.cache_detail or self.r.cache_text or ""
            tail = ("\n更新时间: " + self.r.last_update) if self.r.last_update else ""
            alert = NSAlert.alloc().init()
            alert.setMessageText_("DeepSeek 余额")
            alert.setInformativeText_(body + ("\n\n" + extra if extra else "") + tail)
            alert.addButtonWithTitle_("好")
            alert.runModal()

        def pick_text_color(self) -> None:
            self._activate()
            panel = NSColorPanel.sharedColorPanel()
            r, g, b = self.r.text_color
            panel.setColor_(
                NSColor.colorWithSRGBRed_green_blue_alpha_(r / 255.0, g / 255.0, b / 255.0, 1.0)
            )
            panel.setTarget_(self.bridge)
            panel.setAction_("colorChanged:")
            panel.makeKeyAndOrderFront_(None)

        def apply_color_from_panel(self, nscolor) -> None:
            try:
                c = nscolor.colorUsingColorSpaceName_("NSCalibratedRGBColorSpace")
                rgb = (
                    int(round(c.redComponent() * 255)),
                    int(round(c.greenComponent() * 255)),
                    int(round(c.blueComponent() * 255)),
                )
            except Exception:
                return
            self.r.text_color = rgb
            self.r.balance_color = rgb
            self.cfg["text_color"] = "#%02X%02X%02X" % rgb
            save_config(self.cfg)
            self.refresh()

        # ---------- 余额 / 缓存 ----------
        def refresh_balance(self) -> None:
            if not self.cfg.get("show_balance_info", True):
                return
            key = get_key(self.cfg)
            if not key:
                self.r.balance_text = "未配置 Key"
                self.r.balance_detail = (
                    "尚未配置 DeepSeek API Key。\n\n"
                    "两种方式任选其一：\n"
                    "1) 右键菜单 →「配置 API Key…」\n"
                    "2) 设置环境变量 DEEPSEEK_API_KEY\n\n"
                    "Key 获取: platform.deepseek.com → API Keys"
                )
                self.refresh()
                return
            self.r.balance_text = "查询中…"
            self.refresh()
            threading.Thread(target=self._fetch_balance, args=(key,), daemon=True).start()

        def _fetch_balance(self, api_key: str) -> None:
            try:
                data = fetch_balance(api_key)
                text, value, detail = format_balance(data)
                ok = True
            except urllib.error.HTTPError as e:
                text = "Key 无效" if e.code == 401 else "查询失败"
                value, detail, ok = None, "HTTP %s: %s" % (e.code, e.reason), False
            except Exception as e:
                text, value, detail, ok = "网络错误", None, str(e), False
            on_main(lambda: self._apply_balance(text, value, detail, ok))

        def _apply_balance(self, text, value, detail, ok) -> None:
            self.r.balance_detail = detail
            self.r.last_update = time.strftime("%H:%M:%S")
            prev = self.r.balance_value
            if ok and value is not None:
                self.r.balance_value = value
            self.r.balance_text = text
            self.r.balance_color = self.r.text_color
            self.refresh()
            if ok and prev is not None and value is not None and abs(value - prev) > 1e-9:
                self.flash(value > prev)

        def flash(self, up: bool) -> None:
            self.r.balance_color = UP_COLOR if up else DOWN_COLOR
            self.refresh()
            if self._flash_timer is not None:
                self._flash_timer.invalidate()
            self._flash_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                1.8, self.bridge, "onUnflash:", None, False
            )

        def unflash(self) -> None:
            self._flash_timer = None
            self.r.balance_color = self.r.text_color
            self.refresh()

        def probe_cache_now(self) -> None:
            if not self.cfg.get("show_balance_info", True):
                return
            key = get_key(self.cfg)
            if not key:
                return
            if not self.cfg.get("cache_probe_enabled", True):
                self.r.cache_text = "缓存 关"
                self.r.cache_detail = "缓存命中率监测已关闭（可在 balance_config.json 开启）"
                self.refresh()
                return
            threading.Thread(target=self._probe_cache, args=(key,), daemon=True).start()

        def _probe_cache(self, api_key: str) -> None:
            try:
                result = probe_cache(api_key, self.cfg.get("cache_probe_model", "deepseek-chat"))
                ok, err = True, ""
                rate, hit, miss = result if result else (None, 0, 0)
            except urllib.error.HTTPError as e:
                rate, hit, miss, ok, err = None, 0, 0, False, "HTTP %s" % e.code
            except Exception as e:
                rate, hit, miss, ok, err = None, 0, 0, False, str(e)[:120]
            if ok and rate is not None:
                line = "缓存命中 %.1f%%" % rate
                detail = (
                    "缓存命中率(探测): %.1f%%\n命中 tokens: %s\n未命中 tokens: %s\n"
                    "探测方式: 发送固定提示词两次,取第二次的命中比例"
                ) % (rate, hit, miss)
                log("cache probe OK rate=%s hit=%s miss=%s" % (rate, hit, miss))
            else:
                line = "缓存 --"
                detail = "缓存探测失败: " + (err if not ok else "无数据")
                log("cache probe FAILED: " + (err if not ok else "no data"))
            on_main(lambda: self._apply_cache(line, detail))

        def _apply_cache(self, line, detail) -> None:
            self.r.cache_text = line
            self.r.cache_detail = detail
            self.refresh()

        def poll_balance(self) -> None:
            if self.cfg.get("show_balance_info", True):
                self.refresh_balance()

        def poll_cache(self) -> None:
            if self.cfg.get("show_balance_info", True):
                self.probe_cache_now()

        def monitor(self) -> None:
            if not self.cfg.get("follow_codex", False):
                return
            running = codex_running()
            if running:
                self.seen_codex = True
                if not self.window.isVisible():
                    log("codex detected -> show pet")
                    self.window.orderFrontRegardless()
                    if self.cfg.get("show_balance_info", True):
                        self.refresh_balance()
            elif self.seen_codex and self.window.isVisible():
                log("codex closed -> hide pet")
                self.window.orderOut_(None)

        def quit(self) -> None:
            self.cfg["pos"] = [self.x, self.y]
            save_config(self.cfg)
            AppHelper.stopEventLoop()

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    pet = PetApp(renderer)
    pet.start()

    if smoke:
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            3.0, pet.bridge, "quitNow:", None, False
        )
    if setup:
        # 仅在用户显式传入 --setup 时才弹配置框（用户主动发起，不会误捕获键入）
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.6, pet.bridge, "onFirstRun:", None, False
        )
    return pet, AppHelper


# ---------------- 入口 ----------------


def main() -> int:
    parser = argparse.ArgumentParser(description="透明动画桌宠 + DeepSeek 余额（macOS）")
    parser.add_argument("--smoke", action="store_true", help="自检模式：跑几秒后退出")
    parser.add_argument("--dump-frame", metavar="PNG", help="离屏合成一帧写入 PNG 后退出")
    parser.add_argument("--scale", type=float, default=None, help="初始缩放（0.08-1.0）")
    parser.add_argument("--gif", type=int, default=None, help="初始动画序号")
    parser.add_argument(
        "--setup", action="store_true", help="启动后主动弹出 API Key 配置框"
    )
    args = parser.parse_args()

    if sys.platform != "darwin":
        print("desk_pet_mac.py 仅适用于 macOS；Windows 请运行 desk_pet.py", file=sys.stderr)
        return 2

    cfg = load_config()
    try:
        renderer = PetRenderer(DEFAULT_GIF_DIR, cfg)
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1

    if args.scale:
        renderer.scale = max(0.08, min(1.0, args.scale))
    if args.gif is not None and 0 <= args.gif < len(renderer.gif_paths):
        renderer.current = args.gif

    # 离屏自检：不开窗口，直接验证 GIF 解码 / 缩放 / 中文字体 / 合成
    if args.dump_frame:
        renderer.pixel_ratio = 2.0
        renderer.balance_text = "¥123.45"
        renderer.cache_text = "缓存命中 87.5%"
        renderer.load_raw(renderer.current)
        renderer.ensure_frames(renderer.current)
        img = renderer.compose()
        img.save(args.dump_frame)
        print(
            "dump ok: %s size=%sx%s gif=%s frames=%s"
            % (
                args.dump_frame,
                img.size[0],
                img.size[1],
                renderer.gif_names[renderer.current],
                len(renderer.scaled_frames[renderer.current]),
            )
        )
        return 0

    pet, helper = build_gui(renderer, smoke=args.smoke, setup=args.setup)
    helper.runEventLoop()
    print("桌宠已关闭。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
