# CSDN Markdown 图片助手

在 Windows 上先将本地 Markdown 正文导入 CSDN 编辑器，再逐张上传本地图片并替换正文里的图片路径。无需配置其他图床。不会自动点击“保存草稿”或“发布文章”；处理完请自行检查并保存。

## 启动

### 使用打包好的程序

前往 [GitHub Releases](https://github.com/zjyOverQuartzer/CSDN-MD-Helper/releases)，在版本的 **Assets** 中下载 `CSDN-Markdown-Helper.exe` 并双击运行；如果该版本还提供发行压缩包，也可以下载并解压压缩包后运行其中的 exe。`Source code` 是源码，不是直接运行的程序。

exe 内包含运行所需的 Python、Markdown 解析器和 Playwright 驱动，无需另行安装 Python 或 Playwright。电脑仍需安装 Microsoft Edge 或 Google Chrome；助手使用已安装的浏览器，不附带浏览器本体。

### 从源码启动

双击 `启动助手.cmd`。源码方式需要 Python 3.10 或更新版本、Tkinter 和 `requirements.txt` 中的依赖，以及已安装的 Edge 或 Chrome。Playwright 不需要另外下载浏览器。

如启动器提示缺少依赖，可在此目录新建独立虚拟环境安装（不修改全局 Python 环境）：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\pythonw.exe app.py
```

### 重新打包

在 Windows x64 上双击 `打包助手.cmd`。脚本会在项目的 `build\release-env` 创建独立构建环境，安装 `requirements-build.txt` 中的依赖，并在 `dist\v0.2.0` 生成 `CSDN-Markdown-Helper.exe` 和包含对应源码、许可证的发行压缩包。目标目录已存在时会使用新的编号目录，不覆盖已有文件；安装缺失依赖需要网络。

`requirements-build.txt` 固定了已验证的打包工具和间接依赖版本；源码运行的依赖范围仍在 `requirements.txt`。自动构建使用 64 位 Python 3.12.10，其他 Python 版本或系统环境生成的文件可能有所不同。已有构建虚拟环境中的其他包不会由安装步骤自动删除。

压缩包中的 `BUILD_INFO.json` 记录构建版本与文件摘要，`third_party_licenses/` 整理已收集的第三方许可证。发行前仍应在干净 Windows 环境测试，并核对 [第三方组件说明](THIRD_PARTY_NOTICES.md)。旧版 `v0.1.0` 的源码快照早于本项目添加 `LICENSE`；新发行版应使用包含许可证的源码版本。

## 使用

界面左侧用于选择文档、打开浏览器和选择处理方式；右侧显示进度和处理日志。窗口较窄时会自动改为上下布局，内容超出窗口时可用滚动条或鼠标滚轮查看。

程序启动时按 Windows 的系统 DPI 绘制，并按缩放比例调整默认窗口和图标大小。使用缩放比例不同的多个显示器时，窗口移到另一块屏幕后仍可能被 Windows 缩放；目前不支持跨屏动态调整 DPI。

1. 选择一篇本地 `.md` 或 `.markdown` 文件。保留它与图片之间原有的相对目录关系。
2. 选择 Edge 或 Chrome，点击“打开 CSDN”。浏览器使用助手自己的独立配置目录，首次请自行登录；不会读取现有浏览器的登录资料。
   浏览器关闭后可以重新打开或选择另一种浏览器。若当前助手浏览器仍打开，切换时请先保存文章并确认关闭。
3. 在助手打开的浏览器中打开或新建一篇 **Markdown 编辑器**文章，并关闭助手浏览器中其他 Markdown 编辑标签页。
4. 回到助手，选择处理方式：
   - **导入所选 Markdown**：用所选本地文档替换正文。如果当前已有正文，工具会先询问。
   - **仅修复编辑器当前正文中的本地图片**：保留你在 CSDN 正文中已有的文字修改，只替换可识别的本地图片引用。
   - **处理前备份已有正文**：默认勾选，可自行取消；例如编辑器中只有 CSDN 示例文字时可以不备份。
5. 点击“开始处理正文与图片”。界面会显示已处理图片的进度；处理期间不要编辑 CSDN 正文。
6. 完成后在 CSDN 中检查文章预览，自行保存草稿或发布。

处理过程中可点击“停止处理”。程序会等待当前图片处理完成或超时，再在安全位置停止；已处理的图片保留。检查正文后，可以选择“仅修复图片”继续处理剩余图片。

## 工作方式与注意事项

- 支持常见 `![描述](本地路径)`、带空格或中文的路径、`<img src="本地路径">` 和常见 Markdown 引用式图片；自动跳过代码块与行内代码。远程链接和内嵌 `data:` 数据不会重新上传。
- 上传失败时，助手会尽可能显示相关 HTTP 状态；浏览器没有提供完整请求信息时，可能只能提示“可能相关”，请检查正文后再重试，避免重复上传。
- 支持绝对路径、相对路径和本地 `file:` 网址。**相对路径始终以选中的 Markdown 所在目录为基准**，请确保选择与文章相对应的文件。
- 导入模式先写入全文，再逐张上传并替换图片。遇到缺图或上传失败时，已导入的正文和已完成的图片链接会保留。处理问题后可选择“仅修复编辑器当前正文中的本地图片”继续，避免重复上传已完成的图片。相同文件的多处引用只上传一次。
- 上传过程中请勿修改目标文章。工具会检查用户交互及每一步的正文；发现变化时停止，避免覆盖你的修改。
- 上传通过 CSDN 官方网页编辑器的图片功能进行，因此 CSDN 或浏览器更新可能导致本工具需要调整。该流程依赖网络以及账号的正常上传能力，不承诺不受平台限额或规则影响。
- 图片上传在同一个编辑页内完成，避免 CSDN 在多个编辑标签间同步草稿造成正文覆盖。
- 勾选备份时，已有正文会保存到 `%LOCALAPPDATA%\CSDN-MD-Helper\recovery`；取消勾选则不创建备份。浏览器登录信息保存在同目录下的独立 profile；请不要把这些目录分享给别人。
- 未上传的本地图片源文件和原 Markdown 文件不会被修改。图片上传到 CSDN 后可能可以通过链接访问，含敏感信息的截图请勿上传。

## 测试

```powershell
python -m unittest discover -s tests -v
```

推送或创建 Pull Request 时，GitHub Actions 会在 Windows 上安装固定的构建依赖，运行源码测试、构建 exe 并执行打包自检，也可以在仓库的 **Actions** 页面手动运行。自动测试使用本地模拟编辑器，不会登录 CSDN 或上传图片；它不能代替没有安装 Python 的干净 Windows 电脑试用。

## 版权与许可证

Copyright (c) 2026 zhangjiayu (GitHub: zjyOverQuartzer)

本项目原创代码按 GNU General Public License v3.0 only（GPL-3.0-only）授权，完整条款见 [LICENSE](LICENSE)。第三方组件遵循其各自的许可证，详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
