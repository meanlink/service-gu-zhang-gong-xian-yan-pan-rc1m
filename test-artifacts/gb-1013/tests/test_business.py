"""业务算法（窗口 Jaccard / 有向最近后继时滞）的边界与口径测试，
与额度测试共享 conftest 的环境初始化。
"""
import pytest

from src.services.business import analyze, seed_records, metadata


def _params(**over):
    base = {p['key']: p['default'] for p in metadata()['parameters']}
    base.update(over)
    return base


def test_seed_records_analyze_happy_path():
    records = seed_records()
    assert len(records) >= 30
    result = analyze(records, _params())
    assert result['metrics'][0]['value'] == len(records)
    assert result['table']['columns'] and isinstance(result['table']['rows'], list)
    # 固定种子下 cache→api 跟随链应稳定出现
    pairs = {(r['source'], r['target']) for r in result['table']['rows']}
    assert ('cache', 'api-gateway') in pairs
    row = next(r for r in result['table']['rows']
               if r['source'] == 'cache' and r['target'] == 'api-gateway')
    assert 0 <= row['jaccard'] <= 1 and 0 <= row['follow_pct'] <= 100


def test_empty_candidate_pairs_returns_empty_result():
    # 两个服务在时间上完全错开，窗口内无跟随 → 空候选但不报错
    records = [
        {'event_id': f'A{i}', 'service': 'cache', 'occurred_min': i * 100.0, 'severity': 1}
        for i in range(20)] + [
        {'event_id': f'B{i}', 'service': 'api-gateway', 'occurred_min': i * 100.0 + 50.0, 'severity': 2}
        for i in range(20)]
    result = analyze(records, _params(window_min=5, min_pairs=2))
    assert result['table']['rows'] == []


@pytest.mark.parametrize('records,message', [
    ([], '至少需要30个'),
    ([{'event_id': 'X', 'service': 'a', 'occurred_min': 1.0, 'severity': 1}] * 31, '重复'),
    ([{'event_id': str(i), 'service': '', 'occurred_min': 1.0, 'severity': 1} for i in range(31)], '不能为空'),
    ([{'event_id': str(i), 'service': 'a', 'occurred_min': -1.0, 'severity': 1} for i in range(31)], '非负有限'),
    ([{'event_id': str(i), 'service': 'a', 'occurred_min': 1.0, 'severity': 9} for i in range(31)], '等级'),
])
def test_invalid_inputs_rejected(records, message):
    # 去重/补服务后再断言对应错误信息
    if message == '重复':
        records = [dict(records[0], event_id='DUP') for _ in range(31)]
    if message == '不能为空':
        records = [dict(r, service='  ') for r in records]
    with pytest.raises(ValueError) as exc:
        analyze(records, _params())
    assert message in str(exc.value)


def test_service_count_out_of_range():
    only_one = [{'event_id': str(i), 'service': 'solo',
                 'occurred_min': float(i), 'severity': 1} for i in range(31)]
    with pytest.raises(ValueError, match='2~12个服务'):
        analyze(only_one, _params())
