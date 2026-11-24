import tempfile  # 创建独立临时目录，不依赖用户的真实文档。
import unittest  # 提供测试类、断言和测试运行器。
import os  # 区分 Windows 专属路径测试。
from pathlib import Path  # 构造测试文档与图片路径。

from md_images import (IMAGE_PLACEHOLDER, find_images, local_path, render_image_progress,
                       restore_image_placeholders, rewrite)
from csdn_browser import image_preflight


# 使用临时目录验证 Markdown 解析、路径解析和图片标记恢复。
class MarkdownImagesTest(unittest.TestCase):
    # 作用：验证图片语法识别、路径解析和安全替换。
    # 使用场景：运行 unittest 测试集时使用临时文件执行。
    def setUp(self):
        # 作用：创建当前测试独享的临时目录和 Markdown 路径。
        # 使用场景：每个解析测试开始前自动调用，结束后清理临时目录。
        self.temp = tempfile.TemporaryDirectory()  # 为当前用例创建临时目录。
        self.addCleanup(self.temp.cleanup)  # 用例结束后自动清理目录。
        self.root = Path(self.temp.name)  # 保存临时目录对应的 Path。
        self.md = self.root / "文章.md"  # 构造解析相对路径时使用的文档位置。

    # 只替换图片路径；代码、普通链接和未选中的网络图片保持原样。
    def test_find_and_rewrite_without_touching_code_or_nonimages(self):
        # 作用：只替换图片路径；代码、普通链接和未选中的网络图片保持原样。
        # 使用场景：修改图片解析或路径处理后，运行该用例检查是否产生回归。
        # 构造混合图片、普通链接、代码、注释和网络图片的示例文档。
        text = (
            '# 文章\n'
            '![甲](./目录/含 空格.png "题目")\n'
            '![乙](<./目录/括号(1).png>)\n'
            '<img src="./目录/截图.png" width="200">\n'
            '[普通链接](./目录/截图.png)\n'
            '`![示例](./不上传.png)`\n'
            '```markdown\n![示例](./不上传2.png)\n```\n'
            '<!-- ![隐藏](./不上传3.png) -->\n'
            '![网络](https://example.org/a.png)\n'
        )
        refs = find_images(text)  # 识别文档中的有效图片引用。
        # 验证代码和 HTML 注释内的示例不会计入，网络图片仍可被识别。
        self.assertEqual([r.source for r in refs], [
            './目录/含 空格.png', './目录/括号(1).png', './目录/截图.png',
            'https://example.org/a.png',
        ])
        result = rewrite(text, [(r, f'https://i-blog.csdnimg.cn/{i}.png') for i, r in enumerate(refs[:3])])  # 替换前三张图。
        self.assertIn('![甲](https://i-blog.csdnimg.cn/0.png "题目")', result)  # 普通图片保留标题。
        self.assertIn('![乙](<https://i-blog.csdnimg.cn/1.png>)', result)  # 尖括号仍然保留。
        self.assertIn('<img src="https://i-blog.csdnimg.cn/2.png" width="200">', result)  # HTML 保留宽度。
        # 以下断言检查未选中的代码、普通链接和网络图片保持不变。
        self.assertIn('`![示例](./不上传.png)`', result)
        # 验证普通链接的目标没有被图片替换逻辑改写。
        self.assertIn('[普通链接](./目录/截图.png)', result)
        # 验证未选中替换的远程图片原样保留。
        self.assertIn('![网络](https://example.org/a.png)', result)

    # 带括号路径和引用式图片能识别，同时不改共用的普通链接定义。
    def test_nested_parentheses_and_reference_images(self):
        # 作用：带括号路径和引用式图片能识别，同时不改共用的普通链接定义。
        # 使用场景：修改图片解析或路径处理后，运行该用例检查是否产生回归。
        text = '![pic](./a(1).png)\n![标识][ref]\n[普通链接][ref]\n\n[ref]: ./folder/image.png "标题"\n'  # 普通图片与引用图片共存。
        refs = find_images(text)  # 识别两种图片语法。
        self.assertEqual([ref.source for ref in refs], ['./a(1).png', './folder/image.png'])  # 检查解析出的路径。
        result = rewrite(text, [(refs[1], 'https://i-blog.csdnimg.cn/image.png')])  # 只替换引用式图片。
        self.assertIn('![标识](https://i-blog.csdnimg.cn/image.png "标题")', result)  # 图片保留标题。
        self.assertIn('[普通链接][ref]', result)  # 同一标签的普通链接不受影响。
        self.assertIn('[ref]: ./folder/image.png', result)  # 原引用定义也不改写。

    def test_reference_definitions_inside_nested_blockquotes(self):
        # 作用：识别引用块中的完整、折叠和简写引用，保留原定义与普通链接。
        # 使用场景：导出的 Markdown 把图片和引用定义放在同一引用块内。
        text = ('> ![完整][fig]\r\n> ![折叠][]\r\n> ![简写]\r\n>\r\n'
                '> [FIG]: <./目录/图 一.png> "标题"\r\n'
                '> [折叠]: ./other.png\r\n> > [简写]: ./third.png (说明)\r\n'
                '> [普通链接][fig]\r\n')
        refs = find_images(text)
        self.assertEqual([ref.source for ref in refs], ['./目录/图 一.png', './other.png', './third.png'])
        urls = [f'https://i-blog.csdnimg.cn/{index}.png' for index in range(3)]
        result = rewrite(text, list(zip(refs, urls)))
        self.assertIn('> ![完整](https://i-blog.csdnimg.cn/0.png "标题")', result)
        self.assertIn('> ![折叠](https://i-blog.csdnimg.cn/1.png)', result)
        self.assertIn('> ![简写](https://i-blog.csdnimg.cn/2.png (说明))', result)
        self.assertIn('> [FIG]: <./目录/图 一.png> "标题"', result)
        self.assertIn('> [普通链接][fig]', result)
        self.assertEqual(restore_image_placeholders(render_image_progress(text, refs, [])), text)

    def test_quoted_reference_definition_with_multiline_path_and_file_url(self):
        # 作用：处理引用块、列表和跨行定义，并继续识别本地 file: 网址。
        # 使用场景：图片路径含空格或从其他工具导出为绝对文件网址。
        file_url = (self.root / '文件.png').as_uri()
        text = ('> - ![照片][pic]\n>\n>   [pic]:\n>     <./目录/带 空格.png>\n'
                '>     "测试标题"\n\n> ![文件][file]\n>\n'
                f'> [file]: <{file_url}> "文件标题"\n')
        refs = find_images(text)
        self.assertEqual(len(refs), 2)
        self.assertEqual(local_path(refs[0].source, self.md), (self.root / '目录' / '带 空格.png').resolve())
        self.assertEqual(local_path(refs[1].source, self.md), (self.root / '文件.png').resolve())
        result = rewrite(text, [(refs[0], 'https://i-blog.csdnimg.cn/photo.png'),
                                (refs[1], 'https://i-blog.csdnimg.cn/file.png')])
        self.assertIn('![照片](https://i-blog.csdnimg.cn/photo.png "测试标题")', result)
        self.assertIn('![文件](https://i-blog.csdnimg.cn/file.png "文件标题")', result)
        self.assertIn(f'> [file]: <{file_url}> "文件标题"', result)

    def test_quoted_paragraph_is_not_a_reference_definition(self):
        # 作用：引用块中只是正文续行的 [标签]: 不应变成图片定义。
        # 使用场景：文章中展示引用定义示例，但没有真正声明该标签。
        text = '> 这是一段正文\n> [fake]: missing.png\n\n![未定义][fake]\n'
        self.assertEqual(find_images(text), [])

    def test_first_valid_reference_definition_wins_across_blockquote(self):
        # 作用：按 Markdown 的首次有效定义解析同名标签。
        # 使用场景：引用块与正文中都出现相同标签，或无效正文续行先出现。
        text = ('> 这是一段正文\n> [ref]: ignored.png\n\n'
                '> [ref]: first.png\n\n[ref]: second.png\n\n![图片][ref]\n')
        self.assertEqual([ref.source for ref in find_images(text)], ['first.png'])
        unicode_text = '> ![图片][ı]\n>\n> [ı]: unicode.png\n'
        self.assertEqual([ref.source for ref in find_images(unicode_text)], ['unicode.png'])

    def test_preformatted_html_keeps_examples_but_finds_real_html_images(self):
        # 作用：忽略 pre 块中的 Markdown 示例，仍处理真正的 HTML img 和块外图片。
        # 使用场景：文章同时包含 HTML 格式的代码示例和需要上传的真实图片。
        text = ('<pre class="x > y">\r\n![示例](missing.png)\r\n[bad]: missing-def.png\r\n'
                '&lt;img src="example.png"&gt;\r\n<img src="inside-real.png" alt="真实">\r\n'
                '</pre>\r\n\r\n![普通](visible.png)\r\n'
                '<span>![行内](inline.png)</span>\r\n<img src="outside.png">\r\n')
        refs = find_images(text)
        self.assertEqual([ref.source for ref in refs], ['inside-real.png', 'visible.png', 'inline.png', 'outside.png'])
        result = rewrite(text, [(ref, f'https://i-blog.csdnimg.cn/{index}.png')
                                for index, ref in enumerate(refs)])
        self.assertIn('![示例](missing.png)', result)
        self.assertIn('[bad]: missing-def.png', result)
        self.assertIn('&lt;img src="example.png"&gt;', result)
        self.assertIn('<img src="https://i-blog.csdnimg.cn/0.png" alt="真实">', result)

    def test_preformatted_html_inside_blockquote_or_div(self):
        # 作用：处理嵌套 pre 和未闭合的 pre，同时保留块外真实图片。
        # 使用场景：引用块或 HTML 容器中嵌套代码示例。
        text = ('> <pre>\n> ![引用示例](quote-example.png)\n> </pre>\n\n'
                '<div>\n<pre>![嵌套示例](nested-example.png)</pre>\n'
                '<img src="inside-div.png">\n</div>\n\n![外部](outside.png)\n')
        self.assertEqual([ref.source for ref in find_images(text)], ['inside-div.png', 'outside.png'])
        unclosed = '<pre>\n![未闭合示例](missing.png)\n<img src="real.png">\n'
        self.assertEqual([ref.source for ref in find_images(unclosed)], ['real.png'])

    # 相对路径、绝对路径和 file: 地址能定位；网络与内嵌资源跳过。
    def test_relative_absolute_file_urls_and_remote(self):
        # 作用：相对路径、绝对路径和 file: 地址能定位；网络与内嵌资源跳过。
        # 使用场景：修改图片解析或路径处理后，运行该用例检查是否产生回归。
        relative = local_path('./图片%20一.png', self.md)  # 解析带 URL 编码空格的相对路径。
        self.assertEqual(relative, (self.root / '图片 一.png').resolve())  # 相对于 Markdown 目录且已解码。
        absolute = self.root / 'a.png'  # 构造一个绝对路径。
        self.assertEqual(local_path(str(absolute), self.md), absolute.resolve())  # 绝对路径保持指向原文件。
        self.assertEqual(local_path(absolute.as_uri(), self.md), absolute.resolve())  # file: URL 指向同一路径。
        for remote in ['https://example.org/x.png', 'data:image/png;base64,AAA', '//example.org/a.png']:
            self.assertIsNone(local_path(remote, self.md))  # 远程或内嵌资源不作为本地文件。

    # 波浪线代码块中的图片要忽略，未配对反引号不会遮住后续正文。
    def test_tilde_fences_and_unmatched_backticks(self):
        # 作用：波浪线代码块中的图片要忽略，未配对反引号不会遮住后续正文。
        # 使用场景：修改图片解析或路径处理后，运行该用例检查是否产生回归。
        text = '~~~\n![hidden](a.png)\n~~~\n` unmatched\n![shown](b.png)\n'
        # 只应识别围栏之外的 b.png，不能识别代码块中的图片。
        self.assertEqual([r.source for r in find_images(text)], ['b.png'])

    def test_indented_code_is_ignored_without_losing_list_images(self):
        # 作用：区分真正的缩进代码块和列表中的普通缩进内容。
        # 使用场景：文章中同时包含示例代码、列表图片和引用内代码时。
        text = (
            '    ![代码示例](code.png)\r\n'
            '    <img src="code-html.png">\r\n'
            '\r\n'
            '- 列表项\r\n'
            '    ![列表图片](list.png)\r\n'
            '\r\n'
            '>     ![引用内代码](quote-code.png)\r\n'
            '\r\n'
            '正文段落\r\n'
            '    ![段落续行](continuation.png)\r\n'
            '\r\n'
            '![正文图片](normal.png)\r\n'
        )
        refs = find_images(text)
        self.assertEqual([ref.source for ref in refs], ['list.png', 'continuation.png', 'normal.png'])
        result = rewrite(text, [(refs[0], 'https://i-blog.csdnimg.cn/list.png')])
        self.assertIn('    ![代码示例](code.png)', result)
        self.assertIn('    ![列表图片](https://i-blog.csdnimg.cn/list.png)', result)

    def test_html_image_uses_exact_src_attribute(self):
        # 作用：确保 data-src、标题文字与包含 > 的属性不会冒充 src。
        # 使用场景：HTML 图片含懒加载属性、不同引号或多行属性时。
        text = (
            '<img data-src="wrong.png" title="src=bogus > example" SRC = "actual.png">\n'
            '<img data-src="only-data.png">\n'
            "<img alt='描述'\n src='second.png' width='200'>\n"
            '<img src=third.png>\n'
        )
        refs = find_images(text)
        self.assertEqual([ref.source for ref in refs], ['actual.png', 'second.png', 'third.png'])
        result = rewrite(text, [(refs[0], 'https://i-blog.csdnimg.cn/actual.png')])
        self.assertIn('data-src="wrong.png"', result)
        self.assertIn('SRC = "https://i-blog.csdnimg.cn/actual.png"', result)

    # Windows 带括号的目录前，反斜杠应保留为路径分隔符。
    @unittest.skipUnless(os.name == 'nt', 'Windows path separator case')
    def test_windows_parenthesized_directory_preserves_separator(self):
        # 作用：Windows 带括号的目录前，反斜杠应保留为路径分隔符。
        # 使用场景：修改图片解析或路径处理后，运行该用例检查是否产生回归。
        source = str(self.root / '(笔记)' / 'a.png')  # 构造目录名中包含圆括号的 Windows 路径。
        self.assertEqual(local_path(source, self.md), (self.root / '(笔记)' / 'a.png').resolve())  # 分隔符不能丢失。

    # 未完成图片先加占位符，恢复时还原原文；已完成图片保留上传地址。
    def test_unresolved_images_are_inert_and_restorable(self):
        # 作用：未完成图片先加占位符，恢复时还原原文；已完成图片保留上传地址。
        # 使用场景：修改图片解析或路径处理后，运行该用例检查是否产生回归。
        text = ('正文\n![普通](./a.png "标题")\n<img src="./b.png" width="200">\n'
                '![引用][pic]\n\n[pic]: ./c.png "引用标题"\n'
                '`![代码](./ignore.png)`\n')
        refs = find_images(text)  # 收集普通、HTML 和引用式图片。
        protected = render_image_progress(text, refs, [])  # 尚无上传结果时保护全部本地图。
        self.assertIn('!' + IMAGE_PLACEHOLDER + '[普通](./a.png "标题")', protected)
        # 验证未上传的 HTML 图片标记含有可恢复的保护字符。
        self.assertIn('<' + IMAGE_PLACEHOLDER + 'img src="./b.png" width="200">', protected)
        # 验证引用式图片同样被临时保护。
        self.assertIn('!' + IMAGE_PLACEHOLDER + '[引用][pic]', protected)
        self.assertIn('`![代码](./ignore.png)`', protected)
        self.assertEqual(restore_image_placeholders(protected), text)  # 还原后必须与原文完全一致。
        partial = render_image_progress(text, refs, [(refs[0], 'https://i-blog.csdnimg.cn/a.png')])  # 模拟只上传第一张。
        self.assertIn('![普通](https://i-blog.csdnimg.cn/a.png "标题")', partial)  # 已上传图片使用新网址。
        # 验证未上传的 HTML 图片标记含有可恢复的保护字符。
        self.assertIn('<' + IMAGE_PLACEHOLDER + 'img src="./b.png"', partial)  # 未完成的 HTML 图片继续受保护。


    def test_preflight_groups_references_and_reports_original_lines(self):
        # 作用：检查重复图片、缺图、引用式图片及多行 HTML 的原始行号。
        # 使用场景：处理前清单需要展示每张图片的全部引用位置。
        (self.root / 'a.png').write_bytes(b'image')
        text = ('# 文档\n![一](a.png)\n<img\n  src="missing.png">\n'
                '![再](a.png)\n![引用][ref]\n[ref]: missing.png\n')
        plan = image_preflight(text, self.md)
        self.assertEqual(plan.reference_count, 4)
        self.assertEqual(len(plan.images), 2)
        self.assertEqual((plan.images[0].path.name, plan.images[0].lines, plan.images[0].missing),
                         ('a.png', (2, 5), False))
        self.assertEqual((plan.images[1].path.name, plan.images[1].lines, plan.images[1].missing),
                         ('missing.png', (3, 6), True))


if __name__ == '__main__':
    unittest.main()  # 直接运行该文件时执行全部测试方法。
