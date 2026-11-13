# 第三方组件说明

本项目的原创代码按照根目录的 [LICENSE](LICENSE) 使用 GPL-3.0-only 授权；第三方组件保留各自的版权和许可证，不因本项目的许可证而改变。

| 组件 | 作用 | 许可证资料 |
| --- | --- | --- |
| Python / Tk | Python 运行环境和桌面界面 | [Python 许可说明](https://docs.python.org/3/license.html)；Tk 许可证随打包文件提供 |
| Playwright / Node.js | 控制已安装的 Edge 或 Chrome | [Playwright 许可证](https://github.com/microsoft/playwright-python/blob/main/LICENSE)；Node.js 及 Playwright 内嵌依赖的许可证随打包文件提供 |
| pyee、greenlet、typing-extensions | Playwright 的 Python 依赖 | 各组件的许可证文本随打包文件提供 |
| markdown-it-py、mdurl | 识别 Markdown 中的代码块 | 各组件的许可证文本随打包文件提供 |
| PyInstaller | 构建单文件程序 | [PyInstaller 许可证及例外](https://github.com/pyinstaller/pyinstaller/blob/develop/COPYING.txt) |

运行 `打包助手.cmd` 后生成的发行压缩包包含 `third_party_licenses/`，其中整理了构建环境中上述 Python 包的许可证，以及可执行文件内携带的第三方声明。`BUILD_INFO.json` 记录实际构建时的版本、文件摘要和打包进程序的 DLL 名称。

Python 或 Windows 发行环境还可能带入 OpenSSL、压缩库和 Microsoft 运行库等原生组件。发行前应根据实际构建环境继续核对其来源和许可证；本说明不代表已完成全部第三方组件的法律合规审查。
