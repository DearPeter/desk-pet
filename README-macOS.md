# macOS 移植说明（desk_pet_mac.py）

上游 `desk_pet.py` 是 **Windows 专用**的：它靠 `ctypes.windll` + `UpdateLayeredWindow`
实现逐像素透明窗口，靠 `CreateToolhelp32Snapshot` 枚举进程，在 macOS 上 import 就会报
`AttributeError: module 'ctypes' has no attribute 'windll'`。

`desk_pet_mac.py` 是等价功能的 macOS 原生实现。**上游 `desk_pet.py` 未作任何修改**，
Windows 上继续用它，`git pull` 也不会冲突。

## 运行

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install numpy pillow pyobjc-framework-Cocoa pyobjc-framework-Quartz
./.venv/bin/python desk_pet_mac.py
```

或在 Finder 里双击 `启动桌宠.command`。

## 移植对照表

| 能力 | Windows 原版 | macOS 移植版 |
|---|---|---|
| 逐像素透明 | `UpdateLayeredWindow` + 预乘 BGRA DIB | 无边框 `NSWindow` + `clearColor` + `setOpaque_(False)`，自绘 `NSView` |
| 置顶 | `WS_EX_TOPMOST` | `NSStatusWindowLevel` ⇄ `NSNormalWindowLevel` |
| 右键菜单 | PIL 自绘 + 分层窗口 | 原生 `NSMenu`（含子菜单、勾选态、内嵌缩放滑块） |
| 取色器 | `tkinter.colorchooser` | 原生 `NSColorPanel` |
| 对话框 | `tk.Toplevel` / `messagebox` | 原生 `NSAlert` |
| 缩放 | Ctrl + 滚轮 | Ctrl 或 ⌘ + 滚轮 |
| 跟随 Codex | `CreateToolhelp32Snapshot` 枚举 exe | `pgrep -x ChatGPT/codex` |
| 中文字体 | 微软雅黑 `msyhbd.ttc` | 冬青黑体 GB W6（新版 macOS 已无独立 PingFang 字体文件） |
| 定时器 | `tk.after` | `NSTimer` |

## macOS 版的额外改进

- **Retina 高清渲染**：按屏幕 `backingScaleFactor` 以 2 倍像素合成，再按点尺寸显示，
  文字和动画在 Retina 屏上不发虚（Windows 版无此概念）。
- **透明像素点击穿透**：`hitTest_` 会查合成图的 alpha 通道，完全透明的区域
  （包括文字周围的空白）点击会穿透到桌面，不会用一个隐形方块挡住图标。
- **不再自动弹出 API Key 配置框**（安全考虑，见下）。

## ⚠️ 安全变更：移除首次运行自动弹窗

上游行为是「首次运行且未配置 Key 时自动弹出输入框」。在 macOS 上这个模态框会
**抢走键盘焦点**——移植调试期间它确实把当时敲进去的字符当成 API Key 保存进了
明文 `balance_config.json`，并作为 `Bearer` token 发给了 `api.deepseek.com`（返回 401）。

因此 macOS 版改为：

1. 未配置 Key 时**只在桌宠旁显示「未配置 Key」**，不弹任何窗口；
2. 配置入口改为用户主动触发：右键菜单 →「配置 API Key…」，或 `--setup` 参数；
3. 保存前做格式校验，**不以 `sk-` 开头的输入一律拒绝保存、不发送**，防止误把密码等
   敏感内容写进明文文件。

## 命令行参数

| 参数 | 作用 |
|---|---|
| `--setup` | 启动后主动弹出 API Key 配置框 |
| `--smoke` | 自检模式：跑 3 秒后自动退出 |
| `--dump-frame out.png` | **不开窗口**，离屏合成一帧写入 PNG（验证 GIF 解码/缩放/字体/合成） |
| `--scale 0.4` | 指定初始缩放 |
| `--gif 3` | 指定初始动画序号 |

环境变量 `DESK_PET_DEBUG=1` 会把每帧的 tick / drawRect 打点写入 `widget.log`。

## 已知限制

- `balance_config.json` 与 Windows 版格式完全兼容（窗口位置同样以屏幕左上角为原点存储），
  但两个平台的默认字体不同，视觉上会有细微差异。
- 打包成 `.app` 需要用 `py2app` 而不是 `pyinstaller --onefile`（后者是 Windows 的 exe 路线）。
- 「跟随 Codex」匹配的是进程名 `ChatGPT` 与 `codex`；若你的客户端进程名不同，
  请修改 `desk_pet_mac.py` 顶部的 `CODEX_PROCESSES`。
