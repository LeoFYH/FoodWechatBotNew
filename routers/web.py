"""routers/web.py —— 导出/记忆/健康 路由（路径逐字不变）。

GET /health、GET /memory/{user_id}、DELETE /memory/{user_id}、GET /exports、GET /exports/orders.xlsx。
handler 函数体与原 main 逐字一致；依赖经 from main import 引用。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse

from main import (
    APP_BUILD_LABEL,
    EXPORT_DIR,
    EXPORT_TOKEN,
    MEMORY_LOCK,
    DeleteMemoryResponse,
    MemoryLengthResponse,
    build_order_export,
    collect_order_records,
    load_memory,
    logger,
    require_export_token,
    save_memory,
)

router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "build": APP_BUILD_LABEL}


@router.get("/memory/{user_id}", response_model=MemoryLengthResponse)
def get_memory_length(user_id: str) -> MemoryLengthResponse:
    user_id = user_id.strip()
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id cannot be empty")

    with MEMORY_LOCK:
        memory = load_memory()
        history = memory.get(user_id, [])

        if not isinstance(history, list):
            raise HTTPException(status_code=500, detail=f"Invalid history for user_id: {user_id}")

    return MemoryLengthResponse(user_id=user_id, history_length=len(history))


@router.get("/exports", response_class=HTMLResponse)
def export_page(request: Request):
    token = request.query_params.get("token", "")

    if EXPORT_TOKEN and token != EXPORT_TOKEN:
        return HTMLResponse(
            """
            <!doctype html>
            <html lang="zh-CN">
              <head>
                <meta charset="utf-8">
                <meta name="viewport" content="width=device-width, initial-scale=1">
                <title>订单导出</title>
                <style>
                  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 48px; color: #172033; }
                  main { max-width: 520px; }
                  label { display: block; margin: 18px 0 8px; font-weight: 600; }
                  input { width: 100%; box-sizing: border-box; padding: 12px 14px; border: 1px solid #ccd3df; border-radius: 8px; font-size: 16px; }
                  button { margin-top: 18px; padding: 12px 18px; border: 0; border-radius: 8px; background: #1f6feb; color: white; font-size: 16px; cursor: pointer; }
                  p { color: #667085; line-height: 1.6; }
                </style>
              </head>
              <body>
                <main>
                  <h1>订单导出</h1>
                  <p>请输入导出口令后进入导出页。</p>
                  <form method="get" action="/exports">
                    <label for="token">导出口令</label>
                    <input id="token" name="token" type="password" autocomplete="current-password">
                    <button type="submit">进入</button>
                  </form>
                </main>
              </body>
            </html>
            """
        )

    records = collect_order_records()
    order_ids = sorted({record["id"] for record in records if record.get("id")})
    stores = sorted({record["store"] for record in records if record.get("store")})
    download_url = "/exports/orders.xlsx"
    if token:
        download_url = f"{download_url}?token={token}"

    store_items = "".join(f"<li>{store}</li>" for store in stores[:30])
    if len(stores) > 30:
        store_items += f"<li>还有 {len(stores) - 30} 个门店...</li>"
    if not store_items:
        store_items = "<li>暂无门店数据</li>"

    return HTMLResponse(
        f"""
        <!doctype html>
        <html lang="zh-CN">
          <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <title>订单导出</title>
            <style>
              body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 48px; color: #172033; background: #f6f8fb; }}
              main {{ max-width: 880px; }}
              .panel {{ background: white; border: 1px solid #e3e8f0; border-radius: 10px; padding: 28px; box-shadow: 0 8px 24px rgba(15, 23, 42, 0.06); }}
              .stats {{ display: flex; gap: 16px; margin: 22px 0; }}
              .stat {{ background: #f1f5fb; border-radius: 8px; padding: 16px 18px; min-width: 140px; }}
              .stat strong {{ display: block; font-size: 28px; color: #0f4c81; }}
              a.button {{ display: inline-block; margin-top: 10px; padding: 13px 18px; border-radius: 8px; background: #1f6feb; color: white; text-decoration: none; font-weight: 700; }}
              p {{ color: #667085; line-height: 1.6; }}
              ul {{ columns: 2; color: #344054; line-height: 1.8; }}
            </style>
          </head>
          <body>
            <main class="panel">
              <h1>订单导出</h1>
              <p>点击按钮生成并下载 Excel。文件包含“全部订单”和“按门店商品汇总”两个 sheet。</p>
              <div class="stats">
                <div class="stat"><strong>{len(order_ids)}</strong>订单数</div>
                <div class="stat"><strong>{len(stores)}</strong>门店数</div>
              </div>
              <a class="button" href="{download_url}">下载 Excel</a>
              <h2>当前门店</h2>
              <ul>{store_items}</ul>
            </main>
          </body>
        </html>
        """
    )


@router.get("/exports/orders.xlsx")
def export_orders(request: Request):
    require_export_token(request)

    output_path = build_order_export(collect_order_records(), EXPORT_DIR)
    return FileResponse(
        output_path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=output_path.name,
    )


@router.delete("/memory/{user_id}", response_model=DeleteMemoryResponse)
def delete_memory(user_id: str) -> DeleteMemoryResponse:
    user_id = user_id.strip()
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id cannot be empty")

    with MEMORY_LOCK:
        memory = load_memory()
        memory.pop(user_id, None)
        save_memory(memory)

    logger.info("memory_deleted user_id=%s", user_id)
    return DeleteMemoryResponse(deleted=True, user_id=user_id)
