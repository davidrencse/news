"""Local 520,000-entry loading benchmark using temporary data and a saved HTML fixture.

Run with the project virtual environment. Requires Playwright Chromium. Source requests
are stubbed by selftest; no personal library is read or written and no crawler starts.
"""
import json
import socket
import shutil
import sys
import threading
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import selftest as T
import uvicorn
from playwright.sync_api import sync_playwright

A = T.A
A.store.stop()
rows = [dict(id=f'perf{i}', url=f'https://example.com/article-{i}', title=f'Performance article {i}',
             topic='cybersecurity', subtopic='malware', source='bulk', notes_count=0,
             added='2026-10-04', pdf=None, doc=None, author='', image=None, snippet='')
        for i in range(520_000)]
folder = Path(A.LIBRARY_DIR) / 'performance'
folder.mkdir(parents=True, exist_ok=True)
(folder/'content.html').write_text('<h1>Saved benchmark article</h1><p>Readable local content.</p>', encoding='utf-8')
rows[0]['doc'] = 'performance/content.html'
A.store.data['articles'] = rows
A.store.reindex()
A.store.version += 1
metrics = {}
for name, fn in [('first_page', A.get_library_first_page), ('cold_metadata', lambda: A.get_library_page(0, 1)),
                 ('warm_page', lambda: A.get_library_page(60, 60))]:
    start = time.perf_counter()
    fn()
    metrics[name + '_ms'] = round((time.perf_counter()-start)*1000, 2)
print(json.dumps(metrics), flush=True)
with socket.socket() as port_probe:
    port_probe.bind(('127.0.0.1', 0))
    port = port_probe.getsockname()[1]
server = uvicorn.Server(uvicorn.Config(A.app, host='127.0.0.1', port=port, lifespan='off', log_level='error'))
thread = threading.Thread(target=server.run, daemon=True)
thread.start()
deadline = time.monotonic() + 20
while not server.started and thread.is_alive() and time.monotonic() < deadline:
    time.sleep(.05)
try:
    if not server.started:
        raise RuntimeError('Benchmark server did not start')
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, **({'executable_path': T._pipeline.CHROMIUM_PATH}
                                                     if T._pipeline.CHROMIUM_PATH else {}))
        for label, viewport in [('desktop', {'width':1440,'height':900}), ('mobile', {'width':390,'height':844})]:
            ctx = browser.new_context(viewport=viewport, service_workers='block')
            page = ctx.new_page()
            A._library_page_cache = None
            start = time.perf_counter()
            page.goto(f'http://127.0.0.1:{port}/', wait_until='domcontentloaded')
            page.locator('.card-title').first.wait_for()
            metrics[label+'_cards_ms'] = round((time.perf_counter()-start)*1000,2)
            start = time.perf_counter()
            page.locator('.card-title').first.click()
            page.locator('#doc').wait_for(state='visible')
            metrics[label+'_saved_article_ms'] = round((time.perf_counter()-start)*1000,2)
            assert 'Saved benchmark article' in page.locator('#doc').inner_text()
            ctx.close()
        browser.close()
    print(json.dumps(metrics, indent=2), flush=True)
finally:
    server.should_exit = True
    thread.join(5)
    A.pipeline.close()
    shutil.rmtree(T.WORK, ignore_errors=True)
