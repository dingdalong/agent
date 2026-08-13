@echo off
rem agent 安装器（Windows）。与 scripts/install.sh 一一对应，参数与提示语保持一致。
rem
rem 双模式，由脚本自身所在目录决定：
rem   - 本地模式：脚本目录里有 agent.exe + _internal\，直接安装这份产物（解压即装）
rem   - 远程模式：目录里没有产物，从 GitHub release 下载
rem
rem 布局：产物落到 %LOCALAPPDATA%\Programs\agent\<版本>\，再在固定的 bin\ 下写一个
rem agent.bat 转发脚本，只把这个 bin\ 目录加进用户 PATH。Windows 非管理员用不了软链，
rem 硬链又会因 _internal\ 必须与 exe 同级而失效，所以转发脚本是唯一可行的做法。
rem 只把 bin\ 而不是版本目录加进 PATH：PATH 一辈子只写一次（升级只重写转发脚本），
rem 且不会把满是 DLL 的目录塞进所有子进程的 DLL 搜索路径。
rem
rem 为什么用 .bat 而不是 .ps1 当入口：.ps1 受默认 Restricted 执行策略约束，且从 zip 解压
rem 出来的脚本带 MOTW 标记，双击跑不起来。本文件只在少数几处内联调 PowerShell，而
rem powershell -Command 不受执行策略约束 —— 执行策略只管脚本文件，不管命令行传入的代码。
rem
rem 用户 PATH 一律用 reg.exe 读写，绝不用 setx：setx 超过 1024 字符会静默截断，且
rem setx PATH "%PATH%;..." 里的 %PATH% 是 Machine+User 合并值，写进 User 作用域会把整个
rem 系统 PATH 永久复制进用户 PATH。reg query 返回 REG_EXPAND_SZ 的未展开原值，
rem reg add /t 保留原类型，两个坑都能避开。
rem
rem 编写约束（改本文件前必读）：
rem   - 必须存为 UTF-8 无 BOM + CRLF。首行带 BOM 会让 cmd 报「不是内部或外部命令」。
rem   - `if COND a & b` 里的 b 会无条件执行，多条命令一律写成 `if COND ( a & b )`。
rem   - 括号块在进入时整体展开变量，块内 set 的值块内读不到，需要就拆成子例程。
rem   两条都由 tests/packaging 守着一部分，但静态检查覆盖不全，改完请在 Windows 上实跑。

setlocal

set "REPO=dingdalong/agent"
set "SHIM_MARK=agent installer shim"
set "PLATFORM="
set "INSTALL_ROOT=%LOCALAPPDATA%\Programs\agent"
set "BIN_DIR=%INSTALL_ROOT%\bin"
set "SHIM=%BIN_DIR%\agent.bat"
set "RAW_BASE=https://raw.githubusercontent.com/%REPO%/main/scripts"

set "OPT_FROM="
set "OPT_TAG="
set "OPT_FORCE="
set "OPT_KEEP_OLD="
set "OPT_VERIFY="
set "OPT_UNINSTALL="
set "OPT_SKIP_PATH="
set "OPT_NO_PAUSE="
set "WORK="

rem 中文提示在默认代码页（简体中文 936）下会乱码，先切 UTF-8，退出时还原
set "ORIG_CP="
for /f "tokens=2 delims=:" %%c in ('chcp') do set "ORIG_CP=%%c"
if defined ORIG_CP set "ORIG_CP=%ORIG_CP: =%"
chcp 65001 >nul 2>&1

call :main %*
set "EXIT_CODE=%ERRORLEVEL%"

if defined WORK rmdir /s /q "%WORK%" 2>nul
if defined ORIG_CP chcp %ORIG_CP% >nul 2>&1
rem 双击运行时窗口跑完即关，用户看不到结果。判据是父进程为 `cmd /c`，
rem 但 build_exe.py 与测试也走 cmd /c，所以它们显式传 --no-pause 兜底。
if not defined OPT_NO_PAUSE call :maybe_pause
exit /b %EXIT_CODE%

:maybe_pause
set "CMDLINE=%cmdcmdline%"
if not "%CMDLINE: /c =%"=="%CMDLINE%" pause
exit /b 0


:main
:parse_args
if "%~1"=="" goto :args_done
if /i "%~1"=="--from" goto :arg_from
if /i "%~1"=="--version" goto :arg_tag
if /i "%~1"=="--print-path-merge" goto :arg_merge
if /i "%~1"=="--force" ( set "OPT_FORCE=1" & shift & goto :parse_args )
if /i "%~1"=="--keep-old" ( set "OPT_KEEP_OLD=1" & shift & goto :parse_args )
if /i "%~1"=="--verify" ( set "OPT_VERIFY=1" & shift & goto :parse_args )
if /i "%~1"=="--uninstall" ( set "OPT_UNINSTALL=1" & shift & goto :parse_args )
if /i "%~1"=="--skip-path" ( set "OPT_SKIP_PATH=1" & shift & goto :parse_args )
if /i "%~1"=="--no-pause" ( set "OPT_NO_PAUSE=1" & shift & goto :parse_args )
if /i "%~1"=="--stage2" ( shift & goto :parse_args )
if /i "%~1"=="-h" goto :arg_help
if /i "%~1"=="--help" goto :arg_help
echo 错误：未知参数 %~1，用 --help 查看用法 1>&2
exit /b 1

:arg_from
if "%~2"=="" ( echo 错误：--from 需要一个目录参数 1>&2 & exit /b 1 )
set "OPT_FROM=%~2"
shift
shift
goto :parse_args

:arg_tag
if "%~2"=="" ( echo 错误：--version 需要一个 tag 参数 1>&2 & exit /b 1 )
set "OPT_TAG=%~2"
shift
shift
goto :parse_args

rem 干跑 PATH 合并逻辑并打印结果，供测试断言；纯查询，绝不落盘也绝不等回车。
:arg_merge
set "OPT_NO_PAUSE=1"
call :merge_path "%~2" "%~3"
set "MERGE_RC=%ERRORLEVEL%"
if defined MERGED echo %MERGED%
exit /b %MERGE_RC%

:arg_help
call :usage
exit /b 0

:args_done
if defined OPT_UNINSTALL goto :do_uninstall

call :detect_platform
if errorlevel 1 exit /b 1

call :resolve_payload
if errorlevel 1 exit /b 1

call :read_version "%PAYLOAD%"
if errorlevel 1 exit /b 1

call :check_shim
if errorlevel 1 exit /b 1

call :install_payload
if errorlevel 1 exit /b 1

call :write_shim
if errorlevel 1 exit /b 1

call :prune_old

if not defined OPT_SKIP_PATH call :add_user_path
rem 让当前窗口立刻可用；这只改本进程，不碰注册表
set "PATH=%PATH%;%BIN_DIR%"

call :verify_install
if errorlevel 1 exit /b 1

echo.
echo 安装完成：%SHIM% 转发到 %DEST%\agent.exe
echo 在任意目录执行 agent 即可启动，工作目录就是启动时所在目录。
echo 卸载："%DEST%\install.bat" --uninstall
exit /b 0


:usage
echo 用法：install.bat [选项]
echo.
echo   --from DIR      从指定的解压目录安装，跳过下载
echo   --version TAG   安装指定 release（如 v0.2.0），默认最新
echo   --force         覆盖已存在但非本安装器创建的 agent.bat
echo   --keep-old      保留旧版本目录，不自动清理
echo   --verify        安装后额外运行 --self-check
echo   --uninstall     卸载：删转发脚本、删程序目录、从用户 PATH 移除 bin 目录
echo   --skip-path     不改用户 PATH
echo   --no-pause      结束时不等待回车，供自动化调用
echo   -h, --help      显示本帮助
echo.
echo 远程安装：
echo   curl -fsSL %RAW_BASE%/install.bat -o "%%TEMP%%\agent-install.bat" ^&^& "%%TEMP%%\agent-install.bat"
exit /b 0


rem 映射表与 scripts/build_exe.py 的 platform_tag() 保持一致。
:detect_platform
set "ARCH=%PROCESSOR_ARCHITECTURE%"
if defined PROCESSOR_ARCHITEW6432 set "ARCH=%PROCESSOR_ARCHITEW6432%"
if /i "%ARCH%"=="AMD64" ( set "PLATFORM=windows-x86_64" & exit /b 0 )
if /i "%ARCH%"=="ARM64" goto :arch_arm64
echo 错误：不支持的架构 %ARCH%，32 位 Windows 没有预构建包 1>&2
exit /b 1

:arch_arm64
set "PLATFORM=windows-x86_64"
echo 提示：当前是 ARM64 Windows，将安装 x86_64 版本，由系统仿真运行。
exit /b 0


rem 确定要安装的产物目录，结果写入 PAYLOAD（已规范化为绝对路径）。
:resolve_payload
set "PAYLOAD="
if defined OPT_FROM goto :payload_from
rem 本地模式：脚本自己就躺在产物目录里
if not exist "%~dp0agent.exe" goto :fetch_remote
if not exist "%~dp0_internal\" goto :fetch_remote
for %%i in ("%~dp0.") do set "PAYLOAD=%%~fi"
call :echo_payload
exit /b 0

:payload_from
if not exist "%OPT_FROM%\agent.exe" ( echo 错误：%OPT_FROM% 不像解压后的产物目录，缺 agent.exe 1>&2 & exit /b 1 )
if not exist "%OPT_FROM%\_internal\" ( echo 错误：%OPT_FROM% 不像解压后的产物目录，缺 _internal 1>&2 & exit /b 1 )
for %%i in ("%OPT_FROM%") do set "PAYLOAD=%%~fi"
call :echo_payload
exit /b 0

:echo_payload
echo 从 %PAYLOAD% 安装
exit /b 0


rem 查 release、下载、解压，产物目录写入 PAYLOAD。
:fetch_remote
if defined AGENT_INSTALL_RELEASE_JSON goto :api_env
if defined OPT_TAG goto :api_tag
set "API=https://api.github.com/repos/%REPO%/releases/latest"
goto :api_done
:api_env
set "API=%AGENT_INSTALL_RELEASE_JSON%"
goto :api_done
:api_tag
set "API=https://api.github.com/repos/%REPO%/releases/tags/%OPT_TAG%"
:api_done
echo 目标平台：%PLATFORM%
echo 查询 release：%API%

rem bat 没有靠得住的 JSON 解析手段，这一步内联 PowerShell。
rem 取 browser_download_url 而不是自己拼 URL：能扛住 git tag 与 pyproject 版本号不一致。
set "URL="
for /f "usebackq delims=" %%u in (`powershell -NoProfile -Command "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; (Invoke-RestMethod '%API%').assets ^| Where-Object { $_.name -like '*-%PLATFORM%.zip' } ^| Select-Object -First 1 -ExpandProperty browser_download_url"`) do set "URL=%%u"
if not defined URL goto :no_asset

set "WORK=%TEMP%\agent-install-%RANDOM%%RANDOM%"
mkdir "%WORK%" 2>nul
set "ZIP=%WORK%\agent.zip"
echo 下载 %URL%
rem Win10 1803+ / Win11 的 System32 自带 curl.exe 与 tar.exe；老系统回退 PowerShell
where curl.exe >nul 2>&1 && goto :dl_curl
powershell -NoProfile -Command "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; Invoke-WebRequest '%URL%' -OutFile '%ZIP%'"
if errorlevel 1 goto :dl_failed
goto :dl_done
:dl_curl
curl -fL --progress-bar "%URL%" -o "%ZIP%"
if errorlevel 1 goto :dl_failed
:dl_done

where tar.exe >nul 2>&1 && goto :ex_tar
rem Expand-Archive 解 _internal 那几百个文件要几分钟，改用 .NET 的 ZipFile
powershell -NoProfile -Command "$ErrorActionPreference='Stop'; Add-Type -AssemblyName System.IO.Compression.FileSystem; [IO.Compression.ZipFile]::ExtractToDirectory('%ZIP%','%WORK%')"
if errorlevel 1 goto :ex_failed
goto :ex_done
:ex_tar
tar -xf "%ZIP%" -C "%WORK%"
if errorlevel 1 goto :ex_failed
:ex_done

for /d %%d in ("%WORK%\*") do call :take_payload "%%d"
if not defined PAYLOAD ( echo 错误：压缩包内未找到产物目录 1>&2 & exit /b 1 )
echo 从 %PAYLOAD% 安装
exit /b 0

:take_payload
if not exist "%~1\agent.exe" exit /b 0
if not exist "%~1\_internal\" exit /b 0
set "PAYLOAD=%~1"
exit /b 0

:no_asset
echo 错误：release 里没有 %PLATFORM% 的资产。若是 GitHub API 限流， 1>&2
echo 可下载压缩包后用 --from 解压目录 安装。 1>&2
exit /b 1

:dl_failed
echo 错误：下载失败 1>&2
exit /b 1

:ex_failed
echo 错误：解压失败 1>&2
exit /b 1


rem 版本号优先取产物里的 VERSION（由 build_exe.py 写入），用户重命名解压目录也不影响。
rem build_exe.py 以 LF 写入该文件：for /f 不剥行尾的 CR，CRLF 会让版本号带一个回车。
:read_version
set "VER="
if exist "%~1\VERSION" for /f "usebackq tokens=* delims=" %%v in ("%~1\VERSION") do set "VER=%%v"
if defined VER set "VER=%VER: =%"
if not defined VER ( echo 错误：%~1 缺少 VERSION 文件，无法确定版本号 1>&2 & exit /b 1 )
set "DEST=%INSTALL_ROOT%\%VER%"
exit /b 0


rem 落地前先查冲突：拷完约 100MB 才报错太浪费。
:check_shim
if not exist "%SHIM%" exit /b 0
findstr /c:"%SHIM_MARK%" "%SHIM%" >nul 2>&1 && exit /b 0
if defined OPT_FORCE exit /b 0
echo 错误：%SHIM% 已存在且不是本安装器创建的。 1>&2
echo 确认可以覆盖后加 --force 重试。 1>&2
exit /b 1


rem 先拷到 .新 再整目录改名换位：升级中断不会留下半写的目标目录。
rem Windows 不让删被占用的 exe，但允许重命名它所在的目录，所以运行中的实例挡不住升级。
:install_payload
if /i "%PAYLOAD%"=="%DEST%" ( echo 产物已在目标位置，跳过复制。& exit /b 0 )
if not exist "%INSTALL_ROOT%\" mkdir "%INSTALL_ROOT%" 2>nul
set "STAGING=%INSTALL_ROOT%\.%VER%.new-%RANDOM%"
set "OLD_DEST="
if exist "%DEST%\" echo 覆盖已安装的 %VER%，若有实例正在运行请重启它
robocopy "%PAYLOAD%" "%STAGING%" /e /njh /njs /ndl /nfl /np >nul
rem robocopy 退出码 0-7 都算成功，8 起才是失败
if errorlevel 8 ( echo 错误：复制产物失败 1>&2 & rmdir /s /q "%STAGING%" 2>nul & exit /b 1 )
rem 必须用 goto 跳过而不是 `if exist ... call`：robocopy 成功时也会留下非 0 的 errorlevel
rem （复制了文件就返回 1），紧跟其后的 `if errorlevel 1` 会把成功误判成失败。
if not exist "%DEST%\" goto :swap_in
call :move_aside
if errorlevel 1 exit /b 1
:swap_in
move "%STAGING%" "%DEST%" >nul
if errorlevel 1 ( echo 错误：无法把产物移入 %DEST% 1>&2 & exit /b 1 )
if defined OLD_DEST call :drop_old_dest
exit /b 0

:move_aside
set "OLD_DEST=%INSTALL_ROOT%\.%VER%.old-%RANDOM%"
move "%DEST%" "%OLD_DEST%" >nul
if errorlevel 1 ( echo 错误：无法移开已安装的 %DEST%，请关闭正在运行的 agent 后重试 1>&2 & exit /b 1 )
exit /b 0

:drop_old_dest
rmdir /s /q "%OLD_DEST%" 2>nul
if exist "%OLD_DEST%\" echo 提示：旧目录被占用，可稍后手动删除 %OLD_DEST%
exit /b 0


rem 用重定向逐行写，天然是 ASCII 无 BOM；cmd 见到 BOM 会让转发脚本首行失效。
rem %%~dp0 与 %%* 里的 %% 在批处理中输出为单个 %，写进文件的是 %~dp0 与 %*。
rem 用 %%~dp0.. 的相对路径而非绝对路径，整棵安装树搬走也照样能用。
:write_shim
if not exist "%BIN_DIR%\" mkdir "%BIN_DIR%" 2>nul
>"%SHIM%" echo @echo off
>>"%SHIM%" echo rem %SHIM_MARK%
>>"%SHIM%" echo "%%~dp0..\%VER%\agent.exe" %%*
if not exist "%SHIM%" ( echo 错误：无法写入 %SHIM% 1>&2 & exit /b 1 )
exit /b 0


rem 只删确凿属于本安装器的目录：跳过 . 开头的临时目录，且必须同时有 agent.exe 与 _internal\。
:prune_old
if defined OPT_KEEP_OLD exit /b 0
for /d %%d in ("%INSTALL_ROOT%\*") do call :prune_one "%%d"
exit /b 0

:prune_one
set "CAND=%~1"
set "CAND_NAME=%~nx1"
if /i "%CAND%"=="%DEST%" exit /b 0
if /i "%CAND%"=="%BIN_DIR%" exit /b 0
if "%CAND_NAME:~0,1%"=="." exit /b 0
if not exist "%CAND%\agent.exe" exit /b 0
if not exist "%CAND%\_internal\" exit /b 0
rmdir /s /q "%CAND%" 2>nul
if not exist "%CAND%\" echo 已清理旧版本 %CAND_NAME%
exit /b 0


rem 合并 PATH：%~1 为原始值，%~2 为待加入目录。
rem 结果写入 MERGED；已存在则 exit /b 2，超长则 exit /b 3（拒绝写入，绝不截断）。
:merge_path
set "MERGED="
set "RAW=%~1"
set "ADD=%~2"
if "%ADD%"=="" ( echo 错误：--print-path-merge 需要原始值与目录两个参数 1>&2 & exit /b 1 )
rem 两端补分号再比，避免把 C:\x\bin 误判成 C:\x\bin2 的子串；加引号保护 ^& 与 ^| 等字符
echo ";%RAW%;" | findstr /i /c:";%ADD%;" >nul && exit /b 2
if "%RAW%"=="" ( set "MERGED=%ADD%" & exit /b 0 )
set "MERGED=%RAW%;%ADD%"
if not "%MERGED:~2000%"=="" exit /b 3
exit /b 0


rem 一律用 reg.exe 读写用户 PATH，理由见文件头。
:add_user_path
call :read_user_path
call :merge_path "%RAW_PATH%" "%BIN_DIR%"
set "MERGE_RC=%ERRORLEVEL%"
if "%MERGE_RC%"=="2" ( echo %BIN_DIR% 已在用户 PATH 中，未改动。& exit /b 0 )
if "%MERGE_RC%"=="3" goto :path_too_long
if not "%MERGE_RC%"=="0" exit /b 0
reg add HKCU\Environment /v Path /t %PATH_KIND% /d "%MERGED%" /f >nul
if errorlevel 1 ( echo 警告：写入用户 PATH 失败，请手动把 %BIN_DIR% 加入 PATH。 1>&2 & exit /b 0 )
call :broadcast_env
echo 已把 %BIN_DIR% 写入用户 PATH。
echo 新开的终端可直接用 agent；当前窗口已就绪，其他已打开的终端请重开。
exit /b 0

:path_too_long
echo 警告：用户 PATH 已超过 2000 字符，拒绝写入以免损坏它。 1>&2
echo 请手动把 %BIN_DIR% 加入 PATH。 1>&2
exit /b 0

rem reg query 返回 REG_EXPAND_SZ 的未展开原值，%VAR% 引用得以保留。
:read_user_path
set "RAW_PATH="
set "PATH_KIND="
for /f "tokens=2,*" %%a in ('reg query HKCU\Environment /v Path 2^>nul ^| findstr /i /c:"REG_"') do call :take_path_row "%%a" "%%b"
if not defined PATH_KIND set "PATH_KIND=REG_EXPAND_SZ"
exit /b 0

:take_path_row
set "PATH_KIND=%~1"
set "RAW_PATH=%~2"
exit /b 0

rem 绕过 .NET 直接写注册表不会自动通知，必须显式广播 WM_SETTINGCHANGE，
rem 否则从资源管理器新开的终端仍拿着缓存的旧 PATH。
rem HWND_BROADCAST=0xffff, WM_SETTINGCHANGE=0x1A, SMTO_ABORTIFHUNG=2
:broadcast_env
powershell -NoProfile -Command "$d='[DllImport(\"user32.dll\", CharSet=CharSet.Auto)] public static extern IntPtr SendMessageTimeout(IntPtr hWnd, int Msg, IntPtr wParam, string lParam, int flags, int timeout, out IntPtr result);'; Add-Type -MemberDefinition $d -Name Native -Namespace Win32 | Out-Null; $r=[IntPtr]::Zero; [void][Win32.Native]::SendMessageTimeout([IntPtr]0xffff, 0x1A, [IntPtr]::Zero, 'Environment', 2, 5000, [ref]$r)" >nul 2>&1
exit /b 0


:verify_install
"%SHIM%" --help >nul 2>&1
if errorlevel 1 goto :verify_failed
if not defined OPT_VERIFY exit /b 0
echo 运行自检……
"%SHIM%" --self-check
if errorlevel 1 ( echo 错误：自检未通过 1>&2 & exit /b 1 )
exit /b 0

:verify_failed
echo 错误：安装完成但 %SHIM% 跑不起来。 1>&2
echo 可能是预构建包与本机架构不符，或被 Defender/SmartScreen 拦下。 1>&2
exit /b 1


rem 正在执行的 .bat 删不掉，从已安装副本发起卸载会删到自己所在的目录。
rem 先把自己拷到 %TEMP% 再转交，--stage2 只是防止无限转交的标记。
:do_uninstall
set "SELF_DIR=%~dp0"
if not "%SELF_DIR:Programs\agent=%"=="%SELF_DIR%" goto :relay_uninstall
goto :uninstall_now

:relay_uninstall
set "RELAY=%TEMP%\agent-uninstall-%RANDOM%%RANDOM%.bat"
copy /y "%~f0" "%RELAY%" >nul
if errorlevel 1 goto :uninstall_now
start "" /wait cmd /c ""%RELAY%" --uninstall --stage2 --no-pause"
del "%RELAY%" 2>nul
exit /b 0

:uninstall_now
if exist "%SHIM%" call :remove_shim
for /d %%d in ("%INSTALL_ROOT%\*") do call :uninstall_one "%%d"
call :read_user_path
if defined RAW_PATH call :strip_user_path
if exist "%INSTALL_ROOT%\" rmdir /s /q "%INSTALL_ROOT%" 2>nul
echo 卸载完成。
exit /b 0

:remove_shim
findstr /c:"%SHIM_MARK%" "%SHIM%" >nul 2>&1 || goto :foreign_shim
del "%SHIM%" 2>nul
if not exist "%SHIM%" echo 已删除 %SHIM%
exit /b 0

:foreign_shim
echo 警告：%SHIM% 不是本安装器创建的，保留不动 1>&2
exit /b 0

:uninstall_one
if /i "%~1"=="%BIN_DIR%" exit /b 0
if not exist "%~1\agent.exe" exit /b 0
if not exist "%~1\_internal\" exit /b 0
rmdir /s /q "%~1" 2>nul
if not exist "%~1\" echo 已删除 %~1
exit /b 0

rem 用 PowerShell 逐段过滤：bat 没有可靠的「从分号分隔列表里删一项」的手段。
rem 仍按原类型写回，避免把用户 PATH 里的 %VAR% 冻成字面量。
:strip_user_path
set "NEW_PATH="
for /f "usebackq delims=" %%p in (`powershell -NoProfile -Command "@('%RAW_PATH%' -split ';' ^| Where-Object { $_ -ne '' -and $_ -ne '%BIN_DIR%' }) -join ';'"`) do set "NEW_PATH=%%p"
if "%NEW_PATH%"=="%RAW_PATH%" exit /b 0
reg add HKCU\Environment /v Path /t %PATH_KIND% /d "%NEW_PATH%" /f >nul 2>&1
if errorlevel 1 ( echo 警告：从用户 PATH 移除 %BIN_DIR% 失败，请手动清理 1>&2 & exit /b 0 )
call :broadcast_env
echo 已从用户 PATH 移除 %BIN_DIR%
exit /b 0
