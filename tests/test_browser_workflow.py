"""Exercise browser upload/rewrite against a local, disposable editor."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # 实现本地模拟服务器。
import json  # 构造模拟的 CSDN 上传响应。
from pathlib import Path  # 创建测试文档和图片路径。
import tempfile  # 为每个测试创建隔离的临时目录。
import threading  # 在后台运行本地 HTTP 服务。
import unittest  # 提供测试用例及断言。
from unittest.mock import patch  # 临时替换函数以模拟写入失败。
from urllib.parse import unquote  # 还原请求头中的编辑器正文。
from types import SimpleNamespace  # 构造多标签检查所需的简化页面对象。

from playwright.sync_api import sync_playwright

from csdn_browser import HelperError, choose_target, editor_text, process_article
from md_images import IMAGE_PLACEHOLDER, restore_image_placeholders


# 这是本地模拟编辑器：模拟本地图片被改写、异步插入图片和额外换行。
# 测试请求只发给本机服务器，返回的 CSDN 图片地址也是模拟值。
# 内嵌脚本监听模拟编辑区的 paste 事件，并阻止浏览器默认粘贴行为。
# 粘贴文本时：把本地 Markdown/HTML 图片模拟改写成失败图，再按测试开关增加换行。
# delayedLeadingNewlines 会在稍后追加前导换行，模拟编辑器异步渲染。
# 粘贴文件时：将字节 POST 到本地 /upload，并把粘贴前正文写入请求头供测试核对。
# 本地服务器返回模拟网址后，延迟把图片插入正文，以复现页面异步更新。
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


# 记录每次收到的文件与当时的正文，并可按序号模拟上传失败。
class TestServer(BaseHTTPRequestHandler):
    # 作用：模拟本地编辑器和图片上传服务，并记录请求。
    # 使用场景：浏览器流程测试运行时使用，不连接真实 CSDN 账号。
    uploads = []  # 按请求顺序记录图片文件内容。
    editor_snapshots = []  # 按请求顺序记录上传开始时的编辑器正文。
    fail = False  # True 时所有上传请求都返回失败。
    fail_indices = set()  # 指定需要失败的上传请求序号。

    def log_message(self, *args):
        # 作用：覆盖默认 HTTP 请求日志输出。
        # 使用场景：测试服务器接收请求时调用，用于避免测试输出被日志淹没。
        pass

    def do_GET(self):
        # 作用：向浏览器返回模拟编辑器页面。
        # 使用场景：测试页面打开本地服务器地址时由 HTTP 服务调用。
        self.send_response(200)  # 返回 HTTP 成功状态。
        self.send_header('Content-Type', 'text/html; charset=utf-8')  # 声明页面及字符编码。
        self.end_headers()  # HTTP 响应头到此结束。
        self.wfile.write(HTML)  # 返回前面定义的模拟编辑器页面。

    def do_POST(self):
        # 作用：记录上传内容并返回模拟图片地址或错误。
        # 使用场景：模拟编辑器向本地服务器提交图片时调用。
        size = int(self.headers.get('Content-Length', 0))  # 从请求头取得上传字节数。
        type(self).uploads.append(self.rfile.read(size))  # 保存本次收到的图片内容。
        type(self).editor_snapshots.append(unquote(self.headers.get('X-Editor-Text', '')))  # 保存上传前正文。
        if type(self).fail or len(type(self).uploads) in type(self).fail_indices:
            self.send_response(503)  # 由测试指定失败时返回服务不可用。
            self.end_headers()  # 结束失败响应的请求头。
            return  # 此次不返回图片地址。
        url = f'https://i-blog.csdnimg.cn/mock-{len(type(self).uploads)}.png'  # 为本次上传生成模拟地址。
        data = json.dumps({'data': {'imageUrl': url}}).encode()  # 模拟真实响应的 JSON 结构。
        self.send_response(200)  # 返回成功状态。
        self.send_header('Content-Type', 'application/json')  # 告诉客户端响应是 JSON。
        self.end_headers()  # 结束响应头。
        self.wfile.write(data)  # 返回模拟上传结果。


class BrowserWorkflowTest(unittest.TestCase):
    # 作用：集中验证编辑器导入、上传、保护和恢复流程。
    # 使用场景：运行 unittest 测试集时使用本地服务器和无界面的 Edge。
    @classmethod
    def setUpClass(cls):
        # 作用：启动测试类共用的本地服务器和浏览器。
        # 使用场景：该测试类的所有用例运行之前自动调用。
        # 使用随机本地端口和无界面的 Edge，避免访问真实 CSDN 账号。
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), TestServer)  # 仅监听本机，端口由系统分配。
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)  # 后台提供模拟页面。
        cls.server_thread.start()  # 启动本地服务器线程。
        cls.address = f'http://127.0.0.1:{cls.server.server_port}/'  # 生成实际测试地址。
        cls.playwright = sync_playwright().start()  # 启动测试专用 Playwright。
        cls.browser = cls.playwright.chromium.launch(channel='msedge', headless=True)  # 无界面运行 Edge。

    @classmethod
    def tearDownClass(cls):
        # 作用：关闭浏览器、Playwright 和本地服务器。
        # 使用场景：该测试类的所有用例结束后自动调用。
        cls.browser.close()  # 关闭测试浏览器。
        cls.playwright.stop()  # 释放 Playwright。
        cls.server.shutdown()  # 停止本地服务器循环。
        cls.server.server_close()  # 关闭服务器套接字。

    def setUp(self):
        # 作用：创建独立临时目录、测试文档、图片和浏览器上下文。
        # 使用场景：每个测试方法运行之前自动调用。
        # 每个测试使用独立临时文件和浏览器上下文，避免相互影响。
        TestServer.uploads = []  # 清空上一用例的上传记录。
        TestServer.editor_snapshots = []  # 清空上一用例的正文快照。
        TestServer.fail = False  # 默认允许上传。
        TestServer.fail_indices = set()  # 默认没有指定失败序号。
        self.tmp = tempfile.TemporaryDirectory()  # 创建当前用例专用目录。
        self.addCleanup(self.tmp.cleanup)  # 用例结束后自动清理该目录。
        self.root = Path(self.tmp.name)  # 保存临时目录路径。
        self.md = self.root / '文章.md'  # 定义待处理的测试文档路径。
        (self.root / '图片 一.png').write_bytes(b'fake-image-A')  # 模拟第一张本地图。
        (self.root / 'two.png').write_bytes(b'fake-image-B')  # 模拟第二张本地图。
        self.md.write_text('Markdown 原文\n![一](图片 一.png)\n![二](two.png)\n', encoding='utf-8')
        self.context = self.browser.new_context()  # 每个用例使用独立浏览器上下文。
        self.addCleanup(self.context.close)  # 用例结束后关闭上下文。
        self.target = self.context.new_page()  # 创建要操作的编辑页面。
        self.target.goto(self.address)  # 打开本机模拟编辑器。

    def fill_target(self, text):
        # 作用：将给定文本写入测试编辑器。
        # 使用场景：需要构造已有正文或模拟用户修改时调用。
        self.target.locator('pre').evaluate('(element, text) => { element.textContent = text; }', text)

    def run_process(self, notify=lambda message: None, **kwargs):
        # 作用：使用测试页面和本地目录调用文章处理流程。
        # 使用场景：各流程测试调用；只允许本地测试服务器作为额外响应来源。
        # extra_hosts 为测试服务器放行 127.0.0.1；timeout_ms 缩短失败等待时间。
        return process_article(self.target, self.md, self.root / 'recovery', notify,
                               timeout_ms=700, extra_hosts={'127.0.0.1'}, **kwargs)

    # 保留页面中的修改，同一张图片被引用两次时只上传一次。
    def test_existing_draft_keeps_edits_and_deduplicates_images(self):
        # 作用：保留页面中的修改，同一张图片被引用两次时只上传一次。
        # 使用场景：修改浏览器处理或正文保护逻辑后，运行该用例检查是否产生回归。
        original = '# 页面中改过的标题\n![一](图片 一.png)\n![再次引用](图片 一.png)\n![二](two.png)'  # 第一张图引用两次。
        self.fill_target(original)  # 把已有文章写入模拟编辑器。
        result = self.run_process()  # 自动模式选择修复已有正文。
        updated = editor_text(self.target)  # 读取处理后的完整正文。
        self.assertIn('# 页面中改过的标题', updated)  # 编辑器中修改过的标题仍存在。
        self.assertEqual(updated.count('https://i-blog.csdnimg.cn/mock-1.png'), 2)  # 两处引用使用同一新地址。
        self.assertIn('https://i-blog.csdnimg.cn/mock-2.png', updated)  # 第二张图片也已替换。
        self.assertEqual(TestServer.uploads, [b'fake-image-A', b'fake-image-B'])  # 两个文件各上传一次。
        self.assertEqual((result.image_count, result.reference_count, result.imported), (2, 3, False))  # 统计准确且未导入。
        self.assertEqual(result.backup_path.read_text(encoding='utf-8'), original)  # 备份保留原正文。
        self.assertIn('Markdown 原文', self.md.read_text(encoding='utf-8'))  # 本地 Markdown 未被改写。

    # 空白编辑器先导入本地全文，随后每次上传都保留正文。
    def test_blank_editor_imports_original_article(self):
        # 作用：空白编辑器先导入本地全文，随后每次上传都保留正文。
        # 使用场景：修改浏览器处理或正文保护逻辑后，运行该用例检查是否产生回归。
        result = self.run_process()  # 模拟编辑器为空时运行自动模式。
        self.assertTrue(result.imported)  # 空编辑器应导入本地文档。
        self.assertIn('Markdown 原文', editor_text(self.target))  # 最终正文包含本地文字。
        self.assertIn('https://i-blog.csdnimg.cn/mock-1.png', editor_text(self.target))  # 第一张图已替换。
        self.assertIsNone(result.backup_path)  # 原编辑器为空，不产生备份。
        self.assertEqual(len(TestServer.editor_snapshots), 2)  # 两张图各触发一次上传。
        self.assertIn('Markdown 原文', TestServer.editor_snapshots[0])  # 第一张上传前已有全文。
        self.assertIn('Markdown 原文', TestServer.editor_snapshots[1])  # 第二张上传前仍保留全文。
        self.assertIn('https://i-blog.csdnimg.cn/mock-1.png', TestServer.editor_snapshots[1])  # 保留第一张结果。

    # 模拟连续处理 18 张图片及逐渐增加的前导换行。
    def test_many_images_preserve_the_entire_text(self):
        # 作用：模拟连续处理 18 张图片及逐渐增加的前导换行。
        # 使用场景：修改浏览器处理或正文保护逻辑后，运行该用例检查是否产生回归。
        self.target.evaluate('() => { window.growLeadingNewlines = true; window.pasteCount = 0; }')  # 模拟换行递增。
        lines = ['# 完整正文', '', '前面的说明文字。']  # 构造文章开头。
        for index in range(18):
            filename = f'image-{index}.png'  # 每个序号生成独立文件名。
            (self.root / filename).write_bytes(f'image-{index}'.encode())  # 写入不同的模拟图片内容。
            lines.extend([f'第 {index + 1} 段文字', f'![图{index}]({filename})'])  # 交替添加文字和图片。
        lines.append('最后一段文字。')  # 文章末尾也需要验证没有丢失。
        self.md.write_text('\n'.join(lines), encoding='utf-8')  # 保存合成测试文档。
        result = self.run_process(mode='import')  # 执行完整导入。
        text = editor_text(self.target)  # 读取最终正文。
        self.assertEqual((result.image_count, result.reference_count), (18, 18))  # 全部 18 张均处理完成。
        self.assertIn('前面的说明文字。', text)  # 开头文字存在。
        self.assertIn('最后一段文字。', text)  # 末尾文字存在。
        self.assertTrue(text.startswith('# 完整正文\n'))  # 多余前导换行已经处理。
        for index in range(18):
            self.assertIn(f'![图{index}](https://i-blog.csdnimg.cn/mock-{index + 1}.png)', text)  # 每张都指向模拟网址。
        # 所有上传阶段的正文快照都应保留文章开头和结尾。
        self.assertTrue(all('前面的说明文字。' in item and '最后一段文字。' in item
                            for item in TestServer.editor_snapshots))

    # 没有本地图片的文章也应当能够导入文字。
    def test_import_without_local_images_still_imports_text(self):
        # 作用：没有本地图片的文章也应当能够导入文字。
        # 使用场景：修改浏览器处理或正文保护逻辑后，运行该用例检查是否产生回归。
        self.md.write_text('# 纯文字\n这篇文章没有本地图。', encoding='utf-8')  # 构造不含图片的文档。
        result = self.run_process(mode='import')  # 显式选择导入模式。
        self.assertEqual((result.image_count, result.reference_count), (0, 0))  # 图片统计为零。
        self.assertIn('这篇文章没有本地图。', editor_text(self.target))  # 文字仍成功导入。
        self.assertEqual(TestServer.uploads, [])  # 没有发送图片上传请求。

    # 两种图片语法都替换路径，同时保留 HTML 属性及前后文字。
    def test_html_and_markdown_images_preserve_attributes_and_text(self):
        # 作用：两种图片语法都替换路径，同时保留 HTML 属性及前后文字。
        # 使用场景：修改浏览器处理或正文保护逻辑后，运行该用例检查是否产生回归。
        # 同时放入保留宽高的 HTML 图片与标准 Markdown 图片。
        self.md.write_text('正文开始\n<img src="图片 一.png" alt="第一张" width="280">\n'
                           '中间文字\n![第二张](two.png)\n正文结束', encoding='utf-8')
        result = self.run_process(mode='import')  # 导入并处理两张图。
        text = editor_text(self.target)  # 获取最终文章内容。
        self.assertEqual((result.image_count, result.reference_count), (2, 2))  # 两个文件、两处引用。
        self.assertIn('<img src="https://i-blog.csdnimg.cn/mock-1.png" alt="第一张" width="280">', text)  # HTML 属性保留。
        self.assertIn('![第二张](https://i-blog.csdnimg.cn/mock-2.png)', text)  # Markdown 图片已替换。
        self.assertIn('正文开始', text)  # 开头文字保留。
        self.assertIn('中间文字', text)  # 中间文字保留。
        self.assertIn('正文结束', text)  # 末尾文字保留。

    # 删除编辑器额外加入的首部换行，但保留原文中有意留出的空行。
    def test_editor_extra_newline_is_removed_without_losing_blank_lines(self):
        # 作用：删除编辑器额外加入的首部换行，但保留原文中有意留出的空行。
        # 使用场景：修改浏览器处理或正文保护逻辑后，运行该用例检查是否产生回归。
        self.md.write_text('\n\n# 正文\n\n第一段\n\n![图片](two.png)\n\n最后一段', encoding='utf-8')  # 原文以两个换行开头。
        self.target.evaluate('() => { window.addLeadingNewline = true; }')  # 模拟编辑器额外添加一个换行。
        self.run_process(mode='import')  # 导入并上传图片。
        # 最终应保留原来的两个换行，去掉模拟编辑器额外增加的换行。
        self.assertEqual(editor_text(self.target),
                         '\n\n# 正文\n\n第一段\n\n'
                         '![图片](https://i-blog.csdnimg.cn/mock-1.png)\n\n最后一段')

    # 没有额外换行时，原文开头的空行不应被删除。
    def test_editor_preserves_intentional_leading_newlines(self):
        # 作用：没有额外换行时，原文开头的空行不应被删除。
        # 使用场景：修改浏览器处理或正文保护逻辑后，运行该用例检查是否产生回归。
        self.md.write_text('\n\n# 正文\n\n第一段', encoding='utf-8')  # 原文以两个空行开始。
        self.run_process(mode='import')  # 不开启模拟编辑器的额外换行。
        self.assertEqual(editor_text(self.target), '\n\n# 正文\n\n第一段')  # 用户原有空行不能丢失。

    # 连续写入时额外换行逐渐增多，也不能中断图片处理。
    def test_growing_extra_newlines_do_not_stop_multiple_uploads(self):
        # 作用：连续写入时额外换行逐渐增多，也不能中断图片处理。
        # 使用场景：修改浏览器处理或正文保护逻辑后，运行该用例检查是否产生回归。
        # 构造原有前导空行及两张 HTML 图片。
        self.md.write_text('\n\n# 正文\n<img src="图片 一.png" />'
                           '<img src="two.png" />\n\n末尾', encoding='utf-8')
        self.target.evaluate('() => { window.growLeadingNewlines = true; window.pasteCount = 0; }')  # 每次粘贴多加换行。
        result = self.run_process(mode='import')  # 执行多图导入。
        self.assertEqual((result.image_count, result.reference_count), (2, 2))  # 两张图片都已处理。
        # 检查原始空行保留、额外换行消除，且两张图都指向上传结果。
        self.assertEqual(editor_text(self.target),
                         '\n\n# 正文\n<img src="https://i-blog.csdnimg.cn/mock-1.png" />'
                         '<img src="https://i-blog.csdnimg.cn/mock-2.png" />\n\n末尾')

    # 即使换行在粘贴之后异步出现，也要等正文稳定后再继续。
    def test_delayed_extra_newlines_do_not_stop_multiple_uploads(self):
        # 作用：即使换行在粘贴之后异步出现，也要等正文稳定后再继续。
        # 使用场景：修改浏览器处理或正文保护逻辑后，运行该用例检查是否产生回归。
        self.md.write_text('\n\n# 正文\n![一](图片 一.png)\n![二](two.png)\n末尾', encoding='utf-8')  # 两张 Markdown 图。
        self.target.evaluate('() => { window.delayedLeadingNewlines = true; }')  # 模拟稍后才插入额外换行。
        result = self.run_process(mode='import')  # 执行导入并等待编辑器稳定。
        self.assertEqual((result.image_count, result.reference_count), (2, 2))  # 两张图片都已处理。
        # 正文及图片链接必须与预期一致，不能残留延迟插入的换行。
        self.assertEqual(editor_text(self.target),
                         '\n\n# 正文\n![一](https://i-blog.csdnimg.cn/mock-1.png)\n'
                         '![二](https://i-blog.csdnimg.cn/mock-2.png)\n末尾')

    # 明确选择导入时，已有的编辑器示例内容不会改变导入来源。
    def test_explicit_import_uses_selected_file_even_when_editor_has_placeholder(self):
        # 作用：明确选择导入时，已有的编辑器示例内容不会改变导入来源。
        # 使用场景：修改浏览器处理或正文保护逻辑后，运行该用例检查是否产生回归。
        self.fill_target('编辑器占位信息')  # 模拟 CSDN 初始化时已有示例内容。
        result = self.run_process(mode='import')  # 用户明确选择导入。
        self.assertTrue(result.imported)  # 必须执行导入。
        self.assertIn('Markdown 原文', editor_text(self.target))  # 本地文档文字已经写入。
        self.assertIn('https://i-blog.csdnimg.cn/mock-1.png', editor_text(self.target))  # 本地图片也已处理。
        self.assertEqual(result.backup_path.read_text(encoding='utf-8'), '编辑器占位信息')  # 原内容已备份。

    # 用户取消备份后，已有正文不会生成恢复文件。
    def test_backup_can_be_disabled_for_existing_editor_text(self):
        # 作用：用户取消备份后，已有正文不会生成恢复文件。
        # 使用场景：修改浏览器处理或正文保护逻辑后，运行该用例检查是否产生回归。
        self.fill_target('CSDN 默认示例')  # 模拟已有示例正文。
        result = self.run_process(mode='import', backup_existing=False)  # 关闭备份并导入。
        self.assertIsNone(result.backup_path)  # 处理结果没有备份路径。
        self.assertFalse((self.root / 'recovery').exists())  # 没有创建备份目录。
        self.assertIn('Markdown 原文', editor_text(self.target))  # 不备份仍可正常导入。

    # 仅包含零宽字符和空白的正文视为空编辑器。
    def test_zero_width_blank_is_treated_as_empty(self):
        # 作用：仅包含零宽字符和空白的正文视为空编辑器。
        # 使用场景：修改浏览器处理或正文保护逻辑后，运行该用例检查是否产生回归。
        self.fill_target('\ufeff\u200b\n')  # 模拟只有零宽标记和换行的编辑器。
        result = self.run_process(mode='auto')  # 自动模式判断正文来源。
        self.assertTrue(result.imported)  # 不可见字符不应阻止导入。
        self.assertIn('Markdown 原文', editor_text(self.target))  # 本地正文已写入。

    # 修复模式下没有图片时给出提示，不上传或覆盖正文。
    def test_repair_mode_reports_how_to_import_if_editor_has_no_images(self):
        # 作用：修复模式下没有图片时给出提示，不上传或覆盖正文。
        # 使用场景：修改浏览器处理或正文保护逻辑后，运行该用例检查是否产生回归。
        self.fill_target('已有正文，但没有本地图片')  # 模拟没有本地图片的现有正文。
        # assertRaisesRegex 同时验证异常类型和提示中包含的文字。
        with self.assertRaisesRegex(HelperError, '导入所选 Markdown'):
            self.run_process(mode='repair')  # 修复模式应报告没有可处理图片。
        self.assertEqual(TestServer.uploads, [])  # 没有发生上传。
        self.assertEqual(editor_text(self.target), '已有正文，但没有本地图片')  # 原正文不变。

    # 用户确认后正文又发生变化时，放弃导入，保留新内容。
    def test_change_after_confirmation_is_not_overwritten(self):
        # 作用：用户确认后正文又发生变化时，放弃导入，保留新内容。
        # 使用场景：修改浏览器处理或正文保护逻辑后，运行该用例检查是否产生回归。
        self.fill_target('已被用户修改')  # 当前页面与确认时的快照不同。
        with self.assertRaisesRegex(HelperError, '处理前编辑器内容发生变化'):
            self.run_process(mode='import', expected_before='确认时的正文')  # 传入较早的确认快照。
        self.assertEqual(TestServer.uploads, [])  # 发现变化后没有上传。
        self.assertEqual(editor_text(self.target), '已被用户修改')  # 新内容未被覆盖。

    # 修复模式遇到缺图时，不上传图片也不修改已有正文。
    def test_missing_image_does_not_upload_or_modify_editor(self):
        # 作用：修复模式遇到缺图时，不上传图片也不修改已有正文。
        # 使用场景：修改浏览器处理或正文保护逻辑后，运行该用例检查是否产生回归。
        text = '不应改变\n![缺失](missing.png)'  # 准备一处不存在的本地图片。
        self.fill_target(text)  # 写入模拟编辑器。
        with self.assertRaisesRegex(HelperError, '不存在'):
            self.run_process()  # 缺图应触发可读错误。
        self.assertEqual(editor_text(self.target), text)  # 修复模式保留原正文。
        self.assertEqual(TestServer.uploads, [])  # 未开始上传。

    # 上传失败后，正文中的临时占位符可以恢复成原来的图片引用。
    def test_failed_upload_keeps_editor_unchanged(self):
        # 作用：上传失败后，正文中的临时占位符可以恢复成原来的图片引用。
        # 使用场景：修改浏览器处理或正文保护逻辑后，运行该用例检查是否产生回归。
        text = '不应改变\n![图片](two.png)'  # 图片存在，但模拟服务将拒绝上传。
        self.fill_target(text)
        TestServer.fail = True  # 强制模拟服务器返回失败。
        with self.assertRaisesRegex(HelperError, '上传失败'):
            self.run_process()
        self.assertEqual(restore_image_placeholders(editor_text(self.target)), text)  # 移除保护标记后正文一致。

    def test_external_click_is_reported_without_claiming_text_changed(self):
        # 作用：页面其他控件发生点击但正文未变时，给出准确的保护提示。
        # 使用场景：用户在处理期间点击浏览器编辑页面的其他区域。
        text = '正文\n![图片](two.png)'
        self.fill_target(text)
        self.target.evaluate("() => { const b = document.createElement('button'); b.id = 'outside'; b.textContent = '其他操作'; document.body.append(b); }")
        def click_outside(message):
            if message.startswith('正在上传'):
                self.target.locator('#outside').click()
        with self.assertRaisesRegex(HelperError, '检测到.*操作'):
            self.run_process(click_outside)
        self.assertEqual(restore_image_placeholders(editor_text(self.target)), text)
        self.assertEqual(TestServer.uploads, [])

    # 处理途中检测到用户操作时停止，不能覆盖用户新输入的内容。
    def test_concurrent_edits_are_not_overwritten(self):
        # 作用：处理途中检测到用户操作时停止，不能覆盖用户新输入的内容。
        # 使用场景：修改浏览器处理或正文保护逻辑后，运行该用例检查是否产生回归。
        self.fill_target('旧正文\n![图片](two.png)')  # 准备有图片的原正文。
        def edit_on_upload(message):
            # 作用：在收到上传进度时模拟用户点击和输入。
            # 使用场景：并发编辑测试中作为进度回调传入，以触发保护机制。
            if message.startswith('正在上传'):  # 在上传开始时触发用户交互。
                self.target.locator('pre').click()  # 模拟点击，触发编辑守卫。
                self.fill_target('用户正在写新内容')  # 模拟用户随后写入新内容。
        with self.assertRaisesRegex(HelperError, '发生变化'):
            self.run_process(edit_on_upload)  # 使用带模拟编辑行为的进度回调。
        self.assertEqual(editor_text(self.target), '用户正在写新内容')  # 用户的新内容必须保留。

    # 导入模式先保留文字，再报告缺失的本地图片。
    def test_missing_image_keeps_imported_text(self):
        # 作用：导入模式先保留文字，再报告缺失的本地图片。
        # 使用场景：修改浏览器处理或正文保护逻辑后，运行该用例检查是否产生回归。
        self.md.write_text('先保留这段文字\n![缺失](missing.png)', encoding='utf-8')  # 本地文档有缺图。
        with self.assertRaisesRegex(HelperError, '正文已导入'):
            self.run_process(mode='import')  # 导入正文后报告缺图。
        self.assertIn('先保留这段文字', editor_text(self.target))  # 文字已保留在编辑器。
        self.assertEqual(TestServer.uploads, [])  # 缺图时没有开始上传。

    # 第二张失败时保留第一张的结果；重试只处理尚未完成的图片。
    def test_failed_second_upload_keeps_text_and_first_result_for_retry(self):
        # 作用：第二张失败时保留第一张的结果；重试只处理尚未完成的图片。
        # 使用场景：修改浏览器处理或正文保护逻辑后，运行该用例检查是否产生回归。
        TestServer.fail_indices = {2}  # 只让第二次图片上传失败。
        with self.assertRaisesRegex(HelperError, '正文已保留'):
            self.run_process(mode='import')  # 导入正文后第二张上传失败。
        partial = editor_text(self.target)  # 读取中断时保留的正文。
        self.assertIn('Markdown 原文', partial)  # 正文文字仍在。
        self.assertIn('![一](https://i-blog.csdnimg.cn/mock-1.png)', partial)  # 第一张的结果已保留。
        self.assertIn('!' + IMAGE_PLACEHOLDER + '[二](two.png)', partial)  # 第二张仍有可恢复的保护标记。
        TestServer.fail_indices = set()  # 解除模拟故障。
        result = self.run_process(mode='repair')  # 从当前正文继续修复未完成图片。
        self.assertEqual(result.image_count, 1)  # 重试时只需处理剩余的一张。
        self.assertEqual(TestServer.uploads, [b'fake-image-A', b'fake-image-B', b'fake-image-B'])  # 仅第二张重试。
        self.assertIn('![一](https://i-blog.csdnimg.cn/mock-1.png)', editor_text(self.target))  # 第一张结果不变。
        self.assertIn('![二](https://i-blog.csdnimg.cn/mock-3.png)', editor_text(self.target))  # 第二张采用重试地址。

    # 模拟第一次写入验证失败、恢复成功，并检查本地备份。
    def test_failed_editor_write_restores_original(self):
        # 作用：模拟第一次写入验证失败、恢复成功，并检查本地备份。
        # 使用场景：修改浏览器处理或正文保护逻辑后，运行该用例检查是否产生回归。
        original = '原文\n![图片](two.png)'  # 构造待保护的原正文。
        self.fill_target(original)
        with patch('csdn_browser._wait_for_text', side_effect=[False, True]):  # 第一次验证失败，第二次恢复验证成功。
            with self.assertRaisesRegex(HelperError, '未确认写入结果'):  # 应报告写入无法确认。
                self.run_process()
        self.assertEqual(restore_image_placeholders(editor_text(self.target)), original)  # 可恢复原正文。
        backups = list((self.root / 'recovery').glob('*.md'))  # 查找创建的本地备份。
        self.assertEqual(len(backups), 1)  # 只生成一份备份。
        self.assertEqual(backups[0].read_text(encoding='utf-8'), original)  # 备份内容与处理前一致。

    # 文件名相同但目录不同的图片，必须作为两个文件上传。
    def test_same_filename_in_different_directories_is_not_confused(self):
        # 作用：文件名相同但目录不同的图片，必须作为两个文件上传。
        # 使用场景：修改浏览器处理或正文保护逻辑后，运行该用例检查是否产生回归。
        for folder, payload in [('one', b'first'), ('two', b'second')]:  # 创建两个不同目录。
            directory = self.root / folder
            directory.mkdir()  # 创建子目录。
            (directory / 'same.png').write_bytes(payload)  # 文件同名，内容不同。
        self.fill_target('![A](one/same.png)\n![B](two/same.png)')  # 正文引用两个不同路径。
        result = self.run_process()
        self.assertEqual(result.image_count, 2)  # 必须识别为两个文件。
        self.assertEqual(TestServer.uploads, [b'first', b'second'])  # 两种不同内容分别上传。
        self.assertIn('![A](https://i-blog.csdnimg.cn/mock-1.png)', editor_text(self.target))  # 第一处对应第一张。
        self.assertIn('![B](https://i-blog.csdnimg.cn/mock-2.png)', editor_text(self.target))  # 第二处对应第二张。

    # 同时打开多个 CSDN 编辑标签时拒绝猜测目标页面。
    def test_multiple_csdn_editor_tabs_are_refused(self):
        # 作用：同时打开多个 CSDN 编辑标签时拒绝猜测目标页面。
        # 使用场景：修改浏览器处理或正文保护逻辑后，运行该用例检查是否产生回归。
        def page(url):
            # 作用：构造只包含必要页面属性的简化对象。
            # 使用场景：多标签测试中模拟已打开的 CSDN 编辑页面。
            return SimpleNamespace(url=url, is_closed=lambda: False)  # 提供目标选择逻辑所需的最小接口。
        # 构造两个都处于打开状态的 CSDN 编辑页。
        context = SimpleNamespace(pages=[page('https://editor.csdn.net/md/'),
                                         page('https://editor.csdn.net/md/')])
        with self.assertRaisesRegex(HelperError, '多个 CSDN'):
            choose_target(context)  # 多个候选页面时不能自动选择。


if __name__ == '__main__':
    unittest.main()  # 直接运行该文件时执行其中的测试。
