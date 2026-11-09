"""Windows 单文件程序的入口。"""

from pathlib import Path
import sys
import traceback
import unittest

from app import Application


def _run_packaging_self_test(report_path: str) -> int:
    # 作用：在打包后的程序内部运行不连接 CSDN 的本地回归测试。
    # 使用场景：构建完成后验证单文件中的 Python、Playwright 驱动和浏览器调用。
    # report_path 是测试结果写入的本地文件；正常双击启动不会进入此分支。
    try:
        with Path(report_path).open('w', encoding='utf-8') as report:
            try:
                # 测试使用本地模拟编辑器和临时图片，不读取用户的浏览器登录信息。
                from tests.test_app_ui import AppUiScalingTest, AppUiTest
                from tests.test_browser_lifecycle import BrowserWorkerLifecycleTest
                from tests.test_browser_workflow import BrowserWorkflowTest
                from tests.test_md_images import MarkdownImagesTest

                loader = unittest.TestLoader()
                suite = unittest.TestSuite((
                    loader.loadTestsFromTestCase(AppUiTest),
                    loader.loadTestsFromTestCase(AppUiScalingTest),
                    loader.loadTestsFromTestCase(BrowserWorkerLifecycleTest),
                    loader.loadTestsFromTestCase(BrowserWorkflowTest),
                    loader.loadTestsFromTestCase(MarkdownImagesTest),
                ))
                result = unittest.TextTestRunner(stream=report, verbosity=2).run(suite)
                return 0 if result.wasSuccessful() else 1
            except Exception:
                # 无控制台模式下将异常写入报告，避免测试失败后没有可检查的信息。
                traceback.print_exc(file=report)
                return 1
    except OSError:
        return 2


def main() -> int:
    # 作用：根据启动参数进入打包自检或正常的桌面界面。
    # 使用场景：双击 exe 时启动界面；构建验证时通过专用参数运行本地测试。
    if len(sys.argv) == 3 and sys.argv[1] == '--packaging-self-test':
        return _run_packaging_self_test(sys.argv[2])
    Application().mainloop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
