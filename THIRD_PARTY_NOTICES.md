# 第三方组件

WoWS 队伍回放助手调用 landaire/wows-toolkit v1.0.2 的独立命令行工具。上游源码、发布包和许可证：

- https://github.com/landaire/wows-toolkit
- https://github.com/landaire/wows-toolkit/releases/tag/v1.0.2
- https://github.com/landaire/wows-toolkit/blob/main/LICENSE

MIT 许可证全文位于随包 `tools/wows-toolkit/LICENSE.txt`。

本程序使用 Python、Tcl/Tk、Requests、urllib3、certifi、charset-normalizer 和 idna；随包 `licenses/` 提供 Python、Tcl/Tk 及依赖许可证，源码环境也可在各包分发中查看。PyInstaller 的 GPL 许可证含独立程序分发例外。

本程序使用 Pillow 绘制信息栏，并独立调用 FFmpeg 进行视频合成。FFmpeg 由 imageio-ffmpeg 的 Windows 二进制分发提供，版本及完整构建信息见 `tools/ffmpeg/BUILD.txt`，许可证及来源见该目录。FFmpeg 与程序通过标准输入输出通信。相关来源：

- https://github.com/imageio/imageio-ffmpeg
- https://ffmpeg.org/
- https://github.com/FFmpeg/FFmpeg/tree/n7.1

World of Warships 是 Wargaming 的产品。本项目不是官方产品。回放、地图、字体和图标等游戏素材的权利归其原权利人；本程序从比赛公开下载接口及工具作者的数据仓库读取这些素材。
