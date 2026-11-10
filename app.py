"""CSDN Markdown 图片助手，Windows 本地界面。"""

# 阅读入口：界面把任务交给 BrowserWorker，后者调用 csdn_browser 处理文章；
# md_images 负责识别 Markdown 中的图片和本地路径。
from __future__ import annotations

import os  # 读取 Windows 用户数据目录等环境信息。
from pathlib import Path  # 以跨平台方式拼接和检查文件路径。
import queue  # 在线程之间安全传递任务、事件和确认结果。
import re  # 从上传进度消息中提取已处理图片数量。
import threading  # 将浏览器操作放到后台线程。
import tkinter as tk  # 创建桌面窗口与可绑定到控件的变量。
from tkinter import filedialog, messagebox, ttk  # 文件对话框、提示框和界面控件。

from csdn_browser import (CSDN_EDITOR_URL, HelperError, choose_target, editor_text,
                          has_meaningful_text, process_article)
from md_images import find_images, local_path


# DATA_ROOT 存放助手自己的浏览器登录状态及可选的正文备份。
# 它与用户平时使用的浏览器配置相互独立。
APP_NAME = 'CSDN Markdown 图片助手'  # 统一用于窗口标题和提示框标题。
DATA_ROOT = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'CSDN-MD-Helper'
# 左侧是界面显示名称，右侧是 Playwright 启动对应浏览器时使用的 channel。
BROWSERS = {'Microsoft Edge': 'msedge', 'Google Chrome': 'chrome'}

# 界面颜色集中定义：白色操作区搭配暖灰进度区，橙色只强调主要操作。
UI_COLORS = {
    'page': '#FAF9F8',  # 主页面背景。
    'surface': '#FFFFFF',  # 卡片和标题栏背景。
    'progress_surface': '#F6F2F0',  # 用轻微的暖色区分进度区域。
    'border': '#DFDDDB',  # 分区、输入框和分隔线的边框。
    'text': '#24313A',  # 主要文字。
    'muted': '#59636B',  # 辅助说明和状态文字。
    'brand': '#C84D2C',  # 主按钮、图标和进度条。
    'brand_hover': '#A83A20',  # 主按钮悬停或按下时的颜色。
    'brand_light': '#F7EBE6',  # 次要按钮的悬停背景。
    'field': '#FFFFFF',  # 输入框及日志区域背景。
    'track': '#E7E4E2',  # 进度条尚未完成部分的背景。
}


def enable_system_dpi_awareness():
    # 作用：让 Windows 按系统显示缩放直接绘制界面，避免把低分辨率窗口当作图片放大。
    # 使用场景：创建第一个 Tk 窗口前调用；打包版也在清单中声明相同设置。
    if os.name != 'nt':  # 其他操作系统不提供此 Windows 接口。
        return
    try:
        import ctypes
        ctypes.windll.user32.SetProcessDPIAware()  # 若进程已由清单设置，Windows 会保留原设置。
    except (AttributeError, OSError):
        pass  # 系统不支持该接口时保留原有行为，不阻止程序启动。


# 浏览器操作可能需要等待网络，放在后台线程中执行，避免 Tk 界面卡住。
# commands 接收界面发出的任务，events 向界面发送日志、确认请求和处理结果。
class BrowserWorker(threading.Thread):
    # 作用：在独立线程中管理浏览器并执行用户提交的任务。
    # 使用场景：界面打开后创建，避免浏览器和网络等待阻塞 Tk 窗口。
    def __init__(self, commands: queue.Queue, events: queue.Queue):
        # 作用：初始化工作线程、消息队列及浏览器状态。
        # 使用场景：Application 创建工作线程时自动调用；commands 接收任务，events 发送结果。
        super().__init__(daemon=True, name='csdn-browser-worker')  # 设置后台线程名称与守护属性。
        self.commands = commands  # 接收主线程发送的任务。
        self.events = events  # 将进度、错误和确认请求发送给主线程。
        self.answers = queue.Queue()  # 接收用户对确认弹窗的回答。
        self.playwright = None  # 尚未启动 Playwright，启动后保存其实例。
        self.context = None  # 保存助手专用的持久化浏览器上下文。
        self.browser_name = None  # 保存当前启动的浏览器名称。

    def send(self, kind: str, message: str = ''):
        # 作用：把指定类型的消息放入界面事件队列。
        # 使用场景：后台线程需要更新进度、请求确认或报告结果时使用。
        self.events.put((kind, message))  # 主线程按 kind 判断如何处理 message。

    def _on_browser_closed(self, context):
        # 作用：同步已关闭的浏览器上下文，并通知界面更新状态。
        # 使用场景：用户关闭浏览器、浏览器退出或助手主动切换浏览器时调用。
        if self.context is context:  # 避免旧上下文的延迟事件影响新浏览器。
            closed_name = self.browser_name or ''
            self.context = None
            self.browser_name = None
            self.send('browser_closed', closed_name)

    def _poll_browser(self):
        # 作用：短暂处理 Playwright 事件，让浏览器关闭通知及时到达。
        # 使用场景：工作线程空闲时及执行新任务之前调用；不读取网页内容。
        context = self.context
        if context is None:
            return
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        try:
            # 同步版 Playwright 需要进入其事件循环，才能分发浏览器关闭事件。
            context.wait_for_event('close', timeout=120)
        except PlaywrightTimeoutError:
            pass  # 等待超时表示浏览器仍在运行，之后继续接收任务。
        except Exception:
            # 驱动断开等异常下，当前上下文已无法继续使用。
            self._on_browser_closed(context)

    def _close_current_browser(self):
        # 作用：关闭助手当前管理的浏览器上下文，并同步关闭状态。
        # 使用场景：用户确认切换浏览器，或旧上下文已无页面时调用。
        context = self.context
        if context is None:
            return
        try:
            context.close()
        except Exception as exc:
            # 如果关闭事件已到达，说明浏览器已关闭；否则停止切换以保护内容。
            if self.context is context:
                raise HelperError('无法关闭当前助手浏览器，请保存文章并手动关闭后重试。') from exc
        self._on_browser_closed(context)  # close 事件已处理时不会重复发送通知。

    def open_browser(self, browser_name: str):
        # 作用：打开、重新显示或安全切换助手专用浏览器。
        # 使用场景：用户点击“打开 CSDN”时执行；切换前会确认是否已保存文章。
        self._poll_browser()  # 先处理可能尚未分发的关闭事件。
        context = self.context
        if context is not None:
            pages = [page for page in context.pages if not page.is_closed()]
            if not pages:
                self._close_current_browser()  # 已无页面时清理旧上下文。
            elif self.browser_name == browser_name:
                try:
                    pages[-1].bring_to_front()  # 相同浏览器已打开时，将现有页面切到前台。
                except Exception as exc:
                    self._poll_browser()  # 页面可能刚刚被用户关闭。
                    if self.context is not None:
                        raise HelperError('无法显示当前浏览器，请保存文章并关闭后重试。') from exc
                else:
                    self.send('log', f'{browser_name} 已经打开，已切换到该浏览器。')
                    self.send('ready', browser_name)
                    return
            else:
                current_name = self.browser_name or '当前浏览器'
                self.send('confirm_switch',
                          f'助手当前打开的是 {current_name}。切换到 {browser_name} 会关闭当前助手浏览器。\n\n'
                          '请先保存浏览器中的文章，是否继续切换？')
                confirmed = self.answers.get()  # 主线程显示确认框并返回用户选择。
                self._poll_browser()  # 用户确认期间可能已自行关闭浏览器。
                if not confirmed:
                    self.send('log', '已取消切换浏览器。')
                    if self.context is not None:
                        self.send('ready', self.browser_name or current_name)
                    return
                self._close_current_browser()

        # 所有 Playwright 调用都在此工作线程中执行；已启动的驱动可复用。
        if self.playwright is None:
            from playwright.sync_api import sync_playwright
            self.playwright = sync_playwright().start()
        try:
            # 两种浏览器使用不同的持久化 profile，保留各自的登录状态。
            profile = DATA_ROOT / ('edge-profile' if browser_name == 'Microsoft Edge' else 'chrome-profile')
            profile.mkdir(parents=True, exist_ok=True)
            # 持久化上下文使用独立 profile；headless=False 让用户能看到并登录。
            self.context = self.playwright.chromium.launch_persistent_context(
                str(profile), channel=BROWSERS[browser_name], headless=False, no_viewport=True,
            )
            self.browser_name = browser_name
            self.context.on('close', self._on_browser_closed)  # 浏览器关闭后通知主界面。
            # 优先复用启动时自带的页面，没有页面时再新建。
            page = self.context.pages[0] if self.context.pages else self.context.new_page()
            # 等待页面主要 DOM 加载，最长等待 60 秒。
            page.goto(CSDN_EDITOR_URL, wait_until='domcontentloaded', timeout=60_000)
            self.send('log', f'{browser_name} 已打开。请在新窗口登录 CSDN，打开或新建 Markdown 文章。')  # 提示登录。
            self.send('ready', browser_name)  # 告诉主界面实际打开的浏览器。
        except Exception:
            # 启动任何一步出错都尽可能释放已创建的资源，再交给外层报告。
            if self.context:
                context = self.context
                try:
                    context.close()
                except Exception:
                    pass
                self._on_browser_closed(context)
            if self.playwright:
                try:
                    self.playwright.stop()
                except Exception:
                    pass
                self.playwright = None
            raise  # 重新抛出异常，由工作线程统一向界面报告。

    def process(self, markdown_path: Path, mode: str, backup_existing: bool):
        # 作用：选择目标页面、请求必要的确认，再调用正文处理流程。
        # 使用场景：用户开始处理后执行；参数包含文档路径、处理模式和备份选项。
        self._poll_browser()  # 处理开始前刷新可能尚未收到的浏览器关闭事件。
        if not self.context:  # 没有助手浏览器时无法定位目标编辑器。
            raise HelperError('请先点击“打开 CSDN”，并在新浏览器窗口里登录。')
        if not markdown_path.is_file():  # 文件可能在选择后被移动或删除，因此再次检查。
            raise HelperError('请选择一个存在的 Markdown 文件。')
        # 先读取当前正文用于确认；处理时会再次比较，防止确认后发生覆盖。
        target = choose_target(self.context)
        before = editor_text(target)  # 保留确认前正文，稍后检查其间是否发生变化。
        if mode == 'import' and has_meaningful_text(before):
            # 根据备份开关生成不同的确认说明，避免用户误以为一定有备份。
            backup_note = ('工具会先保存一份本地备份。' if backup_existing
                           else '当前未选择本地备份。')
            # 向主线程发送确认请求，消息说明本次导入会覆盖已有正文。
            self.send('confirm_import', '当前 CSDN 编辑器已有正文。\n\n'
                      f'导入所选 Markdown 会替换当前正文；{backup_note}\n\n是否继续？')
            # 弹窗由主线程显示，后台线程在 answers 队列中等待用户选择。
            if not self.answers.get():
                self.send('log', '已取消：没有上传图片，也没有修改正文。')  # 向界面记录取消结果。
                return  # 用户取消后不再上传或写入。
        self.send('log', '正在同一篇 CSDN 文章中处理正文与图片，请勿编辑正文。')  # 处理前提醒不要编辑。
        # 把正文快照、备份选项和进度回调交给实际处理函数。
        result = process_article(target, markdown_path, DATA_ROOT / 'recovery',
                                 lambda message: self.send('log', message),
                                 mode=mode, expected_before=before, backup_existing=backup_existing)
        target.bring_to_front()  # 完成后把文章页面切到前台，方便用户检查。
        # 根据处理模式和结果统计拼接成功提示；只有实际创建备份才显示路径。
        action = '已将所选 Markdown 导入编辑器' if result.imported else '已修复编辑器中的原正文'
        message = (f'{action}：{result.image_count} 张图片、{result.reference_count} 处引用。\n'
                   '请在 CSDN 预览中检查图片，再自行保存草稿或发布。')
        if result.backup_path:  # 只有创建过备份时才显示路径。
            message += f'\n原正文备份：{result.backup_path}'  # 把备份位置附在结果末尾。
        self.send('success', message)  # 交给主线程显示成功消息。

    def run(self):
        # 作用：循环接收并执行浏览器任务，结束时释放资源。
        # 使用场景：调用线程的 start() 后自动运行；队列收到 None 时退出。
        # 串行消费任务，None 是退出信号；Playwright 相关操作都留在此线程。
        while True:
            try:
                # 浏览器打开时定期处理关闭事件；没有浏览器时继续阻塞等待任务。
                item = self.commands.get(timeout=0.4 if self.context is not None else None)
            except queue.Empty:
                self._poll_browser()
                continue
            if item is None:  # None 是主线程发来的退出信号。
                break
            command, argument = item  # 命令名决定操作，argument 保存所需参数。
            try:
                if command == 'open':  # 打开浏览器任务携带浏览器名称。
                    self.open_browser(argument)  # 在工作线程执行浏览器启动。
                elif command == 'process':  # 文章处理任务携带一组参数。
                    self.process(*argument)  # 将参数组展开后执行处理。
            except HelperError as exc:
                # 可预期错误直接展示已有的中文提示。
                self.send('error', str(exc))
            except Exception as exc:
                # 不主动记录 Cookie、网络请求内容或文章正文。
                self.send('error', f'操作失败：{type(exc).__name__}：{exc}')
            finally:
                # 无论成功或失败，都通知主线程解除忙碌状态。
                self.send('idle')
        try:
            # 退出任务循环后释放浏览器和 Playwright。
            if self.context:
                self.context.close()  # 关闭助手创建的浏览器上下文。
            if self.playwright:
                self.playwright.stop()  # 停止 Playwright 运行实例。
        except Exception:
            pass


class Application(tk.Tk):
    # 作用：组织本地窗口、用户输入和后台任务的桌面界面。
    # 使用场景：运行本程序时创建，并通过 Tk 的事件循环响应操作。
    def __init__(self):
        # 作用：初始化窗口、界面状态、消息队列和后台工作线程。
        # 使用场景：创建 Application 实例时自动调用。
        enable_system_dpi_awareness()  # 必须在创建 Tk 窗口之前设置 Windows 的 DPI 感知。
        super().__init__()  # 初始化 Tk 主窗口。
        # Tk 会按系统 DPI 缩放以磅为单位的字体；像素尺寸另按比例计算。
        self.ui_scale = max(1.0, self.winfo_fpixels('1i') / 96.0)
        self.title(APP_NAME)  # 设置窗口标题。
        self.geometry(f'{self._dpi_pixels(1060)}x{self._dpi_pixels(720)}')  # 保留高缩放下原有的窗口大小。
        self.minsize(self._dpi_pixels(880), self._dpi_pixels(620))  # 后续会根据可用屏幕空间再次限制。
        self.configure(background=UI_COLORS['page'])  # 统一窗口的浅色背景。
        # Tk 控件只由主线程更新，两个队列负责与浏览器后台线程传递消息。
        self.commands: queue.Queue = queue.Queue()
        self.events: queue.Queue = queue.Queue()  # 保存后台线程发回的界面事件。
        self.worker = BrowserWorker(self.commands, self.events)  # 创建共用两个队列的工作线程。
        self.worker.start()  # 启动后台循环，等待界面提交任务。
        self.markdown_path = tk.StringVar()  # 路径输入框对应的值。
        self.browser = tk.StringVar(value='Microsoft Edge')  # 默认浏览器。
        self.mode = tk.StringVar(value='import')  # 默认导入所选 Markdown。
        self.backup_existing = tk.BooleanVar(value=True)  # 默认备份已有正文。
        self.status = tk.StringVar(value='先选择 Markdown 文件，再打开 CSDN。')  # 状态提示。
        self.browser_status = tk.StringVar(value='浏览器未打开')  # 只反映浏览器状态，不推断登录状态。
        self.progress_value = tk.DoubleVar(value=0)  # 已完成图片所占的百分比。
        self.progress_text = tk.StringVar(value='等待开始')  # 进度条旁边的说明文字。
        self.busy = False  # 当前是否正在执行任务。
        self.browser_open = False  # 助手浏览器是否已成功打开。
        self.current_browser = None  # 记录实际打开的浏览器，与下拉框的当前选择区分。
        self.active_action = None  # 区分打开浏览器和处理文章，避免显示错误的进度状态。
        self.build_ui()  # 根据上述状态变量创建并排列控件。
        self._fit_minimum_window()  # 根据实际字体缩放调整最小尺寸，避免控件被挤出窗口。
        self._poll_after_id = self.after(100, self.poll_events)  # 定时读取事件，退出时取消回调。
        self.protocol('WM_DELETE_WINDOW', self.close_app)  # 接管窗口关闭操作，先做必要确认。

    def build_ui(self):
        # 作用：按用户选择的方案创建左侧操作区和右侧进度区。
        # 使用场景：窗口初始化时调用；原有变量、按钮回调和后台处理流程保持不变。
        colors = UI_COLORS
        style = ttk.Style(self)
        if 'clam' in style.theme_names():  # clam 允许在 Windows 上统一调整控件配色。
            style.theme_use('clam')

        # 主按钮使用橙色；次要按钮保持浅色，并分别设置禁用、悬停和键盘焦点状态。
        style.configure('Primary.TButton', font=('Microsoft YaHei UI', 10, 'bold'),
                        foreground='#FFFFFF', background=colors['brand'], borderwidth=0,
                        padding=(16, 9), focuscolor=colors['text'])
        style.map('Primary.TButton',
                  background=[('disabled', '#C8B2AA'), ('pressed', colors['brand_hover']),
                              ('active', colors['brand_hover'])], foreground=[('disabled', '#FFFFFF')])
        style.configure('Secondary.TButton', font=('Microsoft YaHei UI', 10),
                        foreground=colors['text'], background='#F5F4F3',
                        bordercolor='#8A8886', lightcolor='#8A8886', darkcolor='#8A8886',
                        borderwidth=1, padding=(12, 7), focuscolor=colors['brand'])
        style.map('Secondary.TButton',
                  background=[('disabled', '#EDEBEA'), ('pressed', '#EAD8D1'),
                              ('active', colors['brand_light'])],
                  foreground=[('disabled', '#797979')])

        # 保留原生输入框和下拉选择；焦点边框与主色一致，便于键盘操作。
        style.configure('Form.TEntry', fieldbackground=colors['field'], foreground=colors['text'],
                        bordercolor='#8A8886', lightcolor='#8A8886', darkcolor='#8A8886', padding=(9, 7))
        style.map('Form.TEntry', bordercolor=[('focus', colors['brand'])],
                  lightcolor=[('focus', colors['brand'])], darkcolor=[('focus', colors['brand'])])
        style.configure('Form.TCombobox', fieldbackground=colors['field'], foreground=colors['text'],
                        background='#F5F4F3', bordercolor='#8A8886', padding=(8, 6))
        style.map('Form.TCombobox', fieldbackground=[('readonly', colors['field']), ('disabled', '#EDEBEA')],
                  foreground=[('disabled', '#797979')], bordercolor=[('focus', colors['brand'])])
        style.configure('Choice.TRadiobutton', background=colors['surface'], foreground=colors['text'],
                        font=('Microsoft YaHei UI', 10), padding=(0, 2))
        style.map('Choice.TRadiobutton', background=[('active', colors['surface'])],
                  indicatorcolor=[('selected', colors['brand'])])
        style.configure('Choice.TCheckbutton', background=colors['surface'], foreground=colors['text'],
                        font=('Microsoft YaHei UI', 10), padding=(0, 2))
        style.map('Choice.TCheckbutton', background=[('active', colors['surface'])],
                  indicatorcolor=[('selected', colors['brand'])])
        style.configure('Upload.Horizontal.TProgressbar', troughcolor=colors['track'],
                        background=colors['brand'], bordercolor=colors['track'],
                        lightcolor=colors['brand'], darkcolor=colors['brand'], thickness=9)

        # 标题说明这是一项什么任务；不把原型图中的“方案一”放进正式界面。
        self.header = tk.Frame(self, background=colors['page'], padx=24, pady=14)
        self.header.pack(fill='x')
        title_label = tk.Label(self.header, text=APP_NAME, background=colors['page'], foreground=colors['text'],
                               font=('Microsoft YaHei UI', 17, 'bold'), anchor='w', justify='left')
        title_label.pack(fill='x')
        title_label.bind('<Configure>', self._wrap_label_to_width)
        subtitle = tk.Label(self.header, text='导入 Markdown 正文，上传并替换本地图片', background=colors['page'],
                            foreground=colors['muted'], font=('Microsoft YaHei UI', 9), anchor='w', justify='left')
        subtitle.pack(fill='x', pady=(3, 0))
        subtitle.bind('<Configure>', self._wrap_label_to_width)

        # 外层画布只在内容超出窗口时滚动；窄窗口中两栏会自动改为上下排列。
        page_host = tk.Frame(self, background=colors['page'])
        page_host.pack(fill='both', expand=True, padx=20, pady=(0, 20))
        page_host.grid_rowconfigure(0, weight=1)
        page_host.grid_columnconfigure(0, weight=1)
        self.page_canvas = tk.Canvas(page_host, background=colors['page'], highlightthickness=0,
                                     borderwidth=0, width=850, height=520, takefocus=0)
        self.page_canvas.grid(row=0, column=0, sticky='nsew')
        self.page_scrollbar = ttk.Scrollbar(page_host, orient='vertical', command=self.page_canvas.yview)
        self.page_canvas.configure(yscrollcommand=self.page_scrollbar.set)
        page = tk.Frame(self.page_canvas, background=colors['page'])
        self.page_content = page
        self.page_window = self.page_canvas.create_window(0, 0, anchor='nw', window=page)
        self._page_layout_pending = False
        self._page_scroll_visible = False
        self._stacked_layout = None
        self._compact_form_layout = None

        # 左右分区分别承担操作和观察进度；扩大窗口时两个区域一起伸展。
        page.grid_rowconfigure(0, weight=1)
        page.grid_columnconfigure(0, weight=5)
        page.grid_columnconfigure(1, weight=4)
        self.task_panel = tk.Frame(page, background=colors['surface'], highlightbackground=colors['border'],
                                   highlightthickness=1, borderwidth=0)
        self.task_panel.grid(row=0, column=0, sticky='nsew', padx=(0, 8))
        self.activity_panel = tk.Frame(page, background=colors['progress_surface'],
                                       highlightbackground=colors['border'], highlightthickness=1, borderwidth=0)
        self.activity_panel.grid(row=0, column=1, sticky='nsew', padx=(8, 0))
        left = tk.Frame(self.task_panel, background=colors['surface'], padx=20, pady=16)
        left.pack(fill='both', expand=True)
        right = tk.Frame(self.activity_panel, background=colors['progress_surface'], padx=20, pady=16)
        right.pack(fill='both', expand=True)

        # 文档输入沿用可编辑路径和文件选择按钮，已有 Markdown 解析逻辑无需更改。
        self._section_heading(left, '文档文件', 'document', colors['surface'])
        self._description(left, '选择本地 Markdown 文件，支持 .md 和 .markdown。', colors['surface'])
        self.file_row = tk.Frame(left, background=colors['surface'])
        self.file_row.pack(fill='x', pady=(8, 0))
        self.file_entry = ttk.Entry(self.file_row, textvariable=self.markdown_path, style='Form.TEntry', width=20)
        self.file_button = ttk.Button(self.file_row, text='选择文件…', command=self.select_file,
                                      style='Secondary.TButton')
        self.file_button.pack(side='right', padx=(10, 0))
        self.file_entry.pack(side='left', fill='x', expand=True)

        self._divider(left, colors['surface'])
        self._section_heading(left, '浏览器', 'browser', colors['surface'])
        self._description(left, '打开助手浏览器后，请登录并进入 Markdown 编辑器。', colors['surface'])
        self.browser_row = tk.Frame(left, background=colors['surface'])
        self.browser_row.pack(fill='x', pady=(8, 0))
        self.browser_combo = ttk.Combobox(self.browser_row, textvariable=self.browser, values=list(BROWSERS),
                                          state='readonly', width=16, style='Form.TCombobox')
        self.open_button = ttk.Button(self.browser_row, text='打开 CSDN', command=self.open_browser,
                                       style='Secondary.TButton')
        self.open_button.pack(side='right', padx=(10, 0))
        self.browser_combo.pack(side='left', fill='x', expand=True)
        # 只展示浏览器是否打开，不推测 CSDN 的登录状态。
        tk.Label(left, textvariable=self.browser_status, background=colors['surface'], foreground=colors['muted'],
                 font=('Microsoft YaHei UI', 9), anchor='w').pack(fill='x', pady=(5, 0))

        self._divider(left, colors['surface'])
        self._section_heading(left, '处理方式', 'settings', colors['surface'])
        self.choices = tk.Frame(left, background=colors['surface'])
        self.choices.pack(fill='x', pady=(6, 0))
        self.import_radio = ttk.Radiobutton(self.choices, text='导入 Markdown', variable=self.mode,
                                            value='import', style='Choice.TRadiobutton')
        self.import_radio.pack(side='left', padx=(0, 22))
        self.repair_radio = ttk.Radiobutton(self.choices, text='仅修复图片', variable=self.mode,
                                            value='repair', style='Choice.TRadiobutton')
        self.repair_radio.pack(side='left')
        self._description(left, '导入会替换当前正文；仅修复图片会保留正文。', colors['surface'], top=5)
        self.backup_checkbox = ttk.Checkbutton(left, text='处理前备份已有正文',
                                               variable=self.backup_existing, style='Choice.TCheckbutton')
        self.backup_checkbox.pack(anchor='w', pady=(12, 0))
        self.process_button = ttk.Button(left, text='开始处理正文与图片', command=self.process,
                                          style='Primary.TButton')
        self.process_button.pack(fill='x', pady=(14, 0))

        # 进度文字、进度条和日志保留原状态变量；处理时日志随右侧区域扩展。
        self._section_heading(right, '处理进度', 'progress', colors['progress_surface'])
        tk.Label(right, textvariable=self.progress_text, background=colors['progress_surface'],
                 foreground=colors['muted'], font=('Microsoft YaHei UI', 10), anchor='w').pack(fill='x', pady=(8, 5))
        self.progress_bar = ttk.Progressbar(right, variable=self.progress_value, maximum=100,
                                             style='Upload.Horizontal.TProgressbar')
        self.progress_bar.pack(fill='x')
        self._divider(right, colors['progress_surface'])
        self._section_heading(right, '处理日志', 'log', colors['progress_surface'])
        # 状态栏保持两行，长文件名的完整内容仍可在下方可滚动日志中查看。
        self.status_label = tk.Label(right, textvariable=self.status, background=colors['progress_surface'],
                                     foreground=colors['muted'], font=('Microsoft YaHei UI', 9),
                                     anchor='w', justify='left', wraplength=280, height=2)
        self.status_label.pack(fill='x', pady=(6, 8))
        self.status_label.bind('<Configure>', self._wrap_label_to_width)
        log_frame = tk.Frame(right, background=colors['field'], highlightbackground='#C9C7C5',
                             highlightthickness=1, borderwidth=0)
        log_frame.pack(fill='both', expand=True)
        self.log = tk.Text(log_frame, width=25, height=5, wrap='word', state='disabled',
                           font=('Microsoft YaHei UI', 9), relief='flat', borderwidth=0,
                           background=colors['field'], foreground=colors['text'],
                           padx=10, pady=8, highlightthickness=0)
        scroll = ttk.Scrollbar(log_frame, orient='vertical', command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(side='left', fill='both', expand=True)
        scroll.pack(side='right', fill='y')
        self.append_log('原 Markdown 文件不会被改写；处理完成后请自行检查并发布。')

        # 尺寸变化时重新计算布局；焦点移动到屏幕外控件时自动滚动到该控件。
        page.bind('<Configure>', self._schedule_page_layout)
        self.page_canvas.bind('<Configure>', self._schedule_page_layout)
        self.task_panel.bind('<Configure>', self._schedule_page_layout)
        self.bind('<MouseWheel>', self._scroll_page, add='+')
        for widget in (self.file_entry, self.file_button, self.browser_combo, self.open_button,
                       self.import_radio, self.repair_radio, self.backup_checkbox, self.process_button):
            widget.bind('<FocusIn>', lambda event: self.after_idle(self._ensure_widget_visible, event.widget), add='+')

    def _section_heading(self, parent, title: str, icon: str, background: str):
        # 作用：绘制带统一线条图标的区域标题。
        # 使用场景：文档、浏览器、处理方式、进度和日志区域共用。
        row = tk.Frame(parent, background=background)
        row.pack(fill='x')
        canvas = tk.Canvas(row, width=self._dpi_pixels(24), height=self._dpi_pixels(24),
                           background=background, highlightthickness=0)
        canvas.pack(side='left', padx=(0, 9))
        self._draw_icon(canvas, icon)  # 使用本地矢量线条，无需额外图片文件。
        tk.Label(row, text=title, background=background, foreground=UI_COLORS['text'],
                 font=('Microsoft YaHei UI', 12, 'bold'), anchor='w').pack(side='left')

    def _draw_icon(self, canvas: tk.Canvas, icon: str):
        # 作用：使用相同颜色和线宽画出简洁的功能图标。
        # 使用场景：创建区域标题时调用，不依赖外部字体或网络图标。
        color = UI_COLORS['brand']
        stroke = self._dpi_pixels(2)  # 图标线宽随 DPI 变化，避免高缩放时显得过细。
        line = {'fill': color, 'width': stroke, 'capstyle': 'round', 'joinstyle': 'round'}
        if icon == 'document':
            canvas.create_line(5, 2, 15, 2, 20, 7, 20, 22, 5, 22, 5, 2, **line)
            canvas.create_line(15, 2, 15, 7, 20, 7, **line)
            canvas.create_line(9, 12, 16, 12, **line)
            canvas.create_line(9, 16, 16, 16, **line)
        elif icon == 'browser':
            canvas.create_rectangle(3, 3, 21, 17, outline=color, width=stroke)
            canvas.create_line(12, 17, 12, 21, **line)
            canvas.create_line(8, 21, 16, 21, **line)
        elif icon == 'settings':
            canvas.create_oval(7, 7, 17, 17, outline=color, width=stroke)
            for x1, y1, x2, y2 in ((12, 2, 12, 5), (12, 19, 12, 22), (2, 12, 5, 12),
                                   (19, 12, 22, 12), (5, 5, 7, 7), (17, 17, 19, 19),
                                   (17, 7, 19, 5), (5, 19, 7, 17)):
                canvas.create_line(x1, y1, x2, y2, **line)
        elif icon == 'progress':
            canvas.create_arc(3, 3, 21, 21, start=30, extent=135, style='arc', outline=color, width=stroke)
            canvas.create_arc(3, 3, 21, 21, start=210, extent=135, style='arc', outline=color, width=stroke)
            canvas.create_line(4, 6, 4, 10, 8, 10, **line)
            canvas.create_line(20, 18, 20, 14, 16, 14, **line)
        elif icon == 'log':
            for y in (6, 12, 18):
                canvas.create_oval(3, y - 1, 5, y + 1, outline=color, fill=color)
                canvas.create_line(9, y, 21, y, **line)
        canvas.scale('all', 0, 0, self.ui_scale, self.ui_scale)  # 放大矢量坐标，而不是放大已绘制的位图。

    def _description(self, parent, text: str, background: str, *, top: int = 3):
        # 作用：用一致的字体和对比度显示操作说明。
        # 使用场景：解释文件、浏览器或处理方式时调用。
        label = tk.Label(parent, text=text, background=background, foreground=UI_COLORS['muted'],
                         font=('Microsoft YaHei UI', 9), anchor='w', justify='left', wraplength=460)
        label.pack(fill='x', pady=(top, 0))
        label.bind('<Configure>', self._wrap_label_to_width)

    def _wrap_label_to_width(self, event):
        # 作用：让说明文字按控件实际宽度换行。
        # 使用场景：小屏、极窄窗口或高缩放下避免文字被横向裁掉。
        width = max(1, event.width - 2)
        if int(event.widget.cget('wraplength')) != width:
            event.widget.configure(wraplength=width)

    def _divider(self, parent, background: str):
        # 作用：在同一区域内用一条细线分隔不同操作。
        # 使用场景：左侧步骤之间，以及右侧进度和日志之间。
        line = tk.Frame(parent, height=1, background=UI_COLORS['border'])
        line.pack(fill='x', pady=15)

    def _schedule_page_layout(self, event=None):
        # 作用：合并连续的尺寸变化，避免重复计算界面布局。
        # 使用场景：用户调整窗口，或内容导致所需空间改变时调用。
        if not self._page_layout_pending:
            self._page_layout_pending = True
            self.after_idle(self._update_page_layout)

    def _update_page_layout(self):
        # 作用：按可用宽度切换双栏或单栏，并在高度不足时启用页面滚动。
        # 使用场景：窗口尺寸、系统缩放或屏幕可用空间发生变化时调用。
        self._page_layout_pending = False
        width, height = self.page_canvas.winfo_width(), self.page_canvas.winfo_height()
        if width <= 1 or height <= 1:  # 窗口尚未完成尺寸计算时等待下一次配置事件。
            return
        form_width = self._horizontal_form_width()
        needed_width = max(self.task_panel.winfo_reqwidth(), form_width + 42) + self.activity_panel.winfo_reqwidth() + 16
        stacked = width < needed_width
        if stacked != self._stacked_layout:
            self._stacked_layout = stacked
            if stacked:  # 窄屏时把进度区放到操作区下方，避免左右控件互相挤压。
                self.page_content.grid_columnconfigure(0, weight=1)
                self.page_content.grid_columnconfigure(1, weight=0)
                self.page_content.grid_rowconfigure(0, weight=0)
                self.page_content.grid_rowconfigure(1, weight=1)
                self.task_panel.grid_configure(row=0, column=0, padx=0, pady=(0, 8))
                self.activity_panel.grid_configure(row=1, column=0, padx=0, pady=(8, 0))
            else:  # 空间足够时恢复用户选择的左右双栏方案。
                self.page_content.grid_columnconfigure(0, weight=5)
                self.page_content.grid_columnconfigure(1, weight=4)
                self.page_content.grid_rowconfigure(0, weight=1)
                self.page_content.grid_rowconfigure(1, weight=0)
                self.task_panel.grid_configure(row=0, column=0, padx=(0, 8), pady=0)
                self.activity_panel.grid_configure(row=0, column=1, padx=(8, 0), pady=0)

        # 当单栏仍然很窄时，把输入框、按钮和选项从同行改为上下排列。
        task_width = self.task_panel.winfo_width() - 42
        if task_width > 1:
            self._set_compact_form(task_width < form_width)

        # 页面至少填满视口；内容更高时保留完整高度，让滚动条可以到达底部。
        content_height = max(height, self.page_content.winfo_reqheight())
        old_width = float(self.page_canvas.itemcget(self.page_window, 'width') or 0)
        old_height = float(self.page_canvas.itemcget(self.page_window, 'height') or 0)
        if old_width != width or old_height != content_height:
            self.page_canvas.itemconfigure(self.page_window, width=width, height=content_height)
        self.page_canvas.configure(scrollregion=(0, 0, width, content_height))
        need_scroll = content_height > height
        if need_scroll != self._page_scroll_visible:
            self._page_scroll_visible = need_scroll
            if need_scroll:
                self.page_scrollbar.grid(row=0, column=1, sticky='ns')
            else:
                self.page_scrollbar.grid_remove()

    def _horizontal_form_width(self):
        # 作用：计算文档、浏览器和处理方式保持同行所需的最小宽度。
        # 使用场景：布局变化时判断是否需要把控件改为上下排列。
        file_width = self.file_button.winfo_reqwidth() + max(100, self.file_entry.winfo_reqheight() * 3) + 10
        browser_width = self.browser_combo.winfo_reqwidth() + self.open_button.winfo_reqwidth() + 10
        choices_width = self.import_radio.winfo_reqwidth() + self.repair_radio.winfo_reqwidth() + 22
        return max(file_width, browser_width, choices_width)

    def _set_compact_form(self, compact: bool):
        # 作用：在同行和上下排列之间切换表单控件，不改变控件本身或其焦点顺序。
        # 使用场景：窗口很窄时避免按钮、输入框和单选项被截断。
        if compact == self._compact_form_layout:
            return
        self._compact_form_layout = compact
        for widget in (self.file_entry, self.file_button, self.browser_combo, self.open_button,
                       self.import_radio, self.repair_radio):
            widget.pack_forget()
        if compact:
            self.file_entry.pack(fill='x')
            self.file_button.pack(anchor='w', pady=(6, 0))
            self.browser_combo.pack(fill='x')
            self.open_button.pack(anchor='w', pady=(6, 0))
            self.import_radio.pack(anchor='w')
            self.repair_radio.pack(anchor='w', pady=(4, 0))
        else:
            self.file_button.pack(side='right', padx=(10, 0))
            self.file_entry.pack(side='left', fill='x', expand=True)
            self.open_button.pack(side='right', padx=(10, 0))
            self.browser_combo.pack(side='left', fill='x', expand=True)
            self.import_radio.pack(side='left', padx=(0, 22))
            self.repair_radio.pack(side='left')

    def _scroll_page(self, event):
        # 作用：用鼠标滚轮滚动超出窗口的页面内容。
        # 使用场景：小屏或高缩放时；鼠标在日志框上时保留日志自身的滚动。
        if event.widget is self.log or not self._page_scroll_visible:
            return None
        if event.delta:
            self.page_canvas.yview_scroll(-1 if event.delta > 0 else 1, 'units')
            return 'break'
        return None

    def _ensure_widget_visible(self, widget):
        # 作用：焦点移动到页面外的控件时，将该控件滚动到可见位置。
        # 使用场景：键盘 Tab 在小窗口中依次访问文件、浏览器和处理按钮。
        if not widget.winfo_exists() or not self._page_scroll_visible:
            return
        view_height = self.page_canvas.winfo_height()
        widget_top = widget.winfo_rooty() - self.page_canvas.winfo_rooty()
        widget_bottom = widget_top + widget.winfo_height()
        current = self.page_canvas.canvasy(0)
        if widget_top < 0:
            target = current + widget_top - 8
        elif widget_bottom > view_height:
            target = current + widget_bottom - view_height + 8
        else:
            return
        content_height = max(1, self.page_content.winfo_height())
        self.page_canvas.yview_moveto(max(0, target) / content_height)

    def _dpi_pixels(self, value: int) -> int:
        # 作用：将基于 96 DPI 设计的像素尺寸换算成当前系统 DPI 的尺寸。
        # 使用场景：计算窗口、图标及屏幕边距，保持高缩放下与旧版相近的可见大小。
        return max(1, round(value * self.ui_scale))

    def _available_window_size(self):
        # 作用：估算当前屏幕可容纳的窗口宽高，给标题栏和任务栏保留空间。
        # 使用场景：启动时限制窗口尺寸；只读取屏幕尺寸，不读取屏幕内容。
        return (max(320, self.winfo_screenwidth() - self._dpi_pixels(32)),
                max(320, self.winfo_screenheight() - self._dpi_pixels(100)))

    def _fit_minimum_window(self):
        # 作用：根据字体、缩放和屏幕空间设置启动尺寸与最小尺寸。
        # 使用场景：创建界面后调用；空间不足时由页面滚动和单栏布局承接。
        self.update_idletasks()
        available_width, available_height = self._available_window_size()
        requested_width = self.task_panel.winfo_reqwidth() + self.activity_panel.winfo_reqwidth() + self._dpi_pixels(56)
        requested_height = self.header.winfo_reqheight() + max(self.task_panel.winfo_reqheight(),
                                                               self.activity_panel.winfo_reqheight()) + self._dpi_pixels(20)
        width = min(max(self._dpi_pixels(1060), requested_width), available_width)
        height = min(max(self._dpi_pixels(720), requested_height), available_height)
        self.minsize(min(self._dpi_pixels(880), available_width), min(self._dpi_pixels(620), available_height))
        self.geometry(f'{width}x{height}')

    def append_log(self, message: str):
        # 作用：向只读日志区域追加消息并滚动到末尾。
        # 使用场景：选择文件或收到后台处理进度时使用；message 是显示文本。
        self.log.configure(state='normal')  # 暂时解除只读，允许程序写入。
        self.log.insert('end', message + '\n')  # 追加消息并换行。
        self.log.see('end')  # 自动滚动到最新内容。
        self.log.configure(state='disabled')  # 写完重新设为只读。

    def _update_progress(self, message: str):
        # 作用：从后台进度消息中提取图片数量，更新进度条和文字。
        # 使用场景：处理文章时收到“正在上传”或“已替换”日志后调用。
        match = re.match(r'^(正在上传|已替换)\s+(\d+)/(\d+)', message)  # 识别操作类型和图片序号。
        if match:
            action, current_text, total_text = match.groups()  # 提取文字形式的当前数和总数。
            current, total = int(current_text), int(total_text)  # 转为整数用于计算百分比。
            if total > 0 and 1 <= current <= total:  # 只处理合理的图片数量。
                # 上传尚未完成时不提前计入；链接替换成功后才计为完成。
                completed = current if action == '已替换' else current - 1
                self.progress_value.set(completed * 100 / total)  # 按已替换数量计算百分比。
                self.progress_text.set(f'{action} {current}/{total}')  # 同时展示当前处理进度。
        elif message.startswith('已取消'):
            self.progress_text.set('已取消')  # 用户取消确认时不显示处理成功。

    def select_file(self):
        # 作用：选择 Markdown 文件并预检查其中的本地图片。
        # 使用场景：用户点击“选择文件”时调用；预检查不改写文档。
        # 文件对话框返回路径字符串；取消选择时返回空字符串。
        selected = filedialog.askopenfilename(title='选择 Markdown 文件',
            filetypes=[('Markdown 文件', '*.md *.markdown'), ('所有文件', '*.*')])
        if not selected:  # 用户取消选择时无需更改当前路径。
            return
        self.markdown_path.set(selected)  # 同步到界面路径输入框。
        path = Path(selected)  # 转为 Path，方便读取和解析相对图片路径。
        try:
            body = path.read_text(encoding='utf-8-sig')  # 支持 UTF-8 文件及可选 BOM。
            # 这里仅预检查并显示数量，真正处理时还会按当前正文重新检查。
            refs = find_images(body)
            local = [p for ref in refs if (p := local_path(ref.source, path)) is not None]  # 过滤网络图片。
            missing = sum(not p.is_file() for p in set(local))  # 去重后统计缺失文件。
            self.append_log(f'所选文件包含 {len(local)} 处本地图片引用；其中 {missing} 张不同图片未找到。')
            if missing:  # 预检查发现缺图时补充说明正式处理会列出具体路径。
                self.append_log('提示：开始处理时会检查文章实际引用的图片，并列出缺失文件。')
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            # 文件无法读取、不是 UTF-8 或路径无效时只记录提示。
            self.append_log(f'读取文件失败：{exc}')

    def set_busy(self, value: bool):
        # 作用：更新忙碌状态并启用或禁用相关控件。
        # 使用场景：任务开始和结束时调用；value 为 True 表示正在处理。
        # 操作期间禁用相关控件，避免同一浏览器同时执行多个任务。
        self.busy = value
        state = 'disabled' if value else 'normal'  # Tk 控件使用字符串表示启用状态。
        self.open_button.configure(state=state)  # 切换“打开 CSDN”按钮。
        self.process_button.configure(state=state)  # 切换“开始处理”按钮。
        self.backup_checkbox.configure(state=state)  # 切换备份选项。
        self.file_entry.configure(state=state)  # 处理期间固定所选文档路径。
        self.file_button.configure(state=state)  # 处理期间不能选择另一份文档。
        self.import_radio.configure(state=state)  # 处理期间固定导入模式。
        self.repair_radio.configure(state=state)  # 处理期间固定修复模式。
        # 空闲时允许选择浏览器；切换已打开的浏览器时由后台请求用户确认。
        self.browser_combo.configure(state='disabled' if value else 'readonly')

    def open_browser(self):
        # 作用：向后台线程提交打开浏览器的任务。
        # 使用场景：用户点击“打开 CSDN”时触发，实际浏览器操作不在界面线程执行。
        if self.busy:  # 已有任务时忽略重复点击。
            return
        self.active_action = 'open'
        self.browser_status.set('正在打开浏览器…')
        self.set_busy(True)  # 先禁用控件，防止任务重复进入队列。
        self.status.set('正在打开助手专用浏览器…')  # 立即给用户反馈。
        self.commands.put(('open', self.browser.get()))  # 将选择的浏览器名称发送给工作线程。

    def process(self):
        # 作用：检查文档和浏览器状态，并提交正文处理任务。
        # 使用场景：用户点击“开始处理正文与图片”时触发。
        if self.busy:  # 后台正在处理时不再提交第二次任务。
            return
        value = self.markdown_path.get().strip()  # 读取路径并去掉首尾空白。
        # 即使选择文件后用户手动修改了输入框，也要重新验证文件是否存在。
        if not value or not Path(value).is_file():
            messagebox.showwarning(APP_NAME, '请先选择本地 Markdown 文件。', parent=self)  # 给出缺少文件的提示。
            return  # 不提交无效任务。
        if not self.browser_open:  # 页面由助手专用浏览器提供，需要先启动。
            messagebox.showwarning(APP_NAME, '请先打开 CSDN，在新窗口登录并进入 Markdown 编辑器。', parent=self)
            return  # 没有浏览器时无法继续。
        if self.current_browser and self.browser.get() != self.current_browser:
            # 下拉框只是待切换的选择，不能让用户误以为正文会写入另一个浏览器。
            messagebox.showwarning(APP_NAME,
                                   f'当前打开的是 {self.current_browser}。请先点击“打开 CSDN”完成浏览器切换，'
                                   '或重新选择当前浏览器后再处理文章。', parent=self)
            return
        self.active_action = 'process'
        self.progress_value.set(0)
        self.progress_text.set('正在准备…')
        self.set_busy(True)  # 锁定相关控件直到工作线程报告 idle。
        self.status.set('正在处理，请在完成前不要修改文章正文。')  # 提醒避免并发修改。
        # 按工作线程 process 方法的参数顺序打包路径、模式和备份选项。
        self.commands.put(('process', (Path(value), self.mode.get(), self.backup_existing.get())))

    def poll_events(self):
        # 作用：读取后台消息并更新界面、确认弹窗和日志。
        # 使用场景：由 Tk 定时调用，确保界面控件只由主线程操作。
        # 主线程定时取出后台消息，因此弹窗、日志等 Tk 操作都发生在主线程。
        try:
            while True:  # 一次回调尽量取完当前已经到达的全部消息。
                kind, message = self.events.get_nowait()  # 不阻塞界面线程。
                if kind == 'log':
                    # 普通进度同时显示在日志框和状态栏。
                    self.append_log(message)  # 追加日志。
                    self.status.set(message)  # 更新最近一条状态。
                    if self.active_action == 'process':
                        self._update_progress(message)
                elif kind == 'ready':  # 浏览器启动成功。
                    # 后台已经成功打开浏览器，允许后续处理文章。
                    self.browser_open = True
                    self.current_browser = message or self.browser.get()
                    self.browser.set(self.current_browser)  # 切换取消时恢复实际打开的浏览器。
                    self.browser_status.set(f'{self.current_browser} 已打开')
                elif kind == 'browser_closed':  # 用户关闭浏览器后允许重新打开或切换。
                    self.browser_open = False
                    self.current_browser = None
                    self.browser_status.set('正在打开浏览器…' if self.active_action == 'open' else '浏览器已关闭')
                    notice = f'{message or "助手浏览器"} 已关闭，可重新打开或切换浏览器。'
                    self.append_log(notice)
                    if self.active_action != 'open':
                        self.status.set(notice)
                    self.browser_combo.configure(state='disabled' if self.busy else 'readonly')
                elif kind in ('confirm_import', 'confirm_switch'):
                    # 在主线程确认覆盖或切换，再通过 answers 队列把选择交回后台。
                    answer = messagebox.askyesno(APP_NAME, message, parent=self)
                    self.worker.answers.put(answer)  # 把 True/False 返回给工作线程。
                elif kind == 'success':  # 正文处理完成。
                    # 成功时保留详细结果，并提示用户检查预览。
                    self.append_log(message)  # 把统计信息保留在日志中。
                    self.status.set('处理完成，请检查 CSDN 预览。')  # 更新状态栏。
                    self.progress_value.set(100)
                    self.progress_text.set('处理完成')
                    messagebox.showinfo(APP_NAME, message, parent=self)  # 展示结果弹窗。
                elif kind == 'error':  # 操作过程中发生错误。
                    # 错误信息既写入日志，也通过弹窗提示。
                    self.append_log(message)  # 保留错误文本供用户查看。
                    self.status.set('处理未完成，请查看进度信息。')  # 更新状态栏。
                    if self.active_action == 'open':
                        if self.browser_open and self.current_browser:
                            # 切换失败但原浏览器仍在时，恢复原来的选择和状态。
                            self.browser.set(self.current_browser)
                            self.browser_status.set(f'{self.current_browser} 已打开')
                        else:
                            self.browser_open = False
                            self.current_browser = None
                            self.browser_status.set('浏览器未打开')
                    elif self.active_action == 'process':
                        self.progress_text.set('处理未完成')
                    messagebox.showerror(APP_NAME, message, parent=self)  # 展示错误弹窗。
                elif kind == 'idle':  # 后台完成本次任务后的统一状态。
                    # 后台本轮任务结束，允许用户继续操作。
                    self.set_busy(False)
                    if (self.active_action == 'open' and not self.browser_open
                            and self.browser_status.get() == '正在打开浏览器…'):
                        # 用户在切换确认期间关闭浏览器后又取消，不能一直显示“正在打开”。
                        self.browser_status.set('浏览器已关闭')
                    self.active_action = None
        except queue.Empty:
            pass  # 当前没有更多消息，等待下一次定时回调。
        if self.winfo_exists():  # 窗口尚未销毁时继续轮询。
            self._poll_after_id = self.after(100, self.poll_events)  # 100 毫秒后再次检查。

    def close_app(self):
        # 作用：确认退出，并通知后台线程关闭浏览器资源。
        # 使用场景：用户关闭窗口时调用；任务执行中会暂缓关闭。
        if self.busy:  # 不在处理进行中关闭浏览器，避免中断写入。
            messagebox.showinfo(APP_NAME, '正在处理，请等待本次操作结束。', parent=self)
            return
        # 浏览器将随助手关闭，先提醒用户保存正在编辑的文章。
        if self.browser_open and not messagebox.askokcancel(
                APP_NAME, '退出将关闭助手打开的浏览器窗口。请先确认文章已保存。\n\n确定退出吗？', parent=self):
            return
        self.after_cancel(self._poll_after_id)  # 关闭前取消尚未执行的界面轮询。
        # 通知后台线程关闭浏览器上下文，再销毁本地界面。
        self.commands.put(None)  # 发送退出标记。
        self.worker.join(timeout=5)  # 最多等待后台线程 5 秒。
        self.destroy()  # 销毁 Tk 窗口。


if __name__ == '__main__':
    Application().mainloop()  # 直接运行本文件时创建窗口并处理界面事件。
