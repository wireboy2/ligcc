# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller 打包配置（PaddleOCR 专用）
==================================================================
用法:
    pyinstaller build.spec              # 控制台版（调试/验证用，能看到输出）
    pyinstaller --noconfirm build.spec

产物: dist/SystemHelper/  （onedir 模式，启动快）

打包后把以下文件放到 exe 同目录:
    aiKey.txt            API 密钥配置（必须）
    .paddle_cache/       OCR 模型（约 200MB，首次运行自动下载，可复制现有缓存）
    （build.bat 会自动帮你复制这两项）

说明:
    - onedir 而非 onefile：Paddle 全家桶体积巨大（1.5G+），onefile
      每次启动要解压到临时目录，耗时数分钟，不实用。
    - 排除 torch 系：PaddleOCR 推理不需要（已实测验证），可省 ~2GB。
    - console=True 便于验证；要无控制台隐蔽版，把下面 CONSOLE 改 False 重打。
"""

from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

CONSOLE = True  # 调试期 True；隐蔽部署改 False（--windowed 等效）

datas = []
binaries = []
hiddenimports = []

# paddle / paddleocr / paddlex 有大量动态导入和自带数据文件，全量收集
for pkg in ("paddle", "paddleocr", "paddlex", "paddlex.utils", "pywin32"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

# paddlex 运行时用 importlib.metadata 做依赖自检（模块级
# `if is_dep_available(...): import cv2`），必须把 dist-info 元数据
# 打进 exe，否则 opencv-contrib-python / pyclipper / shapely 等检查
# 失败、条件导入被跳过、运行时 NameError。
# 列表 = paddlex 源码中 is_dep_available/class_requires_deps 检查的全部包名
# （_scan_deps.py 生成）。未安装的 copy_metadata 抛异常自动跳过。
# 注意：torch 系元数据绝不能加——会让 is_dep_available("torch")=True，
# 触发 import torch 失败崩溃。
for pkg in (
    "paddlex", "paddleocr", "paddlepaddle",
    "opencv-contrib-python", "opencv-python",
    "shapely", "soundfile", "lxml", "openpyxl",
    "pydantic", "numpy", "requests", "urllib3",
    "PyYAML", "Pillow", "pywin32",
    # paddlex 依赖自检涉及的包
    "Jinja2", "chinese-calendar", "filetype", "imagesize", "joblib",
    "premailer", "pyclipper", "pycocotools", "pypdfium2", "pypinyin",
    "python-bidi", "regex", "safetensors", "scikit-image", "scikit-learn",
    "starlette", "tokenizers", "tqdm", "transformers", "yarl",
    "aiohttp", "bce-python-sdk", "faiss-cpu", "langchain",
    "langchain-community", "langchain-core", "langchain-text-splitters",
    "openai", "scikit-learn",
):
    try:
        datas += copy_metadata(pkg)
    except Exception:
        pass

# 明确不需要的大件（PaddleOCR CPU 推理链路不依赖）
# 注意：setuptools 不能排除！paddle.cpp_extension 运行时 import 它
excludes = [
    "torch", "torchvision", "torchaudio",
    "matplotlib", "IPython", "jupyter", "notebook",
    "pytest", "pip",
    "tkinter",
    "paddle.distributed",  # 分布式训练，推理不需要
]

a = Analysis(
    ["main.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SystemHelper",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,           # UPX 压缩 paddle DLL 易损坏，禁用
    console=CONSOLE,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="SystemHelper",
)
