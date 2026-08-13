# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（onedir）。

用 onedir 而非 onefile：onefile 每次启动都要把整包解压到临时目录，冷启动 2~5s，
对交互式 TUI 不可接受。

本文件承担三类静态分析看不到的依赖，缺一项都会在冻结产物里表现为**静默降级而非
报错**——应用照常启动，只是工具、命令或编码悄悄不见了：

1. datas —— 靠 builtin_root()（即 Path(__file__).parent.parent）按相对路径读取的
   随包资源，必须落到 _internal/src/ 下的原位置。
2. hiddenimports —— 运行时才决定名字的模块：惰性 import 的 MCP transport、
   靠 pkgutil.iter_modules 枚举的插件式子模块（我们的工具/命令、tiktoken 的编码、
   ddgs 的搜索引擎）。
3. copy_metadata —— 运行时调 importlib.metadata 读自身版本的包。

构建前需先跑 scripts/warm_tiktoken_cache.py 生成 build/tiktoken_cache（Makefile 已代劳）。
"""

from importlib import metadata
from pathlib import Path
import sys

from PyInstaller.utils.hooks import collect_submodules, copy_metadata

ROOT = Path(SPECPATH)
TIKTOKEN_CACHE = ROOT / "build" / "tiktoken_cache"


def _ripgrep_binary():
    """定位 ripgrep 包随 wheel 装进环境 bin 目录的 rg 可执行文件。

    Returns:
        [(源路径, 包内目标目录)]；未找到时返回空列表，产物将退化为依赖宿主预装 rg。
    """
    exe = "rg.exe" if sys.platform == "win32" else "rg"
    try:
        dist = metadata.distribution("ripgrep")
    except metadata.PackageNotFoundError:
        return []
    for f in dist.files or []:
        if f.name == exe:
            path = Path(dist.locate_file(f)).resolve()
            if path.exists():
                return [(str(path), ".")]
    return []


if not TIKTOKEN_CACHE.is_dir():
    raise SystemExit(
        f"缺少 tiktoken 预热缓存：{TIKTOKEN_CACHE}\n"
        f"请先运行：python scripts/warm_tiktoken_cache.py {TIKTOKEN_CACHE}"
    )

datas = [
    # builtin_root() 相对定位的随包资源，目标路径必须与源码树一致
    (str(ROOT / "src" / "config.yaml"), "src"),
    (str(ROOT / "src" / "interfaces" / "tui" / "agent.tcss"), "src/interfaces/tui"),
    (str(ROOT / "src" / "llm" / "tokenizer"), "src/llm/tokenizer"),
    (str(ROOT / "src" / "roles"), "src/roles"),
    # 预热的 BPE 缓存，由 src.mgr.frozen.setup_tiktoken_cache 在启动时指向
    (str(TIKTOKEN_CACHE), "tiktoken_cache"),
]
datas += copy_metadata("mcp")       # mcp 多处调 importlib.metadata.version("mcp")
datas += copy_metadata("textual")   # textual.__version__ 走 version("textual")

hiddenimports = [
    # mcp_mgr 按 transport 惰性 import，静态分析看不到
    "mcp.client.stdio",
    "mcp.client.streamable_http",
    "mcp.client.sse",
    "httpx",
    # tiktoken 用 pkgutil 扫 tiktoken_ext 命名空间包发现编码构造器
    "tiktoken_ext",
    "tiktoken_ext.openai_public",
]
# 以下三个包的子模块都没有静态引用点，全靠运行时 pkgutil 枚举
hiddenimports += collect_submodules("src.tools.builtin")
hiddenimports += collect_submodules("src.commands.builtin")
hiddenimports += collect_submodules("ddgs.engines")

a = Analysis(
    ["main.py"],
    pathex=[str(ROOT)],
    binaries=_ripgrep_binary(),
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "pytest",
        "tkinter",
        # 已从依赖中移除，列在这里防止环境里的残留被牵连进来
        "transformers",
        "numpy",
        "safetensors",
        # tokenizers 的传递依赖，但 Tokenizer.from_file 用不到（已验证不会被顶层 import）
        "huggingface_hub",
        "hf_xet",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="agent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="agent",
)
