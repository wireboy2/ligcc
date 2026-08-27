# ligcc

**一块在屏幕共享里不存在的浮层。**

![platform](https://img.shields.io/badge/platform-Windows%2010%202004%2B-0078D4)
![python](https://img.shields.io/badge/python-3.10%2B-3776AB)
![gui%20framework](https://img.shields.io/badge/GUI%20framework-none-lightgrey)
![deps](https://img.shields.io/badge/outbound%20calls-1-success)
![license](https://img.shields.io/badge/license-Apache--2.0-D22128)

按一个键 → 截屏 → **本地** OCR → 大模型作答 → 答案浮在你眼前；
而腾讯会议 / Zoom / Teams / OBS / 微信共享的那份画面里，这块区域是**空的**。
不是"调低透明度"、不是"贴在别的窗口后面"—— 是合成器层面把它从捕获流里摘掉。

纯 Python + 手写 Win32：没有 Qt、没有 Electron、没有 WebView，
零 GUI 框架依赖；OCR 在本机 CPU 上跑，全程只有**一次**出网请求（调模型）。

> ### ⚠️ 合规声明（先读这段）
>
> 本项目是 Windows 窗口隐蔽机制（`WDA_EXCLUDEFROMCAPTURE`、分层窗口、点击穿透、
> 低级鼠标钩子）与本地 OCR 流水线的**技术研究与教学示例**，适用于：
> 个人学习、桌面自动化研究、无障碍辅助显示、对自有系统的自动化测试。
>
> **把它用于在线面试、在线考试或任何有诚信约定的评测场景，几乎一定违反平台协议，
> 后果由使用者自己承担。** 作者不鼓励、不支持这类用法。
>
> 同理，请不要在未经对方同意的情况下，用它在他人的会议/共享中隐藏内容。

---

## 它到底能做什么

- **一键解题**：按 `Ctrl+Alt+Q` → 截当前屏 → PaddleOCR 提取文字 → 调模型 → 答案出现在浮层。
- **共享里隐身**：浮层调用 `SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)`，
  腾讯会议 / Zoom / Teams / OBS / 微信共享屏幕都抓不到它（本机肉眼可见）。
- **不挡操作**：浮层是「点击穿透」窗口，鼠标点击照常落到下层 IDE / 浏览器。
- **能随手挪走**：按住 `Ctrl` 在浮层上拖动（或按住鼠标中键拖动）即可移动，位置会被记住。
- **长题分段合并**：题目一屏截不完时，滚动页面后按追加键，多段 OCR 结果会被当成同一道题合并作答。
- **长答案翻页**：光标移到浮层上滚滚轮即可上下翻页，底部显示页码。
- **长行自动折行**：长代码行/长句子按浮层宽度软换行（中英文按实际字宽算，
  续行保留原缩进），不会被右边界裁掉。
- **多显示器**：一个热键循环切换截图屏，浮层跟着走。
- **全程有反馈**：按下热键后浮层依次显示「识别中…」→「AI 作答中…（已识别 N 字）」→ 答案；
  截屏失败、这一屏没识别到文字、模型调用失败也都直接写在浮层上（提示里的按键是
  本次实际生效的那个）。无控制台的打包版不再是黑盒。
- **面试后复盘**：每道题自动落盘 `history/qa_log.jsonl`，还能让 AI 把 OCR 噪音清理成规范
  存档，一条命令导出 Markdown 复盘文档。

一眼看完的关键数字：

| | |
| --- | --- |
| 截一帧 | 5–15 ms（`mss` 直读 GDI 帧缓冲） |
| 本地 OCR | 约 0.5 s / 帧，CPU 推理，线程数限流 4 —— 满载时鼠标不卡 |
| 出网请求 | 1 次（调模型）。截屏、OCR、存档、渲染全在本机 |
| GUI 依赖 | 0。窗口、绘制、钩子、热键都是直接调 `user32` / `gdi32` / `gdiplus` |
| 模型体积 | 约 200 MB，首次运行自动下载到 `.paddle_cache/` |
| 权限 | 不需要管理员，不注入进程，不 hook 系统组件，不需要 GPU |

## 工作原理

```
Ctrl+Alt+Q
   │
   ├─ capture.py    mss 抓帧（BGRA→BGR，约 5-15ms）
   ├─ ocr.py        PaddleOCR PP-OCRv5 mobile，本地 CPU 推理，画面指纹缓存
   ├─ main.py       拼提示词 → Anthropic Messages 格式 API（urllib，带 3 次重试）
   └─ overlay.py    GDI+ 画到 ARGB 位图 → UpdateLayeredWindow 每像素 Alpha 提交
        └─ gdiplus_render.py
```

隐蔽性由四件事叠加实现，每一件都是官方 API，没有注入、没有 hook 系统进程：

| 目的 | 做法 |
| --- | --- |
| 屏幕共享/录屏里不可见 | `SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)`（Win10 2004+）；老系统自动退化为 `WDA_MONITOR`（共享侧显示纯黑块，内容仍不泄露） |
| 不出现在 Alt-Tab / 任务栏 | `WS_EX_TOOLWINDOW` |
| 不抢焦点（避免触发页面 blur 日志） | `WS_EX_NOACTIVATE` + `SW_SHOWNOACTIVATE` |
| 鼠标点击照常穿透到下层 | `WS_EX_TRANSPARENT` + `WS_EX_LAYERED`，配合 `UpdateLayeredWindow` 的每像素 Alpha |

一个值得一提的细节：因为窗口带 `WS_EX_TRANSPARENT`，它**收不到任何鼠标消息**，
所以「拖动浮层」和「滚轮翻页」不能靠窗口消息，只能装一个 `WH_MOUSE_LL` 低级鼠标钩子
自己做命中测试，并且只吞掉这两种手势（Ctrl+左键拖动、中键拖动、滚轮），
普通点击一律放行——这样才能同时拥有「可拖动」和「点击穿透」。

更完整的架构、技术选型对比与路线图见 [docs/技术方案.md](docs/技术方案.md)。

## 系统要求

- Windows 10 版本 2004（build 19041）或更高 / Windows 11
  —— 低于此版本仍可运行，但只能退化到 `WDA_MONITOR`
- Python 3.10+（用到 `X | Y` 类型标注）
- 约 200MB 磁盘装 OCR 模型（首次运行自动下载到 `.paddle_cache/`）
- 不需要管理员权限；不需要 GPU（CPU 推理约 0.5s/帧）

## 快速开始

```bash
git clone https://github.com/wireboy2/ligcc.git
cd ligcc

pip install -r interview_ai/requirements.txt

# 配置模型接口：复制模板并填上自己的 key
cp aiKey.example.txt aiKey.txt        # Windows: copy aiKey.example.txt aiKey.txt

# 验证一次完整链路（截屏 + OCR + 解答）
python interview_ai/main.py --once
# 只想看 30 秒就自动关：python interview_ai/main.py --once --duration 30

# 正式使用：启动热键循环
python interview_ai/main.py
```

首次运行会下载 OCR 模型，视网络需要几分钟，控制台有 paddle 的输出。
`aiKey.txt` 支持三行：`apiKey=`、`url=`、`模型：`，端点用 Anthropic Messages 格式
（官方或兼容代理均可）。也可以改用 `config.yaml` 的 `api:` 段，优先级更高。

## 热键

| 热键 | 作用 |
| --- | --- |
| `Ctrl+Alt+Q` | 识别当前屏并解答（清空之前的累积，只按本次识别作答） |
| `Ctrl+Alt+A` | **追加识别**并合并解答（长题分几次截，见下文） |
| `Ctrl+Alt+V` | 显示 / 隐藏浮层 |
| `Ctrl+Alt+C` | 清空浮层内容 |
| `Ctrl+Alt+M` | 切换截图显示器（多屏循环 1→2→1，浮层跟随） |
| `Ctrl+Alt+W` | 浮层停靠：右上→右下→左下→左上→居中（拖丢了用它拉回来） |
| `Ctrl+Alt+=` / `Ctrl+Alt+-` | 字号 +2 / -2（长行立刻按新字号重折） |
| `Ctrl+Alt+[` / `Ctrl+Alt+]` | 背板更透 / 更实（每次 15/255） |
| `Ctrl+Alt+Shift+←` / `→` | 浮层变窄 / 变宽 60px（长行按新宽度重折） |
| `Ctrl+Alt+Shift+↑` / `↓` | 浮层变矮 / 变高 60px（一屏行数跟着变） |
| `Ctrl+Alt+←` `→` `↑` `↓` | 浮层移动 20px（纯键盘摆位置，摆完记住） |
| `Ctrl+Alt+X` | 退出 |

> **热键被占用会自动换键。** `A`（常被微信截图占用）会退到 `F`/`G`，
> `M`（微信/QQ）退到 `N`/`B`，`W` 退到 `E`/`T`，
> **`Ctrl+Alt+方向键`（Intel 显卡驱动默认拿它转屏幕、IDEA 系也占）退到
> `Ctrl+Alt+Shift+H/J/K/L`**（vim 式左下上右）。
> **启动时控制台会打印实际生效的键，以那个为准。**
>
> 也可以在 `config.yaml` 里自己指定，修饰键 + 主键随意组合：
>
> ```yaml
> hotkeys:
>   solve:   Ctrl+Shift+F9   # 字母、数字、F1-F24、方向键、Space/Esc、符号键都行
>   monitor: Win+Alt+M       # 至少要带一个修饰键（裸键会在全局吞掉这个键）
> ```
>
> 只有**还在用默认键**的项才会自动换键；你显式写了什么就注册什么，
> 被别的程序占用时直接报「本次不可用」，不会擅自换成别的键。
> 写错（解析不了）会打印提示并退回默认值。

## 使用技巧

**长题分几次识别、合并作答**
按 `Q` 识别上半部分 → 滚动页面露出剩余部分 → 按 `A` 追加识别 →
模型会收到`【第 1 次识别】``【第 2 次识别】`…，先在心里去重对齐还原成完整题目再作答。
画面没变时按 `A` 不会重复追加。

**答案太长**
把鼠标移到浮层上滚滚轮翻页（每格约 3 行），底部显示当前页码。
滚轮会被浮层截获，普通点击不会。页码里的行数是**折行后**的显示行数。

**移动浮层**
在浮层上**按住 Ctrl 拖动**，或**按住鼠标中键拖动**；也可以用 `Ctrl+Alt+方向键`
每次挪 20px（纯键盘摆位置，摆最后几像素比拖鼠标准）。松手/松键后位置写入
`history/overlay_state.json`，下次启动沿用。拖不丢：始终保证至少 80px 露在某块显示器上
（多屏 DPI 不同导致的「虚拟桌面空洞」也会被拉回来）。
不想记忆位置或想钉死位置，写 `config.yaml`：

```yaml
overlay:
  pos: [1200, 60]        # 固定在这个屏幕坐标（写了就不再记忆拖动）
  remember_pos: false    # 只关记忆、保留拖动
```

**字太小看不清 / 浮层太窄**
运行中直接按 `Ctrl+Alt+=` / `Ctrl+Alt+-` 加减字号（每次 2px，长行立刻按新字号重折），
`Ctrl+Alt+[` / `Ctrl+Alt+]` 调背板透明度，`Ctrl+Alt+Shift+方向键` 改浮层尺寸
（左右调宽度、上下调高度，每次 60px；宽度一变长行按新宽度重折，高度一变一屏行数跟着变）。
尺寸最小 240x120、最大不超过当前显示器工作区 —— 比屏幕还大的浮层没法用也拖不回来。
调完的值和位置一起记在 `history/overlay_state.json`，下次启动沿用
（`remember_pos: false` 就只影响本次）。
想钉死就写 `config.yaml` 的 `overlay.font_size` / `overlay.size`——**显式写了的以 config
为准**，不会被上次热键调的值盖掉；行高按比例跟着字号放大（也可以用 `line_height` 单独钉死）。
`font_name` 换成别的等宽字体也行，装不到会自动退回 `Consolas` 并在控制台提示一次。
启动时会打印实际字号/行高/尺寸和一屏能放多少行。

**多显示器**
启动时会打印显示器列表（`0`=全部合并，`1`=主屏，`2`=副屏…）。三种指定方式：
命令行 `--monitor 2`、运行中按 `Ctrl+Alt+M`、或 `config.yaml` 里 `monitor: 2`。

**OCR 时鼠标不卡**
CPU 推理线程默认限制为 4（`config.yaml: ocr_cpu_threads`），解答工作线程跑在
`BELOW_NORMAL` 优先级，所以 OCR 满载时系统输入依然顺滑。

## 复盘记录

每次解答自动落盘到 `history/qa_log.jsonl`（一道题一条记录：
`id / 时间 / 题目 / 答案 / 识别次数 / OCR 置信度`）。按 `Q` 新增记录，
按 `A` 追加识别会更新同一条（题目合并、答案刷新），重启后 id 接续。

原始记录先落盘保证不丢数据，随后后台自动调模型把题目和答案整理成规范格式
（剔除窗口标题/行号/网站统计等界面噪音、合并重复、保留完整约束）覆盖入库
（标 `cleaned` 字段）；整理失败则保留原始记录。

```bash
python interview_ai/main.py --export-md   # 导出 history/复盘记录.md
python interview_ai/main.py --refine      # 给旧记录批量补 AI 整理（先备份 .bak）
```

## 命令行参数

```
--once                    只做一次 截屏+OCR+解答（浮层留着给你看，按退出键关）
--duration <秒>           跑够这么多秒自动退出（挂在脚本里跑不会卡住）
--config <path>           指定配置文件（默认 config.yaml）
--delivery overlay|clipboard   答案投递方式（默认 overlay）
--answer-mode api|none    none = 只 OCR 不解答
--monitor 0|1|2|3|4       截图显示器
--export-md               导出复盘 Markdown
--refine                  批量整理历史记录
```

## 配置

所有配置项都有默认值，`config.yaml` 可以完全不存在。要改就把
[`config.example.yaml`](config.example.yaml) 复制成 `config.yaml`，里面每一项都有注释。

## 项目结构

```
ligcc/
├─ README.md                 本文件
├─ LICENSE / NOTICE          Apache-2.0 许可与依赖声明
├─ aiKey.example.txt         API 配置模板 → 复制为 aiKey.txt
├─ config.example.yaml       配置模板 → 复制为 config.yaml（可选）
├─ docs/
│  └─ 技术方案.md            完整技术方案（架构、选型对比、路线图）
└─ interview_ai/
   ├─ main.py                主流程：热键、消息循环、API 调用、投递
   ├─ capture.py             mss 截屏（支持区域 / 指定显示器 / 后台采集线程）
   ├─ ocr.py                 PaddleOCR 封装（2.x/3.x 兼容、画面指纹缓存、线程限流）
   ├─ overlay.py             隐形浮层：WDA、点击穿透、鼠标钩子、拖动、翻页、位置记忆
   ├─ gdiplus_render.py      GDI+ 绘制 → UpdateLayeredWindow 每像素 Alpha
   ├─ history.py             问答记录 JSONL 存储（原子重写）+ Markdown 导出
   ├─ stealth.py             早期独立实现，现为可复用的隐蔽工具函数库（非主路径）
   ├─ verify_stealth.py      隐蔽性自检脚本
   ├─ build.spec             PyInstaller 打包配置（onedir）
   ├─ run_tests.py           测试统一入口（按平台跳过跑不了的，末尾汇总）
   ├─ test_validate.py       静态架构约束检查（AST，任何平台可跑）
   ├─ test_config.py         config 解析单测：热键字符串、颜色（任何平台可跑）
   ├─ test_render_mock.py    mock Win32 DLL 跑通渲染逻辑（任何平台可跑）
   └─ test_move.py           浮层拖动/停靠/翻页/字号透明度/状态记忆（需真实 Windows 桌面）
```

运行时会生成（均已 gitignore）：`.paddle_cache/`（OCR 模型）、
`history/`（问答记录、复盘文档、浮层位置与尺寸/字号/透明度）、`aiKey.txt`、`config.yaml`。

## 打包 exe

```bash
cd interview_ai
build.bat            # 打包 + 自动把 aiKey.txt、.paddle_cache/ 复制进产物目录
build.bat deps       # 先装依赖再打包
```

`build.bat` 只是 `pyinstaller --noconfirm build.spec` 加上复制运行时文件那两步，
想手动来也可以：

```bash
pyinstaller --noconfirm build.spec
# 然后把 aiKey.txt 和 .paddle_cache/ 复制到 dist/SystemHelper/ 下
```

产物是 `dist/SystemHelper/`（onedir，约 1.1GB —— paddle 全家桶就是这么大；
onefile 每次启动要解压几分钟，不实用）。
`build.spec` 顶部 `CONSOLE = True` 是调试版，改成 `False` 重打即无控制台版。

> `build.bat` 刻意全英文：cmd.exe 用 OEM 代码页解析 .bat，中文注释会被切成
> 乱码命令导致报错（实测会在中途静默跳过步骤）。项目其它文档仍是中文。

## 测试与自检

```bash
cd interview_ai

# 一条命令跑完所有能自动跑的测试（按平台自动跳过跑不了的，末尾给汇总）
python run_tests.py
python run_tests.py -q          # 只在失败时打细节
python run_tests.py config      # 只跑名字含 config 的

# 也可以单独跑：
# 静态架构约束（语法、跨模块引用、隐蔽性关键约束）—— 任何平台
python test_validate.py

# config 解析单测（热键字符串 ⇄ (mod, vk)、颜色写错要退回默认）—— 任何平台
python test_config.py

# 渲染逻辑（mock 掉 gdiplus/user32，验证句柄生命周期与 UpdateLayeredWindow 参数）
python test_render_mock.py

# 浮层交互（拖动/停靠/边界夹取/翻页/字号透明度/状态记忆）—— 需要真实 Windows 桌面会话
python test_move.py

# 隐蔽性人工验证：先用会议/OBS 共享屏幕，再运行
python verify_stealth.py
# 确认三件事：本机看得见浮层、共享画面看不到、鼠标可穿透点击

# 浮层单独目视自测（可手动试 Ctrl+拖动）
python overlay.py
```

`test_validate.py` 会强制检查几条**架构约束**，比如「overlay 不得使用 `LWA_COLORKEY`」
（它和 WDA 捕获排除冲突）、「必须走 `UpdateLayeredWindow` 的 `AC_SRC_ALPHA`」、
「版本门槛必须是 19041」——这些靠肉眼 review 很容易漏掉。

## 常见问题

**浮层在共享里还是能看到？**
先跑 `python verify_stealth.py` 看系统版本。低于 Win10 2004 只能退化成 `WDA_MONITOR`
（共享侧一块纯黑，内容不泄露但藏不住存在）。另外部分**硬件采集卡 / 手机拍屏**
是物理层面的，任何软件机制都拦不住。

**热键按了没反应？**
看启动时控制台打印的实际按键——被占用时会自动换键。如果打印了「注册失败」，
说明有别的程序（微信、QQ、网易云、Intel 显卡驱动）占用了同一组合，
可以在 `config.yaml` 的 `hotkeys:` 段换一组自己顺手的键。

**浮层不见了 / 被拖到屏幕外？**
按 `Ctrl+Alt+W`（或它的回退键）循环停靠回四角/居中。

**首次启动卡住不动？**
第一次按解答键时浮层会显示「首次运行：正在下载 OCR 模型（约 200MB）…」——
模型存到 `.paddle_cache/`，视网速要几分钟，下完这一次以后就不用等了。
用源码跑还能看到 paddle 自己的进度输出。

**答案有一半看不见？**
长行默认会软换行，不该再被裁掉。如果确实还看不到（比如 ASCII 表格被折断了不好读），
可以把 `overlay.size` 调宽，或 `overlay.wrap: false` 关掉折行回到旧行为。

## 已知限制

- **仅 Windows。** 隐蔽机制、分层窗口、钩子全部是 Win32 API，没有跨平台可能。
- 长行按宽度折行，但没有横向滚动（宽表格 / ASCII 图会被折断）。
- 浮层的尺寸/字号/透明度可以用热键运行中调，但**配色（背板色、文字色）只能改
  `config.yaml` 后重启**。
- `capture_backend` 只有 `mss` 一种；`wgc`（能抓被遮挡的窗口）尚未实现，
  配了会提示一句并自动降级到 `mss`。
- 答案不是流式的，长代码题要等模型完整返回。
- 进程是 DPI-unaware 的，高 DPI 屏上坐标是虚拟化后的值（多屏混合缩放已做兼容）。

## 路线图

按「做完最能提升手感」排的，欢迎按条认领：

- **WGC 采集后端** —— `Windows.Graphics.Capture` 能抓**被其它窗口遮挡**的目标窗口，
  「题目在后面那个窗口里」就不用先切前台了，帧率也高得多。代价是引入 WinRT 绑定
  加一小段 D3D 互操作，打包体积会明显变大。（`capture.py:_grab_wgc`）
- **流式答案** —— 现在 `_call_api` 一次性等完整响应；改 SSE（`"stream": true`）
  边收边 `set_text`，长代码题的感知延迟能从「十几秒空白」变成「一秒出头」。
  （`main.py:AnswerProvider._call_api`）
- **复盘导出增强** —— 按日期/关键词筛选、导出 HTML 或单页；`--export-md`
  目前是全量重写 `复盘记录.md`。（`history.py`、`main.py:cmd_export_md`）
- **锁依赖版本** —— `requirements.txt` 现在全是 `>=`。PaddleOCR 2.x/3.x 的 API
  差异代码里做了兼容分支，但上游确实会破坏兼容（见 `ocr.py` 里 oneDNN/PIR 那段注释），
  至少该给出实测通过的版本组合。
- **CI** —— GitHub Actions 上跑 `python interview_ai/run_tests.py` 即可：
  ubuntu runner 会自动跳过需要真实桌面的 `test_move.py`，退出码就是结论。

## 贡献

Issue / PR 都欢迎，上面路线图里随便挑一条都行。
提 PR 前请跑一遍 `python run_tests.py`（Windows 上四套全跑，其它平台会自动
跳过需要真实桌面的 `test_move.py`），改到浮层交互的话再补 `test_move.py` 的用例。

## 许可证

[Apache License 2.0](LICENSE) —— 可自由使用、修改、商用，含显式专利授权；
修改后分发需保留版权与 [NOTICE](NOTICE) 声明，不授予商标使用权。

注意：许可证给的是**著作权层面**的授权，不改变本文开头的合规声明 ——
拿它去在线面试/考试仍然可能违反平台协议，这是使用者自己的责任。

## 致谢

[PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR) · [mss](https://github.com/BoboTiG/python-mss)
· [pywin32](https://github.com/mhammond/pywin32) · [PyInstaller](https://pyinstaller.org/)
