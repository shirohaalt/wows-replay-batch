# 发布与更新

桌面程序通过公开 GitHub 仓库的 Releases 检查正式版本。使用者无需登录 GitHub，也无需提供令牌。

## 发布步骤

1. 修改 `wows_replay_batch/__init__.py` 中的版本号，使用 `主版本.次版本.修订号` 格式。
2. 运行自动测试：`python -m unittest discover -s tests -v`。
3. 安装 PyInstaller，运行 `python build_release.py` 构建 Windows 程序。
4. 运行 `python release_package.py`，生成 Windows 压缩包、源码压缩包和 `SHA256SUMS.txt`。
5. 在 GitHub 仓库的 Releases 页面创建版本，标签为 `v` 加程序版本号。填写更新说明，上传上述附件，然后发布为正式版本。

Windows 附件必须命名为 `WoWSReplayBatch-v版本号-Windows.zip`，并与 Release 标签版本一致。草稿和预发布版本不会参与检查。

程序优先使用 GitHub 附件的 SHA-256 digest；没有该值时，读取 `SHA256SUMS.txt` 中对应附件的完整校验值。校验失败时不保留新下载文件。更新包只会下载到本机，不会自动执行或覆盖程序。

## 公开内容

源码与发布文件按白名单收集。发布包只包含程序、说明、许可证、转换工具及可选的公共游戏素材；本机设置、队伍使用记录、回放、视频、日志、截图和私钥均不发布。

如果需要附带公共游戏素材，只复制 `cache/game-data` 中的公共素材，不复制整份 `cache`。`release_package.py` 会继续按文件名白名单筛选，并检查本机路径。

维护另一个公开仓库时，在程序「检查更新」窗口修改仓库地址即可；该配置只保存在使用者本机。生产客户端无需写入发布凭据。
