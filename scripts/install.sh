#!/bin/sh
# agent 安装器（macOS / Linux）。
#
# 双模式，由脚本自身所在目录决定：
#   - 本地模式：脚本目录里有 agent + _internal/，直接安装这份 payload（解压即装）
#   - 远程模式：没有脚本目录（curl | sh）或目录里没有 payload，从 GitHub release 下载
#
# 布局：payload 落到 ~/.local/share/agent/<版本>/，再软链 ~/.local/bin/agent 指向其中的 agent。
# onedir 产物的 agent 必须与同级 _internal/ 在一起，所以不能只把二进制拷进 ~/.local/bin。
#
# ~/.local/bin 不在冻结包的 _internal 之下，因此 src/mgr/frozen.py 的 clean_env() 不会把它从
# 子进程 PATH 里剥掉 —— hook 与 MCP server 里能调到 agent 正是靠这一点，不要去"修"那段逻辑。
#
# 全流程非交互：curl | sh 时 stdin 就是脚本本身，读 stdin 会吃掉未执行的脚本正文。

set -eu

REPO=dingdalong/agent
MARK_BEGIN='# >>> agent installer >>>'
MARK_END='# <<< agent installer <<<'

BIN_DIR=''
SHARE_DIR=''

FROM=''
VERSION_TAG=''
FORCE=0
KEEP_OLD=0
VERIFY=0
UNINSTALL=0

PAYLOAD=''
DEST=''

# 供 trap 清理的临时路径，空表示无需清理
TMP_DIR=''
STAGING=''
OLD_DIR=''

die() {
    printf '错误：%s\n' "$*" >&2
    exit 1
}

warn() {
    printf '警告：%s\n' "$*" >&2
}

note() {
    printf '%s\n' "$*"
}

have() {
    command -v "$1" >/dev/null 2>&1
}

cleanup() {
    if [ -n "$TMP_DIR" ]; then
        rm -rf "$TMP_DIR"
    fi
    if [ -n "$STAGING" ]; then
        rm -rf "$STAGING"
    fi
    if [ -n "$OLD_DIR" ]; then
        rm -rf "$OLD_DIR"
    fi
}

usage() {
    printf '%s\n' '用法：install.sh [选项]'
    printf '%s\n' ''
    printf '%s\n' '  --from DIR      从指定的解压目录安装，跳过下载'
    printf '%s\n' '  --version TAG   安装指定 release（如 v0.2.0），默认最新'
    printf '%s\n' '  --force         覆盖已存在但非本安装器创建的 ~/.local/bin/agent'
    printf '%s\n' '  --keep-old      保留旧版本目录，不自动清理'
    printf '%s\n' '  --verify        安装后额外运行 --self-check（约 4 秒）'
    printf '%s\n' '  --uninstall     卸载：删链接、删程序目录、移除 shell 配置里的 PATH 段'
    printf '%s\n' '  -h, --help      显示本帮助'
    printf '%s\n' ''
    printf '%s\n' '远程安装：'
    printf '%s\n' '  curl -fsSL https://raw.githubusercontent.com/dingdalong/agent/main/scripts/install.sh | sh'
    printf '%s\n' '  curl -fsSL <同上> | sh -s -- --version v0.2.0'
}

# 返回 {os}-{arch}。映射表与 scripts/build_exe.py 的 platform_tag() 必须保持一致。
detect_platform() {
    os=$(uname -s)
    arch=$(uname -m)
    case "$os" in
        Darwin) os=macos ;;
        Linux) os=linux ;;
        *) die "不支持的系统：${os}（仅支持 macOS 与 Linux；Windows 请用 install.bat）" ;;
    esac
    case "$arch" in
        arm64 | aarch64) arch=arm64 ;;
        x86_64 | amd64) arch=x86_64 ;;
        *) die "不支持的架构：$arch" ;;
    esac
    # Rosetta 下 uname -m 谎报 x86_64，实际该装 arm64 包
    if [ "$os" = macos ] && [ "$arch" = x86_64 ]; then
        if [ "$(sysctl -n sysctl.proc_translated 2>/dev/null || printf 0)" = 1 ]; then
            arch=arm64
        fi
    fi
    printf '%s-%s\n' "$os" "$arch"
}

require_tools() {
    if ! have curl && ! have wget; then
        die "需要 curl 或 wget（Debian/Ubuntu：apt install curl；RHEL/Fedora：dnf install curl）"
    fi
    if ! have tar; then
        die "需要 tar"
    fi
}

# 组装认证 header。GITHUB_TOKEN 只为抬高 API 限额，缺省匿名访问即可。
auth_header() {
    token=${GITHUB_TOKEN:-${GH_TOKEN:-}}
    if [ -n "$token" ]; then
        printf 'Authorization: Bearer %s\n' "$token"
    else
        printf 'X-Installer: agent\n'
    fi
}

http_get() {
    hdr=$(auth_header)
    if have curl; then
        curl -fsSL -H "$hdr" "$1"
    else
        wget -qO- --header="$hdr" "$1"
    fi
}

http_download() {
    hdr=$(auth_header)
    if have curl; then
        curl -fL --progress-bar -H "$hdr" "$1" -o "$2"
    else
        wget -q --show-progress --header="$hdr" -O "$2" "$1"
    fi
}

# 从 release JSON 里挑出本平台的 tar.gz 下载地址，无匹配则输出空。
# 用 tr 把逗号换成换行，保证一个字段一行，避免 sed 的贪婪匹配跨字段取错值。
# 取 browser_download_url 而不是自己拼 URL：能扛住 git tag 与 pyproject 版本号不一致。
pick_asset() {
    printf '%s' "$1" | tr ',' '\n' |
        sed -n 's/.*"browser_download_url"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' |
        grep -- "-$2\.tar\.gz\$" |
        head -n 1
}

# 脚本自身所在目录。curl | sh 时 $0 是 sh 之类，没有脚本文件，返回非零。
script_dir() {
    case "${0:-}" in
        sh | -sh | dash | -dash | bash | -bash | zsh | -zsh | -) return 1 ;;
    esac
    [ -f "$0" ] || return 1
    CDPATH='' cd -- "$(dirname "$0")" && pwd -P
}

is_payload() {
    [ -x "$1/agent" ] && [ -d "$1/_internal" ]
}

find_local_payload() {
    dir=$(script_dir) || return 1
    is_payload "$dir" || return 1
    printf '%s\n' "$dir"
}

# 版本号优先取 payload 里的 VERSION（由 build_exe.py 写入），这样用户重命名解压目录也不影响。
payload_version() {
    if [ -f "$1/VERSION" ]; then
        tr -d ' \t\r\n' < "$1/VERSION"
        printf '\n'
        return 0
    fi
    # 回退：从 agent-{版本}-{os}-{arch} 目录名里剥出版本段
    name=${1##*/}
    name=${name#agent-}
    name=${name%-*}
    name=${name%-*}
    if [ -n "$name" ] && [ "$name" != "${1##*/}" ]; then
        printf '%s\n' "$name"
    else
        printf 'unknown\n'
    fi
}

# 下载并解压 release 资产，把 payload 目录写入全局 PAYLOAD。
fetch_remote() {
    platform=$1
    # 当前 release 矩阵只有这三个平台，Intel Mac 与 ARM Linux 需自行构建
    case "$platform" in
        macos-arm64 | linux-x86_64) ;;
        *)
            die "暂未提供 $platform 的预构建包（现有：macos-arm64、linux-x86_64、windows-x86_64）。
请从源码构建：git clone https://github.com/$REPO && cd agent && make build"
            ;;
    esac

    if [ -n "${AGENT_INSTALL_RELEASE_JSON:-}" ]; then
        api=$AGENT_INSTALL_RELEASE_JSON
    elif [ -n "$VERSION_TAG" ]; then
        api="https://api.github.com/repos/$REPO/releases/tags/$VERSION_TAG"
    else
        api="https://api.github.com/repos/$REPO/releases/latest"
    fi

    note "查询 release：$api"
    if ! json=$(http_get "$api"); then
        die "取 release 信息失败。若是 GitHub API 限流（未认证 60 次/小时），
可设置 GITHUB_TOKEN，或下载压缩包后用 --from <解压目录> 安装。"
    fi

    asset=$(pick_asset "$json" "$platform")
    if [ -z "$asset" ]; then
        die "release 里没有 $platform 的资产。可用资产：
$(printf '%s' "$json" | tr ',' '\n' | sed -n 's/.*"name"[[:space:]]*:[[:space:]]*"\(agent-[^"]*\)".*/  \1/p')"
    fi

    TMP_DIR=$(mktemp -d)
    note "下载 $asset"
    http_download "$asset" "$TMP_DIR/agent.tar.gz"
    tar -xzf "$TMP_DIR/agent.tar.gz" -C "$TMP_DIR"
    for d in "$TMP_DIR"/*; do
        if [ -d "$d" ] && is_payload "$d"; then
            PAYLOAD=$d
            return 0
        fi
    done
    die "压缩包内未找到产物目录（缺 agent 或 _internal）"
}

# 把 payload 落到 ~/.local/share/agent/<版本>/，结果写入全局 DEST。
# 先拷到 .新.$$ 再整目录改名换位：升级中断不会留下半写的目标目录；
# POSIX 下删除仍被打开的旧目录是合法的，所以运行中的实例不会挡住升级。
install_payload() {
    payload=$1
    version=$2
    DEST=$SHARE_DIR/$version
    mkdir -p "$SHARE_DIR"

    src=$(cd "$payload" && pwd -P)
    dst=$(cd "$DEST" 2>/dev/null && pwd -P || printf '')
    if [ -n "$dst" ] && [ "$src" = "$dst" ]; then
        note "产物已在目标位置，跳过复制。"
        return 0
    fi

    if [ -d "$DEST" ]; then
        note "覆盖已安装的 ${version}（若有实例正在运行，请重启它）"
    fi
    STAGING=$SHARE_DIR/.$version.new.$$
    rm -rf "$STAGING"
    cp -R "$payload" "$STAGING"
    if [ -d "$DEST" ]; then
        OLD_DIR=$SHARE_DIR/.$version.old.$$
        mv "$DEST" "$OLD_DIR"
    fi
    mv "$STAGING" "$DEST"
    STAGING=''
    if [ -n "$OLD_DIR" ]; then
        rm -rf "$OLD_DIR"
        OLD_DIR=''
    fi
}

# 未签名产物从浏览器下载会带隔离标记，双击解压出来的目录尤其需要清。永不因此中断安装。
strip_quarantine() {
    if [ "$(uname -s)" != Darwin ]; then
        return 0
    fi
    if ! have xattr; then
        return 0
    fi
    if ! xattr -dr com.apple.quarantine "$1" 2>/dev/null; then
        warn "去除隔离标记失败。若首次运行被 Gatekeeper 拦下，请手动执行：
    xattr -dr com.apple.quarantine $1"
    fi
}

# 落地前先查冲突：拷贝约 100MB 之后才报错太浪费。
check_bin_dir() {
    if [ -e "$BIN_DIR" ] && [ ! -d "$BIN_DIR" ]; then
        die "$BIN_DIR 已存在且不是目录，请先处理它"
    fi
    target=$BIN_DIR/agent
    if [ ! -e "$target" ] && [ ! -L "$target" ]; then
        return 0
    fi
    case "$(readlink "$target" 2>/dev/null || printf '')" in
        "$SHARE_DIR"/*) return 0 ;;
    esac
    if [ "$FORCE" != 1 ]; then
        die "$target 已存在且不是本安装器创建的（可能来自 pipx / uv tool install）。
确认可以覆盖后加 --force 重试。"
    fi
}

# 软链用绝对路径目标：~/.local/bin 本身是 dotfiles 软链时仍然有效。
# 先建临时链再 mv -f 覆盖，是单次 rename(2)，不存在 agent 短暂缺失的窗口。
link_binary() {
    mkdir -p "$BIN_DIR"
    tmp_link=$BIN_DIR/.agent.$$
    rm -f "$tmp_link"
    ln -s "$DEST/agent" "$tmp_link"
    mv -f "$tmp_link" "$BIN_DIR/agent"
}

# 只删确凿属于本安装器的目录：跳过 . 开头的临时目录，且必须同时有 agent 与 _internal/。
prune_old() {
    if [ "$KEEP_OLD" = 1 ]; then
        return 0
    fi
    for d in "$SHARE_DIR"/*; do
        if [ ! -d "$d" ] || [ "$d" = "$DEST" ]; then
            continue
        fi
        case "${d##*/}" in
            .*) continue ;;
        esac
        if is_payload "$d"; then
            rm -rf "$d"
            note "已清理旧版本 ${d##*/}"
        fi
    done
}

# 选一个 shell 配置文件。依据 $SHELL 而非 $0：curl | sh 下 $0 恒为 sh。
rc_file() {
    name=${SHELL:-}
    name=${name##*/}
    case "$name" in
        zsh)
            printf '%s\n' "${ZDOTDIR:-$HOME}/.zshrc"
            ;;
        bash)
            # macOS 的终端起的是 login shell，根本不读 ~/.bashrc
            if [ "$(uname -s)" = Darwin ]; then
                if [ -f "$HOME/.bash_profile" ]; then
                    printf '%s\n' "$HOME/.bash_profile"
                elif [ -f "$HOME/.profile" ]; then
                    printf '%s\n' "$HOME/.profile"
                else
                    printf '%s\n' "$HOME/.bash_profile"
                fi
            else
                printf '%s\n' "$HOME/.bashrc"
            fi
            ;;
        fish)
            printf '%s\n' "${XDG_CONFIG_HOME:-$HOME/.config}/fish/config.fish"
            ;;
        '')
            warn "无法从 \$SHELL 判断 shell 类型，改写 ~/.profile"
            printf '%s\n' "$HOME/.profile"
            ;;
        *)
            printf '%s\n' "$HOME/.profile"
            ;;
    esac
}

# 只在 ~/.local/bin 不在 PATH 上时改配置，只追加不重写，靠围栏标记保证幂等。
ensure_path() {
    case ":${PATH:-}:" in
        *":$BIN_DIR:"*)
            note "$BIN_DIR 已在 PATH 中，未改动任何 shell 配置。"
            return 0
            ;;
    esac

    rc=$(rc_file)
    if [ -e "$rc" ] && [ ! -f "$rc" ]; then
        warn "$rc 不是普通文件，跳过 PATH 配置。请自行把 $BIN_DIR 加入 PATH。"
        return 0
    fi
    if [ -f "$rc" ] && grep -Fq "$MARK_BEGIN" "$rc"; then
        note "$rc 已包含 agent 的 PATH 配置，未重复写入。"
        return 0
    fi

    mkdir -p "$(dirname "$rc")"
    # 末尾缺换行会让追加内容粘到最后一行上。命令替换会吃掉尾部换行，故取到非空即表示缺换行。
    if [ -s "$rc" ] && [ -n "$(tail -c 1 "$rc")" ]; then
        printf '\n' >> "$rc"
    fi
    # 格式串用单引号，$HOME 与 $PATH 按字面量写进配置，家目录搬迁后仍然有效
    if [ "${rc##*/}" = config.fish ]; then
        printf '%s\nset -gx PATH "$HOME/.local/bin" $PATH\n%s\n' "$MARK_BEGIN" "$MARK_END" >> "$rc"
        note "已写入 ${rc}。当前终端立即生效请执行："
        note '    set -gx PATH "$HOME/.local/bin" $PATH'
    else
        printf '%s\nexport PATH="$HOME/.local/bin:$PATH"\n%s\n' "$MARK_BEGIN" "$MARK_END" >> "$rc"
        note "已写入 ${rc}。当前终端立即生效请执行："
        note '    export PATH="$HOME/.local/bin:$PATH"'
    fi
    note "或重开一个终端。"
}

verify_install() {
    target=$BIN_DIR/agent
    if ! "$target" --help > /dev/null 2>&1; then
        die "安装完成但 $target 跑不起来。可能原因：预构建包与本机架构不符
（Exec format error / Bad CPU type）、被 Gatekeeper 拦下，或所在分区以 noexec 挂载。"
    fi
    if [ "$VERIFY" = 1 ]; then
        note "运行自检……"
        if ! "$target" --self-check; then
            die "自检未通过"
        fi
    fi
    found=$(command -v agent 2>/dev/null || printf '')
    if [ -n "$found" ] && [ "$found" != "$target" ]; then
        warn "PATH 中 $found 排在前面，$target 被它遮蔽。"
    fi
}

uninstall() {
    target=$BIN_DIR/agent
    if [ -L "$target" ]; then
        case "$(readlink "$target" 2>/dev/null || printf '')" in
            "$SHARE_DIR"/*)
                rm -f "$target"
                note "已删除 $target"
                ;;
            *)
                warn "$target 不是本安装器创建的，保留不动"
                ;;
        esac
    elif [ -e "$target" ]; then
        warn "$target 不是符号链接，保留不动"
    fi

    if [ -d "$SHARE_DIR" ]; then
        rm -rf "$SHARE_DIR"
        note "已删除 $SHARE_DIR"
    fi

    tmp=$(mktemp)
    for rc in "$HOME/.zshrc" "${ZDOTDIR:-$HOME}/.zshrc" "$HOME/.bashrc" \
        "$HOME/.bash_profile" "$HOME/.profile" \
        "${XDG_CONFIG_HOME:-$HOME/.config}/fish/config.fish"; do
        if [ -f "$rc" ] && grep -Fq "$MARK_BEGIN" "$rc"; then
            cp "$rc" "$rc.agent.bak"
            awk -v b="$MARK_BEGIN" -v e="$MARK_END" '
                $0 == b { skip = 1 }
                !skip { print }
                $0 == e { skip = 0 }
            ' "$rc" > "$tmp"
            # 用重定向写回而非 mv：保住 stow/dotfiles 软链的 inode 与权限
            cat "$tmp" > "$rc"
            note "已从 $rc 移除 PATH 配置（备份：$rc.agent.bak）"
        fi
    done
    rm -f "$tmp"
    note "卸载完成。"
}

main() {
    if [ -z "${HOME:-}" ]; then
        printf '错误：环境变量 HOME 未设置\n' >&2
        exit 1
    fi
    BIN_DIR=$HOME/.local/bin
    SHARE_DIR=$HOME/.local/share/agent

    while [ $# -gt 0 ]; do
        case $1 in
            --from)
                [ $# -ge 2 ] || die "--from 需要一个目录参数"
                FROM=$2
                shift 2
                ;;
            --version)
                [ $# -ge 2 ] || die "--version 需要一个 tag 参数"
                VERSION_TAG=$2
                shift 2
                ;;
            --force) FORCE=1; shift ;;
            --keep-old) KEEP_OLD=1; shift ;;
            --verify) VERIFY=1; shift ;;
            --uninstall) UNINSTALL=1; shift ;;
            -h | --help) usage; exit 0 ;;
            *) die "未知参数：${1}（用 --help 查看用法）" ;;
        esac
    done

    trap cleanup EXIT INT TERM HUP

    if [ "$UNINSTALL" = 1 ]; then
        uninstall
        exit 0
    fi

    if [ "$(id -u)" = 0 ] && [ -n "${SUDO_USER:-}" ]; then
        warn "正在以 root 运行：会装到 $HOME 下，且不会改 $SUDO_USER 的 shell 配置。"
    fi

    check_bin_dir

    if [ -n "$FROM" ]; then
        PAYLOAD=$FROM
        if ! is_payload "$PAYLOAD"; then
            die "$PAYLOAD 不像解压后的产物目录（缺 agent 或 _internal）"
        fi
        note "从 $PAYLOAD 安装"
    elif PAYLOAD=$(find_local_payload); then
        note "从 $PAYLOAD 安装"
    else
        require_tools
        platform=$(detect_platform)
        note "目标平台：$platform"
        fetch_remote "$platform"
    fi

    version=$(payload_version "$PAYLOAD")
    install_payload "$PAYLOAD" "$version"
    strip_quarantine "$DEST"
    link_binary
    prune_old
    ensure_path
    verify_install

    note ""
    note "安装完成：$BIN_DIR/agent -> $DEST/agent"
    note "在任意目录执行 agent 即可启动，工作目录就是启动时所在目录。"
    note "卸载：sh $DEST/install.sh --uninstall"
}

# 唯一的顶层语句，且位于文件末尾：curl | sh 半截截断时不会执行任何东西。
# AGENT_INSTALL_SOURCE_ONLY=1 供测试 source 本文件后单独调用内部函数。
[ "${AGENT_INSTALL_SOURCE_ONLY:-}" = 1 ] || main "$@"
