"""验证发行包构建不会覆盖文件或混入其他软件的运行库。"""

from pathlib import Path
import importlib.util
import tempfile
import unittest
from unittest.mock import patch

if importlib.util.find_spec('PyInstaller') is not None:
    import build_release
else:
    build_release = None


@unittest.skipIf(build_release is None, '需要安装打包依赖后验证发行脚本')
class BuildReleaseTest(unittest.TestCase):
    # 作用：检查构建目录和二进制来源的安全边界。
    # 使用场景：调整发行包脚本时运行，不需要执行完整打包。

    def setUp(self):
        # 作用：创建独立临时目录，避免改动真实发行文件。
        # 使用场景：每个测试开始时使用。
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'project'
        self.root.mkdir()
        self.root_patch = patch.object(build_release, 'ROOT', self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)

    def test_output_directory_does_not_overwrite_existing_release(self):
        # 作用：重复打包时保留原有发行目录和用户文件。
        # 使用场景：同一版本被再次构建时。
        first = build_release._new_output_directory()
        (first / 'existing.txt').write_text('keep', encoding='utf-8')
        second = build_release._new_output_directory()
        self.assertNotEqual(first, second)
        self.assertEqual((first / 'existing.txt').read_text(encoding='utf-8'), 'keep')

    def test_unrelated_binary_source_is_rejected(self):
        # 作用：阻止把用户机器上其他软件目录中的 DLL 打入发行包。
        # 使用场景：PyInstaller 的 Analysis 表出现意外来源时。
        table = self.root / 'Analysis-00.toc'
        outside = Path(self.temporary.name) / 'other-app' / 'unrelated.dll'
        table.write_text(repr(([('unrelated.dll', str(outside), 'BINARY')],)), encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, '预期目录之外'):
            build_release._validate_binary_sources(table)

    def test_project_binary_source_is_allowed(self):
        # 作用：允许来自项目自身目录的打包依赖。
        # 使用场景：项目虚拟环境内的依赖被 PyInstaller 收集时。
        table = self.root / 'Analysis-00.toc'
        table.write_text(repr(([('inside.dll', str(self.root / 'inside.dll'), 'BINARY')],)), encoding='utf-8')
        build_release._validate_binary_sources(table)

    def test_build_uses_isolated_source_snapshot(self):
        # 作用：打包过程中工作区再次编辑时，构建和发行源码仍基于同一份快照。
        # 使用场景：避免 exe 与发行包中的源码来自不同修改版本。
        source = self.root / 'app.py'
        source.write_text('first version', encoding='utf-8')
        with patch.object(build_release, 'SOURCE_FILES', ('app.py',)):
            snapshot = self.root / 'build-snapshot'
            captured = build_release._snapshot_sources(snapshot)
        source.write_text('later edit', encoding='utf-8')
        self.assertEqual(captured['app.py'], b'first version')
        self.assertEqual((snapshot / 'app.py').read_bytes(), b'first version')

    def test_snapshot_rejects_link_to_outside_directory(self):
        # 作用：避免源码白名单穿过目录链接而收集项目外的文件。
        # 使用场景：构建时项目目录意外包含指向外部的符号链接。
        outside = Path(self.temporary.name) / 'outside'
        outside.mkdir()
        (outside / 'private.py').write_text('private', encoding='utf-8')
        try:
            (self.root / 'linked').symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f'当前环境不能创建目录符号链接：{exc}')
        with patch.object(build_release, 'SOURCE_FILES', ('linked/private.py',)):
            with self.assertRaisesRegex(RuntimeError, '预期源码文件'):
                build_release._snapshot_sources(self.root / 'snapshot')

    def test_project_module_outside_snapshot_is_rejected(self):
        # 作用：检查 PyInstaller 没有从工作区误读另一份应用源码。
        # 使用场景：同时存在工作区和构建快照两份同名模块时。
        table = self.root / 'Analysis-00.toc'
        table.write_text(repr(([('app', str(self.root / 'app.py'), 'PYMODULE')],)), encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, '源码快照'):
            build_release._validate_project_sources(table, self.root / 'snapshot')


if __name__ == '__main__':
    unittest.main()
