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

from md_images import (find_images, local_path, render_image_progress,
                       restore_image_placeholders)


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
    if len(candidates) > 1:
        raise HelperError('检测到多个 CSDN Markdown 编辑标签页。CSDN 可能同步它们的草稿，'
                          '请在助手浏览器中只保留要处理的文章，再重试。')
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


def scan_local_images(body: str, markdown_path: Path):
    refs_by_path: dict[Path, list] = {}
    missing: list[Path] = []
    for ref in find_images(body):
        try:
            path = local_path(ref.source, markdown_path)
            if path is None:
                continue
            refs_by_path.setdefault(path, []).append(ref)
            if not path.is_file():
                if path not in missing:
                    missing.append(path)
        except (OSError, ValueError) as exc:
            raise HelperError(f'无法读取图片路径：{ref.source}\n{exc}') from exc
    return refs_by_path, missing


def collect_local_images(body: str, markdown_path: Path):
    refs_by_path, missing = scan_local_images(body, markdown_path)
    if missing:
        sample = '\n'.join(str(path) for path in missing[:10])
        suffix = f'\n……还有 {len(missing) - 10} 个' if len(missing) > 10 else ''
        raise HelperError(f'找到 {len(missing)} 个不存在的本地图片，未开始上传：\n{sample}{suffix}')
    if not refs_by_path:
        raise HelperError('当前文章没有识别到本地图片。请检查正文中的图片语法和路径。')
    return refs_by_path


def _protect_local_images(body: str, markdown_path: Path) -> str:
    """Keep local image paths inert when restoring an editor snapshot."""
    restored = restore_image_placeholders(body)
    refs_by_path, _ = scan_local_images(restored, markdown_path)
    refs = [ref for images in refs_by_path.values() for ref in images]
    return render_image_progress(restored, refs, [])


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
        const range = document.createRange();
        range.selectNodeContents(editor);
        range.collapse(false);
        const selection = window.getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
        const bytes = Uint8Array.from(atob(data.base64), c => c.charCodeAt(0));
        const file = new File([bytes], data.name, {type: data.mime});
        const transfer = new DataTransfer();
        transfer.items.add(file);
        editor.dispatchEvent(new ClipboardEvent('paste', {
            clipboardData: transfer, bubbles: true, cancelable: true
        }));
    }''', {'base64': payload, 'name': path.name, 'mime': mime})


def _replace_editor_text(page, text: str) -> bool:
    editor = page.locator(EDITOR_SELECTOR)
    replaced = editor.evaluate('''(editor, text) => {
        if (window.__csdnMdHelperGuard?.interacted) return false;
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
        return true;
    }''', text)
    if not replaced:
        return False
    _remove_extra_leading_newlines(page, text)
    return True


def _remove_extra_leading_newlines(page, text: str) -> None:
    # CSDN can leave extra newlines before pasted text after successive image
    # uploads. Remove them only when the remaining text matches exactly,
    # preserving any leading blank lines already present in the requested text.
    page.locator(EDITOR_SELECTOR).evaluate(r'''(editor, expected) => {
        if (window.__csdnMdHelperGuard?.interacted) return;
        const normalize = value => value.replace(/\r\n/g, '\n').replace(/\n+$/, '');
        const current = editor.textContent || '';
        if (normalize(current) === normalize(expected)) return;
        let extra = 0;
        while (current[extra] === '\n') {
            extra++;
            if (normalize(current.slice(extra)) === normalize(expected)) break;
        }
        if (!extra || normalize(current.slice(extra)) !== normalize(expected)) return;
        const walker = document.createTreeWalker(editor, NodeFilter.SHOW_TEXT);
        const range = document.createRange();
        let node, first = true, remaining = extra;
        while ((node = walker.nextNode()) && remaining) {
            if (!node.data.length) continue;
            const take = Math.min(remaining, node.data.length);
            if (node.data.slice(0, take) !== '\n'.repeat(take)) return;
            if (first) { range.setStart(node, 0); first = false; }
            remaining -= take;
            if (!remaining) range.setEnd(node, take);
        }
        if (remaining) return;
        const selection = window.getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
        document.execCommand('delete');
    }''', text)


def _equivalent_editor_text(actual: str, expected: str) -> bool:
    # The CSDN editor may add its own final line break during highlighting.
    return actual.replace('\r\n', '\n').rstrip('\n') == expected.replace('\r\n', '\n').rstrip('\n')


def _wait_for_text(page, expected: str, timeout_ms: int = 5000) -> bool:
    deadline = time.monotonic() + timeout_ms / 1000
    matching_since = None
    while time.monotonic() < deadline:
        actual = editor_text(page)
        if _equivalent_editor_text(actual, expected):
            if matching_since is None:
                matching_since = time.monotonic()
            elif time.monotonic() - matching_since >= 0.3:
                return True
        else:
            matching_since = None
            # Rendering may insert the extra newlines after the initial paste.
            # Correct only a prefix-only difference, then verify the full text.
            normalized_actual = actual.replace('\r\n', '\n').rstrip('\n')
            normalized_expected = expected.replace('\r\n', '\n').rstrip('\n')
            extra = len(normalized_actual) - len(normalized_expected)
            if (extra > 0 and normalized_actual[:extra] == '\n' * extra
                    and normalized_actual[extra:] == normalized_expected):
                _remove_extra_leading_newlines(page, expected)
        page.wait_for_timeout(150)
    return False


def _start_edit_guard(page) -> None:
    page.evaluate('''() => {
        const state = { interacted: false };
        const handler = event => { if (event.isTrusted) state.interacted = true; };
        state.events = ['keydown', 'pointerdown', 'paste', 'cut', 'drop'];
        state.handler = handler;
        for (const name of state.events) document.addEventListener(name, handler, true);
        window.__csdnMdHelperGuard = state;
    }''')


def _user_interacted(page) -> bool:
    return bool(page.evaluate('() => !!window.__csdnMdHelperGuard?.interacted'))


def _stop_edit_guard(page) -> None:
    try:
        page.evaluate('''() => {
            const state = window.__csdnMdHelperGuard;
            if (!state) return;
            for (const name of state.events) document.removeEventListener(name, state.handler, true);
            delete window.__csdnMdHelperGuard;
        }''')
    except Exception:
        pass


def _ensure_editor_unchanged(page, expected: str | None = None) -> None:
    if _user_interacted(page):
        raise HelperError('检测到编辑器内容发生变化，已停止处理并保留当前正文；请检查后重试。')
    others = [other for other in page.context.pages
              if other != page and not other.is_closed() and is_csdn_editor(other)]
    if others:
        raise HelperError('检测到其他 CSDN Markdown 编辑标签页，已停止处理并保留当前正文；'
                          '请关闭其他标签页后重试。')
    if expected is not None and not _equivalent_editor_text(editor_text(page), expected):
        raise HelperError('检测到编辑器内容发生变化，已停止处理并保留当前正文；请检查后重试。')


def _write_verified(page, text: str, previous: str, backup_path: Path | None) -> None:
    _ensure_editor_unchanged(page)
    try:
        if not _replace_editor_text(page, text):
            raise HelperError('检测到编辑器内容发生变化，已停止处理并保留当前正文；请检查后重试。')
        written = _wait_for_text(page, text)
    except HelperError:
        raise
    except Exception:
        written = False
    if written:
        return
    _ensure_editor_unchanged(page)
    try:
        restored = _replace_editor_text(page, previous) and _wait_for_text(page, previous)
    except Exception:
        restored = False
    note = '已尝试恢复上一步正文。' if restored else '未能确认正文恢复成功。'
    if backup_path:
        note += f' 本地备份：{backup_path}'
    raise HelperError(f'CSDN 编辑器未确认写入结果，{note}')


def upload_one(page, path: Path, pending: deque, current_requests: set, *, timeout_ms: int = 60_000,
               extra_hosts: set[str] | None = None) -> str:
    extra_hosts = extra_hosts or set()
    before = editor_text(page)
    pending.clear()
    current_requests.clear()
    _paste_file(page, path)
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        page.wait_for_timeout(120)
        while pending:
            response = pending.popleft()
            if _response_may_contain_upload(response, extra_hosts):
                url = _from_response(response)
                if url:
                    # CSDN may update the editor after the network response.
                    # Wait for that update before reconstructing the article,
                    # otherwise a late insertion can corrupt the next write.
                    insertion_deadline = time.monotonic() + min(timeout_ms / 1000, 10)
                    while time.monotonic() < insertion_deadline:
                        current = editor_text(page)
                        if url in current and (url not in before or current != before):
                            return url
                        page.wait_for_timeout(120)
                    raise HelperError(f'图片已上传，但未能确认 CSDN 完成插入：{path.name}')
    raise HelperError(f'图片上传失败或超时：{path.name}')


def process_article(target, markdown_path: Path, recovery_dir: Path,
                    notify=lambda message: None, *, timeout_ms: int = 60_000,
                    extra_hosts: set[str] | None = None, mode: str = 'auto',
                    expected_before: str | None = None,
                    backup_existing: bool = True) -> ProcessResult:
    """Import text first, then upload and replace each image in the same tab."""
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
    body = md_text if imported else restore_image_placeholders(before)
    refs_by_path, missing = scan_local_images(body, markdown_path)
    local_refs = [ref for refs in refs_by_path.values() for ref in refs]
    # CSDN eagerly rewrites pasted local image paths to failure placeholders.
    # Temporarily break only those image markers while preserving the text and
    # original paths, then restore each marker when its CSDN URL is available.
    working = render_image_progress(body, local_refs, [])
    safe_before = _protect_local_images(before, markdown_path)
    backup_path = None
    if backup_existing and has_meaningful_text(before):
        recovery_dir.mkdir(parents=True, exist_ok=True)
        backup_path = recovery_dir / f'编辑器备份_{datetime.now():%Y%m%d_%H%M%S}_{uuid4().hex[:6]}.md'
        backup_path.write_text(before, encoding='utf-8')

    _start_edit_guard(target)
    pending: deque = deque()
    current_requests: set = set()

    def on_request(request):
        if request.method == 'POST':
            current_requests.add(request)

    def on_response(response):
        if response.request in current_requests:
            pending.append(response)

    target.on('request', on_request)
    target.on('response', on_response)
    try:
        if imported:
            notify('正在导入完整 Markdown 正文。')
            _write_verified(target, working, safe_before, backup_path)
            notify('正文已导入，开始处理图片。')
        elif working != before and not missing:
            notify('正在保护尚未上传的本地图片链接。')
            _write_verified(target, working, safe_before, backup_path)

        if missing:
            sample = '\n'.join(str(path) for path in missing[:10])
            suffix = f'\n……还有 {len(missing) - 10} 个' if len(missing) > 10 else ''
            prefix = '正文已导入，但' if imported else ''
            raise HelperError(f'{prefix}找到 {len(missing)} 个不存在的本地图片，未开始上传：\n{sample}{suffix}')
        if not refs_by_path:
            if imported:
                notify('正文已导入，文档中没有需要上传的本地图片。')
                return ProcessResult(0, 0, True, backup_path)
            raise HelperError('当前编辑器正文中没有识别到本地图片。若要直接发布所选 Markdown，'
                              '请在助手中选择“导入所选 Markdown”。')
        count = len(refs_by_path)
        occurrences = sum(len(refs) for refs in refs_by_path.values())
        notify(f'找到 {count} 张不同图片，共 {occurrences} 处引用。')

        replacements = []
        for index, (path, refs) in enumerate(refs_by_path.items(), 1):
            notify(f'正在上传 {index}/{count}：{path.name}')
            _ensure_editor_unchanged(target, working)
            try:
                url = upload_one(target, path, pending, current_requests,
                                 timeout_ms=timeout_ms, extra_hosts=extra_hosts)
            except Exception as exc:
                _ensure_editor_unchanged(target)
                _write_verified(target, working, working, backup_path)
                raise HelperError(f'正文已保留，已完成 {index - 1}/{count} 张图片。{exc}\n'
                                  '请检查正文；可选择“仅修复当前正文”继续处理剩余图片。') from exc
            _ensure_editor_unchanged(target)
            replacements.extend((ref, url) for ref in refs)
            updated = render_image_progress(body, local_refs, replacements)
            _write_verified(target, updated, working, backup_path)
            working = updated
            notify(f'已替换 {index}/{count} 张图片的链接。')
        return ProcessResult(count, occurrences, imported, backup_path)
    finally:
        target.remove_listener('request', on_request)
        target.remove_listener('response', on_response)
        _stop_edit_guard(target)
