# 企微群聊问题收集与整理

这是面向 Windows 长期运行场景的第一版开发骨架。目标链路是：

1. 通过已登录的 Windows 企微客户端本地数据库读取指定客户群消息；
2. 只筛选 @指定企微账号 的消息，并保留原文和上下文；
3. 整理成统一的反馈记录；
4. 由已授权的企微机器人更新智能表格；
5. 按定时任务生成摘要，并由 Windows 端真实企微账号发送回群聊。

会话内容存档不作为本项目方案。默认接收方式是只读企微进程内存取得当前会话的数据库密钥，对 `message.db/session.db/user.db` 及已提交 WAL 帧生成内存明文快照；不落盘、不注入 DLL、不修改企微文件，也不要求群聊窗口保持前台。OCR/UI 接收保留为兼容回退。

## 快速开始

```powershell
Copy-Item .env.example .env
python -m wecom_feedback init-db
python -m wecom_feedback health
python -m wecom_feedback demo-ingest --content "@系统反馈助手 登录后看不到订单"
python -m wecom_feedback demo-summary
python -m wecom_feedback web
python -m wecom_feedback diagnose-local
python -m wecom_feedback run-local --once
python -m wecom_feedback run-local
python -m wecom_feedback run-ui
python -m wecom_feedback desktop
```

浏览器打开 `http://127.0.0.1:8765` 即可进入本机配置控制台。控制台可以保存 `.env`、查看健康状态、查看反馈/发送任务，并运行 dry-run 演示。首次接入真实企微前建议保持 dry-run 开启。

`diagnose-local` 只返回进程、数据库、密钥校验、目标群和数量状态，不输出密钥或聊天正文。`run-local` 是推荐的长期接收入口；企微需要保持登录，但窗口可以最小化或被其他窗口遮挡。

`run-ui` 是 OCR/UI 兼容入口。它需要打开目标群并保持窗口可见，仅在本地数据库读取暂不支持某个企微版本时使用。

Windows 桌面发送使用 [windows_ui.py](wecom_feedback/adapters/windows_ui.py)：最大化企微后通过 `Ctrl+F` 搜索目标群，不再点击固定坐标；发送前 OCR 核对群名，并从消息编辑器复制回读全文。按下 Enter 后还必须在企微本地消息库中找到完全相同的消息才会标记为成功。若无法确认，任务进入“待人工确认”且不会自动重试。安装桌面依赖：`python -m pip install -e ".[windows]"`。

## 桌面常驻运行

运行 `python -m pip install -e ".[windows,desktop]"` 后执行 `python -m wecom_feedback desktop`。程序会使用系统 WebView2 显示内嵌配置窗口，并同时驻留 Windows 系统托盘、启动消息接收器和摘要调度器。关闭配置窗口只会隐藏到托盘；托盘菜单可重新打开窗口或安全退出。若系统缺少内嵌组件，程序会自动回退到浏览器配置页。

配置页可启用“登录 Windows 后自动启动”。该功能写入当前用户的 `HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run`，不需要管理员权限，也可随时取消。自动发送默认关闭；只有同时关闭 dry-run 并显式开启自动发送后，计划任务才会通过已登录的企微账号发送。

构建独立 EXE：

```powershell
.\\build_windows.ps1
```

脚本会把产物复制为项目根目录下的 `WeComFeedbackCollector.exe`，这样它可以直接使用同目录的 `.env` 和 `data`。桌面程序可以全天后台采集，但企微账号发送仍依赖交互式桌面：Windows 必须已登录且未锁屏；企微可以最小化，发送时程序会短暂恢复并切换到目标群。

## 产品界面

桌面程序不再只提供配置表单，主界面包含：

- 工作台：展示消息接收、反馈整理、智能表格同步、摘要生成和群内发送的完整链路状态；
- 反馈中心：搜索、筛选、编辑、忽略反馈，并可重新同步智能表格；
- 摘要与发送：从智能表格读取今日新增、待确认、处理中、今日完成和任务总数，预览或立即发送，并查看发送历史；
- 自动化：管理后台接收、智能表格、定时发送、开机启动、每 N 小时生效时段和摘要模板；
- 运行记录：查看群消息和发送任务的时间线；
- 设置：业务配置在前，高级连接参数折叠显示。

已有 `.env`、SQLite 数据库和智能表格连接会直接沿用，不需要重新配置。

同一时间只监听一个群。需要换群时，在“自动化”或“设置”中点击“更换监听群”，程序会先只读检测当前企微账号能否找到新群；验证成功才保存并重启接收器，旧群历史仍保留在本地数据库和智能表格中。摘要统计按“来源群”隔离。

智能表格的“反馈记录”工作表会维护 `任务编号`、`状态`、`优先级`、`来源群` 和 `来源消息ID`。同步时按任务编号或来源消息更新已有行，避免编辑反馈后重复新增。摘要默认以智能表格为统计来源；读取异常时才使用本地数据并在界面提示。

本地数据库接收不需要 Corp ID、Secret 或私钥；只需要保持当前企微账号登录。

`WECOM_DRY_RUN=true` 时不会发送真实消息，也不会调用外部企微服务。正式接入前需要配置目标群名称和账号名称。本地读取使用企微真实消息 ID 做幂等；企微升级后如果诊断失败会停止读取，不能盲用旧版本定位信息。

智能表格写入使用企微官方 CLI：先安装 `npm install -g @wecom/cli`，并完成一次 `wecom-cli auth init` 授权。控制台中的“表格机器人接口地址”可留空；程序会使用已授权的 CLI 长连接。新建的目标智能表格必须由该机器人创建或拥有，避免机器人无权写入其他成员创建的表格。

## 目录结构

```text
wecom_feedback/
  adapters/       外部系统接口（Windows 本地库/UI 接收、桌面发送）
  services/       采集、反馈整理、摘要编排
  config.py       环境变量配置
  db.py           SQLite 状态和幂等存储
  models.py       领域数据结构
  health.py       本地健康检查
  main.py         最小命令行入口
```

## Windows 运行原则

本地数据库接收器可作为后台进程运行，不依赖前台窗口，但企微必须保持登录。密钥和明文数据库快照只存在于程序内存中。真实企微账号发送代理仍必须运行在已登录的交互式 Windows 会话中。
