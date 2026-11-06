"""CSDN Markdown 图片助手，Windows 本地界面。"""

from __future__ import annotations

import os
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from csdn_browser import (CSDN_EDITOR_URL, HelperError, choose_target, editor_text,
                          has_meaningful_text, process_article)
from md_images import find_images, local_path


APP_NAME = 'CSDN Markdown 图片助手'
DATA_ROOT = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'CSDN-MD-Helper'
BROWSERS = {'Microsoft Edge': 'msedge', 'Google Chrome': 'chrome'}


class BrowserWorker(threading.Thread):
    def __init__(self, commands: queue.Queue, events: queue.Queue):
        super().__init__(daemon=True, name='csdn-browser-worker')
        self.commands = commands
        self.events = events
        self.answers = queue.Queue()
        self.playwright = None
        self.context = None
        self.browser_name = None

    def send(self, kind: str, message: str = ''):
        self.events.put((kind, message))

    def open_browser(self, browser_name: str):
        if self.context:
            if any(not page.is_closed() for page in self.context.pages):
                self.send('log', '助手浏览器已经打开，请在该窗口里操作。')
                self.send('ready')
                return
            self.context.close()
            self.context = None
            self.playwright.stop()
            self.playwright = None
        from playwright.sync_api import sync_playwright
        self.playwright = sync_playwright().start()
        profile = DATA_ROOT / ('edge-profile' if browser_name == 'Microsoft Edge' else 'chrome-profile')
        profile.mkdir(parents=True, exist_ok=True)
        try:
            self.context = self.playwright.chromium.launch_persistent_context(
                str(profile), channel=BROWSERS[browser_name], headless=False, no_viewport=True,
            )
            self.browser_name = browser_name
            page = self.context.pages[0] if self.context.pages else self.context.new_page()
            page.goto(CSDN_EDITOR_URL, wait_until='domcontentloaded', timeout=60_000)
            self.send('log', f'{browser_name} 已打开。请在新窗口登录 CSDN，打开或新建 Markdown 文章。')
            self.send('ready')
        except Exception:
            if self.context:
                self.context.close()
                self.context = None
            if self.playwright:
                self.playwright.stop()
                self.playwright = None
            raise

    def process(self, markdown_path: Path, mode: str, backup_existing: bool):
        if not self.context:
            raise HelperError('请先点击“打开 CSDN”，并在新浏览器窗口里登录。')
        if not markdown_path.is_file():
            raise HelperError('请选择一个存在的 Markdown 文件。')
        target = choose_target(self.context)
        before = editor_text(target)
        if mode == 'import' and has_meaningful_text(before):
            backup_note = ('工具会先保存一份本地备份。' if backup_existing
                           else '当前未选择本地备份。')
            self.send('confirm_import', '当前 CSDN 编辑器已有正文。\n\n'
                      f'导入所选 Markdown 会替换当前正文；{backup_note}\n\n是否继续？')
            if not self.answers.get():
                self.send('log', '已取消：没有上传图片，也没有修改正文。')
                return
        self.send('log', '正在同一篇 CSDN 文章中处理正文与图片，请勿编辑正文。')
        result = process_article(target, markdown_path, DATA_ROOT / 'recovery',
                                 lambda message: self.send('log', message),
                                 mode=mode, expected_before=before, backup_existing=backup_existing)
        target.bring_to_front()
        action = '已将所选 Markdown 导入编辑器' if result.imported else '已修复编辑器中的原正文'
        message = (f'{action}：{result.image_count} 张图片、{result.reference_count} 处引用。\n'
                   '请在 CSDN 预览中检查图片，再自行保存草稿或发布。')
        if result.backup_path:
            message += f'\n原正文备份：{result.backup_path}'
        self.send('success', message)

    def run(self):
        while True:
            item = self.commands.get()
            if item is None:
                break
            command, argument = item
            try:
                if command == 'open':
                    self.open_browser(argument)
                elif command == 'process':
                    self.process(*argument)
            except HelperError as exc:
                self.send('error', str(exc))
            except Exception as exc:
                # Do not log cookies, network payloads or article contents.
                self.send('error', f'操作失败：{type(exc).__name__}：{exc}')
            finally:
                self.send('idle')
        try:
            if self.context:
                self.context.close()
            if self.playwright:
                self.playwright.stop()
        except Exception:
            pass


class Application(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry('760x685')
        self.minsize(680, 610)
        self.commands: queue.Queue = queue.Queue()
        self.events: queue.Queue = queue.Queue()
        self.worker = BrowserWorker(self.commands, self.events)
        self.worker.start()
        self.markdown_path = tk.StringVar()
        self.browser = tk.StringVar(value='Microsoft Edge')
        self.mode = tk.StringVar(value='import')
        self.backup_existing = tk.BooleanVar(value=True)
        self.status = tk.StringVar(value='先选择 Markdown 文件，再打开 CSDN。')
        self.busy = False
        self.browser_open = False
        self.build_ui()
        self.after(100, self.poll_events)
        self.protocol('WM_DELETE_WINDOW', self.close_app)

    def build_ui(self):
        style = ttk.Style(self)
        if 'vista' in style.theme_names():
            style.theme_use('vista')
        style.configure('Title.TLabel', font=('Microsoft YaHei UI', 17, 'bold'))
        style.configure('Body.TLabel', font=('Microsoft YaHei UI', 10))
        style.configure('Step.TLabel', font=('Microsoft YaHei UI', 10, 'bold'))
        outer = ttk.Frame(self, padding=(22, 18))
        outer.pack(fill='both', expand=True)
        ttk.Label(outer, text=APP_NAME, style='Title.TLabel').pack(anchor='w')
        ttk.Label(outer, text='先导入完整正文，再上传本地图片并替换文章中的图片路径。',
                  style='Body.TLabel').pack(anchor='w', pady=(3, 15))

        ttk.Label(outer, text='1  选择本地 Markdown 文件', style='Step.TLabel').pack(anchor='w')
        row = ttk.Frame(outer)
        row.pack(fill='x', pady=(6, 14))
        ttk.Entry(row, textvariable=self.markdown_path).pack(side='left', fill='x', expand=True)
        ttk.Button(row, text='选择文件…', command=self.select_file).pack(side='left', padx=(8, 0))

        ttk.Label(outer, text='2  打开 CSDN 并登录', style='Step.TLabel').pack(anchor='w')
        browser_row = ttk.Frame(outer)
        browser_row.pack(fill='x', pady=(6, 3))
        self.browser_combo = ttk.Combobox(browser_row, textvariable=self.browser,
                                          values=list(BROWSERS), state='readonly', width=19)
        self.browser_combo.pack(side='left')
        self.open_button = ttk.Button(browser_row, text='打开 CSDN', command=self.open_browser)
        self.open_button.pack(side='left', padx=(8, 0))
        ttk.Label(outer, text='将打开助手专用浏览器窗口；首次使用请自行登录。',
                  style='Body.TLabel').pack(anchor='w', pady=(0, 12))

        ttk.Label(outer, text='3  选择处理方式，然后开始处理', style='Step.TLabel').pack(anchor='w')
        ttk.Radiobutton(outer, text='导入所选 Markdown（替换当前正文）',
                        variable=self.mode, value='import').pack(anchor='w', pady=(5, 2))
        ttk.Radiobutton(outer, text='仅修复编辑器当前正文中的本地图片（保留正文）',
                        variable=self.mode, value='repair').pack(anchor='w', pady=(0, 2))
        self.backup_checkbox = ttk.Checkbutton(outer, text='处理前备份已有正文（可取消勾选）',
                                               variable=self.backup_existing)
        self.backup_checkbox.pack(anchor='w', pady=(0, 5))
        self.process_button = ttk.Button(outer, text='开始处理正文与图片', command=self.process)
        self.process_button.pack(anchor='w', pady=(0, 12))

        ttk.Label(outer, text='处理进度', style='Step.TLabel').pack(anchor='w')
        log_frame = ttk.Frame(outer)
        log_frame.pack(fill='both', expand=True, pady=(6, 8))
        self.log = tk.Text(log_frame, height=8, wrap='word', state='disabled',
                           font=('Microsoft YaHei UI', 9), relief='solid', borderwidth=1)
        scroll = ttk.Scrollbar(log_frame, orient='vertical', command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(side='left', fill='both', expand=True)
        scroll.pack(side='right', fill='y')
        ttk.Label(outer, textvariable=self.status, style='Body.TLabel', wraplength=690).pack(anchor='w')
        self.append_log('原 Markdown 文件不会被改写；处理完成后请自行检查并发布。')

    def append_log(self, message: str):
        self.log.configure(state='normal')
        self.log.insert('end', message + '\n')
        self.log.see('end')
        self.log.configure(state='disabled')

    def select_file(self):
        selected = filedialog.askopenfilename(title='选择 Markdown 文件',
            filetypes=[('Markdown 文件', '*.md *.markdown'), ('所有文件', '*.*')])
        if not selected:
            return
        self.markdown_path.set(selected)
        path = Path(selected)
        try:
            body = path.read_text(encoding='utf-8-sig')
            refs = find_images(body)
            local = [p for ref in refs if (p := local_path(ref.source, path)) is not None]
            missing = sum(not p.is_file() for p in set(local))
            self.append_log(f'所选文件包含 {len(local)} 处本地图片引用；其中 {missing} 张不同图片未找到。')
            if missing:
                self.append_log('提示：开始处理时会检查文章实际引用的图片，并列出缺失文件。')
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            self.append_log(f'读取文件失败：{exc}')

    def set_busy(self, value: bool):
        self.busy = value
        state = 'disabled' if value else 'normal'
        self.open_button.configure(state=state)
        self.process_button.configure(state=state)
        self.backup_checkbox.configure(state=state)
        self.browser_combo.configure(state='disabled' if value or self.browser_open else 'readonly')

    def open_browser(self):
        if self.busy:
            return
        self.set_busy(True)
        self.status.set('正在打开助手专用浏览器…')
        self.commands.put(('open', self.browser.get()))

    def process(self):
        if self.busy:
            return
        value = self.markdown_path.get().strip()
        if not value or not Path(value).is_file():
            messagebox.showwarning(APP_NAME, '请先选择本地 Markdown 文件。', parent=self)
            return
        if not self.browser_open:
            messagebox.showwarning(APP_NAME, '请先打开 CSDN，在新窗口登录并进入 Markdown 编辑器。', parent=self)
            return
        self.set_busy(True)
        self.status.set('正在处理，请在完成前不要修改文章正文。')
        self.commands.put(('process', (Path(value), self.mode.get(), self.backup_existing.get())))

    def poll_events(self):
        try:
            while True:
                kind, message = self.events.get_nowait()
                if kind == 'log':
                    self.append_log(message)
                    self.status.set(message)
                elif kind == 'ready':
                    self.browser_open = True
                elif kind == 'confirm_import':
                    answer = messagebox.askyesno(APP_NAME, message, parent=self)
                    self.worker.answers.put(answer)
                elif kind == 'success':
                    self.append_log(message)
                    self.status.set('处理完成，请检查 CSDN 预览。')
                    messagebox.showinfo(APP_NAME, message, parent=self)
                elif kind == 'error':
                    self.append_log(message)
                    self.status.set('处理未完成，请查看进度信息。')
                    messagebox.showerror(APP_NAME, message, parent=self)
                elif kind == 'idle':
                    self.set_busy(False)
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(100, self.poll_events)

    def close_app(self):
        if self.busy:
            messagebox.showinfo(APP_NAME, '正在处理，请等待本次操作结束。', parent=self)
            return
        if self.browser_open and not messagebox.askokcancel(
                APP_NAME, '退出将关闭助手打开的浏览器窗口。请先确认文章已保存。\n\n确定退出吗？', parent=self):
            return
        self.commands.put(None)
        self.worker.join(timeout=5)
        self.destroy()


if __name__ == '__main__':
    Application().mainloop()
