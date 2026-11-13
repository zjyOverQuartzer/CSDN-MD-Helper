"""Locate local Markdown/HTML images without rewriting unrelated content."""

# 本模块负责识别图片、定位本地文件并生成替换后的文本，不负责网络上传。
from __future__ import annotations

from dataclasses import dataclass  # 使用数据类集中保存图片引用信息。
import html  # 处理 HTML 实体及输出地址中的特殊字符。
from html.parser import HTMLParser  # 解析 HTML 标签，避免把 data-src 当成 src。
import os  # 根据操作系统区分 Windows 路径。
from pathlib import Path  # 拼接、规范化和比较文件路径。
import re  # 识别 Markdown 定义、HTML 图片及其他语法。
from urllib.parse import unquote, urlsplit  # 解析 file: 地址并还原百分号编码。

from markdown_it import MarkdownIt  # 识别代码块，保留列表与引用中的正常图片。

# 两个不可见字符用于暂时拆开图片标记，例如在 ! 和 [ 之间插入。
# 这样编辑器不会提前处理本地路径；上传完成或重试时仍可恢复原始标记。
IMAGE_PLACEHOLDER = '\u200b\u2060'
_MARKDOWN_BLOCK_PARSER = MarkdownIt('commonmark')  # 只使用块级结果，不改写文章内容。


@dataclass(frozen=True)
class ImageRef:
    # 作用：保存一处图片引用的原文坐标、路径及替换信息。
    # 使用场景：find_images 识别图片时创建；start/end 表示左闭右开的字符区间。
    # start/end 是待替换内容在原始 Markdown 中的字符区间，end 不包含在内。
    start: int  # 替换区间起点，包含该位置。
    end: int  # 替换区间终点，不包含该位置。
    source: str  # 图片原始路径或网址。
    # 引用式图片要替换整段图片语法；普通图片只替换路径，保留描述和其他属性。
    reference_alt: str | None = None  # 引用式图片的描述，None 表示不是引用式图片。
    reference_title: str = ''  # 引用定义中保留的标题及其前导空白。
    # 未上传时插入占位字符的位置；该位置同样以原始文本为基准。
    placeholder_at: int | None = None  # 用于插入临时保护标记的原文位置。

    def replacement(self, url: str) -> str:
        # 作用：根据图片类型生成替换后的内容。
        # 使用场景：写入新地址时调用；引用式图片返回完整语法，普通图片只返回转义后的地址。
        # 引用式图片改为带网址的普通图片语法；其他情况只返回转义后的网址。
        if self.reference_alt is not None:  # 引用式图片要替换整段引用语法。
            return f"![{self.reference_alt}]({url}{self.reference_title})"
        return html.escape(url, quote=True)  # 转义引号等字符，避免破坏 HTML 属性。


def _masked_markdown(text: str) -> str:
    # 作用：用等长空格遮住代码块、行内代码及 HTML 注释。
    # 使用场景：扫描图片前调用，避免把示例代码当作真实图片，且保留原文坐标。
    """Keep offsets while hiding code blocks, inline code and HTML comments."""
    chars = list(text)  # 字符串不可原地修改，先转成可按位置修改的列表。

    def mask(start: int, end: int) -> None:
        # 作用：把指定范围内的非换行字符改为空格。
        # 使用场景：外层方法发现需要忽略的片段时调用；start/end 是左闭右开区间。
        # 用空格覆盖而不是删除字符，保证扫描结果的位置仍对应原始文本。
        for index in range(start, end):
            if chars[index] not in "\r\n":  # 换行符必须保留，行结构才能保持一致。
                chars[index] = " "  # 其他字符替换成等长空格。

    # 用 Markdown 块级解析结果判断代码块，不能简单把四个空格都视作代码：
    # 列表项和引用中也可能有正常缩进，解析器能保留这些上下文。
    line_starts = [0] + [match.end() for match in re.finditer(r'\r\n|\r|\n', text)]
    for token in _MARKDOWN_BLOCK_PARSER.parse(text):
        if token.type not in {'code_block', 'fence'} or token.map is None:
            continue
        start_line, end_line = token.map  # Markdown 解析器返回左闭右开的行区间。
        start = line_starts[start_line]
        end = line_starts[end_line] if end_line < len(line_starts) else len(text)
        mask(start, end)  # 转回原文字符区间，保持后续替换的坐标不变。

    masked = "".join(chars)  # 把当前遮罩列表转回字符串供正则查找。
    for match in re.finditer(r"<!--[\s\S]*?(?:-->|\Z)", masked):
        mask(*match.span())  # 连未闭合但延续到文末的 HTML 注释也一起遮住。

    # 行内代码要求首尾反引号数量相同；未配对的反引号按普通文本处理。
    masked = "".join(chars)
    index = 0  # 从头遍历已经遮住代码块和注释的内容。
    while index < len(masked):
        # 不是反引号就继续向后找，不改变当前字符。
        if masked[index] != "`":
            # 跳过一个普通字符。
            index += 1
            continue
        # 先把连续反引号序列的终点放到下一个字符。
        run_end = index + 1
        # 持续前进，直到找到这一串反引号的末尾。
        while run_end < len(masked) and masked[run_end] == "`":
            # 把当前反引号计入连续序列。
            run_end += 1
        width = run_end - index  # 开始反引号连续出现的次数就是所需结束长度。
        closing = re.search(r"(?<!`)`{" + str(width) + r"}(?!`)", masked[run_end:])
        # 只有找到等长结束反引号时才将该范围视为行内代码。
        if closing:
            end = run_end + closing.end()  # 把局部匹配位置转换为全文位置。
            mask(index, end)  # 遮住整段行内代码，包括两端围栏。
            index = end  # 下次从这段代码之后继续扫描。
        else:
            index = run_end  # 没有结束符时，按普通文本跳过当前反引号序列。
    return "".join(chars)  # 返回长度和换行位置不变的遮罩文本。


def _label(value: str) -> str:
    # 作用：规范化引用式链接的标签。
    # 使用场景：保存或查找引用定义时调用，合并空白并忽略大小写。
    # 引用式链接的标签按空白合并、忽略大小写的形式匹配。
    return " ".join(value.split()).casefold()


def _definitions(text: str, visible: str) -> dict[str, tuple[str, str]]:
    # 作用：收集引用标签对应的图片路径和可选标题。
    # 使用场景：识别引用式图片前调用；visible 用于匹配，text 提供原文内容。
    # 找到 [标签]: 路径 "标题" 定义；匹配使用遮罩文本，内容从原文读取。
    result: dict[str, tuple[str, str]] = {}  # 标签 -> (原始路径, 可选标题)。
    # 正则支持尖括号路径或普通非空白路径，以及可选的引号/括号标题。
    pattern = re.compile(
        r'''(?m)^ {0,3}\[([^\]\r\n]+)\]:[ \t]*(?:<([^>\r\n]+)>|([^\s]+))'''
        r'''(?P<title>[ \t]+(?:"[^"\r\n]*"|'[^'\r\n]*'|\([^()\r\n]*\)))?'''
    )
    # 在遮罩后的可见文本中逐个查找引用定义。
    for match in pattern.finditer(visible):
        start, end = match.span(2) if match.group(2) is not None else match.span(3)  # 选中实际路径分组。
        title = text[slice(*match.span('title'))] if match.group('title') else ''  # 保留标题原文。
        result.setdefault(_label(match.group(1)), (text[start:end], title))  # 同名标签保留首次定义。
    return result  # 供后续图片引用按规范化标签查询。


def _find_bracket_end(text: str, start: int) -> int:
    # 作用：寻找与起点对应的右方括号，处理嵌套和转义。
    # 使用场景：解析图片描述或引用标签时调用；无法闭合则返回 -1。
    # 跟踪方括号嵌套深度，跳过转义的方括号和反斜杠；未闭合则返回 -1。
    depth = 1  # 调用方已读到一个左方括号，因此初始深度为 1。
    pos = start  # 从左方括号之后开始读取。
    while pos < len(text):
        # 读取当前位置字符，供本轮括号和转义判断使用。
        char = text[pos]  # 取当前位置字符，判断是否影响嵌套层数。
        if char == "\\" and pos + 1 < len(text) and text[pos + 1] in "[]\\":
            pos += 2  # 被反斜杠转义的括号不参与配对。
            continue  # 跳过反斜杠和被转义的字符。
        if char == "[":
            depth += 1  # 遇到嵌套的左方括号。
        elif char == "]":
            depth -= 1  # 遇到右方括号，关闭当前一层。
            if depth == 0:
                return pos  # 最外层闭合，返回该右括号的位置。
        if char in "\r\n":
            return -1  # 当前实现不允许描述或标签跨行。
        pos += 1  # 继续检查下一个字符。
    return -1  # 扫描到末尾仍未闭合。


def _inline_destination(text: str, start: int) -> tuple[int, int, int] | None:
    # 作用：定位普通 Markdown 图片语法中的路径。
    # 使用场景：解析 ![描述](路径) 时调用；返回路径区间及右括号后的坐标。
    """Return the URL span and position after the closing parenthesis."""
    pos = start  # 从图片语法左圆括号之后开始读取。
    while pos < len(text) and text[pos] in " \t":
        pos += 1  # 忽略路径之前的空格和制表符。
    if pos >= len(text):  # 已经到达正文结尾，没有可解析路径。
        return None
    if text[pos] == "<":  # 尖括号包裹的路径以 > 作为终点。
        end = text.find(">", pos + 1)
        # 尖括号路径缺少右尖括号时，不能继续解析。
        if end < 0:
            return None  # 尖括号没有闭合。
        closing = text.find(")", end + 1)  # 再定位图片语法的右圆括号。
        if closing < 0 or "\n" in text[end + 1:closing]:
            return None  # 没有闭合或语法跨行时不作为有效图片。
        return pos + 1, end, closing + 1  # 不把尖括号本身算进路径区间。

    # 普通路径允许包含成对括号；深度归零后的右括号才是图片语法的结尾。
    dest_start = pos  # 记录普通路径的第一个字符。
    depth = 0  # 路径内部的圆括号嵌套深度。
    while pos < len(text):
        # 读取当前位置字符，供本轮括号和转义判断使用。
        char = text[pos]
        if char in "\r\n":
            return None  # 普通路径在闭合前换行，视为无效。
        if char == "\\" and pos + 1 < len(text) and text[pos + 1] in "()":
            pos += 2  # 转义的圆括号视作路径内容，不影响嵌套深度。
            continue
        if char == "(":
            depth += 1  # 进入路径内部的一对圆括号。
        elif char == ")":
            if depth == 0:  # 这是图片语法自身的结束括号。
                raw_end = pos  # 暂时把路径末尾设为结束括号之前。
                raw = text[dest_start:raw_end]  # 取出路径和可能存在的标题。
                # Typora 的路径可以含空格，只去掉末尾的引号标题，不拆分路径。
                title = re.search(r'''[ \t]+(?:"[^"\r\n]*"|'[^'\r\n]*')[ \t]*$''', raw)
                # 确有匹配的结尾标题时，缩短路径范围以保留该标题。
                if title:
                    raw_end = dest_start + title.start()  # 排除末尾的引号标题。
                while raw_end > dest_start and text[raw_end - 1] in " \t":
                    raw_end -= 1  # 再去掉路径末尾的分隔空白。
                return dest_start, raw_end, pos + 1  # 返回路径区间及整个图片后的坐标。
            depth -= 1  # 关闭路径内部的一层圆括号。
        pos += 1  # 继续扫描下一个字符。
    return None  # 一直没有找到图片语法的结束括号。


class _HtmlImageLocator(HTMLParser):
    # 作用：在不修改原文的前提下，定位 HTML img 标签的真实 src 属性。
    # 使用场景：Typora 的 HTML 图片含 data-src、引号或多行属性时。
    _attribute = re.compile(
        r'''(?P<name>[^\s"'<>/=]+)(?:\s*=\s*(?:"(?P<double>[^"]*)"|'(?P<single>[^']*)'|(?P<bare>[^\s>]+)))?''',
        re.DOTALL,
    )

    def __init__(self, visible: str, original: str):
        # 作用：保存等长遮罩及原文，建立 HTMLParser 行号到原文坐标的映射。
        # 使用场景：每次扫描文章时创建独立实例，避免多个文档互相影响。
        super().__init__(convert_charrefs=False)
        self.original = original
        self.line_starts = [0] + [match.end() for match in re.finditer('\n', visible)]
        self.refs: list[ImageRef] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        # 作用：从真实 img 标签中找到名字恰好为 src 的第一个有值属性。
        # 使用场景：HTMLParser 读到起始标签时调用；其他标签不处理。
        if tag != 'img':
            return
        raw = self.get_starttag_text() or ''
        line, column = self.getpos()
        tag_start = self.line_starts[line - 1] + column
        name = re.match(r'<\s*img\b', raw, re.IGNORECASE)
        if not name:
            return
        position = name.end()
        while position < len(raw):
            while position < len(raw) and raw[position].isspace():
                position += 1
            if position >= len(raw) or raw[position] in '/>':
                break
            match = self._attribute.match(raw, position)
            if not match:
                position += 1  # 不认识的字符不应导致扫描死循环。
                continue
            position = match.end()
            if match.group('name').lower() != 'src':
                continue
            group = next((key for key in ('double', 'single', 'bare') if match.group(key) is not None), None)
            if group is not None:
                start, end = match.span(group)
                src_start, src_end = tag_start + start, tag_start + end
                self.refs.append(ImageRef(src_start, src_end, self.original[src_start:src_end],
                                          placeholder_at=tag_start + 1))
            return  # 浏览器使用第一个同名属性，后续 src 不再覆盖它。


def find_images(text: str) -> list[ImageRef]:
    # 作用：识别 Markdown 和 HTML 图片，并记录原文中的位置。
    # 使用场景：文件预检查和正式处理都会调用；返回按位置排序的 ImageRef 列表。
    # 先隐藏代码等不应替换的区域，再分别查找 Markdown 与 HTML 图片。
    visible = _masked_markdown(text)  # 遮住不应识别图片的区域。
    definitions = _definitions(text, visible)  # 建立引用式图片用到的定义表。
    refs: list[ImageRef] = []  # 保存识别出的全部图片引用。
    pos = 0  # 从遮罩文本的开头开始查找。
    while pos < len(visible):
        start = visible.find("![", pos)  # 查找下一个 Markdown 图片标记。
        if start < 0:
            break  # 已没有更多 Markdown 图片标记。
        if start and visible[start - 1] == "\\":
            pos = start + 2  # 被转义的标记按普通文本跳过。
            continue
        end_alt = _find_bracket_end(visible, start + 2)  # 找到描述的右方括号。
        if end_alt < 0:
            pos = start + 2  # 描述未闭合时继续寻找后续标记。
            continue
        alt = text[start + 2:end_alt]  # 从原文读取图片描述，保留原有内容。
        after = end_alt + 1  # 描述结束后的下一个字符决定图片写法。
        # 普通语法 ![描述](路径) 记录路径区间，以便保留描述和标题。
        if after < len(visible) and visible[after] == "(":
            parsed = _inline_destination(visible, after + 1)  # 解析普通图片路径区间。
            if parsed:
                src_start, src_end, after_image = parsed  # 路径起点、终点及完整图片之后的位置。
                if src_start < src_end:
                    # 记录图片路径在原文中的坐标和保护标记插入位置。
                    refs.append(ImageRef(src_start, src_end, text[src_start:src_end],
                                         placeholder_at=start + 1))
                pos = after_image  # 从当前图片结束之后继续查找。
                continue
        # 引用式语法从定义表取得路径，替换时只改图片，不改共用的链接定义。
        elif after < len(visible) and visible[after] == "[":
            end_label = _find_bracket_end(visible, after + 1)  # 找到引用标签的结束括号。
            if end_label >= 0:
                name = visible[after + 1:end_label] or alt  # 空标签使用图片描述作为标签。
                definition = definitions.get(_label(name))  # 到定义表中寻找对应路径。
                if definition:
                    # 记录整段引用式图片及其解析出的路径和标题。
                    refs.append(ImageRef(start, end_label + 1, definition[0], alt, definition[1], start + 1))
                pos = end_label + 1  # 跳过已经处理的引用部分。
                continue
        elif _label(alt) in definitions:  # 简写引用 ![标签]，直接用描述查定义。
            src, title = definitions[_label(alt)]
            # 记录简写引用图片及定义表中对应的路径。
            refs.append(ImageRef(start, end_alt + 1, src, alt, title, start + 1))
        pos = end_alt + 1  # 当前标记无法继续解析时，至少跳过描述区域。

    # 由 HTML 解析器区分真实 src 与 data-src，并保留原文中属性值的位置。
    html_images = _HtmlImageLocator(visible, text)
    html_images.feed(visible)
    html_images.close()
    refs.extend(html_images.refs)
    return sorted(refs, key=lambda ref: (ref.start, ref.end))  # 合并两种语法并按原文位置排序。


def local_path(source: str, md_path: Path) -> Path | None:
    # 作用：把图片引用转换为本地绝对路径，或判断它不是本地资源。
    # 使用场景：扫描图片时调用；相对路径基于所选 Markdown 所在目录。
    """Return a resolved local path, or None for remote/embedded resources."""
    value = html.unescape(source.strip())  # 去掉首尾空白并还原 HTML 实体。
    if not value or value.startswith("#"):
        return None  # 空引用或页内锚点不是要上传的本地文件。
    # 先区分 Windows 盘符和 URL 协议；网络地址、data 等资源不上传。
    windows_drive = bool(re.match(r"^[a-zA-Z]:[\\/]", value))
    if value.lower().startswith("file:"):  # 本地 file: URL 需要先还原路径部分。
        parsed = urlsplit(value)  # 拆分协议、主机和路径。
        if parsed.netloc and parsed.netloc.lower() != "localhost":
            value = f"//{parsed.netloc}{parsed.path}"  # 保留非 localhost 的网络共享主机名。
        else:
            value = parsed.path  # 本机文件只取 URL 路径。
            if os.name == "nt" and re.match(r"^/[A-Za-z]:/", value):
                value = value[1:]  # Windows 的 /C:/... 要去掉盘符前多余的斜杠。
    elif value.startswith("//") or (not windows_drive and re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", value)):
        return None  # 跳过协议相对网址及其他非本地协议。
    value = unquote(value)  # 例如把 %20 还原为空格。
    # Windows 目录名前的反斜杠可能是路径分隔符，不能误当 Markdown 转义。
    if os.name != 'nt' or not ('\\' in value and ('/' not in value or windows_drive)):
        value = re.sub(r"\\([()])", r"\1", value)  # 仅在合适的路径类型中去掉括号转义。
    path = Path(value)  # 转为 Path，便于判断绝对路径和拼接目录。
    # 相对路径始终相对于所选 Markdown 文件所在目录，而不是程序启动目录。
    if not path.is_absolute():
        path = md_path.parent / path  # 从 Markdown 所在目录解析相对路径。
    return path.resolve(strict=False)  # 规范化路径；是否存在由后续步骤检查。


def rewrite(text: str, replacements: list[tuple[ImageRef, str]]) -> str:
    # 作用：按照原文坐标批量替换指定图片引用。
    # 使用场景：已获得目标网址并需要生成替换后正文时使用；重叠区间会报错。
    # 按原始位置从前向后拼接，避免前一次替换改变后续图片的位置。
    last = 0  # 上一次处理完毕的位置。
    parts: list[str] = []  # 依次收集未修改文本和替换内容。
    for ref, url in sorted(replacements, key=lambda pair: pair[0].start):
        if ref.start < last:  # 新区间不能与前一个已经处理的区间重叠。
            raise ValueError("图片标记重叠，无法安全替换")
        parts.extend((text[last:ref.start], ref.replacement(url)))  # 保留中间原文并追加新内容。
        last = ref.end  # 下次从当前引用的终点继续。
    parts.append(text[last:])  # 保留最后一张图片之后的所有正文。
    return "".join(parts)  # 拼成最终 Markdown。


def restore_image_placeholders(text: str) -> str:
    # 作用：还原助手插入的未完成图片标记。
    # 使用场景：中断后重新识别本地图片，或检查受保护正文时调用。
    """Restore only markers inserted by this helper for unfinished images."""
    # 只恢复助手插入在图片标记处的占位符，供中断后的修复流程重新识别。
    text = text.replace('!' + IMAGE_PLACEHOLDER + '[', '![')  # 恢复 Markdown 图片标记。
    return re.sub('<' + IMAGE_PLACEHOLDER + r'(?=img\b)', '<', text, flags=re.IGNORECASE)  # 恢复 HTML 图片标记。


def render_image_progress(text: str, refs: list[ImageRef],
                          replacements: list[tuple[ImageRef, str]]) -> str:
    # 作用：根据图片上传进度生成当前应写入的全文。
    # 使用场景：上传前保护图片、每次上传后更新正文时调用；未完成图片保留可恢复标记。
    """Keep unresolved local images inert until their CSDN URL is known."""
    # 始终以原文坐标生成本轮正文：已上传的写入网址，未上传的插入占位符。
    uploaded = dict(replacements)  # 便于按 ImageRef 查询是否已有上传地址。
    edits: list[tuple[int, int, str]] = []  # 每项为原文起点、终点和要插入的文本。
    for ref in refs:
        if ref in uploaded:  # 已完成图片：记录把原路径替换成新地址的操作。
            edits.append((ref.start, ref.end, ref.replacement(uploaded[ref])))
        elif ref.placeholder_at is not None:  # 未完成图片：在标记内部插入保护字符。
            edits.append((ref.placeholder_at, ref.placeholder_at, IMAGE_PLACEHOLDER))
        else:
            raise ValueError('无法安全标记待上传图片')  # 没有插入位置时不能安全继续。
    parts: list[str] = []  # 存放重建过程中的文本片段。
    last = 0  # 跟踪已处理到的原文位置。
    for start, end, value in sorted(edits):
        if start < last:  # 插入/替换区间不能与之前的编辑重叠。
            raise ValueError('图片标记重叠，无法安全替换')
        parts.extend((text[last:start], value))  # 先保留间隔文本，再写入占位符或新地址。
        last = end  # 更新原文读取位置。
    parts.append(text[last:])  # 保留文章末尾。
    return ''.join(parts)  # 返回当前进度对应的完整正文。
