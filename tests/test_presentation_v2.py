"""Scoped deterministic UI regressions; secret strings are synthetic markers."""
import copy
import json

import pytest

from cmux_fleet.presentation import (
    CardError, MECHANICAL_KINDS, STOP_CONFIRMED, STOP_RUNNING, STOP_UNKNOWN,
    classify_routing, event_id, mask_secrets, result_card, routing_card, sanitize_text,
    timeline_line,
)

BUSINESS = '报告 /tmp/report.md，代码 print(1/2)；https://example.org/a'


def test_business_text_preserved_and_transport_fields_excluded_by_schema():
    record = dict(status='ok', worker='worker-1.1', request_id='req-synthetic-123456',
                  message_id='m-synthetic-123456', run_id='run-synthetic-123456',
                  delivery_id='d-synthetic-123456', attempt=9,
                  metadata={'transport_only': 'transport-only'}, result={'reply': BUSINESS})
    before = copy.deepcopy(record)
    card = result_card(record)
    assert card['render'] == BUSINESS
    assert record == before
    for marker in ('req-synthetic', 'm-synthetic', 'run-synthetic', 'd-synthetic', 'transport-only'):
        assert marker not in card['render']
    assert card['metadata']['request_id'] == record['request_id']
    assert card['metadata']['slot'] == 'worker-1.1'


def test_nested_business_json_named_like_transport_fields_is_preserved():
    record = {'status': 'ok', 'id': 'ui-json', 'worker': 'worker-1',
              'result': {'reply': {'request_id': 'business-1', 'attempt': 2,
                                   'metadata': {'unit': 'USD'}}}}
    before = copy.deepcopy(record)
    card = result_card(record)
    assert json.loads(card['render']) == record['result']['reply']
    assert record == before and 'ui-json' not in card['render']
    assert card['metadata']['request_id'] == 'ui-json'


@pytest.mark.parametrize('business', [None, False, 0, [],
    [{'request_id': 'business-1', 'attempt': 2, 'metadata': {'unit': 'USD'}}],
    {'metadata': {'request_id': 'business-1', 'attempt': 2}}])
def test_arbitrary_business_json_keeps_fields_values_and_types(business):
    record = {'status': 'ok', 'request_id': 'outer-request', 'result': business}
    before = copy.deepcopy(record)
    card = result_card(record)
    assert json.loads(card['render']) == business
    assert record == before and 'outer-request' not in card['render']


def test_sanitize_preserves_business_identifiers_numbers_whitespace_and_paths():
    text = BUSINESS + '\n订单 123456789012345；2026-10-11T22:55:31Z；  a  b\n' + \
           '业务 UUID 12345678-abcd-abcd-abcd-123456789012；/a/.inbox/msg-business.md'
    assert sanitize_text(text) == text


def test_terminal_controls_are_escaped_independently():
    text = '安全\x1b]0;FAKE_TITLE\x07\x1b[31m' + BUSINESS + '\r\x00\x9b\ud800'
    output = sanitize_text(text)
    assert BUSINESS in output and '\\x1b' in output and '\\x07' in output
    assert '\\x0d' in output and '\\x00' in output and '\\x9b' in output and '\\ud800' in output
    assert all(ch not in output for ch in ('\x1b', '\x07', '\r', '\x00', '\x9b', '\ud800'))
    card = result_card({'status': 'ok', 'result': {'reply': text}})
    assert '\x1b' not in card['render'] and BUSINESS in card['render']


def test_distinct_requests_have_distinct_stable_cards_and_raw_metadata():
    a = {'id': 'ui-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'status': 'ok', 'worker': 'worker-1',
         'result': {'reply': 'OK'}}
    b = dict(a, id='ui-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb')
    card_a = result_card(a, task_title='相同标题', now=1700000000)
    replay = result_card(a, task_title='改过的标题', now=1700000099)
    card_b = result_card(b, task_title='相同标题', now=1700000000)
    assert card_a['metadata']['request_id'] == a['id']
    assert card_b['metadata']['request_id'] == b['id']
    assert card_a['metadata']['event_id'] == replay['metadata']['event_id']
    assert card_a['metadata']['event_id'] != card_b['metadata']['event_id']
    assert a['id'] not in card_a['render'] and b['id'] not in card_b['render']


def test_distinct_deliveries_and_raw_event_identity_do_not_collide():
    base = dict(status='failed', slot='worker-1', request_id='req-shared',
                delivery_id='d-first', event_id='event-first')
    a, b = result_card(base), result_card(dict(base, delivery_id='d-next', event_id='event-next'))
    assert a['metadata']['event_id'] != b['metadata']['event_id']
    assert result_card(base)['metadata']['event_id'] == a['metadata']['event_id']
    assert a['metadata']['source_event_id'] == 'event-first'
    assert event_id('result', 'x'*300 + 'a') != event_id('result', 'x'*300 + 'b')


@pytest.mark.parametrize('kind', sorted(MECHANICAL_KINDS))
def test_mechanical_kinds_always_ui_only_even_with_business_hints(kind):
    card = classify_routing('请总结并判断下一步 ' + '业务' * 100,
                            request_kind=kind, reply_spec={'required': ['decision']})
    assert card['kind'] == 'routing' and card['wake_model'] is False
    assert routing_card('业务判断', kind=kind)['wake_model'] is False


@pytest.mark.parametrize('kind', ['operator_input', 'user_input', 'human_input', 'instruction',
                                  'user', 'followup', 'business', 'review', 'summary', 'decision'])
def test_explicit_human_business_followup_wakes_despite_simple_reply_spec(kind):
    card = classify_routing('请让 worker-1 回复 OK', request_kind=kind,
                            reply_spec={'required': ['reply']})
    assert card['kind'] == 'followup' and card['wake_model'] is True


def test_untyped_simple_results_and_business_followup_are_deterministic():
    for text in ('ACK', 'completed', '/tmp/report.md', '10 条', ''):
        assert classify_routing(text)['wake_model'] is False
    assert classify_routing('请评估风险')['wake_model'] is True
    assert classify_routing('答案', reply_spec={'required': ['decision']})['wake_model'] is True
    assert classify_routing('答案', reply_spec={'required': ['reply']})['wake_model'] is False
    assert classify_routing('普通文字') == classify_routing('普通文字')
    with pytest.raises(CardError):
        classify_routing('hi', request_kind={})


@pytest.mark.parametrize('status', ['failed', 'invalid'])
def test_short_fault_card_never_exposes_raw_backend_error(status):
    record = {'status': status, 'worker': 'worker-1',
              'error': 'Authorization: Bearer FAKE_TEST_SENTINEL /backend/raw/path',
              'reason': 'INTERNAL_SYNTHETIC_DETAIL'}
    before = copy.deepcopy(record)
    card = result_card(record)
    assert record == before
    for marker in ('FAKE_TEST_SENTINEL', 'Authorization', '/backend/raw/path', 'INTERNAL_SYNTHETIC_DETAIL'):
        assert marker not in card['render']
        assert marker not in str(card['metadata'])
    assert '详情见 control。' in card['render'] and STOP_UNKNOWN in card['render']
    assert not card['wake_model'] and card['metadata']['has_detail']


@pytest.mark.parametrize('header', ['Authorization: Bearer FAKE_TEST_SENTINEL',
    'authorization = Basic FAKE_TEST_SENTINEL EXTRA_SYNTHETIC',
    'Proxy-Authorization: Bearer FAKE_TEST_SENTINEL',
    'Cookie: a=FAKE_TEST_SENTINEL; b=EXTRA_SYNTHETIC',
    'Set-Cookie: a=FAKE_TEST_SENTINEL; HttpOnly',
    'error {"Authorization": "Bearer FAKE_TEST_SENTINEL"}'])
def test_known_auth_headers_redact_whole_values(header):
    output = mask_secrets(header + '\nnext line remains')
    assert 'FAKE_TEST_SENTINEL' not in output and 'EXTRA_SYNTHETIC' not in output
    assert 'Bearer' not in output and 'Basic' not in output
    assert 'next line remains' in output and '***' in output


def test_control_known_secret_fields_masked_without_destroying_counters():
    data = {'headers': {'Authorization': 'Bearer FAKE_TEST_SENTINEL',
                        'Set-Cookie': 'a=EXTRA_SYNTHETIC'},
            'auth': {'access_token': 'FAKE_TEST_SENTINEL', 'client_secret': 'EXTRA_SYNTHETIC'},
            'config.api-key': 'FAKE_TEST_SENTINEL', 'token_count': 42, 'max_tokens': 100,
            'notes': ['password="FAKE TEST SENTINEL"', 'api_key=FAKE_TEST_SENTINEL'],
            'free_text': 'unlabeled synthetic business text'}
    original = copy.deepcopy(data)
    out = mask_secrets(data)
    assert data == original
    assert 'FAKE' not in str(out) and 'EXTRA_SYNTHETIC' not in str(out)
    assert out['token_count'] == 42 and out['max_tokens'] == 100
    assert out['free_text'] == data['free_text']  # No arbitrary secret detection claim.


def test_public_skipped_result_is_sensible_ui_only():
    card = result_card({'status': 'skipped', 'worker': 'worker-1.1',
                        'id': 'ui-skipped', 'reason': 'no work required'})
    assert card['render'] == 'worker-1.1 已跳过，本次未执行。'
    assert not card['wake_model'] and card['metadata']['status'] == 'skipped'


@pytest.mark.parametrize('stopped,line', [(None, STOP_UNKNOWN), (False, STOP_RUNNING), (True, STOP_CONFIRMED)])
def test_exact_timeout_601_8_seconds_has_control_hint_and_truthful_stop_state(stopped, line):
    card = result_card({'status': 'timeout', 'worker': 'worker-1', 'elapsed_s': 601.8,
                        'id': 'ui-timeout'}, execution_stopped=stopped)
    assert 'worker-1 处理超时（约 10 分钟），未取得有效结果。' in card['render']
    assert '详情见 control。' in card['render'] and line in card['render']
    assert 'ui-timeout' not in card['render'] and not card['wake_model']
    if stopped is not True:
        assert STOP_CONFIRMED not in card['render']


def test_original_title_wrapper_removed_without_stripping_business_title():
    card = result_card({'status': 'timeout', 'worker': 'worker-1'},
                       task_title='[Operator result ui-SYNTHETIC] 报告 /tmp/report.md')
    assert card['metadata']['title'] == '报告 /tmp/report.md'
    assert 'Operator result' not in card['render'] and 'ui-SYNTHETIC' not in card['render']


def test_long_result_is_bounded_with_explicit_detail_and_control_timeline():
    card = result_card({'status': 'ok', 'result': {'reply': BUSINESS * 100}, 'id': 'ui-long'})
    assert len(card['render']) <= 900 and 'control' in card['render']
    assert card['metadata']['has_detail'] and '详情' in timeline_line(card)


def test_invalid_status_or_stop_flag_is_rejected():
    with pytest.raises(CardError):
        result_card({'status': 'unknown'})
    with pytest.raises(CardError):
        result_card({'status': 'ok', 'confirmed': False})
    with pytest.raises(CardError):
        result_card({'status': 'timeout'}, execution_stopped=1)
