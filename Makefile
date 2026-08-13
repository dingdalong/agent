.DEFAULT_GOAL := build

# PyInstaller 不能交叉编译：本目标只产出当前平台的包，产物名自带 {os}-{arch}。
# 三平台发布由 .github/workflows/release.yml 在各自 runner 上跑同一个脚本完成。
build:
	uv run python scripts/build_exe.py

# 复用已预热的 tiktoken 缓存，省掉一次下载；改依赖或换 tiktoken 版本后请用 build。
rebuild:
	uv run python scripts/build_exe.py --skip-warm

# 对当前平台的构建产物跑冻结态自检
check:
	uv run pytest tests/packaging -q

# 构建并用产物内的安装脚本装到 ~/.local/bin，等价于用户解压后执行包内 install.sh
install:
	uv run python scripts/build_exe.py --install

clean:
	rm -rf dist/ build/ src/*.egg-info

.PHONY: build rebuild check install clean
