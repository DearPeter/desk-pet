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

> ⚠️ **仅支持 DeepSeek 余额查询**：本程序目前只实现了 DeepSeek 的余额接口。
> 填入其他厂商的 API Key 无法查询余额（DeepSeek 服务器会拒绝陌生 Key），
> 桌宠动画等功能不受影响，只是余额/缓存命中率不可用。

### 各家大模型厂商余额查询支持情况

| 厂商 | 是否支持 API 查余额 | 接口方式 | 说明 |
|---|---|---|---|
| DeepSeek | ✅ 官方支持 | `GET https://api.deepseek.com/user/balance` | 本程序已实现；Bearer Key 鉴权 |
| Moonshot / Kimi | ✅ 官方支持 | `GET https://api.moonshot.ai/v1/users/me/balance` | 返回现金余额与代金券余额 |
| 硅基流动 SiliconFlow | ✅ 官方支持 | `GET https://api.siliconflow.com/v1/user/info` | 返回用户信息含余额 |
| OpenRouter | ✅ 官方支持 | `GET https://openrouter.ai/api/v1/key`（普通 API Key 即可）；`GET /api/v1/credits`（需 Management Key） | 普通 Key 可查该 Key 的剩余额度；Management Key 才能查账户总余额 |
| MiniMax | ⚠️ 部分支持 | `GET https://www.minimaxi.com/v1/token_plan/remains` | Token Plan 配额接口，非通用余额 |
| 智谱 GLM | ⚠️ 非官方 | `GET https://open.bigmodel.cn/api/monitor/usage/quota/limit` | GLM Coding Plan 配额监控接口（ClaudeBar 等社区工具在用），非官方文档化，可能变动 |
| 火山方舟（豆包） | ⚠️ 部分支持 | 用量统计查询 API | 走云账单体系，需额外鉴权，非简单余额 |
| 腾讯混元 | ⚠️ 部分支持 | 腾讯云账单/套餐查询 API | 需腾讯云 SecretId/SecretKey 签名，非简单 API Key |
| OpenAI | ❌ 不支持 | 无公开余额 API | `dashboard/billing` 是网页会话接口，API Key 无法调用 |
| Anthropic / Claude | ❌ 不支持 | 无公开余额 API | 官方确认 `GET /v1/organizations/balance` 返回 404 |
| Google Gemini | ❌ 不支持 | 无 API | 余额只能在 AI Studio「结算」网页查看 |
| Groq | ❌ 不支持 | 无公开余额 API | 余额在控制台网页查看 |
| 百度千帆（文心） | ❌ 未发现 | — | 无公开余额查询接口，控制台查看 |
| 讯飞星火 | ❌ 未发现 | — | 无公开余额查询接口，控制台查看 |

**结论**：如果希望支持其他厂商，需要为每家单独实现余额接口适配器
（URL、鉴权方式、返回字段解析各不相同），并且像 OpenAI、Anthropic、
Gemini 这类没有公开余额接口的厂商，只能提示"该厂商不支持余额查询"。

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
