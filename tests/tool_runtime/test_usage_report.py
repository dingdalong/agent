"""离线计费边界、去重与未知 attempt 的回归。"""
import json
import pytest

from scripts.analyze_session_usage import analyze


def test_plan_boundary_dedup_unknown_and_pricing(tmp_path):
    def assistant(call, tool):
        return {'kind': 'assistant', 'timestamp': 1, 'correlation_id': call,
                'model_message': {'tool_calls': [{'function': {'name': tool}}]},
                'view': {'data': {'caller_uuid': 'agent'}}}
    state = tmp_path / 'state.json'
    state.write_text(json.dumps({'records': [
        {'kind': 'user', 'timestamp': 0}, assistant('search', 'exec_command'),
        assistant('plan', 'submit_plan'), assistant('implementation', 'apply_patch')]}))
    log = tmp_path / 'agent.log'
    def item(call, start, usage):
        return 'llm_usage_record ' + json.dumps({'call_id': call, 'attempt': 1, 'caller_uuid': 'agent',
            'started_at': start, 'completed_at': start + 1, 'usage_known': bool(usage), 'usage': usage}) + '\n'
    usage = {'input_tokens': 100, 'output_tokens': 20, 'cache_read_input_tokens': 80, 'reasoning_output_tokens': 15}
    log.write_text(item('search', 0, usage) + item('plan', 3, usage) + item('timeout', 2, {}) + item('implementation', 5, usage))
    report = analyze(state, [log, log], prices=(1, 0.1, 2))
    assert report['calls_with_usage'] == 2
    assert report['input_tokens'] == 200
    assert report['output_tokens'] == 40  # reasoning 是子集，不能再加 30。
    assert report['unknown_usage_attempts'] == 1
    assert report['cost_complete'] is False
    assert report['known_usage_cost'] == (40 + 16 + 80) / 1_000_000
    assert report['api_seconds_sum'] == 3


def test_codex_stops_after_plan_usage(tmp_path):
    path = tmp_path / 'rollout.jsonl'
    usage = {'type': 'token_usage_record', 'payload': {'usage': {'input_tokens': 10, 'cached_input_tokens': 8, 'output_tokens': 2}}}
    plan = {'type': 'response_item', 'payload': {'role': 'assistant', 'content': [{'text': '<proposed_plan>计划</proposed_plan>'}]}}
    path.write_text('\n'.join(json.dumps(x) for x in [usage, plan, usage, usage]))
    result = analyze(path)
    assert result['calls_with_usage'] == 2
    assert result['uncached_input_tokens'] == 4


def test_provider_logs_failed_attempt_as_unknown_without_request_body(caplog):
    import asyncio
    import logging
    from tests.test_llm_retry import _provider
    from src.llm.base import LLMResponse
    provider = _provider()
    provider.script = [TimeoutError('sensitive-request-body'), LLMResponse(
        content='ok', token_usage={'input_tokens': 100, 'output_tokens': 20})]
    with caplog.at_level(logging.INFO, logger='src.llm.base'):
        asyncio.run(provider.chat([{'role': 'user', 'content': 'sensitive-user-content'}], caller_uuid='agent'))
    rows = [json.loads(r.message.split('llm_usage_record ', 1)[1]) for r in caplog.records if 'llm_usage_record ' in r.message]
    assert len(rows) == 2
    assert [row['attempt'] for row in rows] == [1, 2]
    assert rows[0]['usage_known'] is False
    assert rows[1]['usage_known'] is True
    assert rows[0]['call_id'] != rows[1]['call_id']
    assert 'sensitive' not in json.dumps(rows)


def test_per_call_report_ranks_reasoning_without_exposing_content(tmp_path):
    state = tmp_path / 'state.json'
    records = [{'kind': 'user', 'timestamp': 0}]
    log_lines = []
    for i, (name, reasoning) in enumerate([('exec_command', 30), ('read_file', 90), ('submit_plan', 10)], 1):
        records.append({'kind': 'assistant', 'timestamp': i, 'correlation_id': str(i),
                        'model_message': {'content': 'private-content', 'tool_calls': [{'function': {'name': name}}]},
                        'view': {'data': {'thinking': 'private-thinking', 'caller_uuid': 'agent'}}})
        log_lines.append('llm_usage_record ' + json.dumps({'call_id': str(i), 'started_at': i,
                         'usage_known': True, 'usage': {'input_tokens': 100, 'output_tokens': 100,
                         'cache_read_input_tokens': 80, 'reasoning_output_tokens': reasoning}}))
    state.write_text(json.dumps({'records': records}))
    log = tmp_path / 'log'
    log.write_text('\n'.join(log_lines))
    report = analyze(state, [log], prices=(1, 0.1, 2))
    assert [call['record_index'] for call in report['calls']] == [1, 2, 3]
    assert [call['tool_count'] for call in report['calls']] == [1, 1, 1]
    assert report['top_reasoning_calls'][0]['record_index'] == 2
    assert report['calls'][-1]['cumulative_known_cost'] == pytest.approx(report['known_usage_cost'])
    assert 'private-' not in json.dumps(report)


def test_exit_counts_do_not_conflate_framework_and_external_errors(tmp_path):
    records = [{'kind': 'user', 'timestamp': 0}]
    for i, (name, status, metadata) in enumerate([
        ('exec_command', 'error', {'error_code': 'nonzero_exit', 'exit_code': 1}),
        ('exec_command', 'success', {'exit_code': 0, 'stage_results': [{'exit_code': 2}, {'exit_code': 0}]}),
        ('exec_command', 'error', {'error_code': 'nonzero_exit', 'exit_code': 0}),
        ('exec_command', 'error', {'error_code': 'policy_unsupported'}),
        ('mcp__vendor__query', 'error', {'error_code': 'mcp_error'}),
    ]):
        records.append({'kind': 'tool', 'timestamp': i + 1,
                        'model_message': {'tool_call_id': str(i), 'content': json.dumps(metadata) + '\nraw'},
                        'view': {'data': {'completed': {'tool_name': name, 'status': status}}}})
    path = tmp_path / 'state.json'
    path.write_text(json.dumps({'records': records}))
    report = analyze(path)
    assert report['framework_errors'] == {'policy_unsupported': 1}
    assert report['external_nonzero_exits'] == {'1': 1}
    assert report['stage_exit_codes'] == {'2': 1, '0': 1}
    assert report['service_errors'] == {'mcp_error': 1}
