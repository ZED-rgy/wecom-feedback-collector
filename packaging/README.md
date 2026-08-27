# Windows 发布流程

1. 在干净的发布环境执行 `..\build_windows.ps1`，确认测试全部通过。
2. 安装 Inno Setup 6，用 `WeComFeedbackCollector.iss` 编译安装包。
3. 如果有代码签名证书，在安装包和 exe 上签名后再发布。
4. 在另一台 Windows 电脑安装，确认 WebView2、企微登录、本地目录写入和 `diagnose-environment` 检查结果。
5. 使用各自账号在配置控制台填写目标群和智能表格；不要复制开发机的 `.env`、`data`、`logs`。

安装包使用当前用户目录安装，不需要管理员权限。配置和运行数据存放在 `%LOCALAPPDATA%\\WeComFeedbackCollector`，卸载时默认保留配置和数据库，便于升级和恢复。
