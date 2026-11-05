"""Exercise browser upload/rewrite against a local, disposable editor."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from playwright.sync_api import sync_playwright

from csdn_browser import HelperError, editor_text, process_article


HTML = '''<!doctype html><meta charset="utf-8"><pre class="editor__inner" contenteditable="true"></pre>
<script>
const editor = document.querySelector('pre');
editor.addEventListener('paste', async (event) => {
    event.preventDefault();
    const file = event.clipboardData.files[0];
    if (!file) {
        editor.textContent = event.clipboardData.getData('text/plain');
        return;
    }
    const response = await fetch('/upload', {method:'POST', body:file});
    if (response.ok) {
        const body = await response.json();
        editor.textContent = '![图片](' + body.data.imageUrl + ')';
    }
});
</script>'''.encode('utf-8')


class TestServer(BaseHTTPRequestHandler):
    uploads = []
    fail = False

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(HTML)

    def do_POST(self):
        size = int(self.headers.get('Content-Length', 0))
        type(self).uploads.append(self.rfile.read(size))
        if type(self).fail:
            self.send_response(503)
            self.end_headers()
            return
        url = f'https://i-blog.csdnimg.cn/mock-{len(type(self).uploads)}.png'
        data = json.dumps({'data': {'imageUrl': url}}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(data)


class BrowserWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), TestServer)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.address = f'http://127.0.0.1:{cls.server.server_port}/'
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(channel='msedge', headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        TestServer.uploads = []
        TestServer.fail = False
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.md = self.root / '文章.md'
        (self.root / '图片 一.png').write_bytes(b'fake-image-A')
        (self.root / 'two.png').write_bytes(b'fake-image-B')
        self.md.write_text('Markdown 原文\n![一](图片 一.png)\n![二](two.png)\n', encoding='utf-8')
        self.context = self.browser.new_context()
        self.addCleanup(self.context.close)
        self.target = self.context.new_page()
        self.scratch = self.context.new_page()
        self.target.goto(self.address)
        self.scratch.goto(self.address)

    def fill_target(self, text):
        self.target.locator('pre').evaluate('(element, text) => { element.textContent = text; }', text)

    def run_process(self, notify=lambda message: None, **kwargs):
        return process_article(self.target, self.scratch, self.md, self.root / 'recovery', notify,
                               timeout_ms=700, extra_hosts={'127.0.0.1'}, **kwargs)

    def test_existing_draft_keeps_edits_and_deduplicates_images(self):
        original = '# 页面中改过的标题\n![一](图片 一.png)\n![再次引用](图片 一.png)\n![二](two.png)'
        self.fill_target(original)
        result = self.run_process()
        updated = editor_text(self.target)
        self.assertIn('# 页面中改过的标题', updated)
        self.assertEqual(updated.count('https://i-blog.csdnimg.cn/mock-1.png'), 2)
        self.assertIn('https://i-blog.csdnimg.cn/mock-2.png', updated)
        self.assertEqual(TestServer.uploads, [b'fake-image-A', b'fake-image-B'])
        self.assertEqual((result.image_count, result.reference_count, result.imported), (2, 3, False))
        self.assertEqual(result.backup_path.read_text(encoding='utf-8'), original)
        self.assertIn('Markdown 原文', self.md.read_text(encoding='utf-8'))

    def test_blank_editor_imports_original_article(self):
        result = self.run_process()
        self.assertTrue(result.imported)
        self.assertIn('Markdown 原文', editor_text(self.target))
        self.assertIn('https://i-blog.csdnimg.cn/mock-1.png', editor_text(self.target))
        self.assertIsNone(result.backup_path)

    def test_explicit_import_uses_selected_file_even_when_editor_has_placeholder(self):
        # CSDN can expose placeholder/other invisible editor text as content.
        self.fill_target('编辑器占位信息')
        result = self.run_process(mode='import')
        self.assertTrue(result.imported)
        self.assertIn('Markdown 原文', editor_text(self.target))
        self.assertIn('https://i-blog.csdnimg.cn/mock-1.png', editor_text(self.target))
        self.assertEqual(result.backup_path.read_text(encoding='utf-8'), '编辑器占位信息')

    def test_zero_width_blank_is_treated_as_empty(self):
        self.fill_target('\ufeff\u200b\n')
        result = self.run_process(mode='auto')
        self.assertTrue(result.imported)
        self.assertIn('Markdown 原文', editor_text(self.target))

    def test_repair_mode_reports_how_to_import_if_editor_has_no_images(self):
        self.fill_target('已有正文，但没有本地图片')
        with self.assertRaisesRegex(HelperError, '导入所选 Markdown'):
            self.run_process(mode='repair')
        self.assertEqual(TestServer.uploads, [])
        self.assertEqual(editor_text(self.target), '已有正文，但没有本地图片')

    def test_change_after_confirmation_is_not_overwritten(self):
        self.fill_target('已被用户修改')
        with self.assertRaisesRegex(HelperError, '处理前编辑器内容发生变化'):
            self.run_process(mode='import', expected_before='确认时的正文')
        self.assertEqual(TestServer.uploads, [])
        self.assertEqual(editor_text(self.target), '已被用户修改')

    def test_missing_image_does_not_upload_or_modify_editor(self):
        text = '不应改变\n![缺失](missing.png)'
        self.fill_target(text)
        with self.assertRaisesRegex(HelperError, '不存在'):
            self.run_process()
        self.assertEqual(editor_text(self.target), text)
        self.assertEqual(TestServer.uploads, [])

    def test_failed_upload_keeps_editor_unchanged(self):
        text = '不应改变\n![图片](two.png)'
        self.fill_target(text)
        TestServer.fail = True
        with self.assertRaisesRegex(HelperError, '上传失败'):
            self.run_process()
        self.assertEqual(editor_text(self.target), text)

    def test_concurrent_edits_are_not_overwritten(self):
        self.fill_target('旧正文\n![图片](two.png)')
        def edit_on_upload(message):
            if message.startswith('正在上传'):
                self.fill_target('用户正在写新内容')
        with self.assertRaisesRegex(HelperError, '发生变化'):
            self.run_process(edit_on_upload)
        self.assertEqual(editor_text(self.target), '用户正在写新内容')

    def test_failed_editor_write_restores_original(self):
        original = '原文\n![图片](two.png)'
        self.fill_target(original)
        with patch('csdn_browser._wait_for_text', side_effect=[False, True]):
            with self.assertRaisesRegex(HelperError, '未确认写入结果'):
                self.run_process()
        self.assertEqual(editor_text(self.target), original)
        backups = list((self.root / 'recovery').glob('*.md'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding='utf-8'), original)

    def test_same_filename_in_different_directories_is_not_confused(self):
        for folder, payload in [('one', b'first'), ('two', b'second')]:
            directory = self.root / folder
            directory.mkdir()
            (directory / 'same.png').write_bytes(payload)
        self.fill_target('![A](one/same.png)\n![B](two/same.png)')
        result = self.run_process()
        self.assertEqual(result.image_count, 2)
        self.assertEqual(TestServer.uploads, [b'first', b'second'])
        self.assertIn('![A](https://i-blog.csdnimg.cn/mock-1.png)', editor_text(self.target))
        self.assertIn('![B](https://i-blog.csdnimg.cn/mock-2.png)', editor_text(self.target))


if __name__ == '__main__':
    unittest.main()
