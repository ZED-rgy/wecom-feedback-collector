# 企微群聊问题收集与整理

这是面向 Windows 长期运行场景的第一版开发骨架。目标链路是：

1. 通过已登录的 Windows 企微客户端读取指定客户群消息（实验性 UI 接收）；
2. 只筛选 @指定企微账号 的消息，并保留原文和上下文；
3. 整理成统一的反馈记录；
4. 由已授权的企微机器人更新智能表格；
5. 按定时任务生成摘要，并由 Windows 端真实企微账号发送回群聊。

当前版本先完成领域模型、SQLite 状态库、适配器接口和本地演示链路。会话内容存档不作为本项目方案；Windows UI 接收适配器只读取当前打开的群聊窗口，适合先在“测试群”验证。

## 快速开始

```powershell
Copy-Item .env.example .env
python -m wecom_feedback init-db
python -m wecom_feedback health
python -m wecom_feedback demo-ingest --content "@系统反馈助手 登录后看不到订单"
python -m wecom_feedback demo-summary
python -m wecom_feedback web
python -m wecom_feedback run-ui
```

浏览器打开 `http://127.0.0.1:8765` 即可进入本机配置控制台。控制台可以保存 `.env`、查看健康状态、查看反馈/发送任务，并运行 dry-run 演示。首次接入真实企微前建议保持 dry-run 开启。

`python -m wecom_feedback run-ui` 是 UI 接收测试入口。运行前请在企微桌面端打开“测试群”，并保持窗口可见。程序通过 UI Automation 读取可见文本，按 @账号名称筛选并去重入库。

Windows 桌面发送第一阶段使用 [windows_ui.py](wecom_feedback/adapters/windows_ui.py)：先定位窗口、搜索目标群并填入文本，默认不会点击发送；必须经过人工视觉确认后显式调用 `confirm_and_send`。安装桌面依赖：`python -m pip install -e ".[windows]"`。

UI 接收不需要 Corp ID、Secret 或私钥；只需要保持当前登录的企微账号和目标群窗口可用。

`WECOM_DRY_RUN=true` 时不会发送真实消息，也不会调用外部企微服务。正式接入前需要配置目标群名称和账号名称。由于 UI 读取没有官方消息 ID，客户端升级、窗口遮挡、锁屏和远程操作验证都可能导致漏读或暂停。

## 目录结构

```text
wecom_feedback/
  adapters/       外部系统接口（会话存档、机器人、桌面发送）
  services/       采集、反馈整理、摘要编排
  config.py       环境变量配置
  db.py           SQLite 状态和幂等存储
  models.py       领域数据结构
  health.py       本地健康检查
  main.py         最小命令行入口
```

## Windows 运行原则

核心服务可作为后台进程运行；真实企微账号发送代理必须运行在已登录的交互式 Windows 会话中。发送前应通过群名称/备注和 OCR/视觉校验确认目标群，发送结果与截图写入状态库，避免误发。
