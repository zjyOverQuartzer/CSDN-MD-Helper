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

# 界面颜色集中定义，便于统一调整文字对比度、卡片和按钮样式。
UI_COLORS = {
    'page': '#F2F5FA',  # 主页面背景。
    'surface': '#FFFFFF',  # 卡片和标题栏背景。
    'border': '#DFE7F2',  # 卡片和输入框边框。
    'text': '#172B49',  # 主要文字。
    'muted': '#536780',  # 辅助说明和状态文字。
    'brand': '#2456D3',  # 主按钮、进度条和步骤编号。
    'brand_hover': '#1D46B5',  # 主按钮悬停或按下时的颜色。
    'brand_light': '#EAF1FF',  # 次要按钮和步骤编号的浅色背景。
    'field': '#F7F9FD',  # 输入框及日志区域背景。
    'track': '#DFE8F6',  # 进度条尚未完成部分的背景。
}


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

    def open_browser(self, browser_name: str):
        # 作用：启动或复用助手专用浏览器，并打开 CSDN 编辑器。
        # 使用场景：用户点击“打开 CSDN”后执行；browser_name 指定 Edge 或 Chrome。
        # 同一个助手窗口只维护一个浏览器上下文；窗口仍存在时直接复用。
        if self.context:
            # 只要上下文内还有未关闭的页面，认为已有助手浏览器可用。
            if any(not page.is_closed() for page in self.context.pages):
                self.send('log', '助手浏览器已经打开，请在该窗口里操作。')  # 告知用户复用现有窗口。
                self.send('ready')  # 通知界面浏览器已就绪。
                return  # 不再重复启动浏览器。
            # 页面都已关闭时，先释放旧上下文和 Playwright，再重新启动。
            self.context.close()
            self.context = None  # 清空已关闭的上下文引用。
            self.playwright.stop()  # 停止之前的 Playwright 实例。
            self.playwright = None  # 清空已停止的实例引用。
        # 在工作线程内导入并启动同步版 Playwright，之后由同一线程使用。
        from playwright.sync_api import sync_playwright
        self.playwright = sync_playwright().start()  # 启动后保存实例，供创建浏览器和退出时使用。
        # 持久化 profile 使助手重启后仍有机会复用用户之前的登录状态。
        profile = DATA_ROOT / ('edge-profile' if browser_name == 'Microsoft Edge' else 'chrome-profile')
        profile.mkdir(parents=True, exist_ok=True)  # 首次运行时创建该浏览器的专用配置目录。
        try:
            # 持久化上下文使用独立 profile；headless=False 让用户能看到并登录。
            self.context = self.playwright.chromium.launch_persistent_context(
                str(profile), channel=BROWSERS[browser_name], headless=False, no_viewport=True,
            )
            self.browser_name = browser_name  # 保存实际启动的浏览器名称。
            # 优先复用启动时自带的页面，没有页面时再新建。
            page = self.context.pages[0] if self.context.pages else self.context.new_page()
            # 等待页面主要 DOM 加载，最长等待 60 秒。
            page.goto(CSDN_EDITOR_URL, wait_until='domcontentloaded', timeout=60_000)
            self.send('log', f'{browser_name} 已打开。请在新窗口登录 CSDN，打开或新建 Markdown 文章。')  # 提示登录。
            self.send('ready')  # 告诉主界面浏览器已成功打开。
        except Exception:
            # 启动任何一步出错都尽可能释放已创建的资源，再交给外层报告。
            if self.context:
                self.context.close()  # 如果上下文已经创建，先关闭它。
                self.context = None  # 清空引用，避免被误认为可用。
            if self.playwright:
                self.playwright.stop()  # 如果 Playwright 已启动，则将其停止。
                self.playwright = None  # 清空引用。
            raise  # 重新抛出异常，由工作线程统一向界面报告。

    def process(self, markdown_path: Path, mode: str, backup_existing: bool):
        # 作用：选择目标页面、请求必要的确认，再调用正文处理流程。
        # 使用场景：用户开始处理后执行；参数包含文档路径、处理模式和备份选项。
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
            item = self.commands.get()  # 队列为空时等待，不持续占用 CPU。
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
        super().__init__()  # 初始化 Tk 主窗口。
        self.title(APP_NAME)  # 设置窗口标题。
        self.geometry('860x760')  # 为分组卡片和进度区域预留初始空间。
        self.minsize(720, 620)  # 防止窗口缩得过小而无法使用。
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
        self.active_action = None  # 区分打开浏览器和处理文章，避免显示错误的进度状态。
        self.build_ui()  # 根据上述状态变量创建并排列控件。
        self._fit_minimum_window()  # 根据实际字体缩放调整最小尺寸，避免控件被挤出窗口。
        self._poll_after_id = self.after(100, self.poll_events)  # 定时读取事件，退出时取消回调。
        self.protocol('WM_DELETE_WINDOW', self.close_app)  # 接管窗口关闭操作，先做必要确认。

    def build_ui(self):
        # 作用：创建顶部说明、操作卡片、图片进度和处理日志。
        # 使用场景：窗口初始化时调用；所有控件仍使用原来的变量和处理方法。
        colors = UI_COLORS  # 使用统一调色板，避免各个控件颜色不一致。
        style = ttk.Style(self)  # 创建 ttk 组件样式管理器。
        # clam 主题支持统一设置按钮、输入框和进度条颜色。
        if 'clam' in style.theme_names():
            style.theme_use('clam')
        # 主按钮使用蓝底白字，并为禁用、按下和悬停状态分别设置颜色。
        style.configure('Primary.TButton', font=('Microsoft YaHei UI', 10, 'bold'),
                        foreground='#FFFFFF', background=colors['brand'], borderwidth=0,
                        padding=(17, 8), focuscolor=colors['brand'])
        style.map('Primary.TButton',
                  background=[('disabled', '#A8B7CD'), ('pressed', colors['brand_hover']),
                              ('active', colors['brand_hover'])],
                  foreground=[('disabled', '#FFFFFF')])
        # 次要按钮使用浅蓝背景，视觉上与主要的“开始处理”按钮区分。
        style.configure('Secondary.TButton', font=('Microsoft YaHei UI', 10),
                        foreground=colors['brand'], background=colors['brand_light'],
                        borderwidth=0, padding=(14, 8), focuscolor=colors['brand_light'])
        style.map('Secondary.TButton',
                  background=[('disabled', '#E9EDF3'), ('active', '#DCE8FF')],
                  foreground=[('disabled', '#78889C')])
        # 输入框和浏览器下拉框统一使用浅色背景与细边框。
        style.configure('Form.TEntry', fieldbackground=colors['field'],
                        foreground=colors['text'], bordercolor=colors['border'],
                        lightcolor=colors['border'], darkcolor=colors['border'], padding=(9, 7))
        style.configure('Form.TCombobox', fieldbackground=colors['field'],
                        foreground=colors['text'], background=colors['field'],
                        bordercolor=colors['border'], padding=(8, 6))
        style.map('Form.TCombobox',
                  fieldbackground=[('readonly', colors['field']), ('disabled', '#E9EDF3')],
                  foreground=[('disabled', '#78889C')])
        # 单选框、复选框的背景与白色卡片保持一致。
        style.configure('Choice.TRadiobutton', background=colors['surface'],
                        foreground=colors['text'], font=('Microsoft YaHei UI', 10), padding=(0, 2))
        style.map('Choice.TRadiobutton', background=[('active', colors['surface'])])
        style.configure('Choice.TCheckbutton', background=colors['surface'],
                        foreground=colors['text'], font=('Microsoft YaHei UI', 9), padding=(0, 2))
        style.map('Choice.TCheckbutton', background=[('active', colors['surface'])])
        # 进度条的蓝色部分表示已成功替换链接的图片比例。
        style.configure('Upload.Horizontal.TProgressbar', troughcolor=colors['track'],
                        background=colors['brand'], bordercolor=colors['track'],
                        lightcolor=colors['brand'], darkcolor=colors['brand'], thickness=8)

        # 顶部区域突出工具名称和用途，不显示无法确认的登录状态。
        header = tk.Frame(self, background=colors['surface'], padx=24, pady=13)  # 创建白色标题栏。
        header.pack(fill='x')  # 标题栏横向填充窗口。
        # 左侧蓝色标识和标题说明；右侧展示“不需要额外图床”的提示。
        tk.Label(header, text='M', background=colors['brand'], foreground='#FFFFFF',
                 font=('Microsoft YaHei UI', 16, 'bold'), width=3, height=1).pack(side='left')
        heading = tk.Frame(header, background=colors['surface'])  # 标题和副标题容器。
        heading.pack(side='left', padx=(12, 0), fill='x', expand=True)  # 随窗口宽度伸展。
        tk.Label(heading, text=APP_NAME, background=colors['surface'], foreground=colors['text'],
                 font=('Microsoft YaHei UI', 16, 'bold'), anchor='w').pack(fill='x')
        tk.Label(heading, text='导入 Markdown 正文，自动上传并替换本地图片',
                 background=colors['surface'], foreground=colors['muted'],
                 font=('Microsoft YaHei UI', 9), anchor='w').pack(fill='x', pady=(2, 0))
        tk.Label(header, text='无需额外图床', background=colors['brand_light'],
                 foreground=colors['brand'], font=('Microsoft YaHei UI', 9, 'bold'),
                 padx=10, pady=5).pack(side='right')

        # 四个卡片对应选文件、打开浏览器、处理文章和查看结果。
        page = tk.Frame(self, background=colors['page'])  # 承载步骤卡片的主区域。
        page.pack(fill='both', expand=True, padx=20, pady=(12, 12))  # 随窗口大小伸展。
        file_card = self._make_card(page, '01', '选择 Markdown 文件', '支持 .md / .markdown')
        file_row = tk.Frame(file_card, background=colors['surface'])  # 文档路径和按钮同一行。
        file_row.pack(fill='x')
        self.file_entry = ttk.Entry(file_row, textvariable=self.markdown_path, style='Form.TEntry')
        self.file_entry.pack(side='left', fill='x', expand=True)  # 路径框占据剩余横向空间。
        self.file_button = ttk.Button(file_row, text='选择文件…', command=self.select_file,
                                      style='Secondary.TButton')
        self.file_button.pack(side='left', padx=(10, 0))  # 保留按钮和路径框之间的间距。

        browser_card = self._make_card(page, '02', '打开 CSDN 并登录', '请在新窗口打开 Markdown 编辑器')
        browser_row = tk.Frame(browser_card, background=colors['surface'])  # 浏览器选择和状态同一行。
        browser_row.pack(fill='x')
        self.browser_combo = ttk.Combobox(browser_row, textvariable=self.browser, values=list(BROWSERS),
                                          state='readonly', width=19, style='Form.TCombobox')
        self.browser_combo.pack(side='left')  # 下拉框只允许选择预设浏览器。
        self.open_button = ttk.Button(browser_row, text='打开 CSDN', command=self.open_browser,
                                       style='Secondary.TButton')
        self.open_button.pack(side='left', padx=(10, 0))  # 打开按钮跟随下拉框。
        # 只展示浏览器是否打开，不判断用户是否已经登录 CSDN。
        tk.Label(browser_row, textvariable=self.browser_status, background=colors['surface'],
                 foreground=colors['muted'], font=('Microsoft YaHei UI', 9)).pack(side='left', padx=(12, 0))

        mode_card = self._make_card(page, '03', '选择处理方式', '处理期间请勿编辑文章')
        self.import_radio = ttk.Radiobutton(mode_card, text='导入所选 Markdown（替换当前正文）',
                                            variable=self.mode, value='import', style='Choice.TRadiobutton')
        self.import_radio.pack(anchor='w')  # 默认导入所选文档。
        self.repair_radio = ttk.Radiobutton(mode_card, text='仅修复当前正文中的本地图片（保留正文）',
                                            variable=self.mode, value='repair', style='Choice.TRadiobutton')
        self.repair_radio.pack(anchor='w')  # 可切换为保留正文的修复模式。
        action_row = tk.Frame(mode_card, background=colors['surface'])  # 主按钮和备份选项同一行。
        action_row.pack(fill='x', pady=(5, 0))
        self.process_button = ttk.Button(action_row, text='开始处理正文与图片', command=self.process,
                                          style='Primary.TButton')
        self.process_button.pack(side='left')  # 点击后执行原有处理流程。
        self.backup_checkbox = ttk.Checkbutton(action_row, text='处理前备份已有正文',
                                               variable=self.backup_existing, style='Choice.TCheckbutton')
        self.backup_checkbox.pack(side='left', padx=(16, 0))  # 备份选项默认勾选，用户可取消。

        progress_card = self._make_card(page, '04', '处理进度', '完成后请检查预览并自行保存', expand=True)
        progress_row = tk.Frame(progress_card, background=colors['surface'])  # 进度条与数字提示同一行。
        progress_row.pack(fill='x', pady=(0, 7))
        tk.Label(progress_row, textvariable=self.progress_text, background=colors['surface'],
                 foreground=colors['muted'], font=('Microsoft YaHei UI', 9),
                 anchor='e').pack(side='right', padx=(10, 0))
        self.progress_bar = ttk.Progressbar(progress_row, variable=self.progress_value, maximum=100,
                                             style='Upload.Horizontal.TProgressbar')
        self.progress_bar.pack(side='left', fill='x', expand=True)  # 进度条随窗口宽度伸展。
        # 当前状态放在底部，日志区域占用卡片的剩余高度。
        tk.Label(progress_card, textvariable=self.status, background=colors['surface'],
                 foreground=colors['muted'], font=('Microsoft YaHei UI', 9), anchor='w',
                 wraplength=650).pack(side='bottom', fill='x', pady=(7, 0))
        log_frame = tk.Frame(progress_card, background=colors['field'])  # 日志框及滚动条的容器。
        log_frame.pack(fill='both', expand=True)
        self.log = tk.Text(log_frame, height=5, wrap='word', state='disabled',
                           font=('Microsoft YaHei UI', 9), relief='flat', borderwidth=0,
                           background=colors['field'], foreground=colors['text'],
                           padx=9, pady=7, highlightthickness=0)
        scroll = ttk.Scrollbar(log_frame, orient='vertical', command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)  # 文本与滚动条互相同步。
        self.log.pack(side='left', fill='both', expand=True)  # 日志框占用主要空间。
        scroll.pack(side='right', fill='y')  # 滚动条在右侧纵向排列。
        self.append_log('原 Markdown 文件不会被改写；处理完成后请自行检查并发布。')  # 初始提示。

    def _make_card(self, parent, number: str, title: str, hint: str, *, expand: bool = False):
        # 作用：创建统一样式的步骤卡片，并返回用于放置控件的内部容器。
        # 使用场景：构建四个操作区域时复用；expand 为 True 时填满剩余空间。
        colors = UI_COLORS  # 使用与主界面一致的颜色。
        card = tk.Frame(parent, background=colors['surface'], highlightbackground=colors['border'],
                        highlightthickness=1, borderwidth=0)  # 用浅色描边区分卡片。
        card.pack(fill='both' if expand else 'x', expand=expand, pady=(0, 8))  # 日志卡片可向下伸展。
        content = tk.Frame(card, background=colors['surface'], padx=15, pady=9)  # 设置卡片内边距。
        content.pack(fill='both', expand=True)
        heading = tk.Frame(content, background=colors['surface'])  # 放置步骤编号、标题和辅助提示。
        heading.pack(fill='x', pady=(0, 7))
        tk.Label(heading, text=number, background=colors['brand_light'], foreground=colors['brand'],
                 font=('Microsoft YaHei UI', 9, 'bold'), width=3, pady=2).pack(side='left')
        tk.Label(heading, text=title, background=colors['surface'], foreground=colors['text'],
                 font=('Microsoft YaHei UI', 10, 'bold')).pack(side='left', padx=(9, 0))
        tk.Label(heading, text=hint, background=colors['surface'], foreground=colors['muted'],
                 font=('Microsoft YaHei UI', 9), anchor='e').pack(side='right')
        return content  # 调用方在该容器内添加各步骤的具体控件。

    def _fit_minimum_window(self):
        # 作用：根据当前字体和系统缩放比例，计算能容纳控件的窗口最小尺寸。
        # 使用场景：界面创建后调用，避免高缩放时按钮或日志区域被裁掉。
        self.update_idletasks()  # 先让 Tk 计算所有控件需要的空间。
        min_width = max(720, self.winfo_reqwidth())  # 至少容纳所有控件需要的宽度。
        # 日志可以缩小，但至少保留 45 像素用于查看最近的处理信息。
        min_height = max(620, self.winfo_reqheight() - self.log.winfo_reqheight() + 45)  # 为日志保留高度。
        self.minsize(min_width, min_height)  # 用户缩放窗口时不能小于该尺寸。
        self.geometry(f'{max(860, min_width)}x{max(760, min_height)}')  # 初始尺寸也不能小于最小尺寸。

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
        # 浏览器已经打开时也固定浏览器选择，避免与现有上下文不一致。
        self.browser_combo.configure(state='disabled' if value or self.browser_open else 'readonly')

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
                    self.browser_status.set('浏览器已打开')
                elif kind == 'confirm_import':  # 后台等待用户确认覆盖。
                    # 在主线程显示确认框，再通过 answers 队列把选择交回后台。
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
                        self.browser_open = False
                        self.browser_status.set('浏览器未打开')
                    elif self.active_action == 'process':
                        self.progress_text.set('处理未完成')
                    messagebox.showerror(APP_NAME, message, parent=self)  # 展示错误弹窗。
                elif kind == 'idle':  # 后台完成本次任务后的统一状态。
                    # 后台本轮任务结束，允许用户继续操作。
                    self.set_busy(False)
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
