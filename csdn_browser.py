"""Upload local images using the CSDN editor and repair a Markdown draft."""

# 阅读入口是 process_article：选择正文、保护本地图片、逐张上传并验证写回。
from __future__ import annotations

import base64  # 将本地图片字节编码后传入浏览器页面。
from bisect import bisect_left  # 将图片字符位置快速转换为正文行号。
from collections import deque  # 按顺序缓存页面收到的网络响应。
from dataclasses import dataclass  # 定义结构化的处理结果。
from datetime import datetime  # 为正文备份生成时间标识。
from email import policy  # 按标准解析 multipart 表单中的文件字段。
from email.parser import BytesParser  # 从请求字节中提取每个表单部分。
import mimetypes  # 根据图片扩展名推测文件类型。
from pathlib import Path  # 表示文档、图片和备份路径。
import time  # 使用单调时钟计算等待时长。
from urllib.parse import urlsplit  # 校验编辑器与上传响应的域名。
from uuid import uuid4  # 为备份文件补充随机后缀，降低重名概率。

from md_images import (find_images, local_path, render_image_progress,
                       restore_image_placeholders)


# 编辑器正文的选择器，以及允许作为上传结果使用的 CSDN 图片域名。
# 网页结构或 CSDN 返回格式变化时，相关适配逻辑需要一起检查。
EDITOR_SELECTOR = 'pre.editor__inner[contenteditable="true"]'  # 定位 Markdown 编辑区域。
CSDN_EDITOR_URL = 'https://editor.csdn.net/md/'  # 助手启动浏览器时打开的编辑页。
ALLOWED_IMAGE_HOSTS = {'i-blog.csdnimg.cn', 'img-blog.csdnimg.cn'}  # 允许写入正文的图片域名。


# 预期内、可以直接展示给用户的处理错误。
class HelperError(Exception):
    # 作用：表示可以直接展示给用户的处理错误。
    # 使用场景：图片缺失、编辑器不可用或保护机制中止时抛出。
    pass


class ProcessingCancelled(HelperError):
    # 作用：区分用户主动停止与上传错误。
    # 使用场景：在正文安全检查点收到停止请求时抛出，由界面显示停止结果。
    pass


@dataclass(frozen=True)
class PreflightImage:
    # 作用：保存一张本地图片的路径、所有引用行号和缺失状态。
    # 使用场景：处理前向用户展示，也用于确认之后检查图片状态是否发生变化。
    path: Path
    lines: tuple[int, ...]
    missing: bool


@dataclass(frozen=True)
class ImagePreflight:
    # 作用：保存一次正文检查的完整图片清单和引用总数。
    # 使用场景：在后台线程与桌面界面之间传递不可变的检查结果。
    images: tuple[PreflightImage, ...]
    reference_count: int


@dataclass(frozen=True)
class ProcessResult:
    # 作用：保存文章处理后的图片数量、引用数量和备份位置。
    # 使用场景：process_article 成功后返回，供界面显示结果。
    # 图片文件数与引用数可能不同：同一文件可以在文章里出现多次。
    image_count: int  # 实际处理的不同本地图片文件数。
    reference_count: int  # 正文中这些文件出现的引用次数。
    imported: bool  # True 表示导入了所选 Markdown，False 表示修复原正文。
    backup_path: Path | None  # 已创建的备份路径；没有备份时为 None。


def is_csdn_editor(page) -> bool:
    # 作用：判断页面是否为官方 CSDN Markdown 编辑器。
    # 使用场景：选择目标页面和检查其他编辑标签时调用。
    # 限定 HTTPS、官方编辑器域名和 Markdown 路径，不误操作其他页面。
    try:
        url = urlsplit(page.url)  # 分别读取协议、主机名和路径。
        return url.scheme == 'https' and url.hostname == 'editor.csdn.net' and url.path.startswith('/md')
    except Exception:
        return False  # 页面关闭或地址不可解析时，不把它当作编辑器。


def choose_target(context):
    # 作用：从浏览器上下文中选出唯一目标编辑页。
    # 使用场景：处理文章前调用；多个候选页面时停止以避免误操作。
    # 多个编辑标签可能同步同一草稿；有歧义时停止而不是猜测目标页面。
    # 从当前上下文中过滤未关闭的 CSDN Markdown 页面。
    candidates = [page for page in reversed(context.pages)
                  if not page.is_closed() and is_csdn_editor(page)]
    if len(candidates) > 1:  # 多个候选页可能互相同步，不能任意选择。
        raise HelperError('检测到多个 CSDN Markdown 编辑标签页。CSDN 可能同步它们的草稿，'
                          '请在助手浏览器中只保留要处理的文章，再重试。')
    if candidates:  # 已排除多页，此时非空代表只有一个目标。
        return candidates[0]  # 经过多标签检查后，列表中只剩一个候选页面。
    raise HelperError('没有找到 CSDN Markdown 编辑器。请先在助手打开的浏览器中登录，并打开要处理的文章。')  # 无候选页。


def editor_text(page) -> str:
    # 作用：等待并读取可编辑区域中的 Markdown 正文。
    # 使用场景：读取初始快照、检查内容和验证写入时调用；失败抛出 HelperError。
    # 读取可编辑区域的 Markdown 文本，而不是右侧渲染后的预览。
    try:
        page.locator(EDITOR_SELECTOR).wait_for(state='attached', timeout=15_000)  # 最多等待 15 秒。
        return page.locator(EDITOR_SELECTOR).evaluate('(element) => element.textContent || ""')  # 返回源文本。
    except Exception as exc:
        # 把页面不可访问或选择器失效转换为用户可以处理的错误。
        raise HelperError('没有找到可编辑的 Markdown 正文。请确认已登录 CSDN，并切换到 Markdown 编辑器。') from exc


def has_meaningful_text(text: str) -> bool:
    # 作用：判断正文是否包含可见的有效内容。
    # 使用场景：自动选择导入方式、询问覆盖和决定备份时调用。
    """Ignore zero-width editor markers as well as ordinary whitespace."""
    # 零宽标记和空白不算有效正文，避免把空编辑器误判成已有文章。
    invisible = dict.fromkeys(map(ord, '\ufeff\u200b\u200c\u200d\u2060'), None)  # 建立不可见字符删除表。
    return bool(text.translate(invisible).strip())  # 删除这些字符和首尾空白后判断是否仍有内容。


def scan_local_images(body: str, markdown_path: Path):
    # 作用：按本地文件路径分组图片引用，并收集缺失文件。
    # 使用场景：上传前创建处理清单；返回路径到引用的字典及缺失文件列表。
    # 按解析后的本地文件路径分组：同一张图可对应多处 ImageRef。
    # 同时记录缺失文件，但由调用方决定何时报告和是否保留已导入的正文。
    refs_by_path: dict[Path, list] = {}  # 每个规范化文件路径对应正文中的全部引用。
    missing: list[Path] = []  # 保存不重复的缺失图片路径。
    for ref in find_images(body):
        try:
            path = local_path(ref.source, markdown_path)  # 按 Markdown 所在目录解析本地路径。
            if path is None:
                continue  # 远程或内嵌资源不需要上传。
            refs_by_path.setdefault(path, []).append(ref)  # 重复引用加入同一文件分组。
            if not path.is_file():
                if path not in missing:  # 重复引用同一缺图也只登记一次。
                    missing.append(path)  # 同一张缺图只报告一次。
        except (OSError, ValueError) as exc:
            # 路径无效或操作系统无法解析时，附上原始引用方便排查。
            raise HelperError(f'无法读取图片路径：{ref.source}\n{exc}') from exc
    return refs_by_path, missing  # 由调用者决定如何处理缺图。


def read_markdown(markdown_path: Path) -> str:
    # 作用：按统一的编码读取 Markdown，并将读取错误转为用户可理解的提示。
    # 使用场景：文件预检和正式处理均调用，避免两处对编码的判断不一致。
    try:
        return markdown_path.read_text(encoding='utf-8-sig')
    except (OSError, UnicodeDecodeError) as exc:
        raise HelperError(f'无法读取 Markdown 文件（请使用 UTF-8 编码）：{exc}') from exc


def image_preflight(body: str, markdown_path: Path, *, refs_by_path=None, missing=None) -> ImagePreflight:
    # 作用：将正文中的本地图片整理为去重后的路径、引用行号和缺失状态。
    # 使用场景：处理前检查文件或编辑器正文；正式处理时可复用已扫描结果。
    if refs_by_path is None or missing is None:
        refs_by_path, missing = scan_local_images(body, markdown_path)
    missing_paths = set(missing)
    newline_positions = [index for index, character in enumerate(body) if character == '\n']
    images = []
    for path, refs in refs_by_path.items():
        # placeholder_at 紧跟图片起始标记；引用式及多行 HTML 要显示标记所在行。
        positions = (ref.placeholder_at - 1 if ref.placeholder_at is not None else ref.start for ref in refs)
        lines = tuple(bisect_left(newline_positions, position) + 1 for position in positions)
        images.append(PreflightImage(path, lines, path in missing_paths))
    return ImagePreflight(tuple(images), sum(len(refs) for refs in refs_by_path.values()))


def collect_local_images(body: str, markdown_path: Path):
    # 作用：严格检查图片清单并报告缺图或无图片。
    # 使用场景：调用方需要在处理前确认至少有一张存在的本地图片时使用。
    # 严格检查模式：缺图或没有本地图片时直接给出可读错误。
    refs_by_path, missing = scan_local_images(body, markdown_path)  # 先取得完整扫描结果。
    if missing:
        sample = '\n'.join(str(path) for path in missing[:10])  # 最多列出前 10 个缺失路径。
        suffix = f'\n……还有 {len(missing) - 10} 个' if len(missing) > 10 else ''  # 提示剩余数量。
        raise HelperError(f'找到 {len(missing)} 个不存在的本地图片，未开始上传：\n{sample}{suffix}')
    if not refs_by_path:  # 没有任何本地图片也不满足严格检查要求。
        raise HelperError('当前文章没有识别到本地图片。请检查正文中的图片语法和路径。')
    return refs_by_path  # 至少一张图片存在且没有缺图，返回分组清单。


def _protect_local_images(body: str, markdown_path: Path) -> str:
    # 作用：为正文中的本地图片插入可恢复的保护标记。
    # 使用场景：恢复先前正文时调用，避免编辑器提前改写本地图片。
    """Keep local image paths inert when restoring an editor snapshot."""
    # 恢复旧正文时也要保护本地图片，避免编辑器把它们自动替换成失败图片。
    restored = restore_image_placeholders(body)  # 先还原可能存在的旧保护标记，方便重新扫描。
    refs_by_path, _ = scan_local_images(restored, markdown_path)  # 找到所有本地图片引用。
    refs = [ref for images in refs_by_path.values() for ref in images]  # 将分组结果展开成列表。
    return render_image_progress(restored, refs, [])  # 空替换表表示所有本地图片都先保护。


def _response_may_contain_upload(response, extra_hosts: set[str]) -> bool:
    # 作用：筛选可信域名返回的 POST 响应，包括失败状态。
    # 使用场景：等待上传结果时调用；extra_hosts 用于允许本地测试地址。
    # 先检查来源；状态和请求是否属于当前图片由调用方继续判断。
    try:
        host = (urlsplit(response.url).hostname or '').lower()  # 规范化响应域名便于比较。
        # CSDN 上传可能经过 CSDN 域名、图片域名或华为云域名。
        trusted = (host in extra_hosts or host == 'csdn.net' or host.endswith('.csdn.net')
                   or host == 'csdnimg.cn' or host.endswith('.csdnimg.cn')
                   or host == 'myhuaweicloud.com' or host.endswith('.myhuaweicloud.com'))
        return trusted and response.request.method == 'POST'  # 失败响应也需要交给后续图片匹配判断。
    except Exception:
        return False  # 响应不可读取时按不可信处理。


def _valid_image_url(value) -> str | None:
    # 作用：检查候选图片地址的类型、HTTPS 协议和域名。
    # 使用场景：读取上传响应后调用；不符合要求时返回 None。
    # 返回的图片地址还需单独检查协议和域名，不能把任意响应内容写入正文。
    if not isinstance(value, str):  # 上传结果必须是字符串地址。
        return None
    parsed = urlsplit(value)  # 不能仅靠字符串前缀判断协议和真实主机名。
    if parsed.scheme == 'https' and parsed.hostname in ALLOWED_IMAGE_HOSTS:
        return value  # 协议和图片域名均符合要求。
    return None  # 其他协议或域名不作为有效图片结果。


def _from_response(response) -> str | None:
    # 作用：从网络响应的 data.imageUrl 中提取并校验地址。
    # 使用场景：逐张上传时解析候选响应；格式不符或读取失败返回 None。
    # 从 CSDN 上传响应的 data.imageUrl 提取地址；格式不符则继续等待。
    try:
        obj = response.json()  # 解析网页返回的 JSON 响应。
        if not isinstance(obj, dict):
            return None  # 响应顶层不是对象，不符合预期格式。
        data = obj.get('data')  # CSDN 的图片地址位于 data 对象中。
        return _valid_image_url(data.get('imageUrl')) if isinstance(data, dict) else None  # 继续校验地址。
    except Exception:
        return None  # JSON 解析失败或响应不可读取时忽略该响应。


def _request_contains_image(request, path: Path) -> bool:
    # 作用：保守判断失败请求是否包含本轮上传的图片字节。
    # 使用场景：只对可关联的失败响应报告 HTTP 状态，避免误判页面其他请求。
    try:
        content_type = request.headers.get('content-type', '')
        media_type = content_type.split(';', 1)[0].strip().lower()
        body = request.post_data_buffer
        data = path.read_bytes()
        if not body or not data:
            return False  # 无法读取请求体时不能确认归属。
        if media_type.startswith('image/') or media_type == 'application/octet-stream':
            return body == data  # 直接上传时，完整请求体必须等于图片内容。
        if media_type == 'multipart/form-data':
            # 保留 boundary 原始大小写，并仅检查文件字段，不能把普通文本字段误认作图片。
            header = b'MIME-Version: 1.0\r\nContent-Type: ' + content_type.encode('ascii') + b'\r\n\r\n'
            message = BytesParser(policy=policy.default).parsebytes(header + body)
            return message.is_multipart() and any(
                part.get_filename() is not None and part.get_payload(decode=True) == data
                for part in message.iter_parts()
            )
        return False
    except Exception:
        return False  # 浏览器无法提供请求体时不猜测归属。


def _request_may_contain_image(request, path: Path) -> bool:
    # 作用：保守识别图片或表单类型的可能上传请求，但不把它当成确定证据。
    # 使用场景：浏览器对 File/Blob 不暴露请求体时，记录可能相关的失败状态。
    try:
        content_type = request.headers.get('content-type', '').lower().split(';', 1)[0].strip()
        mime = (mimetypes.guess_type(path.name)[0] or '').lower()
        return ((bool(mime) and content_type == mime and content_type.startswith('image/'))
                or content_type == 'multipart/form-data')
    except Exception:
        return False


def _http_upload_error(status: int, path: Path) -> str:
    # 作用：为已确认属于当前图片的 HTTP 失败生成不含敏感响应体的提示。
    # 使用场景：上传接口返回错误状态时，告诉用户下一步检查什么。
    if status in (401, 403):
        detail = '可能登录过期或账号没有上传权限，请检查 CSDN 登录状态。'
    elif status == 413:
        detail = '服务可能认为图片过大，请压缩图片后重试。'
    elif status == 429:
        detail = '上传可能过于频繁，请稍后重试。'
    elif status >= 500:
        detail = '上传服务暂时不可用，请稍后重试。'
    else:
        detail = '上传服务拒绝了该图片，请检查文件和网络后重试。'
    return f'图片上传失败（HTTP {status}）：{path.name}。{detail}'


def _check_cancelled(cancel_event, completed: int, total: int, *, before_write: bool = False) -> None:
    # 作用：在安全检查点响应停止请求，说明已处理数量和后续操作。
    # 使用场景：写入前或某张图片已替换完成后调用，不打断进行中的上传。
    if cancel_event is None or not cancel_event.is_set():
        return
    if before_write:
        raise ProcessingCancelled('已停止：尚未修改正文或上传图片。')
    raise ProcessingCancelled(f'已安全停止，已完成 {completed}/{total} 张图片。\n'
                              '请检查当前正文；可选择“仅修复图片”继续处理剩余图片。')


def _paste_file(page, path: Path) -> None:
    # 作用：在编辑页模拟一次包含本地文件的粘贴操作。
    # 使用场景：上传每张图片时调用；page 为页面，path 为要读取的文件。
    # 将本地文件编码后传到页面，构造文件粘贴事件，让 CSDN 自己执行上传。
    # 光标先移动到正文末尾，避免把文件插入当前选中的文字中间。
    mime = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'  # 未识别类型时使用通用二进制。
    payload = base64.b64encode(path.read_bytes()).decode('ascii')  # 文件字节转成页面可接收的文本。
    # 以下页面脚本依次完成：
    # 1. 聚焦编辑区，创建文本范围并折叠到末尾，避免替换用户原有选区。
    # 2. 把 base64 解码为字节数组，使用文件名和 MIME 类型构造 File。
    # 3. 把 File 放进 DataTransfer，派发 paste 事件交给 CSDN 的上传逻辑。
    # evaluate 的第二个参数把 payload、文件名和类型传给脚本中的 data。
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


def _replace_editor_text(page, text: str, *, expected_current: str | None = None) -> bool:
    # 作用：通过删除和粘贴事件写入正文。
    # 使用场景：导入、替换链接及恢复正文时调用；检测到用户交互则拒绝覆盖。
    # 通过编辑器的删除和文本粘贴流程写入，让网页自身处理内容变更。
    # 如果守卫已发现用户操作，则拒绝覆盖正文。
    editor = page.locator(EDITOR_SELECTOR)  # 定位页面中可编辑的 Markdown 正文。
    # 以下页面脚本先检查用户交互标记，然后选中编辑区全部内容。
    # execCommand 删除选区后，将 text 放进 DataTransfer 并派发文本粘贴事件。
    # 检查快照与写入在同一段页面脚本中进行，避免中间的异步变化被覆盖。
    replaced = editor.evaluate(r'''(editor, data) => {
        const guard = window.__csdnMdHelperGuard;
        if (!guard || guard.interacted) return 'guard';
        const normalize = value => value.replace(/\r\n/g, '\n').replace(/\n+$/, '');
        if (data.expected !== null && normalize(editor.textContent || '') !== normalize(data.expected)) {
            return 'changed';
        }
        editor.focus();
        const range = document.createRange();
        range.selectNodeContents(editor);
        const selection = window.getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
        document.execCommand('delete');
        const transfer = new DataTransfer();
        transfer.setData('text/plain', data.text);
        editor.dispatchEvent(new ClipboardEvent('paste', {
            clipboardData: transfer, bubbles: true, cancelable: true
        }));
        return 'written';
    }''', {'text': text, 'expected': expected_current})
    if replaced == 'changed':
        raise HelperError('检测到编辑器内容发生变化，已停止处理并保留当前正文；请检查后重试。')
    if replaced != 'written':  # 守卫丢失或用户已操作时没有执行替换。
        return False
    _remove_extra_leading_newlines(page, text)  # 清理可能由编辑器额外插入的前导换行。
    return True  # 调用者仍会等待和验证正文。


def _remove_extra_leading_newlines(page, text: str) -> None:
    # 作用：安全删除编辑器额外插入的开头换行。
    # 使用场景：粘贴正文后调用，只有确认剩余内容与目标匹配时才删除。
    # CSDN 连续上传后可能在正文开头额外插入换行。
    # 统一换行并忽略末尾换行后，仅在去掉前缀即可匹配时删除，保留原文空行。
    # 网页可能把正文拆成多个文本节点，因此用 TreeWalker 找到要删除的范围。
    # 页面脚本先确认没有用户交互，再通过 normalize 对比当前文本与期望值。
    # 若仅有额外前导换行，则统计数量，遍历文本节点并用 Range 圈出这部分。
    # 遍历途中如发现其他字符或节点不完整就退出；确认范围后才执行删除。
    page.locator(EDITOR_SELECTOR).evaluate(r'''(editor, expected) => {
        const guard = window.__csdnMdHelperGuard;
        if (!guard || guard.interacted) return;
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
    # 作用：比较统一换行格式并忽略末尾换行后的正文。
    # 使用场景：检查写入是否成功或编辑器内容是否变化时调用。
    # 编辑器可能在末尾添加换行；比较时统一换行符并忽略末尾换行。
    return actual.replace('\r\n', '\n').rstrip('\n') == expected.replace('\r\n', '\n').rstrip('\n')


def _small_text_patches(before: str, after: str) -> list[dict] | None:
    # 作用：计算把旧正文变成新正文的逐行小范围替换。
    # 使用场景：同一张图可能被多行引用；不能用带换行的一大段插入破坏编辑器布局。
    old_lines = before.splitlines(keepends=True)
    new_lines = after.splitlines(keepends=True)
    if len(old_lines) != len(new_lines):
        return None  # 行数改变时无法确认只修改了图片引用。

    def shared_length(left: str, right: str, limit: int, *, suffix: bool = False) -> int:
        # 作用：查找一行中不需要修改的公共前缀或后缀长度。
        # 使用场景：把每行的变更缩小到最短连续区间。
        low, high = 0, limit
        while low < high:
            middle = (low + high + 1) // 2
            same = left[-middle:] == right[-middle:] if suffix else left[:middle] == right[:middle]
            if same:
                low = middle
            else:
                high = middle - 1
        return low

    patches = []
    offset = 0  # 当前行在 Python 字符串中的起点。
    limit = max(4096, min(65536, len(before) // 3))
    for old_line, new_line in zip(old_lines, new_lines):
        if old_line != new_line:
            prefix = shared_length(old_line, new_line, min(len(old_line), len(new_line)))
            suffix = shared_length(old_line, new_line, min(len(old_line), len(new_line)) - prefix,
                                   suffix=True)
            old_part = old_line[prefix:len(old_line) - suffix]
            new_part = new_line[prefix:len(new_line) - suffix]
            # 验证每个局部区间确实能重建目标行，供浏览器复用目标正文而不反复传输全文。
            if old_line[:prefix] + new_part + old_line[len(old_line) - suffix:] != new_line:
                return None
            # insertText 跨行时可能产生额外 DOM 节点并丢失换行；跨度过大也不适合局部操作。
            if ('\n' in old_part or '\r' in old_part or '\n' in new_part or '\r' in new_part or
                    max(len(old_part), len(new_part)) > limit):
                return None
            # DOM Range 使用 UTF-16 偏移；Python 字符坐标遇到 emoji 时不能直接传入浏览器。
            start = len(before[:offset + prefix].encode('utf-16-le')) // 2
            end = start + len(old_part.encode('utf-16-le')) // 2
            patches.append({'start': start, 'end': end, 'old': old_part, 'replacement': new_part})
            if len(patches) > 32:
                return None  # 太多独立修改时使用成熟的全文写回路径。
        offset += len(old_line)
    return list(reversed(patches))  # 从后向前修改，前面的位置不会因后续替换而偏移。


def _try_local_image_update(page, before: str, updated: str, url: str) -> dict:
    # 作用：识别本次临时插图并尝试局部更新正文，返回处理状态。
    # 使用场景：图片上传成功后调用；无法明确识别变化时不覆盖当前编辑器内容。
    # 内容不同时仍先检查上传插入的图片；跨度过大时由调用方安全回退到全文写入。
    patches = _small_text_patches(before, updated)
    return page.locator(EDITOR_SELECTOR).evaluate(r'''(editor, data) => {
        const guard = window.__csdnMdHelperGuard;
        if (!guard) return {status: 'unavailable'};
        if (guard.interacted) return {status: 'interacted'};
        const current = editor.textContent || '';
        const before = guard.uploadSnapshot;
        if (typeof before !== 'string') return {status: 'unavailable'};
        const url = data.url;
        const marker = '](' + url + ')';

        // 只接受由本次上传地址组成的一条 Markdown 图片，其他变化一律不猜测。
        const isSingleImage = value => {
            const text = value.trim();
            const end = text.lastIndexOf(marker);
            return text.startsWith('![') && end >= 2 && end + marker.length === text.length &&
                   !/[\r\n]/.test(text.slice(0, end)) && text.indexOf('](') === end;
        };

        // 有些编辑器会暂时只剩上传插图；它是已知状态，可以安全回退全文写入。
        if (isSingleImage(current)) return {status: 'fallback', reason: 'editor-replaced-body', snapshot: current};

        // 查找能被移除且余下正文恰好等于上传前快照的一段临时插图。
        const added = current.length - before.length;
        let insertion = null;
        if (added > 0) {
            let search = 0;
            while (search < current.length) {
                const markerAt = current.indexOf(marker, search);
                if (markerAt < 0) break;
                const imageAt = current.lastIndexOf('![', markerAt);
                const imageEnd = markerAt + marker.length;
                if (imageAt >= 0 && isSingleImage(current.slice(imageAt, imageEnd))) {
                    const whitespace = added - (imageEnd - imageAt);
                    // 允许临时插图相邻的少量空白，但不把正文一并删除。
                    if (whitespace >= 0 && whitespace <= 256) {
                        for (let left = 0; left <= whitespace; left++) {
                            const start = imageAt - left;
                            const end = imageEnd + whitespace - left;
                            if (start < 0 || end > current.length) continue;
                            if (!/^\s*$/.test(current.slice(start, imageAt)) ||
                                !/^\s*$/.test(current.slice(imageEnd, end))) continue;
                            if (current.slice(0, start) === before.slice(0, start) &&
                                current.slice(end) === before.slice(start)) {
                                insertion = {start, end};
                                break;
                            }
                        }
                    }
                }
                if (insertion) break;
                search = markerAt + marker.length;
            }
        }
        // 除本次插图之外有无法确认的变化：停止，不用全文回退覆盖它。
        if (!insertion) return {status: 'conflict', reason: 'unrecognized-insertion'};
        if (data.patches === null) return {status: 'fallback', reason: 'patch-too-large', snapshot: current};

        // 先在字符串中演算所有修改，确认每处旧内容和完整目标均吻合。
        const states = [before];
        for (const part of data.patches) {
            const previous = states[states.length - 1];
            if (previous.slice(part.start, part.end) !== part.old) {
                return {status: 'fallback', reason: 'patch-old-text-mismatch', snapshot: current};
            }
            states.push(previous.slice(0, part.start) + part.replacement + previous.slice(part.end));
        }
        // textContent 和 DOM 文本节点都以 UTF-16 计数，遍历节点定位选择区间。
        const pointAt = offset => {
            const walker = document.createTreeWalker(editor, NodeFilter.SHOW_TEXT);
            let node, remaining = offset;
            while ((node = walker.nextNode())) {
                if (!node.data.length) continue;
                if (remaining <= node.data.length) return {node, offset: remaining};
                remaining -= node.data.length;
            }
            return null;
        };
        const changeRange = (start, end, replacement) => {
            const first = pointAt(start), last = pointAt(end);
            if (!first || !last) return false;
            const range = document.createRange();
            range.setStart(first.node, first.offset);
            range.setEnd(last.node, last.offset);
            editor.focus();
            const selection = window.getSelection();
            selection.removeAllRanges();
            selection.addRange(range);
            return replacement === null ? document.execCommand('delete') :
                   document.execCommand('insertText', false, replacement);
        };

        // 末尾空行不影响图片在前文的偏移；行内 CRLF 变化则会改变偏移，不能继续局部编辑。
        const normalize = value => value.replace(/\r\n/g, '\n').replace(/\n+$/, '');
        const sameText = (actual, expected) => actual.replace(/\n+$/, '') === expected.replace(/\n+$/, '');
        const equivalent = (actual, expected) => normalize(actual) === normalize(expected);
        // 只在去除前缀后其余正文完全一致时，才把开头换行认作编辑器新增的换行。
        const extraLeadingNewlines = (actual, expected, compare = sameText) => {
            let extra = 0;
            while (actual[extra] === '\n') {
                extra++;
                if (compare(actual.slice(extra), expected)) return extra;
            }
            return 0;
        };
        const removeLeadingNewlines = count => {
            // 用原生删除命令清理时，部分 CSDN 编辑器会再次添加同样的前导换行。
            // 仅删除已经逐字符确认的额外换行，再通知编辑器同步内容。
            const walker = document.createTreeWalker(editor, NodeFilter.SHOW_TEXT);
            const range = document.createRange();
            let node, first = true, remaining = count;
            while ((node = walker.nextNode()) && remaining) {
                if (!node.data.length) continue;
                const take = Math.min(remaining, node.data.length);
                if (node.data.slice(0, take) !== '\n'.repeat(take)) return false;
                if (first) { range.setStart(node, 0); first = false; }
                remaining -= take;
                if (!remaining) range.setEnd(node, take);
            }
            if (remaining) return false;
            range.deleteContents();
            editor.dispatchEvent(new InputEvent('input', {
                bubbles: true, inputType: 'deleteContentBackward'
            }));
            return true;
        };
        const restoreKnownText = expected => {
            for (let attempt = 0; attempt < 2; attempt++) {
                const actual = editor.textContent || '';
                if (sameText(actual, expected)) return true;
                const extra = extraLeadingNewlines(actual, expected);
                if (guard.interacted || !extra) return false;
                if (!removeLeadingNewlines(extra)) return false;
            }
            return sameText(editor.textContent || '', expected);
        };

        // 先移除上传产生的临时插图；每一步失败后都只在已知状态下允许回退。
        const removed = changeRange(insertion.start, insertion.end, null);
        let observed = editor.textContent || '';
        if (guard.interacted) return {status: 'interacted'};
        if (!removed || !restoreKnownText(before)) {
            observed = editor.textContent || '';
            if (observed === current || equivalent(observed, before) ||
                extraLeadingNewlines(observed, before, equivalent)) {
                return {status: 'fallback', reason: 'insertion-removal-not-stable', snapshot: observed};
            }
            return {status: 'conflict', reason: 'unexpected-after-removing-insertion'};
        }
        for (let index = 0; index < data.patches.length; index++) {
            if (guard.interacted) return {status: 'interacted'};
            const part = data.patches[index];
            const inserted = changeRange(part.start, part.end, part.replacement);
            observed = editor.textContent || '';
            if (guard.interacted) return {status: 'interacted'};
            if (!inserted || !restoreKnownText(states[index + 1])) {
                observed = editor.textContent || '';
                // 仅当当前正文仍是预先验证过的某一步，才允许兼容写回。
                if (observed === current || states.some(state =>
                    equivalent(observed, state) || extraLeadingNewlines(observed, state, equivalent))) {
                    return {status: 'fallback', reason: 'local-edit-not-stable', snapshot: observed};
                }
                return {status: 'conflict', reason: 'unexpected-after-local-edit', index};
            }
        }
        // 此正文由已核验的上传前快照和局部补丁生成，供后续稳定性检查直接复用。
        guard.localTarget = states[states.length - 1];
        return {status: 'applied'};
    }''', {'url': url, 'patches': patches})


def _wait_for_text(page, expected: str, timeout_ms: int = 5000, *, use_local_target: bool = False,
                   require_guard: bool = False) -> bool:
    # 作用：等待编辑器内容与目标匹配并保持短暂稳定。
    # 使用场景：每次写入或恢复正文后使用；超时返回 False。
    # 页面可能异步修改正文：匹配后仍需稳定 0.3 秒才认为写入成功。
    # 期望正文只传入页面一次，后续比较仅返回状态，避免反复把长文传回 Python。
    key = '__csdnMdHelperExpected_' + uuid4().hex
    if use_local_target:
        # 局部替换已经在页面中构造并验证目标正文，不必再次传输完整文章。
        page.evaluate(r'''(key) => {
            const text = window.__csdnMdHelperGuard?.localTarget;
            if (typeof text === 'string') window[key] = text.replace(/\r\n/g, '\n').replace(/\n+$/, '');
        }''', key)
    else:
        page.evaluate(r'''({key, text}) => {
            window[key] = text.replace(/\r\n/g, '\n').replace(/\n+$/, '');
        }''', {'key': key, 'text': expected})
    deadline = time.monotonic() + timeout_ms / 1000
    matching_since = None
    try:
        while time.monotonic() < deadline:
            state = page.locator(EDITOR_SELECTOR).evaluate(r'''(editor, data) => {
                const guard = window.__csdnMdHelperGuard;
                if (data.requireGuard && !guard) return 'guard-missing';
                if (guard?.interacted) return 'interacted';
                const key = data.key;
                if (typeof window[key] !== 'string') return 'missing';
                const actual = (editor.textContent || '').replace(/\r\n/g, '\n').replace(/\n+$/, '');
                const target = window[key];
                if (actual === target) return 'match';
                const extra = actual.length - target.length;
                if (extra > 0 && actual.slice(0, extra) === '\n'.repeat(extra) &&
                    actual.slice(extra) === target) return 'extra-newlines';
                return 'different';
            }''', {'key': key, 'requireGuard': require_guard})
            if state == 'guard-missing':
                raise HelperError('编辑页面状态已重置，已停止处理；请检查正文后重试。')
            if state == 'interacted':
                raise HelperError('检测到你操作了编辑页面，已停止处理以保护正文；请检查后重试。')
            if state == 'missing':
                return False  # 页面被重新加载后，不能把旧状态当成写入成功。
            if state == 'match':
                if matching_since is None:
                    matching_since = time.monotonic()
                elif time.monotonic() - matching_since >= 0.3:
                    return True
            else:
                matching_since = None
                if state == 'extra-newlines':
                    _remove_extra_leading_newlines(page, expected)
            page.wait_for_timeout(150)
        return False
    finally:
        try:
            page.evaluate('(key) => { delete window[key]; }', key)  # 不在编辑页面留下正文副本。
        except Exception:
            pass  # 页面已经关闭时不遮盖原来的处理结果。


def _start_edit_guard(page) -> None:
    # 作用：监听页面内的可信用户交互并记录状态。
    # 使用场景：开始处理文章时调用，只记录是否交互，不记录按键内容或屏幕。
    # 监听页面上的可信用户交互；助手自行派发的模拟粘贴事件不计入。
    # 只记录该页面是否发生交互，不记录按键内容；检测到操作后停止覆盖。
    # 页面脚本创建 interacted 标志，为键盘、指针、粘贴、剪切和拖放注册捕获监听。
    # event.isTrusted 用于区分浏览器认可的交互与助手主动派发的模拟事件。
    # 把状态和处理函数保存在页面变量中，便于后续读取并移除监听器。
    page.evaluate('''() => {
        const state = { interacted: false };
        const handler = event => { if (event.isTrusted) state.interacted = true; };
        state.events = ['keydown', 'pointerdown', 'paste', 'cut', 'drop'];
        state.handler = handler;
        for (const name of state.events) document.addEventListener(name, handler, true);
        window.__csdnMdHelperGuard = state;
    }''')


def _user_interacted(page) -> bool | None:
    # 作用：读取是否已有可信用户交互；守卫丢失时返回 None。
    # 使用场景：写入及上传过程中的安全检查会调用，也能识别页面刷新。
    return page.evaluate('''() => {
        const guard = window.__csdnMdHelperGuard;
        return guard ? !!guard.interacted : null;
    }''')


def _stop_edit_guard(page) -> None:
    # 作用：移除页面交互监听器及状态。
    # 使用场景：文章处理成功或异常退出后调用，清理失败不会覆盖原异常。
    # 无论处理成功或失败，结束时都移除监听器。
    try:
        # 页面脚本按注册时保存的事件名和处理函数逐个解除监听，再删除状态。
        page.evaluate('''() => {
            const state = window.__csdnMdHelperGuard;
            if (!state) return;
            for (const name of state.events) document.removeEventListener(name, state.handler, true);
            delete window.__csdnMdHelperGuard;
        }''')
    except Exception:
        pass  # 页面已关闭等情况下，清理失败不应掩盖原来的错误。


def _ensure_editor_unchanged(page, expected: str | None = None, *, use_local_target: bool = False) -> str | None:
    # 作用：检查用户交互、其他编辑标签和可选正文快照。
    # 使用场景：重要写入前后调用；发现风险时抛出 HelperError。
    # 同时检查用户交互、其他编辑标签和正文快照，发现变化立即停止。
    actual = editor_text(page) if expected is not None else None  # 调用者需要时返回精确正文。
    if use_local_target:
        # 局部更新后的目标已留在守卫中；在页面内比较，只返回检查结果。
        matched = page.locator(EDITOR_SELECTOR).evaluate(r'''editor => {
            const target = window.__csdnMdHelperGuard?.localTarget;
            if (typeof target !== 'string') return false;
            const normalize = value => value.replace(/\r\n/g, '\n').replace(/\n+$/, '');
            return normalize(editor.textContent || '') === normalize(target);
        }''')
    else:
        matched = expected is None or _equivalent_editor_text(actual, expected)
    if not matched:
        raise HelperError('检测到编辑器内容发生变化，已停止处理并保留当前正文；请检查后重试。')
    interacted = _user_interacted(page)
    if interacted is None:  # 页面刷新会清除守卫，不能再用旧状态恢复正文。
        raise HelperError('编辑页面状态已重置，已停止处理；请检查正文后重试。')
    if interacted:  # 即使正文暂未变化，也不能假设用户的后续操作不会修改文章。
        raise HelperError('检测到你操作了编辑页面，已停止处理以保护正文；请检查后重试。')
    # 查找除当前页外仍打开的 CSDN Markdown 编辑标签。
    others = [other for other in page.context.pages
              if other != page and not other.is_closed() and is_csdn_editor(other)]
    if others:  # 其他标签可能同步草稿，停止以保护当前内容。
        raise HelperError('检测到其他 CSDN Markdown 编辑标签页，已停止处理并保留当前正文；'
                          '请关闭其他标签页后重试。')
    return actual  # 调用者需要时复用刚检查的正文，避免重复传输全文。


def _capture_upload_snapshot(page, expected: str) -> str:
    # 作用：原子读取并缓存上传前正文，再核对预期内容和编辑器安全状态。
    # 使用场景：每次粘贴图片前调用；后续浏览器内比较复用该快照。
    actual = page.locator(EDITOR_SELECTOR).evaluate('''editor => {
        const guard = window.__csdnMdHelperGuard;
        if (!guard) return null;
        const text = editor.textContent || '';
        if (!guard.interacted) {
            guard.uploadSnapshot = text;
            delete guard.localTarget;
        }
        return text;
    }''')
    if actual is None:
        raise HelperError('编辑页面状态已重置，已停止处理；请检查正文后重试。')
    if not _equivalent_editor_text(actual, expected):
        raise HelperError('检测到编辑器内容发生变化，已停止处理并保留当前正文；请检查后重试。')
    _ensure_editor_unchanged(page)  # 缓存后仍需检查用户交互及其他编辑标签。
    return actual


def _write_verified(page, text: str, previous: str, backup_path: Path | None,
                    *, expected_current: str | None = None) -> None:
    # 作用：写入并验证正文，验证失败时尝试恢复上一步正文。
    # 使用场景：导入、替换及失败恢复时使用；previous 是回退目标。
    # 每次写入后重新读取并等待稳定；无法确认时尝试恢复上一步正文。
    # 若检测到用户正在操作则停止，不继续覆盖；备份路径仅在已创建时提供。
    _ensure_editor_unchanged(page, expected_current)  # 回退写入前也要确认已知快照仍未变化。
    try:
        if not _replace_editor_text(page, text, expected_current=expected_current):  # 写入时再次核对快照。
            raise HelperError('编辑页面状态发生变化，已停止处理；请检查正文后重试。')
        written = _wait_for_text(page, text, require_guard=True)  # 等待实际正文与目标匹配并稳定。
    except HelperError:
        raise  # 保护性错误直接向上传递，不尝试继续覆盖。
    except Exception:
        written = False  # 其他写入异常按无法确认成功处理。
    if written:
        return  # 写入已确认，无需恢复。
    _ensure_editor_unchanged(page)  # 尝试恢复前再次确认没有新的用户操作。
    current = editor_text(page)
    if not (_equivalent_editor_text(current, text) or _equivalent_editor_text(current, previous)):
        # 校验失败后若出现其他内容，无法断定它来自哪次操作，保留当前正文供用户检查。
        note = f' 本地备份：{backup_path}' if backup_path else ''
        raise HelperError('CSDN 编辑器未确认写入结果，正文出现无法确认的变化；'
                          '已停止并保留当前内容，请检查后重试。' + note)
    if _equivalent_editor_text(current, previous):
        note = '上一步正文已保留。'
    else:
        try:
            restored = (_replace_editor_text(page, previous, expected_current=current) and
                        _wait_for_text(page, previous, require_guard=True))
        except Exception:
            restored = False  # 恢复过程异常时如实报告无法确认。
        note = '已尝试恢复上一步正文。' if restored else '未能确认正文恢复成功。'
    if backup_path:  # 有备份时可为用户提供进一步恢复依据。
        note += f' 本地备份：{backup_path}'  # 有备份时提供人工恢复位置。
    raise HelperError(f'CSDN 编辑器未确认写入结果，{note}')


def _write_image_verified(page, previous: str, updated: str, before_upload: str,
                          url: str, backup_path: Path | None) -> bool:
    # 作用：优先局部更新图片引用；只有编辑器处于已知状态时才回退全文写入。
    # 使用场景：一张图片上传成功、已经取得网址后调用；返回是否使用局部更新。
    try:
        result = _try_local_image_update(page, before_upload, updated, url)
    except Exception as exc:
        # 页面关闭或脚本异常时不能猜测当前正文，更不能盲目覆盖。
        raise HelperError('无法确认编辑器当前状态，已停止处理；请检查正文后重试。') from exc
    status = result.get('status') if isinstance(result, dict) else None
    if status == 'interacted':
        backup_note = f' 本地备份：{backup_path}' if backup_path else ''
        raise HelperError('检测到你操作了编辑页面，已停止处理；处理过程可能已部分修改正文，'
                          '请检查后重试。' + backup_note)
    if status == 'unavailable':
        raise HelperError('编辑器状态已重置，已停止处理；请检查正文后重试。')
    if status == 'fallback':
        # 对已识别的插图状态重新核验快照，防止判断和写入之间出现其他变化。
        snapshot = result.get('snapshot')
        if not isinstance(snapshot, str):
            raise HelperError('无法确认编辑器回退状态，已停止处理；请检查正文后重试。')
        _write_verified(page, updated, previous, backup_path, expected_current=snapshot)
        return False
    if status == 'applied':
        # 局部操作仍需验证完整正文和稳定时间，不能只看浏览器命令的返回值。
        if _wait_for_text(page, updated, use_local_target=True, require_guard=True):
            _ensure_editor_unchanged(page, use_local_target=True)
            return True
        _ensure_editor_unchanged(page)
        current = editor_text(page)
        if current == before_upload:
            # 若局部操作没有生效且正文回到上传前快照，可以使用原有全文写回。
            _write_verified(page, updated, previous, backup_path, expected_current=current)
            return False
    # 用户编辑或不明变化可能包含需要保留的内容，不能用旧快照覆盖。
    backup_note = f' 本地备份：{backup_path}' if backup_path else ''
    raise HelperError('上传后编辑器出现无法确认的变化，已停止处理；本次操作可能已部分修改正文，'
                      '图片也可能已经上传，请检查后重试。' + backup_note)


def upload_one(page, path: Path, pending: deque, current_requests: set, *, timeout_ms: int = 60_000,
               extra_hosts: set[str] | None = None, notify=lambda message: None,
               editor_before: str | None = None, snapshot_captured: bool = False) -> str:
    # 作用：上传一张本地图片并获取已插入正文的 CSDN 地址。
    # 使用场景：逐张处理图片时调用；pending 与 current_requests 由网络监听器维护。
    # 清空上一张图的请求记录，粘贴文件后等待本次可信上传响应。
    extra_hosts = extra_hosts or set()  # 未指定额外域名时使用空集合。
    before = editor_before if editor_before is not None else editor_text(page)  # 复用已核验的上传前正文。
    pending.clear()  # 丢弃上一张图片尚未消费的响应。
    current_requests.clear()  # 丢弃上一张图片的请求记录。
    _paste_file(page, path)  # 由 CSDN 编辑器接管文件粘贴并发起上传。
    deadline = time.monotonic() + timeout_ms / 1000  # 计算本轮等待上传响应的截止时间。
    possible_failure = None  # 请求体不可见时只能提示可能相关，不能提前中断其他上传。
    while time.monotonic() < deadline:
        page.wait_for_timeout(120)  # 让浏览器有时间触发请求和响应回调。
        while pending:  # 依次检查已收到的本轮响应。
            response = pending.popleft()
            if _response_may_contain_upload(response, extra_hosts):  # 先检查域名和方法。
                if not response.ok:
                    if 400 <= response.status <= 599 and _request_contains_image(response.request, path):
                        raise HelperError(_http_upload_error(response.status, path))
                    if (400 <= response.status <= 599 and possible_failure is None
                            and _request_may_contain_image(response.request, path)):
                        possible_failure = response.status
                        notify(f'检测到可能相关的图片请求返回 HTTP {response.status}，正在确认上传结果。')
                    continue  # 无法确认属于当前图片的失败请求不能误判。
                url = _from_response(response)  # 再从响应体提取并校验图片地址。
                if url:
                    # 网络响应完成不等于正文插入完成；等待图片网址实际出现，
                    # 防止编辑器的延迟插入覆盖接下来重建的文章。
                    insertion_deadline = time.monotonic() + min(timeout_ms / 1000, 10)  # 最多额外等 10 秒。
                    while time.monotonic() < insertion_deadline:
                        if snapshot_captured:
                            # 在浏览器内用上传前快照比较，只返回状态，不反复传输整篇正文。
                            inserted = page.locator(EDITOR_SELECTOR).evaluate('''(editor, url) => {
                                const guard = window.__csdnMdHelperGuard;
                                if (!guard || typeof guard.uploadSnapshot !== 'string') return 'missing';
                                if (guard.interacted) return 'interacted';
                                const current = editor.textContent || '';
                                return current.includes(url) &&
                                    (!guard.uploadSnapshot.includes(url) || current !== guard.uploadSnapshot)
                                    ? 'yes' : 'no';
                            }''', url)
                            if inserted == 'missing':
                                raise HelperError('编辑器状态已重置，已停止处理；请检查正文后重试。')
                            if inserted == 'interacted':
                                raise HelperError('检测到你操作了编辑页面，已停止处理；请检查正文后重试。')
                            found = inserted == 'yes'
                        else:
                            current = editor_text(page)  # 未缓存快照时沿用兼容路径。
                            found = url in current and (url not in before or current != before)
                        if found:
                            return url  # 地址出现且可确认是本轮插入，才返回。
                        page.wait_for_timeout(120)  # 未插入时继续等待页面更新。
                    raise HelperError(f'图片已上传，但未能确认 CSDN 完成插入：{path.name}')
    hint = (f'可能相关的图片请求返回 HTTP {possible_failure}；' if possible_failure else '')
    raise HelperError(f'图片上传失败或未能确认结果：{path.name}。{hint}请检查网络和 CSDN 登录状态；'
                      '图片可能已经上传，请检查文章后再重试，避免重复上传。')


def process_article(target, markdown_path: Path, recovery_dir: Path,
                    notify=lambda message: None, *, timeout_ms: int = 60_000,
                    extra_hosts: set[str] | None = None, mode: str = 'auto',
                    expected_before: str | None = None,
                    backup_existing: bool = True, cancel_event=None,
                    expected_markdown: str | None = None,
                    expected_preflight: ImagePreflight | None = None) -> ProcessResult:
    # 作用：组织正文导入、图片保护、上传、逐步替换和资源清理。
    # 使用场景：界面要求导入文档或修复现有正文时调用；成功返回 ProcessResult。
    # 参数：target 是目标编辑页；markdown_path 决定文档和图片的相对路径基准。
    # recovery_dir 是备份目录；notify 用于进度通知；timeout_ms 控制上传等待。
    # mode 可为 auto、import 或 repair；expected_before 用于检查确认后是否被修改。
    # backup_existing 控制是否备份已有正文；extra_hosts 主要用于本地模拟测试。
    # cancel_event 仅在写入前和图片处理之间生效，避免中途打断一次上传。
    """Import text first, then upload and replace each image in the same tab."""
    md_text = read_markdown(markdown_path)  # 两种模式都需要文档路径作为图片解析基准。
    if expected_markdown is not None and md_text != expected_markdown:
        raise HelperError('所选 Markdown 在检查后发生变化，未修改正文；请重新检查。')

    # expected_before 是用户确认时的正文；这之后若被改动，不能继续导入。
    before = editor_text(target)  # 处理开始时再次读取目标页面正文。
    if expected_before is not None and not _equivalent_editor_text(before, expected_before):
        # 用户确认覆盖之后如果正文发生变化，不再使用过期快照。
        raise HelperError('处理前编辑器内容发生变化，未开始上传。请检查后重试。')
    if mode not in {'auto', 'import', 'repair'}:  # 仅接受明确支持的处理方式。
        raise HelperError('未知的正文处理方式。')  # 拒绝不受支持的模式值。

    _check_cancelled(cancel_event, 0, 0, before_write=True)  # 已请求停止时不开始写入。
    # 导入模式使用本地全文；修复模式保留编辑器正文并恢复未完成的图片标记。
    imported = mode == 'import' or (mode == 'auto' and not has_meaningful_text(before))  # 确定正文来源。
    body = md_text if imported else restore_image_placeholders(before)  # 修复时先还原旧保护标记。
    refs_by_path, missing = scan_local_images(body, markdown_path)  # 按本地路径分组并收集缺图。
    if expected_preflight is not None:
        current_preflight = image_preflight(body, markdown_path, refs_by_path=refs_by_path, missing=missing)
        if current_preflight != expected_preflight:
            raise HelperError('正文或图片状态在检查后发生变化，未修改正文；请重新检查。')
    local_refs = [ref for refs in refs_by_path.values() for ref in refs]  # 展开为全部本地图片引用。
    # CSDN 会立即把粘贴的本地图片转成失败占位图，因此先暂时拆开图片标记。
    # 路径和原文仍被保留，拿到 CSDN 地址后再逐处恢复并替换。
    working = render_image_progress(body, local_refs, [])  # 未上传的本地图片全部先加保护标记。
    safe_before = _protect_local_images(before, markdown_path)  # 生成可安全写回的旧正文。
    backup_path = None  # 默认没有磁盘备份。
    if backup_existing and has_meaningful_text(before):
        recovery_dir.mkdir(parents=True, exist_ok=True)  # 用户选择备份且原正文非空时创建目录。
        # 时间和随机后缀共同用于区分不同次备份。
        backup_path = recovery_dir / f'编辑器备份_{datetime.now():%Y%m%d_%H%M%S}_{uuid4().hex[:6]}.md'
        backup_path.write_text(before, encoding='utf-8')  # 备份的是处理前的原始正文。

    # 监听当前页面的请求和响应，用于将上传结果与本轮操作关联。
    _start_edit_guard(target)  # 注册用户交互守卫，处理期间避免覆盖用户修改。
    pending: deque = deque()  # 保存已收到、等待解析的候选响应。
    current_requests: set = set()  # 保存当前关注的 POST 请求。

    def on_request(request):
        # 作用：记录处理期间发出的 POST 请求。
        # 使用场景：页面发生网络请求时由 Playwright 调用，供响应筛选使用。
        if request.method == 'POST':  # 只记录可能与上传有关的 POST 请求。
            current_requests.add(request)

    def on_response(response):
        # 作用：收集属于已记录请求的网络响应。
        # 使用场景：页面收到响应时由 Playwright 调用，结果放入 pending 队列。
        if response.request in current_requests:  # 只接收已记录请求对应的响应。
            pending.append(response)  # 由 upload_one 进一步检查来源和内容。

    target.on('request', on_request)  # 处理期间订阅页面请求。
    target.on('response', on_response)  # 处理期间订阅页面响应。
    try:
        _check_cancelled(cancel_event, 0, len(refs_by_path), before_write=True)
        # 导入时先写完整正文；缺图的错误随后报告，已导入的文字仍会保留。
        if imported:  # 导入模式先将保护后的完整正文写入编辑器。
            notify('正在导入完整 Markdown 正文。')
            _write_verified(target, working, safe_before, backup_path,
                            expected_current=before)  # 核对处理前正文，再写入并验证。
            notify('正文已导入，开始处理图片。')
        elif working != before and not missing:  # 修复模式下有可保护的图片且没有缺图。
            # 修复模式下，仅在有变化且没有缺图时先保护现有本地图片。
            notify('正在保护尚未上传的本地图片链接。')
            _write_verified(target, working, safe_before, backup_path,
                            expected_current=before)  # 核对原正文后写入保护标记。

        if missing:  # 任何本地图片缺失时，在开始上传前停止。
            sample = '\n'.join(str(path) for path in missing[:10])  # 错误提示最多展示前 10 个路径。
            suffix = f'\n……还有 {len(missing) - 10} 个' if len(missing) > 10 else ''  # 补充未列出的数量。
            prefix = '正文已导入，但' if imported else ''  # 如实说明文字是否已经写入。
            raise HelperError(f'{prefix}找到 {len(missing)} 个不存在的本地图片，未开始上传：\n{sample}{suffix}')
        if not refs_by_path:  # 没有本地图片时按导入和修复模式分别处理。
            if imported:  # 纯文字文档也可以成功导入。
                notify('正文已导入，文档中没有需要上传的本地图片。')
                return ProcessResult(0, 0, True, backup_path)  # 纯文字导入成功，图片数量为零。
            # 修复模式没有待处理图片，提示用户如需整篇导入应切换模式。
            raise HelperError('当前编辑器正文中没有识别到本地图片。若要直接发布所选 Markdown，'
                              '请在助手中选择“导入所选 Markdown”。')
        count = len(refs_by_path)  # 不同图片文件的数量。
        occurrences = sum(len(refs) for refs in refs_by_path.values())  # 正文引用总次数。
        notify(f'找到 {count} 张不同图片，共 {occurrences} 处引用。')

        # 一张文件只上传一次，再把返回的地址应用到它的全部引用。
        # 每轮都以原文和累计结果重新生成正文，避免替换后字符位置偏移。
        replacements = []  # 逐步累计每处图片引用与上传地址的对应关系。
        fallback_notified = False  # 同一篇文章的兼容回退提示只显示一次。
        for index, (path, refs) in enumerate(refs_by_path.items(), 1):
            _check_cancelled(cancel_event, index - 1, count)  # 两张图片之间是安全停止点。
            notify(f'正在上传 {index}/{count}：{path.name}')  # 向界面显示当前进度。
            _check_cancelled(cancel_event, index - 1, count)  # 避免通知与粘贴之间错过停止请求。
            before_upload = _capture_upload_snapshot(target, working)  # 同步缓存精确快照供后续核验。
            try:  # 单张图片失败时保留前面已完成的结果。
                url = upload_one(target, path, pending, current_requests,
                                 timeout_ms=timeout_ms, extra_hosts=extra_hosts, notify=notify,
                                 editor_before=before_upload, snapshot_captured=True)
            except Exception as exc:
                # 上传失败后先检查正文；出现无法确认的变化时保留当前内容，不能盲目写回旧快照。
                _ensure_editor_unchanged(target)  # 守卫丢失或用户已操作时直接停止。
                current = editor_text(target)
                if not _equivalent_editor_text(current, before_upload):
                    raise HelperError(f'图片上传未完成，编辑器正文发生无法确认的变化，已停止并保留当前内容。'
                                      f'已完成 {index - 1}/{count} 张图片。{exc}\n请检查正文和图片后再重试。') from exc
                raise HelperError(f'正文已保留，已完成 {index - 1}/{count} 张图片。{exc}\n'
                                  '请检查正文；可选择“仅修复当前正文”继续处理剩余图片。') from exc
            _ensure_editor_unchanged(target)  # 上传期间再次检查用户是否操作或打开其他标签。
            replacements.extend((ref, url) for ref in refs)  # 该文件的所有引用共用同一个上传地址。
            updated = render_image_progress(body, local_refs, replacements)  # 根据原文和累计结果重建全文。
            used_local = _write_image_verified(target, working, updated, before_upload, url, backup_path)
            if not used_local and not fallback_notified:
                notify('当前编辑器需要兼容写回，正在保护正文。')
                fallback_notified = True
            working = updated  # 本轮成功后，更新下一轮要保护的正文快照。
            notify(f'已替换 {index}/{count} 张图片的链接。')
        return ProcessResult(count, occurrences, imported, backup_path)  # 返回统计信息供界面显示。
    finally:
        # 及时释放本轮监听器，避免重试时收到上一次处理的事件。
        target.remove_listener('request', on_request)
        target.remove_listener('response', on_response)  # 移除响应监听器。
        _stop_edit_guard(target)  # 清理页面交互守卫。
