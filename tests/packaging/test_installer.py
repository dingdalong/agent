"""安装器测试 —— 断言 install.sh 能把产物装成"任意目录可直接启动"。

关键约束：所有用例都把 HOME 指向 tmp_path，绝不碰开发者真实的 ~/.local/bin 与
~/.zshrc。子进程一律用显式的最小环境，避免开发者自身的 ZDOTDIR / XDG_CONFIG_HOME
改变安装器挑选 rc 文件的结果。

多数用例用 make_fake_payload() 造的最小产物：只有"目录里有可执行 agent 与 _internal/"
这一条对被测逻辑有意义，拷 100MB 真产物纯属浪费。只有真正要验证冻结二进制能跑起来的
用例（软链解析、远程链路）才用真产物。
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from .conftest import DIST, EXE, PAYLOAD, ROOT, requires_build

INSTALL_SH = ROOT / "scripts" / "install.sh"
MARK_BEGIN = "# >>> agent installer >>>"

pytestmark = pytest.mark.skipif(
    platform.system() == "Windows", reason="install.sh 只覆盖 macOS 与 Linux"
)


def run_installer(
    home: Path,
    *args: str,
    shell: str = "/bin/zsh",
    path: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """在隔离的 HOME 下运行安装器。

    Args:
        home: 充当 $HOME 的目录。
        *args: 传给安装器的参数。
        shell: 充当 $SHELL 的路径，决定安装器改写哪个 rc 文件。
        path: 充当 $PATH 的值；默认不含 ~/.local/bin，以便触发 rc 写入分支。
        extra_env: 追加的环境变量。

    Returns:
        已完成的子进程。
    """
    env = {
        "HOME": str(home),
        "SHELL": shell,
        "PATH": path if path is not None else "/usr/bin:/bin:/usr/sbin:/sbin",
    }
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["sh", str(INSTALL_SH), *args],
        capture_output=True,
        text=True,
        timeout=300,
        env=env,
        cwd=str(ROOT),
    )


def make_fake_payload(directory: Path, version: str) -> Path:
    """造一个最小产物目录：可执行 agent + _internal/ + VERSION。

    agent 是个空转的 shell 脚本，能满足安装器的 `--help` 存活探测。
    """
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "_internal").mkdir(exist_ok=True)
    exe = directory / "agent"
    exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    exe.chmod(0o755)
    (directory / "VERSION").write_text(f"{version}\n", encoding="utf-8")
    return directory


def payload_version() -> str:
    """真产物的版本号。"""
    assert PAYLOAD is not None
    return (PAYLOAD / "VERSION").read_text(encoding="utf-8").strip()


@pytest.fixture(scope="module")
def real_install(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """把真产物装进一次性 HOME，供需要真二进制的用例共享。"""
    home = tmp_path_factory.mktemp("home-real")
    assert PAYLOAD is not None
    proc = run_installer(home, "--from", str(PAYLOAD))
    assert proc.returncode == 0, f"安装失败：{proc.stdout}\n{proc.stderr}"
    return home


@requires_build
def test_symlink_points_into_versioned_payload(real_install: Path) -> None:
    """~/.local/bin/agent 是软链，指向 ~/.local/share/agent/<版本>/agent。"""
    link = real_install / ".local/bin/agent"
    assert link.is_symlink()
    expected = real_install / ".local/share/agent" / payload_version() / "agent"
    assert link.resolve() == expected.resolve()


@requires_build
def test_installed_binary_runs_from_any_cwd(real_install: Path, tmp_path: Path) -> None:
    """经软链、从无关目录启动仍能自检通过。

    一次覆盖三件事：PyInstaller 引导器穿透软链解析自身路径、_internal/ 随产物一起
    被搬到安装位置、运行时不依赖 cwd。这三条任一失效都会让"装到别处"悄悄坏掉。
    """
    link = real_install / ".local/bin/agent"
    proc = subprocess.run(
        [str(link), "--self-check"],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(tmp_path),
    )
    assert proc.stdout, f"自检无输出，stderr={proc.stderr[:2000]}"
    report = json.loads(proc.stdout)
    assert report["ok"], f"失败项：{report['failed']}"
    installed_root = str(real_install / ".local/share/agent")
    assert report["checks"]["resources"]["builtin_root"].startswith(installed_root)


def test_rc_written_once_and_idempotent(tmp_path: Path) -> None:
    """首次安装写入围栏块，重复安装不再追加。"""
    payload = make_fake_payload(tmp_path / "payload", "1.0.0")
    home = tmp_path / "home"
    home.mkdir()
    assert run_installer(home, "--from", str(payload)).returncode == 0
    rc = home / ".zshrc"
    assert rc.read_text(encoding="utf-8").count(MARK_BEGIN) == 1
    assert 'export PATH="$HOME/.local/bin:$PATH"' in rc.read_text(encoding="utf-8")

    assert run_installer(home, "--from", str(payload)).returncode == 0
    assert rc.read_text(encoding="utf-8").count(MARK_BEGIN) == 1


def test_rc_untouched_when_already_on_path(tmp_path: Path) -> None:
    """~/.local/bin 已在 PATH 上时，一个 rc 文件都不该被创建。"""
    payload = make_fake_payload(tmp_path / "payload", "1.0.0")
    home = tmp_path / "home"
    home.mkdir()
    proc = run_installer(
        home, "--from", str(payload), path=f"{home}/.local/bin:/usr/bin:/bin"
    )
    assert proc.returncode == 0
    assert not (home / ".zshrc").exists()
    assert "已在 PATH 中" in proc.stdout


def test_rc_uses_fish_syntax_for_fish(tmp_path: Path) -> None:
    """fish 用 set -gx；写成 POSIX export 会让此后每个 fish 都报语法错误。"""
    payload = make_fake_payload(tmp_path / "payload", "1.0.0")
    home = tmp_path / "home"
    home.mkdir()
    assert run_installer(home, "--from", str(payload), shell="/usr/bin/fish").returncode == 0
    rc = home / ".config/fish/config.fish"
    content = rc.read_text(encoding="utf-8")
    assert 'set -gx PATH "$HOME/.local/bin" $PATH' in content
    assert "export PATH=" not in content


def test_rc_preserves_trailing_content(tmp_path: Path) -> None:
    """末尾无换行的 rc 文件，追加时不会把围栏块粘到最后一行上。"""
    payload = make_fake_payload(tmp_path / "payload", "1.0.0")
    home = tmp_path / "home"
    home.mkdir()
    rc = home / ".zshrc"
    rc.write_text('alias ll="ls -l"', encoding="utf-8")  # 故意不带尾换行
    assert run_installer(home, "--from", str(payload)).returncode == 0
    lines = rc.read_text(encoding="utf-8").splitlines()
    assert lines[0] == 'alias ll="ls -l"'
    assert MARK_BEGIN in lines


def test_refuses_foreign_binary_without_force(tmp_path: Path) -> None:
    """不覆盖非本安装器创建的 agent（pyproject 有 [project.scripts]，pipx 装过就会撞）。"""
    payload = make_fake_payload(tmp_path / "payload", "1.0.0")
    home = tmp_path / "home"
    bin_dir = home / ".local/bin"
    bin_dir.mkdir(parents=True)
    foreign = bin_dir / "agent"
    foreign.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
    foreign.chmod(0o755)

    proc = run_installer(home, "--from", str(payload))
    assert proc.returncode != 0
    assert "--force" in proc.stderr
    # 快速失败：不该在拷完 payload 之后才报错
    assert not (home / ".local/share/agent").exists()
    assert not foreign.is_symlink()

    proc = run_installer(home, "--from", str(payload), "--force")
    assert proc.returncode == 0, proc.stderr
    assert (bin_dir / "agent").is_symlink()


def test_prunes_old_versions_unless_kept(tmp_path: Path) -> None:
    """默认清理旧版本目录，--keep-old 保留。"""
    old = make_fake_payload(tmp_path / "old", "1.0.0")
    new = make_fake_payload(tmp_path / "new", "2.0.0")
    share = tmp_path / "home/.local/share/agent"

    home = tmp_path / "home"
    home.mkdir()
    assert run_installer(home, "--from", str(old)).returncode == 0
    assert run_installer(home, "--from", str(new)).returncode == 0
    assert sorted(p.name for p in share.iterdir()) == ["2.0.0"]

    home2 = tmp_path / "home2"
    home2.mkdir()
    share2 = home2 / ".local/share/agent"
    assert run_installer(home2, "--from", str(old)).returncode == 0
    assert run_installer(home2, "--from", str(new), "--keep-old").returncode == 0
    assert sorted(p.name for p in share2.iterdir()) == ["1.0.0", "2.0.0"]


def test_uninstall_removes_everything_it_wrote(tmp_path: Path) -> None:
    """卸载删链接、删程序目录，并只剥掉围栏块、保留用户自有内容。"""
    payload = make_fake_payload(tmp_path / "payload", "1.0.0")
    home = tmp_path / "home"
    home.mkdir()
    rc = home / ".zshrc"
    rc.write_text("export FOO=bar\n", encoding="utf-8")
    assert run_installer(home, "--from", str(payload)).returncode == 0
    rc.write_text(rc.read_text(encoding="utf-8") + 'alias ll="ls -l"\n', encoding="utf-8")

    proc = run_installer(home, "--uninstall")
    assert proc.returncode == 0, proc.stderr
    assert not (home / ".local/bin/agent").exists()
    assert not (home / ".local/share/agent").exists()
    assert rc.read_text(encoding="utf-8") == 'export FOO=bar\nalias ll="ls -l"\n'
    assert (home / ".zshrc.agent.bak").exists()


def test_uninstall_keeps_foreign_binary(tmp_path: Path) -> None:
    """卸载不动别人的 agent —— 误删 pipx 装的命令比留下垃圾严重得多。"""
    home = tmp_path / "home"
    bin_dir = home / ".local/bin"
    bin_dir.mkdir(parents=True)
    foreign = bin_dir / "agent"
    foreign.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")

    proc = run_installer(home, "--uninstall")
    assert proc.returncode == 0
    assert foreign.exists()


def source_helper(fixture: Path, script: str, *args: str) -> subprocess.CompletedProcess[str]:
    """source install.sh 后调用其内部函数（AGENT_INSTALL_SOURCE_ONLY 钩子）。"""
    return subprocess.run(
        ["sh", "-c", f'. "$1"; {script}', "sh", str(INSTALL_SH), str(fixture), *args],
        capture_output=True,
        text=True,
        timeout=60,
        env={
            "AGENT_INSTALL_SOURCE_ONLY": "1",
            "HOME": str(fixture.parent),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        },
    )


def write_release_fixture(path: Path, tarball: Path) -> Path:
    """造一份形似 GitHub release 的 JSON，只有 macos-arm64 指向真实文件。"""
    assets = [
        {"name": "install.sh", "browser_download_url": "file:///dev/null"},
        {
            "name": "agent-9.9.9-linux-x86_64.tar.gz",
            "browser_download_url": "file:///nonexistent/agent-9.9.9-linux-x86_64.tar.gz",
        },
        {
            "name": "agent-9.9.9-windows-x86_64.zip",
            "browser_download_url": "file:///nonexistent/agent-9.9.9-windows-x86_64.zip",
        },
        {
            "name": tarball.name,
            "browser_download_url": f"file://{tarball}",
        },
    ]
    path.write_text(json.dumps({"tag_name": "v9.9.9", "assets": assets}), encoding="utf-8")
    return path


def test_pick_asset_matches_only_its_platform(tmp_path: Path) -> None:
    """从 release JSON 里挑资产：命中本平台的 tar.gz，其他平台一律落空。

    这段解析没有别的验证手段 —— 真实 release 只有发版时才存在。
    """
    fixture = write_release_fixture(tmp_path / "release.json", tmp_path / "agent-9.9.9-macos-arm64.tar.gz")
    got = source_helper(fixture, 'pick_asset "$(cat "$2")" "$3"', "macos-arm64")
    assert got.stdout.strip().endswith("agent-9.9.9-macos-arm64.tar.gz")

    got = source_helper(fixture, 'pick_asset "$(cat "$2")" "$3"', "linux-x86_64")
    assert got.stdout.strip().endswith("agent-9.9.9-linux-x86_64.tar.gz")

    # 这两个平台目前没有预构建包，必须落空而不是错配到别的资产
    for absent in ("macos-x86_64", "linux-arm64"):
        got = source_helper(fixture, 'pick_asset "$(cat "$2")" "$3"', absent)
        assert got.stdout.strip() == "", f"{absent} 不该匹配到任何资产"


def test_unsupported_platform_reports_clearly(tmp_path: Path) -> None:
    """没有预构建包的平台要明确告知，而不是下载失败后抛一堆噪音。"""
    got = source_helper(tmp_path / "unused", "fetch_remote macos-x86_64")
    assert got.returncode != 0
    assert "macos-x86_64" in got.stderr
    assert "make build" in got.stderr


@requires_build
@pytest.mark.skipif(shutil.which("curl") is None, reason="远程链路需要 curl")
def test_remote_install_from_release_json(tmp_path: Path) -> None:
    """走完整远程路径：查 release、下载、解压、定位产物、安装。

    用 file:// 的 fixture 顶替 GitHub API，整条链路真实执行且不依赖网络。
    """
    assert PAYLOAD is not None
    tarball = DIST / f"{PAYLOAD.name}.tar.gz"
    if not tarball.is_file():
        pytest.skip(f"未找到压缩包 {tarball}")
    fixture = write_release_fixture(tmp_path / "release.json", tarball)
    home = tmp_path / "home"
    home.mkdir()

    proc = run_installer(
        home,
        extra_env={"AGENT_INSTALL_RELEASE_JSON": f"file://{fixture}"},
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    link = home / ".local/bin/agent"
    assert link.is_symlink()
    assert link.resolve().is_file()


@requires_build
def test_installer_ships_inside_payload() -> None:
    """安装脚本与 VERSION 必须在产物顶层 —— 用户解压后就该能直接运行它。

    走 agent.spec 的 datas 会落进 _internal/，所以这一步只能由 build_exe.py 完成。
    """
    assert PAYLOAD is not None
    installer = PAYLOAD / "install.sh"
    assert installer.is_file()
    assert os.access(installer, os.X_OK), "install.sh 缺可执行位"
    assert (PAYLOAD / "VERSION").read_text(encoding="utf-8").strip()
    assert (PAYLOAD / EXE).is_file()


def test_sh_has_no_bom_and_lf() -> None:
    """UTF-8 无 BOM + LF，且仓库里带可执行位。"""
    data = INSTALL_SH.read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" not in data, "install.sh 含 CRLF 行尾"
    assert os.access(INSTALL_SH, os.X_OK), "install.sh 需以 0755 提交"


def test_sh_braces_vars_before_non_ascii() -> None:
    r"""`$var` 紧跟中文时必须写成 `${var}`。

    bash（macOS 的 /bin/sh）会把中文字符的首字节吃进变量名，`$rc。` 变成读取
    名为 `rc\xe3` 的变量，在 set -u 下直接报 unbound variable 而中断安装。
    dash 不会，所以这个错误在 Linux 上测不出来。
    """
    offenders = []
    pattern = re.compile(r"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?|\$\{?[0-9]\}?")
    for number, line in enumerate(INSTALL_SH.read_text(encoding="utf-8").splitlines(), start=1):
        if line.lstrip().startswith("#"):
            continue
        for match in pattern.finditer(line):
            if match.group().endswith("}"):
                continue
            tail = line[match.end() : match.end() + 1]
            if tail and ord(tail) > 127:
                offenders.append(f"{number}: {line.strip()}")
    assert not offenders, "以下行的变量需改用 ${...} 包裹：\n" + "\n".join(offenders)
