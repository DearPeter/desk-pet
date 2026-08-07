# 书书桌宠 (DeskPet)

一个透明背景的桌面宠物，使用你自己的 GIF 作为动画，并显示 DeepSeek 余额与缓存命中率。

![deskpet](看书.gif)

## 功能

- 透明背景动画桌宠，Windows 逐像素透明渲染（UpdateLayeredWindow），边缘平滑无锯齿
- 10 个 GIF 动画自由切换（右键菜单）
- DeepSeek 余额 + 缓存命中率实时显示（60 秒轮询，余额变化闪烁提醒）
- 无极缩放（右键滑块或 Ctrl + 滚轮）
- 自定义字体颜色（系统取色器）
- 置顶、拖动记忆位置、跟随 Codex 开关

## 运行

### 方式一：直接运行 Python

需要 Python 3.9+，依赖 `numpy` 和 `Pillow`：

```bash
pip install numpy pillow
python desk_pet.py
```

### 方式二：打包成 exe（发给别人）

```bash
pip install pyinstaller
pyinstaller --onefile --noconsole --name DeskPet \
  --add-data "gifs;gifs" desk_pet.py
```

## 配置 DeepSeek API Key

首次运行会自动弹出配置窗口，或右键菜单 →「配置 API Key…」。

Key 保存在 exe/脚本旁边的 `balance_config.json`（已被 `.gitignore` 排除，不会提交到仓库）。
Key 获取：[platform.deepseek.com](https://platform.deepseek.com) → API Keys。

## 自定义 GIF

把 GIF 放进 `gifs/` 文件夹即可自动出现在「切换动画」菜单里。

## 操作

| 操作 | 效果 |
|---|---|
| 左键按住拖动 | 移动桌宠 |
| 右键 | 打开菜单 |
| Ctrl + 滚轮 | 连续缩放 |

## 目录结构

```text
desk_pet.py              # 主程序
gifs/                    # 动画 GIF（10 个）
balance_config.example.json  # 配置模板（不含 key）
.gitignore               # 排除敏感文件
```

## 许可证

仅供个人学习与娱乐使用。
