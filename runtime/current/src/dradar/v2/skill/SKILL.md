# DRadar 单题候选命令

已有站点认证与本机模型harness认证分别管理。本CLI不创建或搬运凭据。

`dradar run --tasks-root PATH --runtime-binding FILE --runtime-sha256 SHA256`：服务器推荐一道题，执行、提交后结束。
`dradar progress --state-root STATE`：读取原任务。
`dradar stop --state-root STATE`：只停止原任务；退出需原执行证据确认。
`dradar upload-only --state-root STATE`：只重放原结果，不启动模型、不领新题。

多进程/多机器各用独立STATE；同账号继续受服务器预算/准入约束。未知退出或启动确认丢失不自动重跑。
旧CLI跑题命令、旧worker调度、host槽、旧接口和分类表已经删除；旧在途状态不迁移。本候选不代表生产已切换。
