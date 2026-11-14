"""构建 Windows 发行包，并整理源码和第三方许可证。"""

from __future__ import annotations

import ast
from hashlib import sha256
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import ssl
import struct
import subprocess
import sys
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile
import zlib

from PyInstaller.archive.readers import CArchiveReader


ROOT = Path(__file__).resolve().parent
VERSION = '0.2.0'
EXE_NAME = 'CSDN-Markdown-Helper.exe'
PYTHON_PACKAGES = ('playwright', 'pyee', 'greenlet', 'typing-extensions',
                   'markdown-it-py', 'mdurl', 'pyinstaller')
# 只把明确列出的源码打入发行包，避免把浏览器登录数据或私人文档一起分发。
SOURCE_FILES = (
    'README.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md', '.gitignore',
    '.github/workflows/windows-ci.yml',
    'requirements.txt', 'requirements-build.txt', 'dpi-aware.manifest',
    'app.py', 'csdn_browser.py', 'md_images.py', 'packaged_main.py',
    'build_release.py', '启动助手.cmd', '打包助手.cmd',
    'tests/__init__.py', 'tests/test_app_ui.py', 'tests/test_browser_lifecycle.py',
    'tests/test_browser_workflow.py', 'tests/test_md_images.py', 'tests/test_build_release.py',
    'tests/fixtures/example.png', 'tests/fixtures/example2.png', 'tests/fixtures/联调示例.md',
)


def _new_output_directory() -> Path:
    # 作用：创建不会覆盖已有文件的版本目录。
    # 使用场景：重复打包时保留之前的 exe 和发行压缩包。
    root = ROOT / 'dist'
    root.mkdir(exist_ok=True)
    for index in range(1000):
        suffix = '' if index == 0 else f'-{index}'
        target = root / f'v{VERSION}{suffix}'
        try:
            target.mkdir()
            return target
        except FileExistsError:
            continue
    raise RuntimeError('无法创建新的发行目录，请检查 dist。')


def _build_environment() -> dict[str, str]:
    # 作用：只向打包子进程提供 Python 和 Windows 系统目录。
    # 使用场景：防止 PyInstaller 从其他软件的 PATH 目录误收集 DLL。
    env = os.environ.copy()
    base = Path(sys.base_prefix)
    windows = Path(env.get('SYSTEMROOT') or env.get('SystemRoot') or r'C:\Windows')
    paths = [Path(sys.prefix) / 'Scripts', base / 'Library' / 'bin', base / 'DLLs',
             base, windows / 'System32', windows]
    env['PATH'] = os.pathsep.join(str(path) for path in paths if path.is_dir())
    env.pop('PYTHONPATH', None)
    env.pop('PYTHONHOME', None)
    env['PYTHONNOUSERSITE'] = '1'
    return env


def _validate_binary_sources(analysis_path: Path) -> None:
    # 作用：检查 PyInstaller 记录的二进制依赖是否来自预期目录。
    # 使用场景：构建后阻止其他软件的私有 DLL 被误带进发行包。
    allowed = [ROOT.resolve(), Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve(),
               Path(os.environ.get('SystemRoot', r'C:\Windows')).resolve()]
    analysis = ast.literal_eval(analysis_path.read_text(encoding='utf-8'))

    def visit(value):
        # 作用：递归查找 Analysis 表中的二进制文件条目。
        # 使用场景：PyInstaller 的 TOC 可能包含多层列表和元组。
        if (isinstance(value, tuple) and len(value) == 3 and isinstance(value[2], str)
                and value[2] in {'BINARY', 'EXTENSION'}
                and isinstance(value[1], str)):
            source = Path(value[1]).resolve()
            if not any(source == root or source.is_relative_to(root) for root in allowed):
                raise RuntimeError(f'运行库来自预期目录之外：{source.name}；请检查构建环境。')
            return
        if isinstance(value, (list, tuple)):
            for item in value:
                visit(item)

    visit(analysis)


def _validate_project_sources(analysis_path: Path, source_root: Path) -> None:
    # 作用：确认应用模块和打包自检模块都来自本次源码快照。
    # 使用场景：PyInstaller 分析完成后，避免 exe 和源码包取自不同路径。
    required = {'app', 'csdn_browser', 'md_images', 'tests.test_app_ui',
                'tests.test_browser_lifecycle', 'tests.test_browser_workflow', 'tests.test_md_images'}
    found: set[str] = set()
    snapshot = source_root.resolve()
    analysis = ast.literal_eval(analysis_path.read_text(encoding='utf-8'))

    def visit(value):
        # 作用：在 TOC 中定位属于本项目的 Python 模块。
        # 使用场景：检查 PyInstaller 实际选中的源码位置。
        if (isinstance(value, tuple) and len(value) == 3 and isinstance(value[0], str)
                and value[0] in required and value[2] == 'PYMODULE'):
            if not Path(value[1]).resolve().is_relative_to(snapshot):
                raise RuntimeError(f'模块未来自本次源码快照：{value[0]}')
            found.add(value[0])
            return
        if isinstance(value, (list, tuple)):
            for item in value:
                visit(item)

    visit(analysis)
    if found != required:
        raise RuntimeError('发行程序缺少项目模块或打包自检模块：' + ', '.join(sorted(required - found)))


def _package_licenses(bundle: ZipFile, executable: Path) -> tuple[dict[str, str], list[str]]:
    # 作用：收集 Python 依赖及 exe 内已有的许可证和第三方声明。
    # 使用场景：生成可分发压缩包，同时记录每个 Python 组件的实际版本。
    versions: dict[str, str] = {}
    included: list[str] = []
    for name in PYTHON_PACKAGES:
        dist = metadata.distribution(name)
        versions[name] = dist.version
        found = False
        for entry in dist.files or ():
            relative = str(entry).replace('\\', '/')
            if '.dist-info/' not in relative:
                continue
            inside = relative.split('.dist-info/', 1)[1]
            basename = Path(inside).name.lower()
            if not basename.startswith(('license', 'licence', 'copying', 'notice')):
                continue
            source = Path(dist.locate_file(entry))
            if source.is_file():
                destination = f'third_party_licenses/python/{name}/{inside}'
                bundle.writestr(destination, source.read_bytes())
                included.append(destination)
                found = True
        if not found:
            raise RuntimeError(f'未找到 {name} 的许可证文件，无法生成完整的 Python 组件说明。')

    base = Path(sys.base_prefix)
    python_license = next((path for path in (base / 'LICENSE.txt', base / 'LICENSE_PYTHON.txt')
                           if path.is_file()), None)
    if python_license is None:
        raise RuntimeError('未找到 Python 运行环境的许可证文件。')
    destination = 'third_party_licenses/python-runtime/LICENSE.txt'
    bundle.writestr(destination, python_license.read_bytes())
    included.append(destination)

    archive = CArchiveReader(str(executable))
    for name in archive.toc:
        normalized = name.replace('\\', '/')
        basename = normalized.rsplit('/', 1)[-1].lower()
        if not any(marker in basename for marker in ('license', 'licence', 'notice', 'copying')):
            continue
        if any(part in {'', '.', '..'} for part in normalized.split('/')):
            raise RuntimeError('打包文件中存在不安全的许可证路径。')
        destination = f'third_party_licenses/bundled/{normalized}'
        bundle.writestr(destination, archive.extract(name))
        included.append(destination)
    return versions, included


def _snapshot_sources(directory: Path) -> dict[str, bytes]:
    # 作用：将白名单内的源码复制到隔离目录，并保留同一份字节供发行包使用。
    # 使用场景：构建前调用；即使工作区随后变化，exe 和源码也来自相同快照。
    sources: dict[str, bytes] = {}
    for name in SOURCE_FILES:
        path = ROOT / name
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(ROOT.resolve()):
            raise RuntimeError(f'发行包缺少预期源码文件：{name}')
        sources[name] = path.read_bytes()
        target = directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(sources[name])
    return sources


def _create_distribution(output: Path, executable: Path, sources: dict[str, bytes]) -> Path:
    # 作用：把程序、对应源码及许可证整理为一个便于分享的压缩包。
    # 使用场景：PyInstaller 构建成功后调用；sources 是构建前保存的源码快照。
    digest = sha256(executable.read_bytes()).hexdigest()
    destination = output / f'CSDN-Markdown-Helper-v{VERSION}-windows-x64.zip'
    temporary = destination.with_suffix('.zip.tmp')
    archive = CArchiveReader(str(executable))
    native_libraries = sorted(name for name in archive.toc if name.lower().endswith(('.dll', '.pyd')))
    with ZipFile(temporary, 'w', compression=ZIP_DEFLATED, compresslevel=6) as bundle:
        bundle.write(executable, EXE_NAME, compress_type=ZIP_STORED)
        for name in ('LICENSE', 'README.md', 'THIRD_PARTY_NOTICES.md'):
            bundle.writestr(name, sources[name])
        for name, data in sources.items():
            bundle.writestr(f'source/{name}', data)
        versions, license_files = _package_licenses(bundle, executable)
        info = {
            'version': VERSION,
            'python': platform.python_version(),
            'architecture': platform.machine(),
            'openssl': ssl.OPENSSL_VERSION,
            'zlib': zlib.ZLIB_RUNTIME_VERSION,
            'executable_sha256': digest,
            'python_packages': versions,
            'source_sha256': {name: sha256(data).hexdigest() for name, data in sources.items()},
            'included_license_files': license_files,
            'native_libraries': native_libraries,
        }
        bundle.writestr('BUILD_INFO.json', json.dumps(info, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(destination)  # 只有压缩包全部写完才生成正式文件。
    return destination


def main() -> int:
    # 作用：在隔离的路径环境中构建并整理 Windows x64 发行包。
    # 使用场景：由“打包助手.cmd”或构建虚拟环境直接调用。
    if os.name != 'nt' or struct.calcsize('P') != 8 or platform.machine().lower() not in {'amd64', 'x86_64'}:
        raise RuntimeError('请使用 Windows x64 的 64 位 Python 构建发行包。')
    output = _new_output_directory()
    build = ROOT / 'build' / f'release-v{VERSION}-{uuid4().hex[:8]}'
    build.mkdir(parents=True)
    source_root = build / 'source'
    sources = _snapshot_sources(source_root)
    work = build / 'work'
    command = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--onefile', '--windowed',
               '--log-level', 'WARN',
               '--name', Path(EXE_NAME).stem, '--manifest', str(source_root / 'dpi-aware.manifest'),
               '--distpath', str(output), '--workpath', str(work), '--specpath', str(build),
               '--paths', str(source_root), str(source_root / 'packaged_main.py')]
    print('正在构建单文件程序…', flush=True)
    subprocess.run(command, cwd=source_root, env=_build_environment(), check=True)
    analysis_path = work / Path(EXE_NAME).stem / 'Analysis-00.toc'
    _validate_binary_sources(analysis_path)
    _validate_project_sources(analysis_path, source_root)
    executable = output / EXE_NAME
    distribution = _create_distribution(output, executable, sources)
    print(f'程序：{executable}')
    print(f'发行压缩包：{distribution}')
    print('请在干净 Windows 环境验证，并核对第三方许可证后发布。')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError, metadata.PackageNotFoundError) as exc:
        print(f'发行包构建失败：{exc}', file=sys.stderr)
        raise SystemExit(1)
