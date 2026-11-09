"""验证助手浏览器的关闭、重新打开与切换；只使用临时配置和本地页面。"""

import ctypes
from ctypes import wintypes
import gc
import os
from pathlib import Path
import queue
import signal
import tempfile
import time
import unittest
from unittest.mock import patch

import app
import playwright.sync_api as sync_api


class ProcessEntry(ctypes.Structure):
    # 作用：描述 Windows 进程快照中的进程编号和父进程编号。
    # 使用场景：测试中定位由当前测试进程启动的浏览器，避免影响其他浏览器。
    _fields_ = [
        ('size', wintypes.DWORD), ('usage', wintypes.DWORD), ('pid', wintypes.DWORD),
        ('heap', ctypes.c_size_t), ('module', wintypes.DWORD), ('threads', wintypes.DWORD),
        ('parent', wintypes.DWORD), ('priority', wintypes.LONG), ('flags', wintypes.DWORD),
        ('exe', wintypes.WCHAR * 260),
    ]


def owned_browser_root(executable_name: str) -> int:
    # 作用：找到当前测试进程树内唯一的指定浏览器主进程。
    # 使用场景：模拟用户关闭测试浏览器，只允许结束测试自己启动的进程。
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.Process32FirstW.restype = wintypes.BOOL
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.Process32NextW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    processes = {}
    try:
        entry = ProcessEntry()
        entry.size = ctypes.sizeof(entry)
        valid = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while valid:
            processes[entry.pid] = (entry.parent, entry.exe.lower())
            valid = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    descendants = {os.getpid()}
    while True:
        updated = descendants | {pid for pid, (parent, _) in processes.items() if parent in descendants}
        if updated == descendants:
            break
        descendants = updated
    browsers = {pid for pid in descendants if processes.get(pid, (0, ''))[1] == executable_name.lower()}
    roots = [pid for pid in browsers if processes[pid][0] not in browsers]
    if len(roots) != 1:
        raise RuntimeError(f'测试进程中应有一个 {executable_name} 主进程，实际找到 {len(roots)} 个')
    return roots[0]


def chrome_available() -> bool:
    # 作用：检查常见安装位置是否存在 Google Chrome。
    # 使用场景：仅在本机有 Chrome 时执行真实双浏览器切换测试。
    roots = [os.environ.get('PROGRAMFILES'), os.environ.get('PROGRAMFILES(X86)'), os.environ.get('LOCALAPPDATA')]
    return any(root and (Path(root) / 'Google/Chrome/Application/chrome.exe').is_file() for root in roots)


class HeadlessChromium:
    # 作用：将测试启动的浏览器强制设置为无界面模式。
    # 使用场景：验证真实 Playwright 行为而不打开用户可见窗口。
    def __init__(self, inner):
        # 作用：保存真实 Chromium 启动器。
        # 使用场景：创建测试代理时调用。
        self.inner = inner

    def launch_persistent_context(self, *args, **kwargs):
        # 作用：使用临时 profile 启动无界面浏览器。
        # 使用场景：BrowserWorker 在回归测试中请求打开浏览器时调用。
        kwargs['headless'] = True
        return self.inner.launch_persistent_context(*args, **kwargs)


class HeadlessPlaywright:
    # 作用：包装真实 Playwright 对象并替换浏览器启动方式。
    # 使用场景：仅供浏览器生命周期测试使用。
    def __init__(self, inner):
        # 作用：保存真实对象并提供无界面启动代理。
        # 使用场景：测试启动 Playwright 后调用。
        self.inner = inner
        self.chromium = HeadlessChromium(inner.chromium)

    def stop(self):
        # 作用：停止真实 Playwright 驱动。
        # 使用场景：测试工作线程结束时调用。
        self.inner.stop()


class HeadlessManager:
    # 作用：创建使用无界面浏览器的 Playwright 代理。
    # 使用场景：替代测试中的 sync_playwright()。
    def __init__(self, factory):
        # 作用：保存原始 Playwright 工厂。
        # 使用场景：建立测试代理时调用。
        self.factory = factory

    def start(self):
        # 作用：在当前工作线程启动真实 Playwright。
        # 使用场景：BrowserWorker 首次打开测试浏览器时调用。
        return HeadlessPlaywright(self.factory().start())


@unittest.skipUnless(os.name == 'nt', 'Windows 浏览器生命周期测试')
class BrowserWorkerLifecycleTest(unittest.TestCase):
    # 作用：使用独立无界面浏览器验证关闭、重开及切换。
    # 使用场景：浏览器线程、状态同步或切换逻辑修改后执行。
    def setUp(self):
        # 作用：准备隔离的 profile、无界面浏览器代理和工作线程。
        # 使用场景：每个生命周期测试开始前调用。
        # 先在主线程清理前面界面测试留下的 Tk 循环引用，避免浏览器线程回收 Tk 对象。
        gc.collect()
        self.temp = tempfile.TemporaryDirectory(prefix='csdn-browser-lifecycle-')
        self.addCleanup(self.temp.cleanup)
        self.commands = queue.Queue()
        self.events = queue.Queue()
        real_factory = sync_api.sync_playwright
        patches = [
            patch.object(app, 'DATA_ROOT', Path(self.temp.name)),
            patch.object(app, 'CSDN_EDITOR_URL', 'data:text/html,<title>Local test</title>'),
            patch.object(sync_api, 'sync_playwright', lambda: HeadlessManager(real_factory)),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.worker = app.BrowserWorker(self.commands, self.events)
        self.worker.start()
        self.addCleanup(self.stop_worker)

    def stop_worker(self):
        # 作用：解除可能等待的确认并关闭测试工作线程。
        # 使用场景：测试结束或失败后清理测试浏览器。
        if self.worker.is_alive():
            self.worker.answers.put(False)
            self.commands.put(None)
            self.worker.join(timeout=20)
        self.assertFalse(self.worker.is_alive(), '测试工作线程未退出')

    def wait_for(self, marker: str, timeout: float = 30):
        # 作用：收集后台事件，直到指定事件出现。
        # 使用场景：等待浏览器打开、关闭或请求切换确认。
        received = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                event = self.events.get(timeout=max(0.1, deadline - time.monotonic()))
            except queue.Empty:
                break
            received.append(event)
            if event[0] == 'error':
                self.fail(event[1])
            if event[0] == marker:
                return received
        self.fail(f'等待 {marker} 超时，已收到：{received}')

    def open_browser(self, name: str):
        # 作用：提交打开任务并等待工作线程空闲。
        # 使用场景：测试首次打开或重新打开浏览器。
        self.commands.put(('open', name))
        events = self.wait_for('idle')
        self.assertIn(('ready', name), events)
        return events

    def test_reopen_existing_browser_without_creating_another(self):
        # 作用：重复打开同一浏览器时复用已有窗口。
        # 使用场景：用户窗口被遮挡后再次点击“打开 CSDN”。
        self.open_browser('Microsoft Edge')
        before = owned_browser_root('msedge.exe')
        events = self.open_browser('Microsoft Edge')
        self.assertEqual(owned_browser_root('msedge.exe'), before)
        self.assertTrue(any('已经打开' in message for kind, message in events if kind == 'log'))

    def test_closed_browser_is_reported_and_can_reopen(self):
        # 作用：模拟浏览器关闭，验证后台自动更新并能再次打开。
        # 使用场景：用户手动关闭助手浏览器后重新打开同一种浏览器。
        self.open_browser('Microsoft Edge')
        pid = owned_browser_root('msedge.exe')
        os.kill(pid, signal.SIGTERM)  # 只结束当前测试进程启动的无界面浏览器。
        events = self.wait_for('browser_closed')
        self.assertIn(('browser_closed', 'Microsoft Edge'), events)
        self.assertIsNone(self.worker.context)
        self.open_browser('Microsoft Edge')

    def test_reopen_immediately_after_browser_closes(self):
        # 作用：浏览器刚关闭就点击重新打开时，不会复用已经失效的页面。
        # 使用场景：用户关闭窗口后马上点击“打开 CSDN”。
        self.open_browser('Microsoft Edge')
        pid = owned_browser_root('msedge.exe')
        os.kill(pid, signal.SIGTERM)
        events = self.open_browser('Microsoft Edge')
        self.assertTrue(any(kind == 'browser_closed' for kind, _ in events))
        self.assertFalse(any('已经打开' in message for kind, message in events if kind == 'log'))

    @unittest.skipUnless(chrome_available(), '未安装 Google Chrome')
    def test_close_chrome_then_open_edge(self):
        # 作用：关闭 Chrome 后可以直接切换到 Edge，不要求关闭已不存在的浏览器。
        # 使用场景：用户先关闭 Chrome 窗口，再选择 Edge 并打开。
        self.open_browser('Google Chrome')
        pid = owned_browser_root('chrome.exe')
        os.kill(pid, signal.SIGTERM)
        events = self.wait_for('browser_closed')
        self.assertIn(('browser_closed', 'Google Chrome'), events)
        events = self.open_browser('Microsoft Edge')
        self.assertFalse(any(kind == 'confirm_switch' for kind, _ in events))

    @unittest.skipUnless(chrome_available(), '未安装 Google Chrome')
    def test_switch_requires_confirmation_and_can_be_cancelled(self):
        # 作用：检查切换确认、取消保留旧浏览器，以及确认后打开另一种浏览器。
        # 使用场景：助手浏览器仍有打开的页面时，用户从 Edge 切换到 Chrome。
        self.open_browser('Microsoft Edge')
        original = owned_browser_root('msedge.exe')
        self.commands.put(('open', 'Google Chrome'))
        events = self.wait_for('confirm_switch')
        self.assertTrue(any('保存' in message for kind, message in events if kind == 'confirm_switch'))
        self.worker.answers.put(False)
        events = self.wait_for('idle')
        self.assertIn(('ready', 'Microsoft Edge'), events)
        self.assertEqual(owned_browser_root('msedge.exe'), original)

        self.commands.put(('open', 'Google Chrome'))
        self.wait_for('confirm_switch')
        self.worker.answers.put(True)
        events = self.wait_for('idle')
        self.assertIn(('browser_closed', 'Microsoft Edge'), events)
        self.assertIn(('ready', 'Google Chrome'), events)
        self.assertEqual(self.worker.browser_name, 'Google Chrome')


if __name__ == '__main__':
    unittest.main()
