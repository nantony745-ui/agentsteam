"""使用已有 Playwright/Chromium 与临时 SQLite 检查记忆页面，不调用云端。"""
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import urllib.request

from playwright.sync_api import sync_playwright


def main():
    root = Path(__file__).resolve().parents[1]
    directory = Path(tempfile.mkdtemp(prefix="agentsteam-memory-browser-"))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    command = ('from app.main import create_app; from pathlib import Path; import uvicorn,sys; '
               'uvicorn.run(create_app(Path(sys.argv[1])),host="127.0.0.1",port=int(sys.argv[2]),log_level="error")')
    process = subprocess.Popen([str(root / ".venv/Scripts/python.exe"), "-c", command, str(directory / "ui.db"), str(port)],
                               cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/projects", timeout=1).close()
                break
            except OSError:
                time.sleep(.1)
        with sync_playwright() as playwright:
            chromium = Path(os.environ["LOCALAPPDATA"]) / "ms-playwright/chromium-1228/chrome-win64/chrome.exe"
            browser = playwright.chromium.launch(executable_path=str(chromium), headless=True)
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{port}/#settings/memory")
            page.locator("#memory-config input").first.wait_for(state="attached")
            page.locator(".memory-workspace > details > summary").click()
            assert not page.locator("#memory-enabled").is_checked()
            def models(route):
                component = route.request.url.rsplit('/', 1)[-1]
                assert route.request.post_data_json['api_key'] == 'synthetic-key'
                if component == 'rerank':
                    route.fulfill(status=502, json={'detail': '服务未提供模型列表，可手动填写模型'})
                else:
                    route.fulfill(json={'models': [f'{component}-model-a', f'{component}-model-b']})
            page.route('**/api/settings/memory/models/*', models)
            probes = {'embedding_fail': False, 'rerank_fail': True, 'defer': False, 'pending': None}
            def probe(route):
                component = route.request.url.rsplit('/', 1)[-1]
                body = route.request.post_data_json
                assert 'llm' not in body and 'embedding' not in body and 'rerank' not in body
                if component == 'embedding':
                    if probes['defer']:
                        probes['pending'] = route
                    elif probes['embedding_fail']:
                        route.fulfill(status=504, json={'detail': '连接超时，请检查网络或稍后重试'})
                    else:
                        dimensions = body.get('embedding_dimensions') or 1024
                        route.fulfill(json={'ok': True, 'dimensions': dimensions, 'native_dimensions': 4096,
                                            'message': f'连接成功，模型默认 4096 维，采用 {dimensions} 维'})
                elif component == 'rerank' and probes['rerank_fail']:
                    route.fulfill(status=502, json={'detail': '认证失败，请检查 API Key（HTTP 401）'})
                else:
                    route.fulfill(json={'ok': True, 'message': '连接成功，已收到有效响应'})
            page.route('**/api/settings/memory/probe/*', probe)
            for component in ('llm', 'embedding', 'rerank'):
                page.locator(f'#memory-{component}-url').fill('https://models.example/v1')
                page.locator(f'#memory-{component}-key').fill('synthetic-key')
                page.locator(f'#memory-{component}-detect').click()
                if component == 'rerank':
                    page.wait_for_function("document.querySelector('#memory-rerank-models-status').textContent.includes('手动填写')")
                    page.locator('#memory-rerank-model').select_option('__manual__')
                    page.locator('#memory-rerank-custom-model').fill('manual-rerank')
                else:
                    page.locator(f'#memory-{component}-model option[value="{component}-model-b"]').wait_for(state='attached')
                    page.locator(f'#memory-{component}-model').select_option(f'{component}-model-b')
            page.wait_for_function("document.querySelector('#memory-embedding-probe-status').classList.contains('success')")
            assert page.locator('#memory-dimensions').input_value() == '1024'
            assert page.locator('#memory-dimensions').evaluate('(el) => el.readOnly')
            page.locator('#memory-llm-probe').click()
            page.wait_for_function("document.querySelector('#memory-llm-probe-status').classList.contains('success')")
            assert page.locator('#memory-llm-probe-status').evaluate('(el) => getComputedStyle(el).color') == 'rgb(22, 128, 60)'
            page.locator('#memory-rerank-probe').click()
            page.wait_for_function("document.querySelector('#memory-rerank-probe-status').classList.contains('failure')")
            assert '401' in page.locator('#memory-rerank-probe-status').inner_text()
            assert page.locator('#memory-rerank-probe-status').evaluate('(el) => getComputedStyle(el).color') == 'rgb(198, 40, 40)'
            probes['rerank_fail'] = False
            page.locator('#memory-rerank-probe').click()
            page.wait_for_function("document.querySelector('#memory-rerank-probe-status').classList.contains('success')")
            probes['embedding_fail'] = True
            page.locator('#memory-embedding-probe').click()
            page.wait_for_function("document.querySelector('#memory-embedding-probe-status').classList.contains('failure')")
            page.locator('#memory-dimensions-manual').check()
            assert not page.locator('#memory-dimensions').evaluate('(el) => el.readOnly')
            page.locator('#memory-dimensions').fill('512')
            probes['embedding_fail'] = False
            page.locator('#memory-embedding-probe').click()
            page.wait_for_function("document.querySelector('#memory-embedding-probe-status').classList.contains('success')")
            assert page.locator('#memory-dimensions').input_value() == '512'
            probes['defer'] = True
            page.locator('#memory-embedding-model').select_option('embedding-model-a')
            page.wait_for_function("document.querySelector('#memory-embedding-probe-status').classList.contains('pending')")
            assert '正在测试' in page.locator('#memory-embedding-probe-status').inner_text()
            page.locator('#memory-embedding-url').fill('https://changed.example/v1/embeddings')
            probes['pending'].fulfill(json={'ok': True, 'dimensions': 999, 'message': '连接成功'})
            page.wait_for_function("!document.querySelector('#memory-embedding-probe').disabled")
            assert page.locator('#memory-dimensions').input_value() == ''
            assert '配置已修改' in page.locator('#memory-embedding-probe-status').inner_text()
            probes['defer'] = False
            page.locator('#memory-embedding-url').fill('https://models.example/v1/embeddings')
            page.locator('#memory-embedding-model').select_option('__manual__')
            page.locator('#memory-embedding-custom-model').fill('embedding-model-b')
            page.locator('#memory-embedding-probe').click()
            page.wait_for_function("document.querySelector('#memory-embedding-probe-status').classList.contains('success')")
            assert page.evaluate("document.querySelector('#memory-llm-url').compareDocumentPosition(document.querySelector('#memory-llm-key')) & Node.DOCUMENT_POSITION_FOLLOWING")
            assert page.evaluate("document.querySelector('#memory-llm-key').compareDocumentPosition(document.querySelector('#memory-llm-model')) & Node.DOCUMENT_POSITION_FOLLOWING")
            page.locator("#memory-config button[type=submit]").click()
            page.wait_for_function("document.querySelector('#memory-runtime').textContent.includes('配置已保存')")
            assert page.locator('#memory-llm-model').input_value() == 'llm-model-b'
            assert page.locator('#memory-rerank-model').input_value() == 'manual-rerank'
            assert page.locator('#memory-llm-key').input_value() == ''
            for width in (1920, 1440, 1024, 768, 390, 320):
                page.set_viewport_size({"width": width, "height": 900})
                assert page.evaluate("document.documentElement.scrollWidth<=innerWidth"), width
                assert page.evaluate("""() => {
                    const form = document.querySelector('#memory-config').getBoundingClientRect();
                    const panel = document.querySelector('.memory-workspace > details');
                    const style = getComputedStyle(panel);
                    const available = panel.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
                    return Math.abs(form.width - available) < 2;
                }"""), width
                if width >= 1440:
                    assert page.evaluate("""() => {
                        const form = document.querySelector('#memory-config').getBoundingClientRect();
                        const cards = [...document.querySelectorAll('.memory-model-group')].map(el => el.getBoundingClientRect());
                        return cards.every(card => Math.abs(card.top - cards[0].top) < 2 && Math.abs(card.width - cards[0].width) < 2)
                            && Math.abs(cards[0].left - form.left) < 2 && Math.abs(cards[2].right - form.right) < 2;
                    }"""), width
                assert page.evaluate("""() => {
                    const box = document.querySelector('.memory-workspace').getBoundingClientRect();
                    return box.bottom <= innerHeight + 1 && box.height > 400;
                }"""), width
                page.evaluate("document.querySelector('.memory-workspace').scrollTop = 0")
                page.screenshot(path=str(directory / f"memory-{width}.png"), full_page=True)
            page.locator("#memory-query").fill("测试记忆")
            page.locator("#memory-recall").click()
            page.wait_for_function("document.querySelector('#memory-error').textContent.includes('未启用')")
            page.locator("#settings-return").click()
            page.locator(".app-shell").wait_for(state='visible')
            assert not errors, errors
            browser.close()
        print("PASS: independent probes, pending/green/red feedback, auto/manual dimensions, stale response ignored, model selection/save, three equal full-width cards, six widths, navigation, no JS errors;", directory)
    finally:
        process.terminate()
        process.wait(timeout=10)


if __name__ == "__main__":
    main()
