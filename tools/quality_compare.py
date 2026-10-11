"""Compare legacy and context translation on identical local samples; no quality claims from status alone.

Run from root with PYTHONPATH=helper and OLLAMA_HOST set to the local server.
Reuses previously measured OCR text to isolate translation quality. Includes page image for Qwen3.5.
"""
import argparse
import base64
import io
import json
import time
from pathlib import Path

from PIL import Image
from balloonlens.cache import Cache
from balloonlens.translate import Translator, ollama_unload

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--baseline', default=str(ROOT/'bench_out/final-bench/20260913-133900/results.json'))
    parser.add_argument('--models', default='qwen3:8b,qwen3.5:9b')
    parser.add_argument('--no-image', action='store_true')
    args = parser.parse_args()
    baseline = json.loads(Path(args.baseline).read_text(encoding='utf-8'))
    out = ROOT/'bench_out'/'quality-comparison'/str(time.time_ns())
    out.mkdir(parents=True)
    tr = Translator(Cache(out/'scratch.sqlite3'))
    results = []
    print(out, flush=True)
    for model in args.models.split(','):
        for page in baseline['pages']:
            if page['file'] not in ('ja_page.png','en_page.png'):
                continue
            image = None
            if model.startswith('qwen3.5') and not args.no_image:
                im=Image.open(ROOT/'samples'/'synthetic'/page['file']).convert('RGB')
                im.thumbnail((1280,1920))
                buf=io.BytesIO(); im.save(buf,'JPEG',quality=85)
                image=base64.b64encode(buf.getvalue()).decode('ascii')
            texts={}
            started=time.monotonic()
            meta=tr.translate_page([(s['id'],s['src']) for s in page['segments']],page['src'],'ko',model,'comparison',
                lambda sid,text,status:texts.update({sid:{'text':text,'status':status}}),use_cache=False,
                instructions='친한 친구 사이의 대화. 자연스러운 한국어 반말로 번역. 원문에 없는 정보를 추가하지 마세요.',image=image)
            result={'file':page['file'],'model':model,'elapsed_s':round(time.monotonic()-started,2),
                    'meta':meta,'source':page['segments'],'baseline':page['translations'],'translation':texts}
            results.append(result)
            (out/'results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
            print(model,page['file'],result['elapsed_s'],texts,flush=True)
        ollama_unload(model)
    lines=['# Same-page translation comparison','','Status is functional validation, not semantic quality. Real comic review is still required.','']
    for result in results:
        lines += [f"## {result['file']} — {result['model']}",f"Elapsed: {result['elapsed_s']} s",'',
                  '| Source | Previous Qwen | New | State |','|---|---|---|---|']
        for seg in result['source']:
            sid=seg['id']; old=result['baseline'].get('qwen3:8b',{}).get(str(sid),{}).get('text','')
            new=result['translation'][sid]
            cells=[seg['src'],old,new['text'] or '',new['status']]
            lines.append('| '+' | '.join(v.replace('|','/').replace('\n',' ') for v in cells)+' |')
        lines += ['']
    (out/'report.md').write_text('\n'.join(lines),encoding='utf-8')


if __name__=='__main__':
    main()
