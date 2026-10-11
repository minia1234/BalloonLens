"""Actual extension/UI tests with deterministic helper replies; no model calls."""
import json
import shutil
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright
from e2e_extension import Handler, EXT_ID, ROOT


def main():
    out=ROOT/'bench_out'/'convenience'/str(time.time_ns())
    out.mkdir(parents=True)
    ext=out/'extension'
    shutil.copytree(ROOT/'extension',ext)
    manifest=json.loads((ext/'manifest.json').read_text(encoding='utf-8'))
    manifest['host_permissions'].append('<all_urls>')
    (ext/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
    server=ThreadingHTTPServer(('127.0.0.1',8799),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    errors=[]
    try:
        with sync_playwright() as p:
            exe=None
            if not Path(p.chromium.executable_path).exists():
                candidates=sorted(Path(p.chromium.executable_path).parents[2].glob('chromium-*/chrome-win64/chrome.exe'))
                exe=str(candidates[-1]) if candidates else None
            ctx=p.chromium.launch_persistent_context(str(out/'profile'),headless=True,executable_path=exe,
                viewport={'width':1280,'height':900},args=[f'--disable-extensions-except={ext}',f'--load-extension={ext}'])
            sw=ctx.service_workers[0] if ctx.service_workers else ctx.wait_for_event('serviceworker')
            sw.evaluate('''() => {
              globalThis.testCalls=[];
              globalThis.testJobs={};
              const ready={ok:true,version:'0.2.0',ocr_ready:true,ollama:true,models:[
                {name:'qwen3.5:9b',installed:true},{name:'qwen3:8b',installed:true},{name:'translategemma:4b',installed:true}]};
              ensureHelper=async()=>ready;
              handlers.setup=async()=>({helper:ready,native:true,permission:true});
              handlers.status=async()=>ready;
              handlers.translateImage=async()=>{
                const jobId=crypto.randomUUID();
                testJobs[jobId]={id:jobId,status:'partial',src:'ja',tgt:'ko',width:1200,height:1700,
                  image_hash:'a'.repeat(64),segments:[
                    {id:0,bbox:[867,236,79,224],src:'first',tgt:'직접 고친 대사',status:'done',edited:true,presentation:{frame:[650,100,450,700]}},
                    {id:1,bbox:[280,250,80,200],src:'retry this',tgt:null,status:'failed',edited:false},
                    {id:2,bbox:[840,1100,110,200],src:'review this',tgt:'확인할 번역',status:'review',edited:false},
                    {id:3,bbox:[290,1200,100,200],src:'last',tgt:'잘 된 번역',status:'done',edited:false}]};
                return {jobId,fetchMs:0};
              };
              handlers.getJob=async({jobId})=>testJobs[jobId];
              handlers.translateLine=async({text})=>{testCalls.push(text); await new Promise(r=>setTimeout(r,200)); return {text:'복구된 번역',status:'done'};};
              globalThis.testSaved=[];
              handlers.getWork=async({scope})=>({work:{title:'작품 기억 '+scope.split('/').pop(),summary:'',characters:''},pages:{count:3,last_seen:null}});
              handlers.saveWork=async({scope,work})=>{testSaved.push({scope,work}); return {ok:true};};
              handlers.cancelSession=async()=>({cancelled:0});
              handlers.cancelJob=async()=>({ok:true});
              handlers.setPriority=async()=>({ok:true});
            }''')
            # v0.5.0 installs start on the Claude subscription; this test covers the local path with a ready helper.
            sw.evaluate("async()=> chrome.storage.local.set({model:'qwen3.5:9b'})")
            page=ctx.new_page()
            page.on('pageerror',lambda e:errors.append(str(e)))
            page.goto('http://127.0.0.1:8799/tools/e2e_page.html')
            page.evaluate("document.querySelectorAll('img').forEach((img,i)=>{if(i)img.remove()})")
            tab_id=sw.evaluate("async()=> (await chrome.tabs.query({url:'http://127.0.0.1:8799/*'}))[0].id")
            popup=ctx.new_page()
            popup.on('pageerror',lambda e:errors.append(str(e)))
            popup.goto(f'chrome-extension://{EXT_ID}/popup.html?tabId={tab_id}')
            popup.wait_for_selector('#conn.ok')
            popup.click('#toggle')
            def state():
                return sw.evaluate('(tabId)=>handlers.tabState({tabId})',tab_id)
            def wait_for(check):
                end=time.monotonic()+15
                while time.monotonic()<end:
                    value=state()
                    if value and check(value): return value
                    time.sleep(.1)
                raise AssertionError(state())
            initial=wait_for(lambda s:s['failed']==1 and s['edited']==1)
            popup.click('#fontUp')
            popup.wait_for_function("document.querySelector('#fontReset').textContent==='110%'", polling=100)
            assert sw.evaluate("async()=> (await chrome.storage.local.get('fontScale')).fontScale")==1.1
            popup.click('#fontReset')
            time.sleep(.2)
            assert sw.evaluate("async()=> (await chrome.storage.local.get('fontScale')).fontScale")==1
            popup.click('#nextIssue')
            time.sleep(.3)
            cdp=ctx.new_cdp_session(page); cdp.send('DOM.enable')
            nodes=cdp.send('DOM.getFlattenedDocument',{'depth':-1,'pierce':True})['nodes']
            areas=[]
            for node in nodes:
                attrs=dict(zip(node.get('attributes',[])[::2],node.get('attributes',[])[1::2]))
                if 'seg' not in attrs.get('class','').split(): continue
                obj=cdp.send('DOM.resolveNode',{'backendNodeId':node['backendNodeId']})['object']
                result=cdp.send('Runtime.callFunctionOn',{'objectId':obj['objectId'],'returnByValue':True,
                    'functionDeclaration':'''function(){return {width:parseFloat(this.style.width)/100*1200,
                    height:parseFloat(this.style.height)/100*1700,font:parseFloat(getComputedStyle(this).fontSize)};}'''})['result']['value']
                areas.append(result)
                cdp.send('Runtime.releaseObject',{'objectId':obj['objectId']})
            assert areas and areas[0]['width']*areas[0]['height']<=79*224*1.51, areas
            assert areas[1]['width']<=86.01, areas
            (out/'source-fit.json').write_text(json.dumps(areas,indent=2),encoding='utf-8')
            def popover_exists():
                nodes=cdp.send('DOM.getFlattenedDocument',{'depth':-1,'pierce':True})['nodes']
                return any('popover' in dict(zip(n.get('attributes',[])[::2],n.get('attributes',[])[1::2])).get('class','').split() for n in nodes)
            assert popover_exists(), 'next issue did not open its editor'
            popup.click('#retryFailed')
            final=wait_for(lambda s:not s['retrying'] and s['failed']==0)
            assert final['edited']==initial['edited']==1 and final['review']==1
            assert sw.evaluate('testCalls')==['retry this'], 'successful/user-edited/review text was retried'
            popup.reload(); popup.wait_for_selector('#conn.ok')
            popup.screenshot(path=str(out/'popup.png'),full_page=True)
            page.screenshot(path=str(out/'next-issue.png'))

            # ----- dialogue list panel -----
            def shadow_find(match):
                found=[]
                for n in cdp.send('DOM.getFlattenedDocument',{'depth':-1,'pierce':True})['nodes']:
                    attrs=dict(zip(n.get('attributes',[])[::2],n.get('attributes',[])[1::2]))
                    if match(attrs): found.append(n['backendNodeId'])
                return found
            def node_call(backend,fn):
                obj=cdp.send('DOM.resolveNode',{'backendNodeId':backend})['object']
                value=cdp.send('Runtime.callFunctionOn',{'objectId':obj['objectId'],'returnByValue':True,'functionDeclaration':fn})['result'].get('value')
                cdp.send('Runtime.releaseObject',{'objectId':obj['objectId']})
                return value
            def real_click(match,label):
                nodes=shadow_find(match); assert nodes, label
                x,y,w,h=node_call(nodes[0],'function(){const r=this.getBoundingClientRect();return [r.left,r.top,r.width,r.height];}')
                page.mouse.click(x+w/2,y+h/2)
            def items():
                return [node_call(n,'function(){return this.querySelector(".tgt").textContent;}')
                        for n in shadow_find(lambda a:'script-item' in a.get('class','').split())]
            ctx.grant_permissions(['clipboard-read','clipboard-write'],origin='http://127.0.0.1:8799')
            page.keyboard.press('Escape')
            popup.click('#scriptList')
            page.bring_to_front()
            time.sleep(.4)
            listed=items()
            assert listed==['직접 고친 대사','복구된 번역','확인할 번역','잘 된 번역'], listed
            real_click(lambda a:a.get('data-filter')=='issues','issues chip')
            time.sleep(.2)
            assert '확인할 번역' in items() and '잘 된 번역' not in items(), items()
            real_click(lambda a:a.get('data-filter')=='all','all chip')
            real_click(lambda a:a.get('data-ref')=='query','search box')
            page.keyboard.insert_text('last')
            time.sleep(.2)
            assert items()==['잘 된 번역'], items()
            real_click(lambda a:a.get('data-act')=='copyTgt','copy button')
            time.sleep(.3)
            assert page.evaluate('navigator.clipboard.readText()')=='잘 된 번역'
            real_click(lambda a:'script-item' in a.get('class','').split(),'list item')
            time.sleep(.3)
            assert popover_exists(), 'clicking a line did not open its balloon'
            page.screenshot(path=str(out/'script-panel.png'))
            # The pill folds its buttons once idle, so press this one directly.
            node_call(shadow_find(lambda a:a.get('data-act')=='script')[0],'function(){this.click();}')
            time.sleep(.2)
            assert not shadow_find(lambda a:'script' in a.get('class','').split()), 'panel did not close'
            # Popup shortcut to the region editor.
            popup.click('#editRegions')
            time.sleep(.4)
            assert shadow_find(lambda a:'ed-bar' in a.get('class','').split()), 'editor did not open from popup'
            real_click(lambda a:a.get('data-act')=='cancel','editor cancel')
            time.sleep(.2)
            assert not shadow_find(lambda a:'ed-bar' in a.get('class','').split())
            options=ctx.new_page()
            options.on('pageerror',lambda e:errors.append(str(e)))
            options.goto(f'chrome-extension://{EXT_ID}/options.html')
            options.select_option('#speechPreset','casual')
            assert '반말' in options.input_value('#workInstructions')
            options.fill('#workUrl','https://reader.test/work/a')
            options.click('#saveWork')
            options.wait_for_selector('#workList option[value="https://reader.test/work/a"]',state='attached')
            options.reload()
            options.select_option('#workList','https://reader.test/work/a')
            assert '반말' in options.input_value('#workInstructions')
            options.wait_for_selector("#memoryStats:has-text('기억한 이미지 3개')")
            assert options.input_value('#workTitle')=='작품 기억 a'

            # ----- settings backup and restore -----
            sw.evaluate("()=>chrome.storage.local.set({reading:'ltr'})")
            with options.expect_download() as download:
                options.click('#exportSettings')
            backup=json.loads(Path(download.value.path()).read_text(encoding='utf-8'))
            assert backup['app']=='BalloonLens' and 'token' not in backup['settings'], backup
            assert backup['works']['https://reader.test/work/a']['title']=='작품 기억 a', backup
            sw.evaluate("()=>chrome.storage.local.set({workProfiles:[],reading:'ttb'})")
            options.on('dialog',lambda d:d.accept())
            saved_before=len(sw.evaluate('testSaved'))
            backup_file=out/'backup.json'
            backup_file.write_text(json.dumps(backup,ensure_ascii=False),encoding='utf-8')
            options.set_input_files('#importFile',str(backup_file))
            options.wait_for_selector("#backupStatus:has-text('불러왔어요')")
            restored=sw.evaluate("async()=>chrome.storage.local.get(['workProfiles','reading'])")
            assert [p['url'] for p in restored['workProfiles']]==['https://reader.test/work/a'], restored
            assert restored['reading']=='ltr', restored
            assert sw.evaluate('testSaved')[saved_before:][0]['work']['title']=='작품 기억 a'
            options.wait_for_load_state()
            time.sleep(1.5)
            options.screenshot(path=str(out/'options.png'),full_page=True)
            sw.evaluate('(tabId)=>handlers.stop({tabId})',tab_id)
            assert not state()['mounted']
            assert not errors, errors
            ctx.close()
            (out/'result.json').write_text(json.dumps({'passed':True,'errors':errors,'initial':initial,'final':final},ensure_ascii=False,indent=2),encoding='utf-8')
            print(f'Convenience UI tests passed: {out}',flush=True)
    finally:
        server.shutdown(); server.server_close()


if __name__=='__main__':
    main()
