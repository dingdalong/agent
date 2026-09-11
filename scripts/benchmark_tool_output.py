"""固定输出的离线对照；不调用模型，不把近似 token 当成 API 费用。

用法：uv run python scripts/benchmark_tool_output.py --baseline-ref <旧提交>
开发中 --baseline-ref : 可读取旧实现的 Git 暂存快照。快照必须包含旧分页接口。
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time
from types import ModuleType, SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.mgr.tool_output import ToolOutput
from src.tools.display import FileContent, ToolResult


MARKER = 'EVIDENCE_TARGET_314159'


def load_baseline(ref):
    modules = []
    for name, path in [('display', 'src/tools/display.py'), ('output', 'src/mgr/tool_output.py')]:
        spec = f':{path}' if ref == ':' else f'{ref}:{path}'
        code = subprocess.check_output(['git', 'show', spec], cwd=ROOT, text=True)
        module = ModuleType(f'_benchmark_legacy_{name}')
        sys.modules[module.__name__] = module
        exec(compile(code, spec, 'exec'), module.__dict__)
        modules.append(module)
    display, output = modules
    output.ToolResult = display.ToolResult
    if not hasattr(output.ToolOutput, 'limit') or not hasattr(output.ToolOutput, 'read'):
        raise ValueError('baseline-ref 必须指向包含 limit/read 分页接口的旧实现')
    return output.ToolOutput, display.ToolResult


@dataclass
class Measurements:
    calls: int = 0
    returned_bytes: int = 0
    first_bytes: int = 0

    def consume(self, result):
        size = len(str(result).encode())
        self.calls += 1
        self.returned_bytes += size
        if self.calls == 1:
            self.first_bytes = size
        return MARKER in result.text


def legacy_probe(text, kind, agent, output_class, result_class):
    output = output_class()
    stats = Measurements()
    lines = text.splitlines(keepends=True)
    chunks = [''.join(lines[i:i + 200]) for i in range(0, len(lines), 200)] if kind == 'file' else [text]
    for chunk in chunks:
        result = output.limit(result_class(chunk), agent, 4000, tail=kind != 'file')
        for _ in range(1000):
            if stats.consume(result):
                return stats
            if not result.truncated or result.next_offset is None:
                break
            result = output.limit(output.read(result.result_id, result.next_offset, agent), agent, 4000)
        else:
            raise AssertionError('旧机制续读未收敛')
    raise AssertionError('旧机制未找到目标')


def current_probe(text, kind, agent):
    output = ToolOutput()
    stats = Measurements()
    try:
        if kind == 'file':
            lines = text.splitlines(keepends=True)
            cursor = {'offset': 1, 'column': 0}
            for _ in range(1000):
                source = FileContent('/benchmark/sample.py', lines[cursor['offset'] - 1:], len(lines), **cursor)
                result = output.finalize(ToolResult('', file_content=source), agent)
                if stats.consume(result):
                    return stats
                cursor = result.next_read
                if cursor is None:
                    break
        else:
            result = output.finalize(ToolResult(text), agent)
            if stats.consume(result):
                return stats
            # 模拟一次 rg 定向查日志；不再把中间日志逐页送给模型。
            log = Path(result.artifact_path).read_text()
            matches = '\n'.join(line for line in log.splitlines() if MARKER in line)
            if stats.consume(output.finalize(ToolResult(matches), agent)):
                return stats
        raise AssertionError('新机制未找到目标')
    finally:
        output.clear()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-ref', required=True)
    args = parser.parse_args()
    import tiktoken
    cache = ROOT / 'build/tiktoken_cache'
    if cache.is_dir():
        os.environ.setdefault('TIKTOKEN_CACHE_DIR', str(cache))
    encoder = tiktoken.get_encoding('o200k_base')
    agent = SimpleNamespace(uuid='benchmark', deps=SimpleNamespace(session_id='benchmark'),
                            llm=SimpleNamespace(estimate_tokens=lambda messages: sum(len(encoder.encode(m['content'])) for m in messages)))
    old_output, old_result = load_baseline(args.baseline_ref)
    rows = []
    for kind, count in [('file', 400), ('search', 1600), ('log', 3200)]:
        lines = [f'{i:04d} sample_module_{i % 40}: verify request lifecycle and output boundaries 中文说明\n' for i in range(count)]
        lines[count * 3 // 5] += MARKER + '\n'
        text = ''.join(lines)
        for version, probe in [('legacy_4000', lambda: legacy_probe(text, kind, agent, old_output, old_result)),
                               ('current_10000', lambda: current_probe(text, kind, agent))]:
            durations = []
            for _ in range(3):
                started = time.perf_counter()
                stats = probe()
                durations.append((time.perf_counter() - started) * 1000)
            rows.append({'case': kind, 'strategy': version, 'calls_to_evidence': stats.calls,
                         'first_returned_bytes': stats.first_bytes, 'total_returned_bytes': stats.returned_bytes,
                         'median_ms': round(statistics.median(durations), 3)})
    print(json.dumps({'note': '固定目标定位流程：旧文件每次 200 行并顺序续读；新文件按预算读取，日志定向搜索。计时含输出整理和新日志 I/O，不含模型、真实工具执行、网络及历史输入费用。旧预算用 o200k_base 正文估算，新预算用 4 字节/token，不能比较为计费 token。', 'results': rows}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
