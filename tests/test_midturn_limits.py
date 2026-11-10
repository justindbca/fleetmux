"""r3 policy, bounded readers and clocks. No CLI hooks or A/B capability policy."""
import json
import pytest
from cmux_fleet import midturn, roster


def test_defaults_never_clamp_the_200k_window():
    settings, sources = midturn.resolve_settings('pi', 'deepseek', {'handoff_timeout': 900})
    assert settings == {'midturn_ratio': .8, 'midturn_retry': 300, 'midturn_max_splits': 3}
    assert sources['midturn_ratio'] == 'default'
    assert midturn.decide(159999, 200000) == 'none'
    assert midturn.decide(160000, 200000) == 'steer'


@pytest.mark.parametrize('cli,window', [('pi', 200000), ('claude', 1000000), ('codex', 400000), ('opencode', 200000)])
def test_all_clis_use_same_ratio_and_one_retry(cli, window):
    settings, _ = midturn.resolve_settings(cli, '', {})
    assert midturn.decide(int(window * .8), window, **settings) == 'steer'
    assert midturn.decide(None, window, ordered=True, elapsed=299) == 'none'
    assert midturn.decide(1, window, ordered=True, elapsed=300) == 'retry'
    assert midturn.decide(1, window, ordered=True, elapsed=300, tries=2) == 'kill'
    assert midturn.decide(window, window, ordered=True, elapsed=999, guarded=True) == 'none'


def test_exact_model_roster_overrides_then_cli_config_and_default():
    cfg = {'midturn_ratio': .9, 'handoff': {'pi': {'midturn_ratio': .85}}}
    table = {'pi:deepseek': {'midturn_ratio': .7}, 'pi': {'midturn_ratio': .75}}
    assert midturn.resolve_settings('pi', 'deepseek', cfg, table)[0]['midturn_ratio'] == .7
    assert midturn.resolve_settings('pi', 'other', cfg, table)[0]['midturn_ratio'] == .75
    assert midturn.resolve_settings('pi', 'other', cfg)[0]['midturn_ratio'] == .85
    assert midturn.resolve_settings('claude', 'opus', cfg)[0]['midturn_ratio'] == .9
    assert not roster.handoff_errors({'handoff': table})


@pytest.mark.parametrize('bad', [True, 0, 1, -1, '0.8', float('nan'), float('inf')])
def test_bad_ratio_rejected(bad):
    with pytest.raises(ValueError):
        midturn.validate_settings({'midturn_ratio': bad})
    assert roster.handoff_errors({'handoff': {'pi:model': {'midturn_ratio': bad}}})


@pytest.mark.parametrize('bad', [True, -1, '3', 1.5])
def test_bad_split_cap_rejected(bad):
    with pytest.raises(ValueError):
        midturn.validate_settings({'midturn_max_splits': bad})


def test_unknown_idle_cap_and_successor_ready():
    assert midturn.decide(None, 200000) == 'none'
    assert midturn.decide(160000, 0) == 'none'
    assert midturn.decide(160000, 200000, busy=False) == 'none'
    assert midturn.decide(160000, 200000, midturn_splits=2) == 'steer'
    assert midturn.decide(160000, 200000, midturn_splits=3) == 'blocked'
    assert midturn.decide(160000, 200000, busy=False, ready=True) == 'blocked'


def test_prompt_and_continuation_preserve_identity_and_original_text():
    prompt = midturn.MIDTURN_PROMPT.format(midturn_id='fx-' + 'a' * 16, used=160000, window=200000, path='/tmp/draft.md')
    assert len(prompt.splitlines()) == 1 and 'fleet_result/fleet_decision' in prompt
    info = dict(midturn_id='fx-' + 'a' * 16, midturn_typed_g='2', midturn_prompt=prompt)
    assert midturn.registered_prompt(info, 2) == prompt
    assert midturn.registered_prompt(info, 3) == ''
    original = 'Original "quoted" task\n\t下一行 {untouched}'
    assert midturn.continuation('worker-1.1', 'worker-1', '/note', original).endswith(original)
    assert midturn.blocked_text(3) == '这条派工一个会话装不下（已续做 3 次），请拆小'


def write_rows(path, rows):
    path.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows))


def claude_row(used, **fields):
    return dict(type='assistant', message={'usage': {'input_tokens': used}}, **fields)


def codex_row(used, window=400000):
    return {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {
        'last_token_usage': {'total_tokens': used}, 'model_context_window': window,
        'total_token_usage': {'total_tokens': 9000000}}}}


def test_claude_latest_main_thread_usage_includes_cache_and_output(tmp_path):
    path = tmp_path / 'transcript.jsonl'
    row = claude_row(100)
    row['message']['usage'].update(cache_read_input_tokens=200, cache_creation_input_tokens=30, output_tokens=4)
    write_rows(path, [claude_row(99999), row, claude_row(88888, isSidechain=True),
                      claude_row(77777, agentId='subagent'), {'type': 'user'}, [], None])
    with path.open('a') as stream:
        stream.write('{"type":"assistant","message":')  # concurrent writer not finished
    assert midturn.claude_transcript_usage(path, 1000000) == (334, 1000000)


@pytest.mark.parametrize('bad', [True, -1, '999', 1.5, None, float('nan')])
def test_bad_claude_usage_skips_row_without_erasing_previous(tmp_path, bad):
    path = tmp_path / 'claude.jsonl'
    write_rows(path, [claude_row(45), claude_row(bad), {'type': 'assistant', 'message': []}])
    assert midturn.claude_transcript_usage(path, 200000) == (45, 200000)


def test_codex_latest_context_not_cumulative_and_window_fallback(tmp_path):
    path = tmp_path / 'rollout.jsonl'
    write_rows(path, [codex_row(100000), codex_row(25000, 258400),
                      {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': None}},
                      dict(codex_row(999999), type='response_item')])
    assert midturn.codex_rollout_usage(path, 400000) == (25000, 258400)
    write_rows(path, [codex_row(0, None)])
    assert midturn.codex_rollout_usage(path, 400000) == (0, 400000)


@pytest.mark.parametrize('used,window', [(True, 400000), ('9', 400000), (-1, 400000),
                                        (1.5, 400000), (4, True), (4, '400000'), (4, 0)])
def test_invalid_codex_usage_and_window_skip_row(tmp_path, used, window):
    path = tmp_path / 'rollout.jsonl'
    write_rows(path, [codex_row(40), codex_row(used, window)])
    assert midturn.codex_rollout_usage(path, 100000) == (40, 400000)


@pytest.mark.parametrize('reader', [midturn.claude_transcript_usage, midturn.codex_rollout_usage])
def test_missing_or_invalid_measurement_is_unknown_not_file_size(tmp_path, reader):
    path = tmp_path / 'missing.jsonl'
    assert reader(path, 200000) == (None, 200000)
    path.write_text('not JSON\n' * 1000 + '[]\nnull\n')
    assert reader(path, 200000) == (None, 200000)
    assert reader(tmp_path, 200000) == (None, 200000)


@pytest.mark.parametrize('reader,row,size', [
    (midturn.claude_transcript_usage, claude_row, 65536),
    (midturn.codex_rollout_usage, codex_row, 65536),
])
def test_reader_only_reads_bounded_suffix_and_ignores_cut_first_row(tmp_path, monkeypatch, reader, row, size):
    path = tmp_path / 'large.jsonl'
    path.write_bytes((json.dumps(row(99999)) + '\n').encode() + b'x' * (size * 3)
                     + b'\n' + (json.dumps(row(42)) + '\n').encode())
    original_open = open
    reads = []

    class TailOnly:
        def __enter__(self):
            self.stream = original_open(path, 'rb')
            return self

        def __exit__(self, *args):
            self.stream.close()

        def seek(self, *args):
            return self.stream.seek(*args)

        def tell(self):
            return self.stream.tell()

        def read(self, count=-1):
            assert count == size
            assert self.stream.tell() == path.stat().st_size - size
            reads.append(count)
            return self.stream.read(count)

    monkeypatch.setattr(midturn, 'open', lambda *args: TailOnly(), raising=False)
    assert reader(path, 400000) == (42, 400000)
    assert reads == [size]


def test_effective_age_freezes_accumulates_and_resumes_without_mutation():
    request = {'created': '100', 'clock_paused_ms': '1250', 'spec': '{}'}
    assert midturn.effective_age(request, 111.25) == 10
    paused = midturn.pause_clock(request, 111.25)
    assert 'clock_paused_at' not in request
    assert midturn.pause_clock(paused, 120) == paused
    assert midturn.effective_age(paused, 120) == 10
    assert midturn.effective_age(paused, 1000) == 10
    resumed = midturn.resume_clock(paused, 121.5)
    assert resumed['clock_paused_ms'] == 11500
    assert resumed['clock_paused_at'] == ''
    assert paused['clock_paused_at'] == 111.25
    assert midturn.effective_age(resumed, 122.5) == 11
    assert midturn.resume_clock(resumed, 999) == resumed
    again = midturn.resume_clock(midturn.pause_clock(resumed, 130), 135)
    assert again['clock_paused_ms'] == 16500
    assert midturn.effective_age(again, 140) == 23.5
    assert 140 - float(request['created']) == 40  # task hard hours still wall time


def test_pause_at_zero_redis_strings_and_clock_backwards():
    request = {'created': '0', 'clock_paused_at': '0', 'clock_paused_ms': '0'}
    assert midturn.effective_age(request, 10) == 0
    assert midturn.resume_clock(request, 10)['clock_paused_ms'] == 10000
    request = dict(created=50, clock_paused_at=100, clock_paused_ms=1000)
    assert midturn.resume_clock(request, 99)['clock_paused_ms'] == 1000
    assert midturn.effective_age(request, 10) == 0


def test_message_deadline_pause_resume_and_no_double_extension():
    message = {'deadline_at': '200000', 'midturn_splits': '2', 'replay': '1'}
    paused = midturn.pause_message(message, 150000)
    assert paused['deadline_at'] == 980000  # original + (2 * 300 + 180) seconds
    assert paused['deadline_remaining_ms'] == 50000
    assert paused['clock_paused_at'] == 150
    assert midturn.pause_message(paused, 180000) == paused
    resumed = midturn.resume_message(paused, 500000)
    assert resumed['deadline_at'] == 550000
    assert resumed['clock_paused_ms'] == 350000
    assert resumed['clock_paused_at'] == ''
    assert resumed['midturn_splits'] == '2' and resumed['replay'] == '1'
    assert midturn.resume_message(resumed, 800000) == resumed
    assert message == {'deadline_at': '200000', 'midturn_splits': '2', 'replay': '1'}


@pytest.mark.parametrize('at', [200000, 200001])
def test_already_expired_message_cannot_be_paused(at):
    with pytest.raises(ValueError, match='already expired'):
        midturn.pause_message({'deadline_at': '200000'}, at)


@pytest.mark.parametrize('value', ['nan', 'inf', -1, True])
def test_invalid_clock_storage_rejected(value):
    with pytest.raises(ValueError):
        midturn.effective_age({'created': 10, 'clock_paused_ms': value}, 20)


@pytest.mark.parametrize('exit_reason', ['kill_failed', 'blocked', 'completed', 'cancelled'])
def test_every_noncontinuation_exit_can_restore_clock(exit_reason):
    request = midturn.pause_clock({'created': '100', 'status': exit_reason}, 105)
    resumed = midturn.resume_clock(request, 110)
    assert resumed['clock_paused_ms'] == 5000
    assert midturn.effective_age(resumed, 120) == 15

def pi_row(tokens=100, **message):
    return {'type': 'message', 'message': dict(role='assistant', stopReason='stop',
            usage={'totalTokens': tokens, 'input': 999}, content=[], **message)}


def test_pi_tool_result_append_without_another_assistant(tmp_path):
    path = tmp_path / 'pi.jsonl'
    write_rows(path, [pi_row(1234)])
    assert midturn.pi_session_usage(path, 200000) == (1234, 200000)
    with path.open('a') as stream:
        stream.write(json.dumps({'type': 'message', 'message': {'role': 'toolResult',
            'content': [{'type': 'text', 'text': '😀中文abc'}]}}, ensure_ascii=False) + '\n')
    # 7 UTF-16 code units: ceil(7/4), not Python's len(string).
    assert midturn.pi_session_usage(path, 200000) == (1236, 200000)


def test_pi_valid_assistant_sum_fallback_errors_and_each_message_rounding(tmp_path):
    path = tmp_path / 'pi.jsonl'
    row = pi_row(0)
    row['message']['usage'] = {'totalTokens': 0, 'input': 10, 'output': 20, 'cacheRead': 30, 'cacheWrite': 40}
    bad = pi_row(99999)
    bad['message'].update(stopReason='error', content=[{'type': 'thinking', 'thinking': '😀😀😀'},
        {'type': 'toolCall', 'name': 'read', 'arguments': {'p': '😀'}}])
    write_rows(path, [row, bad, {'type': 'message', 'message': {'role': 'user', 'content': 'x'}},
                     {'type': 'message', 'message': {'role': 'custom', 'content': [{'type': 'text', 'text': 'x'}]}}])
    assert midturn.pi_estimate(bad['message']) == 5  # 6 + 4 + 10 UTF-16 units
    assert midturn.pi_session_usage(path, 200000) == (107, 200000)


def test_pi_compaction_unknown_until_a_new_valid_assistant(tmp_path):
    path = tmp_path / 'pi.jsonl'
    rows = [pi_row(180000), {'type': 'compaction'}, pi_row(0)]
    rows[-1]['message']['usage'] = {'totalTokens': 0}
    write_rows(path, rows)
    assert midturn.pi_session_usage(path, 200000) == (None, 200000)
    write_rows(path, rows + [pi_row(500)])
    assert midturn.pi_session_usage(path, 200000) == (500, 200000)


def test_probe_stat_skip_and_generation_message_result(tmp_path, monkeypatch):
    path = tmp_path / 'pi.jsonl'
    write_rows(path, [pi_row(160000)])
    probe = midturn.Probe()
    target = dict(cli='pi', generation=2, message_id='m2', directory=str(tmp_path), window=200000)
    probe.update({'worker-1': target})
    probe.poll()
    result = probe.take()['worker-1']
    assert result['used'] == 160000 and result['generation'] == 2 and result['message_id'] == 'm2'
    monkeypatch.setattr(midturn, 'pi_session_usage', lambda *a: pytest.fail('unchanged stat read again'))
    probe.poll()
    assert probe.take() == {}


@pytest.mark.parametrize('duration,delay', [(.25, .75), (1, 0), (2.75, 0)])
def test_probe_cycle_is_max_one_second_or_scan_time(monkeypatch, duration, delay):
    probe = midturn.Probe()
    ticks = iter([10, 10 + duration])
    monkeypatch.setattr(midturn.time, 'monotonic', lambda: next(ticks))
    waits = []
    def wait(seconds):
        waits.append(seconds)
        probe.stop.set()
    monkeypatch.setattr(probe.stop, 'wait', wait)
    probe.run()
    assert waits == [delay]


def test_pi_cached_anchor_survives_beyond_tail_but_not_compaction_or_a_gap(tmp_path):
    path = tmp_path / 'pi.jsonl'
    write_rows(path, [pi_row(1000)])
    state = {}
    assert midturn.pi_session_usage(path, 200000, state)[0] == 1000
    row = {'type':'message', 'message':{'role':'toolResult', 'content':[{'type':'text', 'text':'x'*40000}]}}
    for n in range(1, 4):
        with path.open('a') as stream:
            stream.write(json.dumps(row) + '\n')
        assert midturn.pi_session_usage(path, 200000, state)[0] == 1000 + n*10000
    with path.open('a') as stream:
        stream.write(json.dumps({'type':'compaction'}) + '\n')
    assert midturn.pi_session_usage(path, 200000, state)[0] is None
    write_rows(path, [pi_row(2000)])
    state = {}
    assert midturn.pi_session_usage(path, 200000, state)[0] == 2000
    with path.open('a') as stream:
        stream.write(json.dumps(row) + '\n' + json.dumps(row) + '\n')
    assert midturn.pi_session_usage(path, 200000, state)[0] is None  # more than 64KB arrived between scans


def test_pi_partial_append_is_counted_exactly_once(tmp_path):
    path = tmp_path / 'pi.jsonl'
    write_rows(path, [pi_row(100)])
    state = {}
    assert midturn.pi_session_usage(path, 200000, state)[0] == 100
    text = json.dumps({'type':'message', 'message':{'role':'toolResult', 'content':'abcdefghi'}})
    with path.open('a') as stream:
        stream.write(text[:30])
    assert midturn.pi_session_usage(path, 200000, state)[0] == 100
    with path.open('a') as stream:
        stream.write(text[30:] + '\n')
    assert midturn.pi_session_usage(path, 200000, state)[0] == 103
    assert midturn.pi_session_usage(path, 200000, state)[0] == 103
