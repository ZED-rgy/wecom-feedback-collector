# 企微群聊问题收集与整理

这是面向 Windows 长期运行场景的第一版开发骨架。目标链路是：

1. 通过企微会话存档读取指定客户群消息；
2. 只筛选 @指定企微账号 的消息，并保留原文和上下文；
3. 整理成统一的反馈记录；
4. 由已授权的企微机器人更新智能表格；
5. 按定时任务生成摘要，并由 Windows 端真实企微账号发送回群聊。

当前版本先完成领域模型、SQLite 状态库、适配器接口和本地演示链路。真实会话存档 SDK、消息理解服务和 Windows 桌面发送适配器均通过接口预留，避免把账号登录和桌面自动化逻辑耦合进核心服务。

## 快速开始

```powershell
Copy-Item .env.example .env
python -m wecom_feedback init-db
python -m wecom_feedback health
python -m wecom_feedback demo-ingest --content "@系统反馈助手 登录后看不到订单"
python -m wecom_feedback demo-summary
python -m wecom_feedback web
python -m wecom_feedback run --once
```

浏览器打开 `http://127.0.0.1:8765` 即可进入本机配置控制台。控制台可以保存 `.env`、查看健康状态、查看反馈/发送任务，并运行 dry-run 演示。首次接入真实企微前建议保持 dry-run 开启。

`python -m wecom_feedback run` 是长期运行入口。当前默认使用未配置适配器和 dry-run 发送器；接入真实会话存档与 Windows 发送器后，只需在启动装配处替换适配器，不需要改动采集、整理和任务队列逻辑。

真实接入所需信息见 [真实企微接入清单](docs/REAL_INTEGRATION_CHECKLIST.md)。

`WECOM_DRY_RUN=true` 时不会发送真实消息，也不会调用外部企微服务。正式接入前需要配置目标群 `roomid`、账号 ID/名称，并确认会话存档已对该账号开放。

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
