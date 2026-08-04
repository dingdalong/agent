VERSION := $(shell python3 -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])")

build:
	uv build --wheel
	uv export --no-dev --format requirements-txt --no-emit-project -o dist/requirements.txt
	uv venv --seed dist/.venv
	dist/.venv/bin/pip download -r dist/requirements.txt -d dist/deps/
	rm -rf dist/.venv dist/requirements.txt
	tar czf dist/agent-$(VERSION)-offline.tar.gz \
	  -C dist agent-$(VERSION)-py3-none-any.whl deps/
	rm -rf dist/deps/
	@echo ""
	@echo "在线包: dist/agent-$(VERSION)-py3-none-any.whl"
	@echo "离线包: dist/agent-$(VERSION)-offline.tar.gz"

clean:
	rm -rf dist/ build/ src/*.egg-info

.PHONY: build clean
