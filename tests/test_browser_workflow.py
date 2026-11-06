"""Exercise browser upload/rewrite against a local, disposable editor."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import unquote
from types import SimpleNamespace

from playwright.sync_api import sync_playwright

from csdn_browser import HelperError, choose_target, editor_text, process_article
from md_images import IMAGE_PLACEHOLDER, restore_image_placeholders


HTML = r'''<!doctype html><meta charset="utf-8"><pre class="editor__inner" contenteditable="true"></pre>
<script>
const editor = document.querySelector('pre');
editor.addEventListener('paste', async (event) => {
    event.preventDefault();
    const file = event.clipboardData.files[0];
    if (!file) {
        const pasted = event.clipboardData.getData('text/plain');
        // CSDN immediately rewrites pasted local image URLs as failure images.
        const processed = pasted.replace(
            /!\[([^\]]*)\]\((?!https:\/\/(?:i-blog|img-blog)\.csdnimg\.cn\/)([^)]+)\)/g,
            '![外链图片转存失败](https://img-home.csdnimg.cn/failure.png)')
            .replace(/<img\b([^>]*?)src=(["'])(?!https:\/\/(?:i-blog|img-blog)\.csdnimg\.cn\/)([^"']+)\2([^>]*)>/gi,
                     '<img src="https://img-home.csdnimg.cn/failure.png">');
        const extra = window.growLeadingNewlines ? ++window.pasteCount :
                      (window.extraLeadingNewlines || (window.addLeadingNewline ? 1 : 0));
        editor.textContent = '\n'.repeat(extra) + processed;
        if (window.delayedLeadingNewlines) {
            const snapshot = editor.textContent;
            setTimeout(() => {
                if (editor.textContent === snapshot) editor.textContent = '\n\n' + snapshot;
            }, 150);
        }
        return;
    }
    const before = editor.textContent;
    const response = await fetch('/upload', {method:'POST', body:file,
        headers: {'X-Editor-Text': encodeURIComponent(before)}});
    if (response.ok) {
        const body = await response.json();
        // Model the real editor's potentially destructive asynchronous insert.
        setTimeout(() => { editor.textContent = '![图片](' + body.data.imageUrl + ')'; }, 60);
    }
});
</script>'''.encode('utf-8')


class TestServer(BaseHTTPRequestHandler):
    uploads = []
    editor_snapshots = []
    fail = False
    fail_indices = set()

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
        type(self).editor_snapshots.append(unquote(self.headers.get('X-Editor-Text', '')))
        if type(self).fail or len(type(self).uploads) in type(self).fail_indices:
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
        TestServer.editor_snapshots = []
        TestServer.fail = False
        TestServer.fail_indices = set()
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
        self.target.goto(self.address)

    def fill_target(self, text):
        self.target.locator('pre').evaluate('(element, text) => { element.textContent = text; }', text)

    def run_process(self, notify=lambda message: None, **kwargs):
        return process_article(self.target, self.md, self.root / 'recovery', notify,
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
        self.assertEqual(len(TestServer.editor_snapshots), 2)
        self.assertIn('Markdown 原文', TestServer.editor_snapshots[0])
        self.assertIn('Markdown 原文', TestServer.editor_snapshots[1])
        self.assertIn('https://i-blog.csdnimg.cn/mock-1.png', TestServer.editor_snapshots[1])

    def test_many_images_preserve_the_entire_text(self):
        self.target.evaluate('() => { window.growLeadingNewlines = true; window.pasteCount = 0; }')
        lines = ['# 完整正文', '', '前面的说明文字。']
        for index in range(18):
            filename = f'image-{index}.png'
            (self.root / filename).write_bytes(f'image-{index}'.encode())
            lines.extend([f'第 {index + 1} 段文字', f'![图{index}]({filename})'])
        lines.append('最后一段文字。')
        self.md.write_text('\n'.join(lines), encoding='utf-8')
        result = self.run_process(mode='import')
        text = editor_text(self.target)
        self.assertEqual((result.image_count, result.reference_count), (18, 18))
        self.assertIn('前面的说明文字。', text)
        self.assertIn('最后一段文字。', text)
        self.assertTrue(text.startswith('# 完整正文\n'))
        for index in range(18):
            self.assertIn(f'![图{index}](https://i-blog.csdnimg.cn/mock-{index + 1}.png)', text)
        self.assertTrue(all('前面的说明文字。' in item and '最后一段文字。' in item
                            for item in TestServer.editor_snapshots))

    def test_import_without_local_images_still_imports_text(self):
        self.md.write_text('# 纯文字\n这篇文章没有本地图。', encoding='utf-8')
        result = self.run_process(mode='import')
        self.assertEqual((result.image_count, result.reference_count), (0, 0))
        self.assertIn('这篇文章没有本地图。', editor_text(self.target))
        self.assertEqual(TestServer.uploads, [])

    def test_html_and_markdown_images_preserve_attributes_and_text(self):
        self.md.write_text('正文开始\n<img src="图片 一.png" alt="第一张" width="280">\n'
                           '中间文字\n![第二张](two.png)\n正文结束', encoding='utf-8')
        result = self.run_process(mode='import')
        text = editor_text(self.target)
        self.assertEqual((result.image_count, result.reference_count), (2, 2))
        self.assertIn('<img src="https://i-blog.csdnimg.cn/mock-1.png" alt="第一张" width="280">', text)
        self.assertIn('![第二张](https://i-blog.csdnimg.cn/mock-2.png)', text)
        self.assertIn('正文开始', text)
        self.assertIn('中间文字', text)
        self.assertIn('正文结束', text)

    def test_editor_extra_newline_is_removed_without_losing_blank_lines(self):
        self.md.write_text('\n\n# 正文\n\n第一段\n\n![图片](two.png)\n\n最后一段', encoding='utf-8')
        self.target.evaluate('() => { window.addLeadingNewline = true; }')
        self.run_process(mode='import')
        self.assertEqual(editor_text(self.target),
                         '\n\n# 正文\n\n第一段\n\n'
                         '![图片](https://i-blog.csdnimg.cn/mock-1.png)\n\n最后一段')

    def test_editor_preserves_intentional_leading_newlines(self):
        self.md.write_text('\n\n# 正文\n\n第一段', encoding='utf-8')
        self.run_process(mode='import')
        self.assertEqual(editor_text(self.target), '\n\n# 正文\n\n第一段')

    def test_growing_extra_newlines_do_not_stop_multiple_uploads(self):
        self.md.write_text('\n\n# 正文\n<img src="图片 一.png" />'
                           '<img src="two.png" />\n\n末尾', encoding='utf-8')
        self.target.evaluate('() => { window.growLeadingNewlines = true; window.pasteCount = 0; }')
        result = self.run_process(mode='import')
        self.assertEqual((result.image_count, result.reference_count), (2, 2))
        self.assertEqual(editor_text(self.target),
                         '\n\n# 正文\n<img src="https://i-blog.csdnimg.cn/mock-1.png" />'
                         '<img src="https://i-blog.csdnimg.cn/mock-2.png" />\n\n末尾')

    def test_delayed_extra_newlines_do_not_stop_multiple_uploads(self):
        self.md.write_text('\n\n# 正文\n![一](图片 一.png)\n![二](two.png)\n末尾', encoding='utf-8')
        self.target.evaluate('() => { window.delayedLeadingNewlines = true; }')
        result = self.run_process(mode='import')
        self.assertEqual((result.image_count, result.reference_count), (2, 2))
        self.assertEqual(editor_text(self.target),
                         '\n\n# 正文\n![一](https://i-blog.csdnimg.cn/mock-1.png)\n'
                         '![二](https://i-blog.csdnimg.cn/mock-2.png)\n末尾')

    def test_explicit_import_uses_selected_file_even_when_editor_has_placeholder(self):
        # CSDN can expose placeholder/other invisible editor text as content.
        self.fill_target('编辑器占位信息')
        result = self.run_process(mode='import')
        self.assertTrue(result.imported)
        self.assertIn('Markdown 原文', editor_text(self.target))
        self.assertIn('https://i-blog.csdnimg.cn/mock-1.png', editor_text(self.target))
        self.assertEqual(result.backup_path.read_text(encoding='utf-8'), '编辑器占位信息')

    def test_backup_can_be_disabled_for_existing_editor_text(self):
        self.fill_target('CSDN 默认示例')
        result = self.run_process(mode='import', backup_existing=False)
        self.assertIsNone(result.backup_path)
        self.assertFalse((self.root / 'recovery').exists())
        self.assertIn('Markdown 原文', editor_text(self.target))

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
        self.assertEqual(restore_image_placeholders(editor_text(self.target)), text)

    def test_concurrent_edits_are_not_overwritten(self):
        self.fill_target('旧正文\n![图片](two.png)')
        def edit_on_upload(message):
            if message.startswith('正在上传'):
                self.target.locator('pre').click()
                self.fill_target('用户正在写新内容')
        with self.assertRaisesRegex(HelperError, '发生变化'):
            self.run_process(edit_on_upload)
        self.assertEqual(editor_text(self.target), '用户正在写新内容')

    def test_missing_image_keeps_imported_text(self):
        self.md.write_text('先保留这段文字\n![缺失](missing.png)', encoding='utf-8')
        with self.assertRaisesRegex(HelperError, '正文已导入'):
            self.run_process(mode='import')
        self.assertIn('先保留这段文字', editor_text(self.target))
        self.assertEqual(TestServer.uploads, [])

    def test_failed_second_upload_keeps_text_and_first_result_for_retry(self):
        TestServer.fail_indices = {2}
        with self.assertRaisesRegex(HelperError, '正文已保留'):
            self.run_process(mode='import')
        partial = editor_text(self.target)
        self.assertIn('Markdown 原文', partial)
        self.assertIn('![一](https://i-blog.csdnimg.cn/mock-1.png)', partial)
        self.assertIn('!' + IMAGE_PLACEHOLDER + '[二](two.png)', partial)
        TestServer.fail_indices = set()
        result = self.run_process(mode='repair')
        self.assertEqual(result.image_count, 1)
        self.assertEqual(TestServer.uploads, [b'fake-image-A', b'fake-image-B', b'fake-image-B'])
        self.assertIn('![一](https://i-blog.csdnimg.cn/mock-1.png)', editor_text(self.target))
        self.assertIn('![二](https://i-blog.csdnimg.cn/mock-3.png)', editor_text(self.target))

    def test_failed_editor_write_restores_original(self):
        original = '原文\n![图片](two.png)'
        self.fill_target(original)
        with patch('csdn_browser._wait_for_text', side_effect=[False, True]):
            with self.assertRaisesRegex(HelperError, '未确认写入结果'):
                self.run_process()
        self.assertEqual(restore_image_placeholders(editor_text(self.target)), original)
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

    def test_multiple_csdn_editor_tabs_are_refused(self):
        def page(url):
            return SimpleNamespace(url=url, is_closed=lambda: False)
        context = SimpleNamespace(pages=[page('https://editor.csdn.net/md/'),
                                         page('https://editor.csdn.net/md/')])
        with self.assertRaisesRegex(HelperError, '多个 CSDN'):
            choose_target(context)


if __name__ == '__main__':
    unittest.main()
