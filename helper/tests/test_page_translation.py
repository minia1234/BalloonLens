import json

import pytest

from balloonlens.cache import Cache
from balloonlens.translate import Translator, TranslateError


def run(tr, lines=None, **kwargs):
    results = {}
    meta = tr.translate_page(lines or [(0, 'はい'), (1, '待って')], 'ja', 'ko',
                             kwargs.pop('model', 'qwen3:8b'), 'work',
                             lambda i, t, s: results.__setitem__(i, (t, s)), **kwargs)
    return results, meta


def responder(calls):
    def chat(model, messages, **kwargs):
        data = json.loads(messages[-1]['content'])
        calls.append((data, messages))
        return json.dumps({'items': [{'id': i, 'text': '그래'} for i in data['target_ids']]}), {}
    return chat


def test_full_page_fixed_and_cache_invalidation(tmp_path):
    calls = []
    tr = Translator(Cache(tmp_path / 'c.db'), responder(calls))
    options = dict(fixed={0: '응'}, context=[{'source': '君', 'translation': '너'}],
                   instructions='반말', glossary='君=너')
    out, meta = run(tr, **options)
    assert out == {0: ('응', 'done'), 1: ('그래', 'done')}
    payload = calls[0][0]
    assert payload['page'] == [{'id': 0, 'text': 'はい'}, {'id': 1, 'text': '待って'}]
    assert payload['target_ids'] == [1]
    assert payload['fixed'] == [{'id': 0, 'text': '응'}]
    assert payload['context'] == options['context']
    assert run(tr, **options)[1]['cache_hits'] == 1
    run(tr, **{**options, 'instructions': '존댓말'})
    run(tr, **{**options, 'context': [{'source': '君', 'translation': '당신'}]})
    run(tr, **{**options, 'fixed': {0: '네'}})
    assert len(calls) == 4


@pytest.mark.parametrize('answer', [[], [{'id': 0, 'text': '그래'}, {'id': 0, 'text': '네'}],
                                    [{'id': 0, 'text': 'Hello'}], [{'id': 0, 'text': '가자'}]])
def test_retry_retains_context_and_quality_not_cached(tmp_path, answer):
    calls = []
    def chat(model, messages, **kwargs):
        calls.append(json.loads(messages[-1]['content']))
        return json.dumps({'items': answer}), {}
    tr = Translator(Cache(tmp_path / 'c.db'), chat)
    options = dict(lines=[(0, '12時'), (1, 'はい')], fixed={1: '응'}, instructions='짧게')
    out, meta = run(tr, **options)
    assert len(calls) == 2
    assert calls[0]['page'] == calls[1]['page']
    assert calls[0]['fixed'] == calls[1]['fixed']
    assert calls[1]['retry_reasons']
    assert meta['quality_errors'] and not meta['network_errors']
    assert out[0][1] == 'review'
    assert run(tr, **options)[1]['cache_hits'] == 0


def test_partial_cache_is_reference_and_batch_is_bounded(tmp_path):
    calls = []
    cache = Cache(tmp_path / 'c.db')
    tr = Translator(cache, responder(calls))
    lines = [(i, 'はい') for i in range(8)]
    run(tr, lines=lines)
    assert [len(c[0]['target_ids']) for c in calls] == [6, 2]
    assert len(calls[1][0]['page']) == 8
    assert 'previous_translations' not in calls[1][0]
    with cache._lock:
        cache._db.execute('DELETE FROM tr WHERE rowid = (SELECT max(rowid) FROM tr)')
        cache._db.commit()
    calls.clear()
    assert run(tr, lines=lines)[1]['cache_hits'] == 7
    assert len(calls[0][0]['page']) == 8
    assert 'previous_translations' not in calls[0][0]


def test_cancel_prevents_retry_and_callback(tmp_path):
    calls = []
    def chat(*args, **kwargs):
        calls.append(True)
        return '{"items": []}', {}
    tr = Translator(Cache(tmp_path / 'c.db'), chat)
    out, meta = run(tr, should_cancel=lambda: bool(calls))
    assert len(calls) == 1 and out == {}
    assert meta['cancelled']


def test_transport_error_is_separate(tmp_path):
    def chat(*args, **kwargs):
        raise TranslateError('offline')
    out, meta = run(Translator(Cache(tmp_path / 'c.db'), chat))
    assert meta['network_errors'] and not meta['quality_errors']
    assert all(status == 'failed' for _, status in out.values())


def test_gemma_official_prompt_and_vision_qwen_only(tmp_path):
    calls = []
    def chat(model, messages, **kwargs):
        calls.append((messages, kwargs))
        return '그래', {}
    tr = Translator(Cache(tmp_path / 'g.db'), chat)
    out, meta = run(tr, model='translategemma:4b', image='abc', instructions='반말')
    assert out[0] == ('그래', 'done')
    assert not calls[0][1] and 'Please translate' in calls[0][0][0]['content']
    assert meta['quality_note']
    calls = []
    tr = Translator(Cache(tmp_path / 'q.db'), responder(calls))
    run(tr, model='qwen3.5:4b', image='abc')
    assert calls[0][1][-1]['images'] == ['abc']


def test_retry_network_failure_does_not_disable_other_line_retry(tmp_path):
    calls = []
    def chat(model, messages, **kwargs):
        data = json.loads(messages[-1]['content'])
        calls.append(data['target_ids'])
        if len(calls) == 1:
            return '{"items": []}', {}
        if data['target_ids'] == [0]:
            raise TranslateError('offline')
        return '{"items": [{"id": 1, "text": "기다려"}]}', {}
    out, meta = run(Translator(Cache(tmp_path / 'c.db'), chat))
    assert out == {0: (None, 'failed'), 1: ('기다려', 'done')}
    assert calls == [[0, 1], [0], [1]]


def test_uncertain_reply_retried_then_not_cached_and_context_bounded(tmp_path):
    calls = []
    def chat(model, messages, **kwargs):
        data = json.loads(messages[-1]['content'])
        calls.append(data)
        return json.dumps({'items': [{'id': i, 'text': '그래', 'review': True}
                                     for i in data['target_ids']]}), {}
    tr = Translator(Cache(tmp_path / 'c.db'), chat)
    out, meta = run(tr, context=[{'source': 'a' * 2000, 'translation': 'b' * 2000}] * 20)
    assert all(status == 'review' for _, status in out.values())
    assert 0 < len(calls[0]['context']) < 12
    assert len(calls[0]['context'][0]['source']) == 1000
    assert all('model_uncertain' in e['reasons'] for e in meta['quality_errors'])


def test_retry_recovers_numeric_output_and_caches(tmp_path):
    calls = []
    def chat(model, messages, **kwargs):
        calls.append(messages)
        text = '시야' if len(calls) == 1 else '12시야'
        return json.dumps({'items': [{'id': 0, 'text': text}]}), {}
    tr = Translator(Cache(tmp_path / 'c.db'), chat)
    out, meta = run(tr, lines=[(0, '１２時')])
    assert out == {0: ('12시야', 'done')} and not meta['quality_errors']
    assert run(tr, lines=[(0, '１２時')])[1]['cache_hits'] == 1


def test_oversized_page_is_review_without_inference_or_cache(tmp_path):
    calls = []
    tr = Translator(Cache(tmp_path / 'c.db'), responder(calls))
    for _ in range(2):
        out, meta = run(tr, lines=[(0, 'あ' * 4000), (1, 'はい')], fixed={1: '응'})
        assert out == {0: (None, 'review'), 1: ('응', 'done')}
        assert meta['quality_errors'] == [{'id': 0, 'reasons': ['context_limit']}]
        assert meta['calls'] == meta['cache_hits'] == 0
    assert not calls


def test_page_transport_uses_8192_context_without_live_call(tmp_path, monkeypatch):
    import balloonlens.translate as module
    bodies = []
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self):
            return json.dumps({'message': {'content': '{"items":[{"id":0,"text":"그래"}]}'}}).encode()
    def urlopen(request, **kwargs):
        bodies.append(json.loads(request.data))
        return Response()
    monkeypatch.setattr(module.urllib.request, 'urlopen', urlopen)
    run(Translator(Cache(tmp_path / 'c.db')), lines=[(0, 'はい')])
    assert bodies[0]['options']['num_ctx'] == 8192


def test_changing_generated_output_does_not_change_inference_reference(tmp_path):
    calls = []
    cache = Cache(tmp_path / 'c.db')
    tr = Translator(cache, responder(calls))
    lines = [(i, 'はい') for i in range(8)]
    run(tr, lines=lines)
    first = calls[-1][0]
    with cache._lock:
        cache._db.execute('UPDATE tr SET text = ?', ('다른 번역',))
        cache._db.execute('DELETE FROM tr WHERE rowid >= 7')
        cache._db.commit()
    calls.clear()
    run(tr, lines=lines)
    assert first == calls[-1][0]


@pytest.mark.parametrize('source,translation', [('はい', '12명이야'), ('12人', '12명과 99명이야')])
def test_added_numbers_are_review_and_not_cached(tmp_path, source, translation):
    def chat(*args, **kwargs):
        return json.dumps({'items': [{'id': 0, 'text': translation}]}), {}
    tr = Translator(Cache(tmp_path / 'c.db'), chat)
    out, meta = run(tr, lines=[(0, source)])
    assert out[0] == (translation, 'review')
    assert 'number_addition' in meta['quality_errors'][0]['reasons']
    assert run(tr, lines=[(0, source)])[1]['cache_hits'] == 0


def test_empty_retry_retains_first_candidate_for_review(tmp_path):
    calls = []
    def chat(*args, **kwargs):
        calls.append(True)
        return ('{"items":[{"id":0,"text":"시야"}]}' if len(calls) == 1 else '{"items":[]}'), {}
    tr = Translator(Cache(tmp_path / 'c.db'), chat)
    out, meta = run(tr, lines=[(0, '12時')])
    assert out[0] == ('시야', 'review')
    assert 'number_omission' in meta['quality_errors'][0]['reasons']
    assert 'missing_or_empty' in meta['quality_errors'][0]['reasons']
    assert run(tr, lines=[(0, '12時')])[1]['cache_hits'] == 0


@pytest.mark.parametrize('source,target', [('三時に会おう','3시에 만나자'),
    ('MEET ME AT THREE.','3시에 만나자.'), ('十二人','12명'), ('もう一度','한 번 더')])
def test_written_numbers_can_be_localized_without_false_alarm(source, target):
    from balloonlens.translate import _page_quality
    assert not [r for r in _page_quality(source, target, 'ko') if r.startswith('number_')]


def test_style_instructions_have_system_priority(tmp_path):
    calls=[]
    run(Translator(Cache(tmp_path/'c.db'),responder(calls)), instructions='친구끼리는 반말')
    assert '친구끼리는 반말' in calls[0][1][0]['content']
    assert '제안' in calls[0][1][0]['content']


def test_vision_translation_is_proofread_against_source_before_acceptance(tmp_path):
    calls=[]
    def chat(model,messages,**kwargs):
        data=json.loads(messages[-1]['content']); calls.append(data)
        text='역에서 만나라.' if len(calls)==1 else '역에서 만나자.'
        return json.dumps({'items':[{'id':0,'text':text}]}),{}
    tr=Translator(Cache(tmp_path/'c.db'),chat)
    out,meta=run(tr,lines=[(0,'駅で会おう')],model='qwen3.5:9b')
    assert out[0] == ('역에서 만나자.','done')
    assert calls[1]['drafts'] == [{'id':0,'text':'역에서 만나라.'}]
    assert meta['proofread_calls']==1
