"""Upload local images using the CSDN editor and repair a Markdown draft."""

# 阅读入口是 process_article：选择正文、保护本地图片、逐张上传并验证写回。
from __future__ import annotations

import base64  # 将本地图片字节编码后传入浏览器页面。
from collections import deque  # 按顺序缓存页面收到的网络响应。
from dataclasses import dataclass  # 定义结构化的处理结果。
from datetime import datetime  # 为正文备份生成时间标识。
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
    # 作用：筛选可信域名返回的成功 POST 响应。
    # 使用场景：等待上传结果时调用；extra_hosts 用于允许本地测试地址。
    # 仅从可信域名的成功 POST 响应中寻找图片；extra_hosts 供本地测试使用。
    try:
        host = (urlsplit(response.url).hostname or '').lower()  # 规范化响应域名便于比较。
        # CSDN 上传可能经过 CSDN 域名、图片域名或华为云域名。
        trusted = (host in extra_hosts or host == 'csdn.net' or host.endswith('.csdn.net')
                   or host == 'csdnimg.cn' or host.endswith('.csdnimg.cn')
                   or host == 'myhuaweicloud.com' or host.endswith('.myhuaweicloud.com'))
        return trusted and response.request.method == 'POST' and response.ok  # 域名、方法、状态都要满足。
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


def _replace_editor_text(page, text: str) -> bool:
    # 作用：通过删除和粘贴事件写入正文。
    # 使用场景：导入、替换链接及恢复正文时调用；检测到用户交互则拒绝覆盖。
    # 通过编辑器的删除和文本粘贴流程写入，让网页自身处理内容变更。
    # 如果守卫已发现用户操作，则拒绝覆盖正文。
    editor = page.locator(EDITOR_SELECTOR)  # 定位页面中可编辑的 Markdown 正文。
    # 以下页面脚本先检查用户交互标记，然后选中编辑区全部内容。
    # execCommand 删除选区后，将 text 放进 DataTransfer 并派发文本粘贴事件。
    # 用户已交互时返回 false；否则只表示已发起写入，仍需后续核验实际正文。
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
    if not replaced:  # 页面脚本返回 False，代表没有执行替换。
        return False  # 页面脚本发现用户交互，没有执行覆盖。
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
    # 作用：比较统一换行格式并忽略末尾换行后的正文。
    # 使用场景：检查写入是否成功或编辑器内容是否变化时调用。
    # 编辑器可能在末尾添加换行；比较时统一换行符并忽略末尾换行。
    return actual.replace('\r\n', '\n').rstrip('\n') == expected.replace('\r\n', '\n').rstrip('\n')


def _wait_for_text(page, expected: str, timeout_ms: int = 5000) -> bool:
    # 作用：等待编辑器内容与目标匹配并保持短暂稳定。
    # 使用场景：每次写入或恢复正文后使用；超时返回 False。
    # 页面可能异步修改正文：匹配后仍需稳定 0.3 秒才认为写入成功。
    deadline = time.monotonic() + timeout_ms / 1000  # 把毫秒超时转为单调时钟的截止时间。
    matching_since = None  # 首次匹配的时间；正文再次变化时会重置。
    while time.monotonic() < deadline:
        actual = editor_text(page)  # 每轮重新读取，观察页面异步渲染是否完成。
        if _equivalent_editor_text(actual, expected):
            if matching_since is None:  # 第一次观察到匹配。
                matching_since = time.monotonic()  # 首次匹配，开始计算稳定时间。
            elif time.monotonic() - matching_since >= 0.3:
                return True  # 连续匹配至少 0.3 秒，确认写入稳定。
        else:
            matching_since = None  # 再次不匹配时必须重新计算稳定时间。
            # 渲染后也可能延迟插入额外换行；只修正可确认的前缀差异。
            normalized_actual = actual.replace('\r\n', '\n').rstrip('\n')  # 规范化当前正文。
            normalized_expected = expected.replace('\r\n', '\n').rstrip('\n')  # 规范化期望正文。
            extra = len(normalized_actual) - len(normalized_expected)  # 差出的字符数可能是额外换行。
            if (extra > 0 and normalized_actual[:extra] == '\n' * extra
                    and normalized_actual[extra:] == normalized_expected):
                _remove_extra_leading_newlines(page, expected)  # 仅前缀全为换行且其余匹配时尝试删除。
        page.wait_for_timeout(150)  # 等待页面继续处理，避免无间隔地反复查询。
    return False  # 到达截止时间仍未确认稳定匹配。


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


def _user_interacted(page) -> bool:
    # 作用：读取页面中是否已有可信用户交互。
    # 使用场景：写入及上传过程中的安全检查会调用。
    return bool(page.evaluate('() => !!window.__csdnMdHelperGuard?.interacted'))  # 读取标志并转为布尔值。


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


def _ensure_editor_unchanged(page, expected: str | None = None) -> None:
    # 作用：检查用户交互、其他编辑标签和可选正文快照。
    # 使用场景：重要写入前后调用；发现风险时抛出 HelperError。
    # 同时检查用户交互、其他编辑标签和正文快照，发现变化立即停止。
    if _user_interacted(page):  # 用户在处理期间操作过页面，不能继续覆盖。
        raise HelperError('检测到编辑器内容发生变化，已停止处理并保留当前正文；请检查后重试。')
    # 查找除当前页外仍打开的 CSDN Markdown 编辑标签。
    others = [other for other in page.context.pages
              if other != page and not other.is_closed() and is_csdn_editor(other)]
    if others:  # 其他标签可能同步草稿，停止以保护当前内容。
        raise HelperError('检测到其他 CSDN Markdown 编辑标签页，已停止处理并保留当前正文；'
                          '请关闭其他标签页后重试。')
    if expected is not None and not _equivalent_editor_text(editor_text(page), expected):  # 可选的快照对比。
        # 内容与上一步快照不符，即便没有检测到用户操作也不能覆盖。
        raise HelperError('检测到编辑器内容发生变化，已停止处理并保留当前正文；请检查后重试。')


def _write_verified(page, text: str, previous: str, backup_path: Path | None) -> None:
    # 作用：写入并验证正文，验证失败时尝试恢复上一步正文。
    # 使用场景：导入、替换及失败恢复时使用；previous 是回退目标。
    # 每次写入后重新读取并等待稳定；无法确认时尝试恢复上一步正文。
    # 若检测到用户正在操作则停止，不继续覆盖；备份路径仅在已创建时提供。
    _ensure_editor_unchanged(page)  # 写入前先排除用户操作和多标签冲突。
    try:
        if not _replace_editor_text(page, text):  # 写入时又发现用户操作。
            raise HelperError('检测到编辑器内容发生变化，已停止处理并保留当前正文；请检查后重试。')
        written = _wait_for_text(page, text)  # 等待实际正文与目标匹配并稳定。
    except HelperError:
        raise  # 保护性错误直接向上传递，不尝试继续覆盖。
    except Exception:
        written = False  # 其他写入异常按无法确认成功处理。
    if written:
        return  # 写入已确认，无需恢复。
    _ensure_editor_unchanged(page)  # 尝试恢复前再次确认没有新的用户操作。
    try:
        restored = _replace_editor_text(page, previous) and _wait_for_text(page, previous)  # 恢复并核验。
    except Exception:
        restored = False  # 恢复过程异常时如实报告无法确认。
    note = '已尝试恢复上一步正文。' if restored else '未能确认正文恢复成功。'
    if backup_path:  # 有备份时可为用户提供进一步恢复依据。
        note += f' 本地备份：{backup_path}'  # 有备份时提供人工恢复位置。
    raise HelperError(f'CSDN 编辑器未确认写入结果，{note}')


def upload_one(page, path: Path, pending: deque, current_requests: set, *, timeout_ms: int = 60_000,
               extra_hosts: set[str] | None = None) -> str:
    # 作用：上传一张本地图片并获取已插入正文的 CSDN 地址。
    # 使用场景：逐张处理图片时调用；pending 与 current_requests 由网络监听器维护。
    # 清空上一张图的请求记录，粘贴文件后等待本次可信上传响应。
    extra_hosts = extra_hosts or set()  # 未指定额外域名时使用空集合。
    before = editor_text(page)  # 记录上传前正文，用于确认图片插入产生了变化。
    pending.clear()  # 丢弃上一张图片尚未消费的响应。
    current_requests.clear()  # 丢弃上一张图片的请求记录。
    _paste_file(page, path)  # 由 CSDN 编辑器接管文件粘贴并发起上传。
    deadline = time.monotonic() + timeout_ms / 1000  # 计算本轮等待上传响应的截止时间。
    while time.monotonic() < deadline:
        page.wait_for_timeout(120)  # 让浏览器有时间触发请求和响应回调。
        while pending:  # 依次检查已收到的本轮响应。
            response = pending.popleft()
            if _response_may_contain_upload(response, extra_hosts):  # 先检查域名、方法和状态。
                url = _from_response(response)  # 再从响应体提取并校验图片地址。
                if url:
                    # 网络响应完成不等于正文插入完成；等待图片网址实际出现，
                    # 防止编辑器的延迟插入覆盖接下来重建的文章。
                    insertion_deadline = time.monotonic() + min(timeout_ms / 1000, 10)  # 最多额外等 10 秒。
                    while time.monotonic() < insertion_deadline:
                        current = editor_text(page)  # 检查网页是否已经把地址插入编辑器。
                        if url in current and (url not in before or current != before):
                            return url  # 地址出现且可确认是本轮插入，才返回。
                        page.wait_for_timeout(120)  # 未插入时继续等待页面更新。
                    raise HelperError(f'图片已上传，但未能确认 CSDN 完成插入：{path.name}')
    raise HelperError(f'图片上传失败或超时：{path.name}')  # 截止时间内没有得到有效结果。


def process_article(target, markdown_path: Path, recovery_dir: Path,
                    notify=lambda message: None, *, timeout_ms: int = 60_000,
                    extra_hosts: set[str] | None = None, mode: str = 'auto',
                    expected_before: str | None = None,
                    backup_existing: bool = True) -> ProcessResult:
    # 作用：组织正文导入、图片保护、上传、逐步替换和资源清理。
    # 使用场景：界面要求导入文档或修复现有正文时调用；成功返回 ProcessResult。
    # 参数：target 是目标编辑页；markdown_path 决定文档和图片的相对路径基准。
    # recovery_dir 是备份目录；notify 用于进度通知；timeout_ms 控制上传等待。
    # mode 可为 auto、import 或 repair；expected_before 用于检查确认后是否被修改。
    # backup_existing 控制是否备份已有正文；extra_hosts 主要用于本地模拟测试。
    """Import text first, then upload and replace each image in the same tab."""
    try:  # 无论导入还是修复，都需要文档路径作为本地图片的解析基准。
        md_text = markdown_path.read_text(encoding='utf-8-sig')  # 支持 UTF-8 文档及可选 BOM。
    except (OSError, UnicodeDecodeError) as exc:
        # 把文件读取或编码错误转换成可直接展示给用户的提示。
        raise HelperError(f'无法读取 Markdown 文件（请使用 UTF-8 编码）：{exc}') from exc

    # expected_before 是用户确认时的正文；这之后若被改动，不能继续导入。
    before = editor_text(target)  # 处理开始时再次读取目标页面正文。
    if expected_before is not None and not _equivalent_editor_text(before, expected_before):
        # 用户确认覆盖之后如果正文发生变化，不再使用过期快照。
        raise HelperError('处理前编辑器内容发生变化，未开始上传。请检查后重试。')
    if mode not in {'auto', 'import', 'repair'}:  # 仅接受明确支持的处理方式。
        raise HelperError('未知的正文处理方式。')  # 拒绝不受支持的模式值。
    # 导入模式使用本地全文；修复模式保留编辑器正文并恢复未完成的图片标记。
    imported = mode == 'import' or (mode == 'auto' and not has_meaningful_text(before))  # 确定正文来源。
    body = md_text if imported else restore_image_placeholders(before)  # 修复时先还原旧保护标记。
    refs_by_path, missing = scan_local_images(body, markdown_path)  # 按本地路径分组并收集缺图。
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
        # 导入时先写完整正文；缺图的错误随后报告，已导入的文字仍会保留。
        if imported:  # 导入模式先将保护后的完整正文写入编辑器。
            notify('正在导入完整 Markdown 正文。')
            _write_verified(target, working, safe_before, backup_path)  # 写入后验证，失败尝试恢复旧正文。
            notify('正文已导入，开始处理图片。')
        elif working != before and not missing:  # 修复模式下有可保护的图片且没有缺图。
            # 修复模式下，仅在有变化且没有缺图时先保护现有本地图片。
            notify('正在保护尚未上传的本地图片链接。')
            _write_verified(target, working, safe_before, backup_path)  # 先把受保护的正文写回编辑器。

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
        for index, (path, refs) in enumerate(refs_by_path.items(), 1):
            notify(f'正在上传 {index}/{count}：{path.name}')  # 向界面显示当前进度。
            _ensure_editor_unchanged(target, working)  # 上传前确认正文仍是上一轮的结果。
            try:  # 单张图片失败时保留前面已完成的结果。
                url = upload_one(target, path, pending, current_requests,
                                 timeout_ms=timeout_ms, extra_hosts=extra_hosts)
            except Exception as exc:
                # 未检测到用户新操作时，恢复上一轮正文并保留已完成的图片。
                _ensure_editor_unchanged(target)  # 若用户已操作则直接停止，不再恢复覆盖。
                _write_verified(target, working, working, backup_path)  # 清除上传过程中可能出现的临时内容。
                raise HelperError(f'正文已保留，已完成 {index - 1}/{count} 张图片。{exc}\n'
                                  '请检查正文；可选择“仅修复当前正文”继续处理剩余图片。') from exc
            _ensure_editor_unchanged(target)  # 上传期间再次检查用户是否操作或打开其他标签。
            replacements.extend((ref, url) for ref in refs)  # 该文件的所有引用共用同一个上传地址。
            updated = render_image_progress(body, local_refs, replacements)  # 根据原文和累计结果重建全文。
            _write_verified(target, updated, working, backup_path)  # 验证写入；失败可回退上一轮。
            working = updated  # 本轮成功后，更新下一轮要保护的正文快照。
            notify(f'已替换 {index}/{count} 张图片的链接。')
        return ProcessResult(count, occurrences, imported, backup_path)  # 返回统计信息供界面显示。
    finally:
        # 及时释放本轮监听器，避免重试时收到上一次处理的事件。
        target.remove_listener('request', on_request)
        target.remove_listener('response', on_response)  # 移除响应监听器。
        _stop_edit_guard(target)  # 清理页面交互守卫。
