"""一次性输出整理：近似字节预算、源文件定位与有界脱敏结果文件。"""
from collections import OrderedDict
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import tempfile
from threading import RLock
import uuid

from src.tools.display import ToolResult


_TRUNCATED = '\n[中间内容已截断]\n'


def _head_tail(text: str, budget: int) -> str:
    """字节预算内保留头尾，不截断 UTF-8 字符。"""
    data = text.encode('utf-8')
    if len(data) <= budget:
        return text
    marker = _TRUNCATED.encode()
    if budget < len(marker):
        return data[:max(0, budget)].decode('utf-8', errors='ignore')
    available = budget - len(marker)
    head = available // 2
    tail = available - head
    return data[:head].decode('utf-8', errors='ignore') + _TRUNCATED + (data[-tail:].decode('utf-8', errors='ignore') if tail else '')


class ToolOutput:
    def __init__(self, config=None):
        config = config or {}
        self.default_tokens = int(config.get('output_tokens', 10000))
        self.max_tokens = int(config.get('max_output_tokens', 16000))
        self.artifact_max_bytes = int(config.get('artifact_max_bytes', 1024 * 1024))
        self.artifact_total_bytes = int(config.get('artifact_total_bytes', 32 * 1024 * 1024))
        if not 128 <= self.default_tokens <= self.max_tokens <= 16000:
            raise ValueError('工具预算要求 128 <= output_tokens <= max_output_tokens <= 16000')
        if not 128 <= self.artifact_max_bytes <= min(self.artifact_total_bytes, 8 * 1024 * 1024):
            raise ValueError('日志要求 128 <= artifact_max_bytes <= artifact_total_bytes，单文件不能超过 8 MiB')
        self.lock = RLock()
        self._directory = None
        self._artifacts = OrderedDict()
        self._sizes = {}

    @staticmethod
    def estimate_tokens(text):
        """近似值不能当作 API usage。"""
        return (len(text.encode('utf-8')) + 3) // 4

    def budget(self, requested=None):
        if requested is None:
            return self.default_tokens
        if isinstance(requested, bool) or not isinstance(requested, int) or not 128 <= requested <= self.max_tokens:
            raise ValueError(f'max_output_tokens 应在 128～{self.max_tokens} 之间')
        return requested

    def clear(self):
        """由应用在会话切换/关闭时调用；与日志落盘共用锁。"""
        with self.lock:
            if self._directory is not None:
                self._directory.cleanup()
                self._directory = None
            self._artifacts.clear()
            self._sizes.clear()

    def _save_artifact(self, text, agent):
        owner = (str(getattr(getattr(agent, 'deps', None), 'session_id', '')), str(getattr(agent, 'uuid', '')))
        session, caller = (hashlib.sha256(value.encode()).hexdigest()[:16] for value in owner)
        data = _head_tail(text, self.artifact_max_bytes).encode('utf-8')
        with self.lock:
            if self._directory is None:
                self._directory = tempfile.TemporaryDirectory(prefix='agent-tool-results-')
            directory = Path(self._directory.name) / session / caller
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            directory.parent.chmod(0o700)
            directory.chmod(0o700)
            target = directory / f'{uuid.uuid4().hex}.log'
            fd, temporary = tempfile.mkstemp(dir=directory)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, target)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            self._artifacts[target] = (session, len(data))
            self._sizes[session] = self._sizes.get(session, 0) + len(data)
            for path, (saved_session, size) in list(self._artifacts.items()):
                if self._sizes[session] <= self.artifact_total_bytes:
                    break
                if saved_session == session:
                    path.unlink(missing_ok=True)
                    del self._artifacts[path]
                    self._sizes[session] -= size
            return str(target), len(text.encode('utf-8')) <= self.artifact_max_bytes

    def finalize(self, result, agent, requested=None, *, control=False):
        """输入必须已经脱敏；完成后事件、UI 与历史复用同一份结果。"""
        budget = (self.max_tokens if control else self.budget(requested)) * 4
        result = replace(result)
        if result.file_content is not None:
            return self._file_result(result, budget)
        if len(str(result).encode('utf-8')) <= budget:
            return result
        if control:
            return replace(ToolResult.failure('control_output_too_large', f'完整控制内容超过 {budget // 4} 近似 token，请缩短后重试'), end_turn=result.end_turn)
        text = result.text + ('\n\n[工具附加说明]\n' + result.annotations if result.annotations else '')
        result.annotations = ''
        try:
            result.artifact_path, complete = self._save_artifact(text, agent)
            result.artifact_complete = complete and not result.truncated
        except OSError as exc:
            # 保存失败不能改变原命令的真实状态，也不能触发命令重跑。
            result.artifact_error = f'日志保存失败（{type(exc).__name__}）；请定向查询，勿自动重跑有副作用的命令'
        result.truncated = True
        result.text = ''
        available = budget - len(str(result).encode('utf-8'))
        if available < 0:
            return ToolResult.failure('output_budget_too_small', '预算无法容纳结果元数据，请增大 max_output_tokens')
        result.text = _head_tail(text, available)
        return result

    @staticmethod
    def _file_result(result, budget):
        source = result.file_content
        result.file_content = None
        # Hook 的附加说明不混入源文件行号；最多使用本次预算的四分之一。
        annotation = result.annotations
        result.annotations = _head_tail(annotation, budget // 4)
        annotation_truncated = result.annotations != annotation
        start = {'offset': source.offset, 'column': source.column}

        def position(line, column):
            return {'offset': line, 'column': column}

        def set_range(end, partial):
            result.file_range = {
                'path': source.path, 'total_lines': source.total_lines,
                'start': start, 'end': end, 'eof': end['offset'] > source.total_lines,
                'line_truncated': partial,
            }
            result.next_read = None if result.file_range['eof'] else end

        # 全部可见时直接返回，包括源文件原始行结束符。
        rendered = []
        for index, line in enumerate(source.lines, source.offset):
            column = source.column if index == source.offset else 0
            prefix = f'{index}:{column} | ' if column else f'{index} | '
            rendered.append(prefix + line[column:])
        end = position(source.offset + len(source.lines), 0)
        set_range(end, False)
        result.text = ''.join(rendered)
        result.truncated = annotation_truncated
        if len(str(result).encode('utf-8')) <= budget:
            return result

        # 用最大可能游标预留元数据空间，正文只走一次线性字节裁剪。
        max_column = max((len(line) for line in source.lines), default=0)
        set_range(position(source.total_lines + 1, max_column), False)
        result.file_range['eof'] = False
        result.next_read = position(source.total_lines + 1, max_column)
        result.truncated = True
        result.text = ''
        available = budget - len(str(result).encode('utf-8'))
        chunks = []
        end = start.copy()
        partial = False
        for index, (line, rendered_line) in enumerate(zip(source.lines, rendered), source.offset):
            data = rendered_line.encode('utf-8')
            if len(data) <= available:
                chunks.append(rendered_line)
                available -= len(data)
                end = position(index + 1, 0)
                continue
            # 已有完整行就停在行边界。首行本身过长才使用行内游标。
            if not chunks:
                column = source.column
                prefix = f'{index}:{column} | ' if column else f'{index} | '
                piece = line[column:].encode('utf-8')[:max(0, available - len(prefix.encode()))].decode('utf-8', errors='ignore')
                # CRLF 视作同一个行结束符，不能在其中间停住。
                if piece.endswith('\r') and line[column + len(piece):].startswith('\n'):
                    piece = piece[:-1]
                if piece:
                    chunks.append(prefix + piece)
                    end = position(index, column + len(piece))
                    partial = True
            break
        if not chunks:
            return ToolResult.failure('output_budget_too_small', '预算无法容纳文件定位与正文，请增大 max_output_tokens')
        set_range(end, partial)
        result.text = ''.join(chunks)
        return result
