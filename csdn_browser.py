"""Upload local images using the CSDN editor and repair a Markdown draft."""

from __future__ import annotations

import base64
from collections import deque
from dataclasses import dataclass
from datetime import datetime
import mimetypes
from pathlib import Path
import time
from urllib.parse import urlsplit
from uuid import uuid4

from md_images import find_images, local_path, rewrite


EDITOR_SELECTOR = 'pre.editor__inner[contenteditable="true"]'
CSDN_EDITOR_URL = 'https://editor.csdn.net/md/'
ALLOWED_IMAGE_HOSTS = {'i-blog.csdnimg.cn', 'img-blog.csdnimg.cn'}


class HelperError(Exception):
    pass


@dataclass(frozen=True)
class ProcessResult:
    image_count: int
    reference_count: int
    imported: bool
    backup_path: Path | None


def is_csdn_editor(page) -> bool:
    try:
        url = urlsplit(page.url)
        return url.scheme == 'https' and url.hostname == 'editor.csdn.net' and url.path.startswith('/md')
    except Exception:
        return False


def choose_target(context):
    candidates = [page for page in reversed(context.pages)
                  if not page.is_closed() and is_csdn_editor(page)]
    for page in candidates:
        try:
            if page.evaluate('document.visibilityState === "visible"'):
                return page
        except Exception:
            continue
    if candidates:
        return candidates[0]
    raise HelperError('没有找到 CSDN Markdown 编辑器。请先在助手打开的浏览器中登录，并打开要处理的文章。')


def editor_text(page) -> str:
    try:
        page.locator(EDITOR_SELECTOR).wait_for(state='attached', timeout=15_000)
        return page.locator(EDITOR_SELECTOR).evaluate('(element) => element.textContent || ""')
    except Exception as exc:
        raise HelperError('没有找到可编辑的 Markdown 正文。请确认已登录 CSDN，并切换到 Markdown 编辑器。') from exc


def has_meaningful_text(text: str) -> bool:
    """Ignore zero-width editor markers as well as ordinary whitespace."""
    invisible = dict.fromkeys(map(ord, '\ufeff\u200b\u200c\u200d\u2060'), None)
    return bool(text.translate(invisible).strip())


def collect_local_images(body: str, markdown_path: Path):
    refs_by_path: dict[Path, list] = {}
    missing: list[Path] = []
    for ref in find_images(body):
        try:
            path = local_path(ref.source, markdown_path)
            if path is None:
                continue
            if not path.is_file():
                if path not in missing:
                    missing.append(path)
                continue
            refs_by_path.setdefault(path, []).append(ref)
        except (OSError, ValueError) as exc:
            raise HelperError(f'无法读取图片路径：{ref.source}\n{exc}') from exc
    if missing:
        sample = '\n'.join(str(path) for path in missing[:10])
        suffix = f'\n……还有 {len(missing) - 10} 个' if len(missing) > 10 else ''
        raise HelperError(f'找到 {len(missing)} 个不存在的本地图片，未开始上传：\n{sample}{suffix}')
    if not refs_by_path:
        raise HelperError('当前文章没有识别到本地图片。请检查正文中的图片语法和路径。')
    return refs_by_path


def _response_may_contain_upload(response, extra_hosts: set[str]) -> bool:
    try:
        host = (urlsplit(response.url).hostname or '').lower()
        trusted = (host in extra_hosts or host == 'csdn.net' or host.endswith('.csdn.net')
                   or host == 'csdnimg.cn' or host.endswith('.csdnimg.cn')
                   or host == 'myhuaweicloud.com' or host.endswith('.myhuaweicloud.com'))
        return trusted and response.request.method == 'POST' and response.ok
    except Exception:
        return False


def _valid_image_url(value) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urlsplit(value)
    if parsed.scheme == 'https' and parsed.hostname in ALLOWED_IMAGE_HOSTS:
        return value
    return None


def _from_response(response) -> str | None:
    try:
        obj = response.json()
        if not isinstance(obj, dict):
            return None
        data = obj.get('data')
        return _valid_image_url(data.get('imageUrl')) if isinstance(data, dict) else None
    except Exception:
        return None


def _paste_file(page, path: Path) -> None:
    mime = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
    payload = base64.b64encode(path.read_bytes()).decode('ascii')
    page.locator(EDITOR_SELECTOR).evaluate('''(editor, data) => {
        editor.focus();
        const bytes = Uint8Array.from(atob(data.base64), c => c.charCodeAt(0));
        const file = new File([bytes], data.name, {type: data.mime});
        const transfer = new DataTransfer();
        transfer.items.add(file);
        editor.dispatchEvent(new ClipboardEvent('paste', {
            clipboardData: transfer, bubbles: true, cancelable: true
        }));
    }''', {'base64': payload, 'name': path.name, 'mime': mime})


def _replace_editor_text(page, text: str) -> None:
    page.locator(EDITOR_SELECTOR).evaluate('''(editor, text) => {
        editor.focus();
        const range = document.createRange();
        range.selectNodeContents(editor);
        const selection = window.getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
        document.execCommand('delete');
        const transfer = new DataTransfer();
        transfer.setData('text/plain', text);
        editor.dispatchEvent(new ClipboardEvent('paste', {
            clipboardData: transfer, bubbles: true, cancelable: true
        }));
    }''', text)


def _equivalent_editor_text(actual: str, expected: str) -> bool:
    # The CSDN editor may add its own final line break during highlighting.
    return actual.replace('\r\n', '\n').rstrip('\n') == expected.replace('\r\n', '\n').rstrip('\n')


def _wait_for_text(page, expected: str, timeout_ms: int = 5000) -> bool:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if _equivalent_editor_text(editor_text(page), expected):
            return True
        page.wait_for_timeout(150)
    return False


def upload_one(scratch, path: Path, pending: deque, current_requests: set, *, timeout_ms: int = 60_000,
               extra_hosts: set[str] | None = None) -> str:
    extra_hosts = extra_hosts or set()
    # A scratch tab has no user content; reset it before every upload so the
    # editor's asynchronous image insertion cannot overwrite the previous one.
    _replace_editor_text(scratch, '')
    scratch.wait_for_timeout(200)
    pending.clear()
    current_requests.clear()
    _paste_file(scratch, path)
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        scratch.wait_for_timeout(120)
        while pending:
            response = pending.popleft()
            if _response_may_contain_upload(response, extra_hosts):
                url = _from_response(response)
                if url:
                    return url
    raise HelperError(f'图片上传失败或超时：{path.name}\n为避免错配，已停止处理，原文章未替换。')


def process_article(target, scratch, markdown_path: Path, recovery_dir: Path,
                    notify=lambda message: None, *, timeout_ms: int = 60_000,
                    extra_hosts: set[str] | None = None, mode: str = 'auto',
                    expected_before: str | None = None) -> ProcessResult:
    """Upload in a separate blank tab; replace target only after every upload."""
    try:
        md_text = markdown_path.read_text(encoding='utf-8-sig')
    except (OSError, UnicodeDecodeError) as exc:
        raise HelperError(f'无法读取 Markdown 文件（请使用 UTF-8 编码）：{exc}') from exc

    before = editor_text(target)
    if expected_before is not None and not _equivalent_editor_text(before, expected_before):
        raise HelperError('处理前编辑器内容发生变化，未开始上传。请检查后重试。')
    if mode not in {'auto', 'import', 'repair'}:
        raise HelperError('未知的正文处理方式。')
    imported = mode == 'import' or (mode == 'auto' and not has_meaningful_text(before))
    body = md_text if imported else before
    try:
        refs_by_path = collect_local_images(body, markdown_path)
    except HelperError as exc:
        if str(exc).startswith('当前文章没有识别到本地图片') and not imported:
            raise HelperError('当前编辑器正文中没有识别到本地图片。若要直接发布所选 Markdown，'
                              '请在助手中选择“导入所选 Markdown”。') from exc
        raise
    count = len(refs_by_path)
    occurrences = sum(len(refs) for refs in refs_by_path.values())
    notify(f'找到 {count} 张不同图片，共 {occurrences} 处引用。')

    # Abort rather than clear any content unexpectedly restored into the tab.
    if has_meaningful_text(editor_text(scratch)):
        raise HelperError('临时编辑器不是空白，为保护内容已停止处理。')

    pending: deque = deque()
    current_requests: set = set()

    def on_request(request):
        if request.method == 'POST':
            current_requests.add(request)

    def on_response(response):
        if response.request in current_requests:
            pending.append(response)

    scratch.on('request', on_request)
    scratch.on('response', on_response)
    replacements = []
    try:
        for index, (path, refs) in enumerate(refs_by_path.items(), 1):
            notify(f'正在上传 {index}/{count}：{path.name}')
            url = upload_one(scratch, path, pending, current_requests,
                             timeout_ms=timeout_ms, extra_hosts=extra_hosts)
            replacements.extend((ref, url) for ref in refs)
        result = rewrite(body, replacements)
        notify('图片已上传，正在检查编辑器内容。')
        if not _equivalent_editor_text(editor_text(target), before):
            raise HelperError('处理期间编辑器内容发生变化。为避免覆盖你的修改，未替换正文；已上传的图片仍保留在 CSDN。')

        backup_path = None
        if has_meaningful_text(before):
            recovery_dir.mkdir(parents=True, exist_ok=True)
            backup_path = recovery_dir / f'编辑器备份_{datetime.now():%Y%m%d_%H%M%S}_{uuid4().hex[:6]}.md'
            backup_path.write_text(before, encoding='utf-8')
        notify('正在替换正文中的图片链接。')
        try:
            _replace_editor_text(target, result)
            written = _wait_for_text(target, result)
        except Exception:
            written = False
        if not written:
            # Do not silently declare success if the site changes its editor.
            try:
                _replace_editor_text(target, before)
                restored = _wait_for_text(target, before)
            except Exception:
                restored = False
            note = '已尝试恢复原正文。' if restored else '未能确认原正文恢复成功。'
            if backup_path:
                note += f' 本地备份：{backup_path}'
            raise HelperError(f'CSDN 编辑器未确认写入结果，{note}')
        return ProcessResult(count, occurrences, imported, backup_path)
    finally:
        scratch.remove_listener('request', on_request)
        scratch.remove_listener('response', on_response)
