"""构建期预热 tiktoken BPE 缓存 — 让冻结产物首次启动无需联网。

tiktoken 在缓存未命中时会从 openaipublic.blob.core.windows.net 下载编码文件。
本脚本把运行时可能用到的编码全部下载进 TIKTOKEN_CACHE_DIR，由 agent.spec 打进
产物的 _internal/tiktoken_cache/，运行时经 src.mgr.frozen.setup_tiktoken_cache
指过去。

缓存文件按 sha1(下载地址) 命名，与 tiktoken 版本无关；但 tiktoken 会用编译进代码
的期望哈希校验内容，不匹配就删缓存重新联网。因此**必须用与打包同一个 tiktoken
版本运行本脚本**（在项目 venv 里跑即自动满足）。

用法：python scripts/warm_tiktoken_cache.py <cache_dir>
"""

import os
import sys
from pathlib import Path

# anthropic provider 固定用 cl100k_base；openai provider 先试 encoding_for_model(模型名)，
# 未收录的模型名回退 o200k_base。其余几个是 tiktoken 为已收录 OpenAI 模型名准备的，
# 一并预热以防将来在 config.yaml 里配置真实 OpenAI 模型。
ENCODINGS = (
    "o200k_base",
    "cl100k_base",
    "p50k_base",
    "p50k_edit",
    "r50k_base",
    "o200k_harmony",
)


def main() -> int:
    if len(sys.argv) != 2:
        print(f"用法：{sys.argv[0]} <cache_dir>", file=sys.stderr)
        return 2

    cache_dir = Path(sys.argv[1]).resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)
    os.environ["TIKTOKEN_CACHE_DIR"] = str(cache_dir)

    import tiktoken

    for name in ENCODINGS:
        try:
            enc = tiktoken.get_encoding(name)
        except Exception as exc:
            print(f"预热失败 {name}：{exc}", file=sys.stderr)
            return 1
        # 触发一次实际编码，确认拿到的缓存可用而不只是文件落了盘
        enc.encode("warm")
        print(f"已预热 {name}")

    files = sorted(p.name for p in cache_dir.iterdir() if p.is_file())
    print(f"缓存目录 {cache_dir} 共 {len(files)} 个文件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
