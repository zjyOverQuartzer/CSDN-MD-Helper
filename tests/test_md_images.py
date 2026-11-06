import tempfile
import unittest
import os
from pathlib import Path

from md_images import (IMAGE_PLACEHOLDER, find_images, local_path, render_image_progress,
                       restore_image_placeholders, rewrite)


class MarkdownImagesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.md = self.root / "文章.md"

    def test_find_and_rewrite_without_touching_code_or_nonimages(self):
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
        refs = find_images(text)
        self.assertEqual([r.source for r in refs], [
            './目录/含 空格.png', './目录/括号(1).png', './目录/截图.png',
            'https://example.org/a.png',
        ])
        result = rewrite(text, [(r, f'https://i-blog.csdnimg.cn/{i}.png') for i, r in enumerate(refs[:3])])
        self.assertIn('![甲](https://i-blog.csdnimg.cn/0.png "题目")', result)
        self.assertIn('![乙](<https://i-blog.csdnimg.cn/1.png>)', result)
        self.assertIn('<img src="https://i-blog.csdnimg.cn/2.png" width="200">', result)
        self.assertIn('`![示例](./不上传.png)`', result)
        self.assertIn('[普通链接](./目录/截图.png)', result)
        self.assertIn('![网络](https://example.org/a.png)', result)

    def test_nested_parentheses_and_reference_images(self):
        text = '![pic](./a(1).png)\n![标识][ref]\n[普通链接][ref]\n\n[ref]: ./folder/image.png "标题"\n'
        refs = find_images(text)
        self.assertEqual([ref.source for ref in refs], ['./a(1).png', './folder/image.png'])
        result = rewrite(text, [(refs[1], 'https://i-blog.csdnimg.cn/image.png')])
        self.assertIn('![标识](https://i-blog.csdnimg.cn/image.png "标题")', result)
        self.assertIn('[普通链接][ref]', result)
        self.assertIn('[ref]: ./folder/image.png', result)

    def test_relative_absolute_file_urls_and_remote(self):
        relative = local_path('./图片%20一.png', self.md)
        self.assertEqual(relative, (self.root / '图片 一.png').resolve())
        absolute = self.root / 'a.png'
        self.assertEqual(local_path(str(absolute), self.md), absolute.resolve())
        self.assertEqual(local_path(absolute.as_uri(), self.md), absolute.resolve())
        for remote in ['https://example.org/x.png', 'data:image/png;base64,AAA', '//example.org/a.png']:
            self.assertIsNone(local_path(remote, self.md))

    def test_tilde_fences_and_unmatched_backticks(self):
        text = '~~~\n![hidden](a.png)\n~~~\n` unmatched\n![shown](b.png)\n'
        self.assertEqual([r.source for r in find_images(text)], ['b.png'])

    @unittest.skipUnless(os.name == 'nt', 'Windows path separator case')
    def test_windows_parenthesized_directory_preserves_separator(self):
        source = str(self.root / '(笔记)' / 'a.png')
        self.assertEqual(local_path(source, self.md), (self.root / '(笔记)' / 'a.png').resolve())

    def test_unresolved_images_are_inert_and_restorable(self):
        text = ('正文\n![普通](./a.png "标题")\n<img src="./b.png" width="200">\n'
                '![引用][pic]\n\n[pic]: ./c.png "引用标题"\n'
                '`![代码](./ignore.png)`\n')
        refs = find_images(text)
        protected = render_image_progress(text, refs, [])
        self.assertIn('!' + IMAGE_PLACEHOLDER + '[普通](./a.png "标题")', protected)
        self.assertIn('<' + IMAGE_PLACEHOLDER + 'img src="./b.png" width="200">', protected)
        self.assertIn('!' + IMAGE_PLACEHOLDER + '[引用][pic]', protected)
        self.assertIn('`![代码](./ignore.png)`', protected)
        self.assertEqual(restore_image_placeholders(protected), text)
        partial = render_image_progress(text, refs, [(refs[0], 'https://i-blog.csdnimg.cn/a.png')])
        self.assertIn('![普通](https://i-blog.csdnimg.cn/a.png "标题")', partial)
        self.assertIn('<' + IMAGE_PLACEHOLDER + 'img src="./b.png"', partial)


if __name__ == '__main__':
    unittest.main()
