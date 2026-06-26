"""routers —— FastAPI 路由层(P9)。

按域分组的 APIRouter：wecom（企业微信回调）、robot（机器人/数据 API）、web（导出/记忆/健康）。
全部 **不带 prefix**，URL 路径与原 @app.* 逐字一致。main 在文件末尾（所有 def/class 之后）
`from routers.xxx import router; app.include_router(router)`，故各 router 模块顶部
`from main import ...` 不会触发 import 期循环（此时 main 已全部定义）。

过渡说明：当前 handler 经 `from main import ...` 引用编排器/dispatcher/env/模型；
待 P10 把 dispatcher 等搬出 main 后，再改为从对应模块 import，去掉对 main 的依赖。
"""
