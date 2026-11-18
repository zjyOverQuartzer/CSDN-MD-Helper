"""验证助手浏览器的关闭、重新打开与切换；只使用临时配置和本地页面。"""

import ctypes
from ctypes import wintypes
import gc
import os
from pathlib import Path
import queue
import signal
import sys
import tempfile
import threading
import time
import traceback
import unittest
from unittest.mock import Mock, patch

import app
import playwright.sync_api as sync_api


# 本地测试页面无需网络：分别约束启动和导航，并为驱动启动留出额外时间。
LAUNCH_TIMEOUT_MS = 60_000
NAVIGATION_TIMEOUT_MS = 10_000
READY_TIMEOUT_SECONDS = (LAUNCH_TIMEOUT_MS + NAVIGATION_TIMEOUT_MS) / 1000 + 20
EVENT_TIMEOUT_SECONDS = 30


class ProcessEntry(ctypes.Structure):
    # 作用：描述 Windows 进程快照中的进程编号和父进程编号。
    # 使用场景：测试中定位由当前测试进程启动的浏览器，避免影响其他浏览器。
    _fields_ = [
        ('size', wintypes.DWORD), ('usage', wintypes.DWORD), ('pid', wintypes.DWORD),
        ('heap', ctypes.c_size_t), ('module', wintypes.DWORD), ('threads', wintypes.DWORD),
        ('parent', wintypes.DWORD), ('priority', wintypes.LONG), ('flags', wintypes.DWORD),
        ('exe', wintypes.WCHAR * 260),
    ]


def owned_process_roots(processes: dict, owner_pid: int, executable_name: str) -> list[int]:
    # 作用：从进程快照中找出指定测试进程的指定类型子进程根节点。
    # 使用场景：隔离测试浏览器与系统中其他浏览器，避免误操作。
    descendants = {owner_pid}
    while True:
        updated = descendants | {pid for pid, (parent, _) in processes.items() if parent in descendants}
        if updated == descendants:
            break
        descendants = updated
    targets = {pid for pid in descendants - {owner_pid}
               if processes.get(pid, (0, ''))[1].lower() == executable_name.lower()}
    return sorted(pid for pid in targets if processes[pid][0] not in targets)


def owned_browser_root(executable_name: str) -> int:
    # 作用：找到当前测试进程树内唯一的指定程序主进程。
    # 使用场景：关闭测试创建的浏览器或驱动，不能操作其他进程。
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
    roots = owned_process_roots(processes, os.getpid(), executable_name)
    if len(roots) != 1:
        raise RuntimeError(f'测试进程中应有一个 {executable_name} 主进程，实际找到 {len(roots)} 个')
    return roots[0]


def terminate_owned_test_processes() -> list[str]:
    # 作用：尝试终止当前测试进程内唯一可确认的浏览器和 Playwright 驱动。
    # 使用场景：正常关闭超时后兜底；找不到唯一子进程时不猜测、不结束其他进程。
    result = []
    for name in ('chrome.exe', 'msedge.exe', 'node.exe'):
        try:
            pid = owned_browser_root(name)
        except (RuntimeError, OSError) as exc:
            result.append(str(exc))
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            result.append(f'已终止测试子进程 {name} ({pid})')
        except OSError as exc:
            result.append(f'无法终止测试子进程 {name} ({pid})：{exc}')
    return result


def cleanup_temp_with_retry(temporary, *, timeout: float = 8, interval: float = 0.25) -> None:
    # 作用：临时文件暂被占用时限时重试，持续失败仍报告原错误。
    # 使用场景：浏览器退出后 Windows 释放缓存文件可能有短暂延迟。
    deadline = time.monotonic() + timeout
    while True:
        try:
            temporary.cleanup()
            return
        except PermissionError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(interval, remaining))


class LifecycleTrace:
    # 作用：记录测试浏览器启动、导航与关闭阶段及其耗时。
    # 使用场景：CI 超时或清理失败时定位阻塞阶段，不记录正文和登录数据。
    def __init__(self):
        # 作用：初始化线程安全的阶段记录。
        # 使用场景：每个生命周期测试开始时创建独立记录。
        self.lock = threading.Lock()
        self.events = []
        self.active = None

    def measure(self, stage: str, operation):
        # 作用：记录操作开始、完成或异常，并保留原始返回值和异常。
        # 使用场景：包装驱动启动、浏览器启动、导航和关闭。
        started = time.monotonic()
        with self.lock:
            self.active = (stage, started)
            self.events.append(f'{stage}：开始')
        if sys.stderr is not None:
            print(f'[浏览器测试] {stage}：开始', file=sys.stderr, flush=True)
        try:
            result = operation()
        except BaseException as exc:
            self._finish(stage, started, f'失败（{type(exc).__name__}）')
            raise
        self._finish(stage, started, '完成')
        return result

    def _finish(self, stage: str, started: float, result: str):
        # 作用：写入操作结果和耗时，并清除正在执行的阶段。
        # 使用场景：被测操作正常返回或抛出异常时调用。
        message = f'{stage}：{result}，耗时 {time.monotonic() - started:.2f} 秒'
        with self.lock:
            self.events.append(message)
            self.active = None
        if sys.stderr is not None:
            print(f'[浏览器测试] {message}', file=sys.stderr, flush=True)

    def describe(self) -> str:
        # 作用：生成最近阶段记录及当前阶段已等待的时间。
        # 使用场景：等待或清理超时时输出诊断信息。
        with self.lock:
            events = list(self.events[-10:])
            active = self.active
        if active:
            events.append(f'{active[0]}仍在进行，已等待 {time.monotonic() - active[1]:.2f} 秒')
        return '；'.join(events) or '尚未进入浏览器启动阶段'


def chrome_available() -> bool:
    # 作用：检查常见安装位置是否存在 Google Chrome。
    # 使用场景：仅在本机有 Chrome 时执行真实双浏览器切换测试。
    roots = [os.environ.get('PROGRAMFILES'), os.environ.get('PROGRAMFILES(X86)'), os.environ.get('LOCALAPPDATA')]
    return any(root and (Path(root) / 'Google/Chrome/Application/chrome.exe').is_file() for root in roots)


class HeadlessChromium:
    # 作用：将测试启动的浏览器强制设置为无界面模式。
    # 使用场景：验证真实 Playwright 行为而不打开用户可见窗口。
    def __init__(self, inner, trace: LifecycleTrace):
        # 作用：保存真实 Chromium 启动器。
        # 使用场景：创建测试代理时调用。
        self.inner = inner
        self.trace = trace

    def launch_persistent_context(self, *args, **kwargs):
        # 作用：使用临时 profile 启动无界面浏览器。
        # 使用场景：BrowserWorker 在回归测试中请求打开浏览器时调用。
        kwargs['headless'] = True
        kwargs['timeout'] = LAUNCH_TIMEOUT_MS
        channel = kwargs.get('channel', 'chromium')
        return self.trace.measure(f'启动 {channel}',
                                  lambda: self.inner.launch_persistent_context(*args, **kwargs))


class HeadlessPlaywright:
    # 作用：包装真实 Playwright 对象并替换浏览器启动方式。
    # 使用场景：仅供浏览器生命周期测试使用。
    def __init__(self, inner, trace: LifecycleTrace):
        # 作用：保存真实对象并提供无界面启动代理。
        # 使用场景：测试启动 Playwright 后调用。
        self.inner = inner
        self.trace = trace
        self.chromium = HeadlessChromium(inner.chromium, trace)

    def stop(self):
        # 作用：停止真实 Playwright 驱动。
        # 使用场景：测试工作线程结束时调用。
        self.trace.measure('停止 Playwright 驱动', self.inner.stop)


class HeadlessManager:
    # 作用：创建使用无界面浏览器的 Playwright 代理。
    # 使用场景：替代测试中的 sync_playwright()。
    def __init__(self, factory, trace: LifecycleTrace):
        # 作用：保存原始 Playwright 工厂。
        # 使用场景：建立测试代理时调用。
        self.factory = factory
        self.trace = trace

    def start(self):
        # 作用：在当前工作线程启动真实 Playwright。
        # 使用场景：BrowserWorker 首次打开测试浏览器时调用。
        inner = self.trace.measure('启动 Playwright 驱动', lambda: self.factory().start())
        return HeadlessPlaywright(inner, self.trace)


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
        self.trace = LifecycleTrace()
        self.addCleanup(self.cleanup_temp)
        self.commands = queue.Queue()
        self.events = queue.Queue()
        real_factory = sync_api.sync_playwright
        real_goto = sync_api.Page.goto
        real_close = sync_api.BrowserContext.close

        def tracked_goto(page, url, *args, **kwargs):
            # 作用：记录本地页面导航并设置独立的导航上限。
            # 使用场景：生命周期测试只打开数据页面，避免掩盖浏览器启动问题。
            if not url.startswith('data:text/html,'):
                raise AssertionError('生命周期测试只允许打开本地数据页面')
            kwargs['timeout'] = NAVIGATION_TIMEOUT_MS
            return self.trace.measure('加载本地测试页面', lambda: real_goto(page, url, *args, **kwargs))

        def tracked_close(context, *args, **kwargs):
            # 作用：记录浏览器上下文关闭是否完成。
            # 使用场景：测试退出或切换浏览器时使用。
            return self.trace.measure('关闭浏览器上下文', lambda: real_close(context, *args, **kwargs))

        patches = [
            patch.object(app, 'DATA_ROOT', Path(self.temp.name)),
            patch.object(app, 'CSDN_EDITOR_URL', 'data:text/html,<title>Local test</title>'),
            patch.object(sync_api, 'sync_playwright', lambda: HeadlessManager(real_factory, self.trace)),
            patch.object(sync_api.Page, 'goto', tracked_goto),
            patch.object(sync_api.BrowserContext, 'close', tracked_close),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.worker = app.BrowserWorker(self.commands, self.events)
        self.worker.start()
        self.addCleanup(self.stop_worker)

    def diagnostic(self) -> str:
        # 作用：生成当前阶段和后台工作线程的堆栈，不读取正文或登录数据。
        # 使用场景：等待或清理超时时，帮助区分驱动、浏览器和导航阶段。
        frame = sys._current_frames().get(self.worker.ident) if self.worker.ident else None
        stack = ''.join(traceback.format_stack(frame)) if frame else '工作线程已结束'
        return f'最近阶段：{self.trace.describe()}\n工作线程堆栈：\n{stack}'

    def stop_worker(self):
        # 作用：解除可能等待的确认并关闭测试工作线程。
        # 使用场景：测试结束或失败后清理测试浏览器。
        if self.worker.is_alive():
            self.worker.answers.put(False)
            self.commands.put(None)
            self.worker.join(timeout=20)
        if self.worker.is_alive():
            diagnostic = self.diagnostic()
            stopped = terminate_owned_test_processes()
            self.worker.join(timeout=10)
            # 即使兜底成功也报告失败，不能把工作线程未按时退出伪装成通过。
            self.fail(f'测试工作线程未在 20 秒内退出。清理结果：{stopped}\n{diagnostic}')

    def cleanup_temp(self):
        # 作用：等待临时目录文件释放，必要时只清理当前测试拥有的进程。
        # 使用场景：浏览器刚退出后 Windows 可能暂时无法删除缓存文件。
        try:
            cleanup_temp_with_retry(self.temp)
        except PermissionError as exc:
            stopped = terminate_owned_test_processes()
            try:
                cleanup_temp_with_retry(self.temp, timeout=5)
            except PermissionError as retry_exc:
                raise AssertionError(f'测试临时目录仍被占用；清理结果：{stopped}') from retry_exc
            raise AssertionError(f'测试临时目录未及时释放；已尝试清理：{stopped}') from exc

    def wait_for(self, marker: str, timeout: float | None = None):
        # 作用：收集后台事件，直到指定事件出现。
        # 使用场景：等待浏览器打开、关闭或请求切换确认。
        received = []
        if timeout is None:
            timeout = READY_TIMEOUT_SECONDS if marker == 'idle' else EVENT_TIMEOUT_SECONDS
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                event = self.events.get(timeout=max(0.1, deadline - time.monotonic()))
            except queue.Empty:
                break
            received.append(event)
            if event[0] == 'error':
                self.fail(f'{event[1]}\n{self.diagnostic()}')
            if event[0] == marker:
                return received
        self.fail(f'等待 {marker} 超时（{timeout} 秒），已收到：{received}\n{self.diagnostic()}')

    def open_browser(self, name: str):
        # 作用：提交打开任务并等待工作线程空闲。
        # 使用场景：测试首次打开或重新打开浏览器。
        self.commands.put(('open', name))
        events = self.wait_for('idle')
        self.assertIn(('ready', name), events)
        return events

    def test_process_selection_excludes_unrelated_browsers(self):
        # 作用：验证兜底清理不会选择测试进程树之外的浏览器。
        # 使用场景：用户自己也打开了 Chrome 或 Edge。
        processes = {
            100: (1, 'python.exe'), 101: (100, 'node.exe'),
            102: (101, 'chrome.exe'), 103: (102, 'chrome.exe'),
            104: (101, 'msedge.exe'), 200: (1, 'chrome.exe'), 201: (200, 'chrome.exe'),
        }
        self.assertEqual(owned_process_roots(processes, 100, 'chrome.exe'), [102])
        self.assertEqual(owned_process_roots(processes, 100, 'msedge.exe'), [104])
        self.assertEqual(owned_process_roots(processes, 100, 'node.exe'), [101])

    def test_forced_cleanup_refuses_ambiguous_processes(self):
        # 作用：无法唯一确认进程时不发送终止信号。
        # 使用场景：测试超时后有多个同名浏览器或没有对应进程。
        def find_root(name):
            if name == 'chrome.exe':
                return 102
            if name == 'msedge.exe':
                raise OSError('无法读取进程快照')
            raise RuntimeError(f'{name} 的测试子进程不唯一')

        with patch(f'{__name__}.owned_browser_root', side_effect=find_root), patch(f'{__name__}.os.kill') as kill:
            result = terminate_owned_test_processes()
        kill.assert_called_once_with(102, signal.SIGTERM)
        self.assertTrue(any('不唯一' in item for item in result))
        self.assertTrue(any('无法读取进程快照' in item for item in result))

    def test_temporary_directory_retry_preserves_errors(self):
        # 作用：临时锁定时重试，持续拒绝访问时仍报告错误。
        # 使用场景：Windows 尚未释放浏览器缓存文件。
        temporary = Mock()
        temporary.cleanup.side_effect = [PermissionError('locked'), None]
        cleanup_temp_with_retry(temporary, timeout=1, interval=0)
        self.assertEqual(temporary.cleanup.call_count, 2)
        temporary.cleanup.side_effect = PermissionError('still locked')
        with self.assertRaisesRegex(PermissionError, 'still locked'):
            cleanup_temp_with_retry(temporary, timeout=0)

    def test_timed_out_worker_triggers_cleanup_and_still_fails(self):
        # 作用：线程关闭超时后尝试清理测试进程，但不将异常情况误判为成功。
        # 使用场景：浏览器或驱动阻塞，工作线程无法及时处理退出命令。
        real_worker = self.worker
        fake_worker = Mock()
        fake_worker.ident = None
        fake_worker.answers = queue.Queue()
        fake_worker.is_alive.return_value = True
        self.worker = fake_worker
        try:
            with patch(f'{__name__}.terminate_owned_test_processes', return_value=['模拟清理']) as terminate:
                with self.assertRaisesRegex(AssertionError, '未在 20 秒内退出'):
                    self.stop_worker()
            terminate.assert_called_once()
            self.assertEqual([call.kwargs['timeout'] for call in fake_worker.join.call_args_list], [20, 10])
        finally:
            self.worker = real_worker

    def test_timeout_reports_active_stage_and_worker_stack(self):
        # 作用：等待事件超时时，报错包含当前阶段及后台线程堆栈。
        # 使用场景：CI 中浏览器启动或导航卡住，需要定位具体阶段。
        started = threading.Event()
        release = threading.Event()

        def blocked_stage():
            def wait():
                started.set()
                release.wait(timeout=5)
            self.trace.measure('模拟浏览器启动', wait)

        thread = threading.Thread(target=blocked_stage, daemon=True)
        thread.start()
        try:
            self.assertTrue(started.wait(timeout=5))
            with self.assertRaisesRegex(AssertionError, '模拟浏览器启动') as error:
                self.wait_for('browser_closed', timeout=0.05)
            self.assertIn('工作线程堆栈', str(error.exception))
        finally:
            release.set()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive(), '模拟阶段线程未退出')

    def test_slow_start_uses_ready_timeout(self):
        # 作用：浏览器启动慢于普通事件等待时，仍使用较长的启动等待时间。
        # 使用场景：CI 冷启动需要更多时间，但并未超过明确设置的启动上限。
        original = HeadlessChromium.launch_persistent_context

        def delayed(chromium, *args, **kwargs):
            time.sleep(0.2)
            return original(chromium, *args, **kwargs)

        with patch(f'{__name__}.EVENT_TIMEOUT_SECONDS', 0.05), \
                patch.object(HeadlessChromium, 'launch_persistent_context', delayed):
            self.open_browser('Microsoft Edge')

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
