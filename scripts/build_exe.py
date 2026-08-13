"""构建当前平台的可执行分发包。

PyInstaller 无法交叉编译：**在哪个平台跑就只能产出那个平台的包**，产物名如实
标注 `{os}-{arch}`，避免像旧的 whl 离线包那样把平台特定产物起成通用名字。

构建逻辑放在这里而不是 Makefile，是因为 CI 的 Windows runner 没有 GNU Make，
放 Makefile 就得在 workflow 里再写一份。Makefile 与 CI 都调用本脚本。

用法：python scripts/build_exe.py [--skip-warm] [--install]
"""

from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
BUILD = ROOT / "build"
TIKTOKEN_CACHE = BUILD / "tiktoken_cache"


def platform_tag() -> str:
    """返回 `{os}-{arch}` 形式的平台标识，用于产物命名。

    Returns:
        如 macos-arm64、linux-x86_64、windows-x86_64。
    """
    system = {"darwin": "macos"}.get(platform.system().lower(), platform.system().lower())
    machine = platform.machine().lower()
    machine = {"amd64": "x86_64", "arm64": "arm64", "aarch64": "arm64"}.get(machine, machine)
    return f"{system}-{machine}"


def project_version() -> str:
    """从 pyproject.toml 读取版本号。

    Returns:
        版本字符串。
    """
    with (ROOT / "pyproject.toml").open("rb") as f:
        return tomllib.load(f)["project"]["version"]


def archive(source: Path, target_stem: Path) -> Path:
    """把构建目录打成压缩包，解压后为一个同名目录。

    Args:
        source: 待打包的目录。
        target_stem: 产物路径（不含扩展名）。

    Returns:
        生成的压缩包路径。
    """
    if platform.system() == "Windows":
        target = target_stem.with_suffix(".zip")
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(source.rglob("*")):
                zf.write(path, source.name / path.relative_to(source))
        return target

    target = Path(f"{target_stem}.tar.gz")
    with tarfile.open(target, "w:gz") as tf:
        tf.add(source, arcname=source.name)
    return target


def stage_installer(staged: Path, version: str) -> Path:
    """把安装脚本与版本标记放进待打包目录，使压缩包解压即可安装。

    只放当前平台对应的那一个脚本：压缩包本身已按平台命名，另一平台的脚本纯属噪音，
    且 .sh 在 Windows 上没有可执行语义、.bat 在 Unix 上无人调用。

    不能走 agent.spec 的 datas：那会落进 `_internal/`，而安装脚本必须在产物顶层。

    Args:
        staged: 待打包目录（dist/agent-{版本}-{平台}）。
        version: 项目版本号，写入 VERSION 供安装器读取（目录被改名后仍可确定版本）。

    Returns:
        落地的安装脚本路径。
    """
    # 固定写 LF：install.bat 用 for /f 读这个文件，而 for /f 不剥行尾的 CR，
    # 在 Windows 上按平台默认写成 CRLF 会让版本号带一个回车，进而拼出坏掉的安装路径。
    (staged / "VERSION").write_text(f"{version}\n", encoding="utf-8", newline="\n")
    name = "install.bat" if platform.system() == "Windows" else "install.sh"
    script = Path(shutil.copy2(ROOT / "scripts" / name, staged / name))
    if name.endswith(".sh"):
        # tarfile 会保留 mode，仓库里丢了可执行位时不至于发出一个跑不起来的安装器
        script.chmod(0o755)
    return script


def main() -> int:
    parser = argparse.ArgumentParser(description="构建当前平台的可执行分发包")
    parser.add_argument(
        "--skip-warm",
        action="store_true",
        help="跳过 tiktoken 缓存预热（缓存已存在时可省下一次下载）",
    )
    parser.add_argument(
        "--install",
        action="store_true",
        help="构建完成后立即用产物内的安装脚本装到用户目录",
    )
    args = parser.parse_args()

    if not args.skip_warm or not TIKTOKEN_CACHE.is_dir():
        # 预热必须用与打包同一个 tiktoken 版本，所以走当前解释器而非独立环境
        subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "warm_tiktoken_cache.py"), str(TIKTOKEN_CACHE)],
            check=True,
        )

    subprocess.run(
        [
            sys.executable, "-m", "PyInstaller", str(ROOT / "agent.spec"),
            "--noconfirm",
            "--distpath", str(DIST),
            "--workpath", str(BUILD / "pyi"),
        ],
        check=True,
        cwd=ROOT,
    )

    name = f"agent-{project_version()}-{platform_tag()}"
    staged = DIST / name
    if staged.exists():
        shutil.rmtree(staged)
    (DIST / "agent").rename(staged)

    script = stage_installer(staged, project_version())
    package = archive(staged, DIST / name)
    size_mb = package.stat().st_size / 1024 / 1024
    print(f"\n可执行包：{package}  ({size_mb:.1f} MB)")
    print(f"构建目录：{staged}")
    print(f"自检：    {staged / 'agent'} --self-check")
    if platform.system() == "Windows":
        print(f"安装：    {script}")
    else:
        print(f"安装：    sh {script}")

    if args.install:
        cmd = ["cmd", "/c", str(script)] if platform.system() == "Windows" else ["sh", str(script)]
        subprocess.run(cmd, check=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
