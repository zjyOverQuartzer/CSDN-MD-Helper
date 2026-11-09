"""验证桌面界面的布局、交互及进度显示，不访问真实 CSDN。"""

import gc
from pathlib import Path
import queue
import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch

import app


class DummyWorker:
    # 作用：代替浏览器工作线程，记录界面提交的任务。
    # 使用场景：界面测试中避免启动真实浏览器和访问用户账号。
    def __init__(self, commands, events):
        # 作用：保留任务与事件队列，并准备确认回答队列。
        # 使用场景：Application 初始化时创建测试替身。
        self.commands = commands
        self.events = events
        self.answers = queue.Queue()

    def start(self):
        # 作用：模拟线程启动。
        # 使用场景：界面初始化时调用；测试不需要真正的后台线程。
        pass

    def join(self, timeout=None):
        # 作用：模拟等待线程退出。
        # 使用场景：测试窗口关闭时调用。
        pass


def hidden_tk_init(original, scale=None):
    # 作用：生成一个初始化函数，让测试窗口在绘制前保持透明。
    # 使用场景：测试自己的 Tk 窗口及控件，不读取或截取用户屏幕。
    def initialize(window, *args, **kwargs):
        # 作用：先执行 Tk 原始初始化，再隐藏测试窗口。
        # 使用场景：测试创建 Application 实例时自动调用。
        original(window, *args, **kwargs)
        window.withdraw()
        window.attributes('-alpha', 0.0)
        if scale is not None:
            window.tk.call('tk', 'scaling', scale)
    return initialize


class AppUiTest(unittest.TestCase):
    # 作用：检查默认选项、按钮、任务提交和进度反馈。
    # 使用场景：调整桌面界面后验证功能没有被布局改动破坏。
    def setUp(self):
        # 作用：创建使用模拟工作线程的独立测试窗口。
        # 使用场景：每个界面测试开始前调用。
        self.worker_patch = patch.object(app, 'BrowserWorker', DummyWorker)
        self.worker_patch.start()
        self.addCleanup(self.worker_patch.stop)
        original = tk.Tk.__init__
        self.tk_patch = patch.object(tk.Tk, '__init__', hidden_tk_init(original))
        self.tk_patch.start()
        self.addCleanup(self.tk_patch.stop)
        try:
            self.window = app.Application()
        except tk.TclError as exc:
            self.skipTest(f'Tk 界面不可用：{exc}')
        self.addCleanup(self.close_window)
        self.window.deiconify()
        self.window.update()

    def close_window(self):
        # 作用：关闭测试窗口，并在 Tk 所在线程回收界面对象。
        # 使用场景：每个用例结束后清理测试资源。
        window = self.window
        if window.winfo_exists():
            window.busy = False
            window.browser_open = False
            window.close_app()
        self.window = None  # 去掉测试对 Tk 控件的引用，再在主线程处理循环引用。
        del window
        gc.collect()

    def drain_events(self):
        # 作用：立即处理模拟线程发送的事件。
        # 使用场景：测试状态、进度和按钮变化时调用。
        self.window.after_cancel(self.window._poll_after_id)
        self.window.poll_events()
        self.window.update_idletasks()

    def make_document(self):
        # 作用：创建包含一处缺失图片的临时 Markdown。
        # 使用场景：测试文件选择与处理命令时提供独立输入。
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / '文章.md'
        path.write_text('正文\n![图片](missing.png)', encoding='utf-8')
        return path

    def test_default_values_and_controls_are_visible(self):
        # 作用：确认默认浏览器、处理模式、备份选项和关键控件。
        # 使用场景：界面初始布局或默认值调整后执行。
        self.assertEqual(self.window.browser.get(), 'Microsoft Edge')
        self.assertEqual(self.window.mode.get(), 'import')
        self.assertTrue(self.window.backup_existing.get())
        self.assertEqual(self.window.browser_status.get(), '浏览器未打开')
        self.assertEqual(self.window.progress_text.get(), '等待开始')
        self.assertEqual(self.window.progress_value.get(), 0)
        for name in ('file_entry', 'file_button', 'browser_combo', 'open_button',
                     'import_radio', 'repair_radio', 'backup_checkbox', 'process_button',
                     'progress_bar', 'log'):
            with self.subTest(widget=name):
                self.assertTrue(getattr(self.window, name).winfo_ismapped())

    def test_file_selection_reports_image_count(self):
        # 作用：验证选择文件后路径更新，缺图信息显示在日志中。
        # 使用场景：文件选择控件或图片预检查流程修改后执行。
        path = self.make_document()
        with patch.object(app.filedialog, 'askopenfilename', return_value=str(path)):
            self.window.file_button.invoke()
        self.assertEqual(self.window.markdown_path.get(), str(path))
        self.assertIn('1 处本地图片引用', self.window.log.get('1.0', 'end'))
        self.assertIn('1 张不同图片未找到', self.window.log.get('1.0', 'end'))

    def test_buttons_submit_selected_options_and_lock_during_work(self):
        # 作用：验证打开浏览器、选择修复模式和取消备份后提交的参数。
        # 使用场景：处理按钮、单选框或忙碌状态逻辑调整后执行。
        self.window.browser.set('Google Chrome')
        self.window.open_button.invoke()
        self.assertEqual(self.window.commands.get_nowait(), ('open', 'Google Chrome'))
        self.assertTrue(self.window.busy)
        self.assertEqual(str(self.window.file_button.cget('state')), 'disabled')
        self.assertEqual(str(self.window.repair_radio.cget('state')), 'disabled')

        self.window.events.put(('ready', 'Google Chrome'))
        self.window.events.put(('idle', ''))
        self.drain_events()
        self.assertTrue(self.window.browser_open)
        self.assertEqual(self.window.browser_status.get(), 'Google Chrome 已打开')
        self.assertEqual(str(self.window.file_button.cget('state')), 'normal')
        self.assertEqual(str(self.window.browser_combo.cget('state')), 'readonly')

        path = self.make_document()
        self.window.markdown_path.set(str(path))
        self.window.repair_radio.invoke()
        self.window.backup_checkbox.invoke()
        self.window.process_button.invoke()
        self.assertEqual(self.window.commands.get_nowait(), ('process', (path, 'repair', False)))
        self.assertEqual(self.window.progress_text.get(), '正在准备…')
        self.assertEqual(str(self.window.process_button.cget('state')), 'disabled')

    def test_progress_tracks_completed_images_and_preserves_error_state(self):
        # 作用：验证上传中、已替换和出错时的进度文字与百分比。
        # 使用场景：进度展示或后台日志格式调整后执行。
        self.window.active_action = 'process'
        self.window.set_busy(True)
        self.window.events.put(('log', '正在上传 2/18：image.png'))
        self.drain_events()
        self.assertAlmostEqual(self.window.progress_value.get(), 100 / 18)
        self.assertEqual(self.window.progress_text.get(), '正在上传 2/18')
        self.window.events.put(('log', '已替换 9/18 张图片的链接。'))
        self.drain_events()
        self.assertEqual(self.window.progress_value.get(), 50)
        self.assertEqual(self.window.progress_text.get(), '已替换 9/18')
        self.window.events.put(('error', '测试错误'))
        self.window.events.put(('idle', ''))
        with patch.object(app.messagebox, 'showerror'):
            self.drain_events()
        self.assertEqual(self.window.progress_value.get(), 50)
        self.assertEqual(self.window.progress_text.get(), '处理未完成')
        self.assertFalse(self.window.busy)

    def test_browser_open_error_clears_previous_open_state(self):
        # 作用：浏览器重新打开失败时，不再显示过期的“已打开”状态。
        # 使用场景：用户关闭旧浏览器后重新打开失败时，仍能重新选择浏览器。
        self.window.browser_open = True
        self.window.browser_status.set('浏览器已打开')
        self.window.open_button.invoke()
        self.window.events.put(('error', '浏览器启动失败'))
        self.window.events.put(('idle', ''))
        with patch.object(app.messagebox, 'showerror'):
            self.drain_events()
        self.assertFalse(self.window.browser_open)
        self.assertEqual(self.window.browser_status.get(), '浏览器未打开')
        self.assertEqual(str(self.window.browser_combo.cget('state')), 'readonly')
        self.assertEqual(self.window.progress_text.get(), '等待开始')

    def test_browser_close_allows_reopen_and_switch(self):
        # 作用：浏览器关闭后更新界面，并允许选择其他浏览器重新打开。
        # 使用场景：用户手动关闭助手打开的浏览器后执行回归检查。
        self.window.browser_open = True
        self.window.browser.set('Google Chrome')
        self.window.browser_status.set('Google Chrome 已打开')
        self.window.events.put(('browser_closed', 'Google Chrome'))
        self.drain_events()
        self.assertFalse(self.window.browser_open)
        self.assertEqual(self.window.browser_status.get(), '浏览器已关闭')
        self.assertEqual(str(self.window.browser_combo.cget('state')), 'readonly')
        self.assertIn('可重新打开或切换', self.window.log.get('1.0', 'end'))
        self.window.browser.set('Microsoft Edge')
        self.window.open_button.invoke()
        self.assertEqual(self.window.commands.get_nowait(), ('open', 'Microsoft Edge'))

    def test_changed_selection_requires_switch_before_processing(self):
        # 作用：防止选择其他浏览器后，文章仍被处理到原来的浏览器中。
        # 使用场景：当前 Edge 已打开，但用户选择了 Chrome 却没有完成切换。
        path = self.make_document()
        self.window.markdown_path.set(str(path))
        self.window.browser_open = True
        self.window.current_browser = 'Microsoft Edge'
        self.window.browser.set('Google Chrome')
        with patch.object(app.messagebox, 'showwarning') as warning:
            self.window.process_button.invoke()
        warning.assert_called_once()
        self.assertIn('Microsoft Edge', warning.call_args.args[1])
        self.assertTrue(self.window.commands.empty())
        self.assertFalse(self.window.busy)

    def test_failed_switch_keeps_current_browser_selected(self):
        # 作用：切换失败且旧浏览器仍打开时，界面恢复原浏览器状态。
        # 使用场景：用户确认切换后，旧浏览器无法关闭。
        self.window.browser_open = True
        self.window.current_browser = 'Microsoft Edge'
        self.window.browser.set('Google Chrome')
        self.window.open_button.invoke()
        self.window.commands.get_nowait()
        self.window.events.put(('error', '无法关闭当前助手浏览器'))
        self.window.events.put(('idle', ''))
        with patch.object(app.messagebox, 'showerror'):
            self.drain_events()
        self.assertTrue(self.window.browser_open)
        self.assertEqual(self.window.current_browser, 'Microsoft Edge')
        self.assertEqual(self.window.browser.get(), 'Microsoft Edge')
        self.assertEqual(self.window.browser_status.get(), 'Microsoft Edge 已打开')

    def test_switch_confirmation_returns_answer_and_restores_selection(self):
        # 作用：确认切换提示会把用户选择交给后台，并显示仍在使用的浏览器。
        # 使用场景：当前浏览器仍打开，用户取消切换时执行。
        self.window.browser_open = True
        self.window.browser.set('Google Chrome')
        self.window.open_button.invoke()
        self.assertEqual(self.window.commands.get_nowait(), ('open', 'Google Chrome'))
        self.window.events.put(('confirm_switch', '请先保存文章，是否切换？'))
        with patch.object(app.messagebox, 'askyesno', return_value=False) as confirm:
            self.drain_events()
        confirm.assert_called_once()
        self.assertFalse(self.window.worker.answers.get_nowait())
        self.window.events.put(('ready', 'Microsoft Edge'))
        self.window.events.put(('idle', ''))
        self.drain_events()
        self.assertEqual(self.window.browser.get(), 'Microsoft Edge')
        self.assertEqual(self.window.browser_status.get(), 'Microsoft Edge 已打开')

    def test_cancel_after_browser_closes_does_not_leave_opening_status(self):
        # 作用：确认切换期间浏览器被关闭并取消后，界面显示正确的关闭状态。
        # 使用场景：用户在确认对话框出现后自行关闭浏览器，再取消切换。
        self.window.browser_open = True
        self.window.current_browser = 'Microsoft Edge'
        self.window.active_action = 'open'
        self.window.set_busy(True)
        self.window.events.put(('browser_closed', 'Microsoft Edge'))
        self.window.events.put(('log', '已取消切换浏览器。'))
        self.window.events.put(('idle', ''))
        self.drain_events()
        self.assertFalse(self.window.browser_open)
        self.assertFalse(self.window.busy)
        self.assertEqual(self.window.browser_status.get(), '浏览器已关闭')
        self.assertEqual(str(self.window.browser_combo.cget('state')), 'readonly')

    def test_success_and_cancel_have_distinct_progress_states(self):
        # 作用：区分处理成功与用户取消，避免取消时显示百分之百。
        # 使用场景：成功或取消提示调整后执行。
        self.window.active_action = 'process'
        self.window.events.put(('log', '已取消：没有上传图片，也没有修改正文。'))
        self.drain_events()
        self.assertEqual(self.window.progress_text.get(), '已取消')
        self.assertEqual(self.window.progress_value.get(), 0)
        self.window.events.put(('success', '处理完成'))
        with patch.object(app.messagebox, 'showinfo'):
            self.drain_events()
        self.assertEqual(self.window.progress_text.get(), '处理完成')
        self.assertEqual(self.window.progress_value.get(), 100)

    def test_missing_document_shows_warning_without_starting(self):
        # 作用：确认没有选择文档时提示用户，而不是提交处理任务。
        # 使用场景：处理按钮校验逻辑调整后执行。
        with patch.object(app.messagebox, 'showwarning') as warning:
            self.window.process_button.invoke()
        warning.assert_called_once()
        self.assertTrue(self.window.commands.empty())
        self.assertFalse(self.window.busy)


class AppUiScalingTest(unittest.TestCase):
    # 作用：验证不同系统缩放比例下关键控件不会被裁掉。
    # 使用场景：修改字体、间距或窗口最小尺寸后执行。
    def test_minimum_size_keeps_controls_inside_window(self):
        # 作用：在普通和较高字体缩放比例下检查最小窗口布局。
        # 使用场景：验证小窗口中仍能操作按钮并查看日志。
        for scale in (1.0, 1.75, 2.0):
            with self.subTest(scale=scale):
                original = tk.Tk.__init__
                with patch.object(app, 'BrowserWorker', DummyWorker), \
                        patch.object(tk.Tk, '__init__', hidden_tk_init(original, scale)):
                    try:
                        window = app.Application()
                    except tk.TclError as exc:
                        self.skipTest(f'Tk 界面不可用：{exc}')
                    try:
                        window.deiconify()
                        window.geometry('1x1')  # 窗口管理器会限制到程序计算出的最小尺寸。
                        window.update()
                        for name in ('file_entry', 'file_button', 'browser_combo', 'open_button',
                                     'import_radio', 'repair_radio', 'backup_checkbox', 'process_button',
                                     'progress_bar', 'log'):
                            widget = getattr(window, name)
                            x = widget.winfo_rootx() - window.winfo_rootx()
                            y = widget.winfo_rooty() - window.winfo_rooty()
                            with self.subTest(widget=name):
                                self.assertTrue(widget.winfo_ismapped())
                                self.assertGreater(widget.winfo_width(), 0)
                                self.assertGreater(widget.winfo_height(), 0)
                                self.assertGreaterEqual(x, 0)
                                self.assertGreaterEqual(y, 0)
                                self.assertLessEqual(x + widget.winfo_width(), window.winfo_width())
                                self.assertLessEqual(y + widget.winfo_height(), window.winfo_height())
                        self.assertGreaterEqual(window.log.winfo_height(), 40)
                    finally:
                        window.close_app()
                        widget = None  # 控件也会持有窗口；清除引用后由主线程回收。
                        window = None
                        gc.collect()


if __name__ == '__main__':
    unittest.main()
