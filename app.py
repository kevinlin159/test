# -*- coding: utf-8 -*-
import io
import os
import re
import time
from pathlib import Path
from typing import List, Tuple

from flask import Flask, request, render_template_string, send_file, redirect, url_for, flash
from playwright.sync_api import sync_playwright

app = Flask(__name__)
app.secret_key = "dev-secret"  # for flash messages

FIND_BIZ_URL = "https://findbiz.nat.gov.tw/fts/query/QueryBar/queryInit.do"
OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)

INDEX_HTML = """
<!doctype html>
<html lang="zh-Hant">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>findbiz 批次匯出「友善列印」PDF</title>
    <style>
      body{font-family:-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Noto Sans TC", "PingFang TC", "Helvetica Neue", Arial, "Microsoft JhengHei", sans-serif; margin:2rem; line-height:1.5;}
      textarea{width:100%; height:240px; font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;}
      .card{max-width:900px; margin:auto; border:1px solid #ddd; border-radius:8px; padding:1rem 1.5rem;}
      .actions{display:flex; gap:.75rem; align-items:center; flex-wrap:wrap;}
      .note{background:#fff7e6; border:1px solid #ffe7ba; padding:.75rem; border-radius:6px; margin-top:1rem;}
      .success{background:#f6ffed; border:1px solid #b7eb8f;}
      .error{background:#fff1f0; border:1px solid #ffa39e;}
      .muted{color:#666}
      label{display:block; margin:.5rem 0;}
      input[type=text]{width:100%; padding:.5rem;}
      button{padding:.6rem 1.1rem; border-radius:6px; border:1px solid #333; background:#111; color:#fff; cursor:pointer;}
      button[disabled]{opacity:.6; cursor:not-allowed;}
      .footer{margin-top:2rem; color:#666}
      .checkbox{display:flex; gap:.5rem; align-items:center;}
      .log{white-space:pre-wrap; font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace; background:#f7f7f7; border:1px solid #eee; padding:.75rem; border-radius:6px;}
    </style>
  </head>
  <body>
    <div class="card">
      <h2>findbiz 批次匯出「友善列印」PDF</h2>
      <p>將多個統一編號（每行一個）貼在下方，按「開始產出」，系統會依序產生 PDF 並打包為 ZIP。</p>

      {% with messages = get_flashed_messages(with_categories=true) %}
        {% if messages %}
          {% for category, message in messages %}
            <div class="note {{'error' if category=='error' else 'success'}}">{{ message }}</div>
          {% endfor %}
        {% endif %}
      {% endwith %}

      <form method="post" action="{{ url_for('run_batch') }}">
        <label>統一編號清單（每行 1 個，必須為 8 碼數字）</label>
        <textarea name="ubns" placeholder="例如：
96942465
84480094
12345678
"></textarea>

        <div class="checkbox">
          <input type="checkbox" id="headed" name="headed">
          <label for="headed">顯示瀏覽器畫面（除錯用，速度較慢）</label>
        </div>

        <div class="actions">
          <button type="submit">開始產出</button>
          <span class="muted">完成後會提供 ZIP 檔下載連結</span>
        </div>
      </form>

      {% if zip_ready %}
        <div class="note success" style="margin-top:1rem;">
          已完成，共 {{count}} 個 PDF。<br>
          <a href="{{ url_for('download_zip', zip_name=zip_name) }}">點此下載：{{ zip_name }}</a>
        </div>
      {% endif %}

      {% if logs %}
      <h3>處理紀錄</h3>
      <div class="log">{{logs}}</div>
      {% endif %}

      <div class="note" style="margin-top:1rem;">
        <strong>提醒：</strong> 請遵守網站使用條款與相關法令。若網站啟用驗證碼/人機驗證，流程可能需要人工協助或會失敗。
      </div>

      <div class="footer">
        <p class="muted">本工具為示範用途。請確保您有合法使用權限。</p>
      </div>
    </div>
  </body>
</html>
"""

def normalize_ubns(raw: str) -> List[str]:
    lines = [re.sub(r"\\D", "", s) for s in (raw or "").splitlines()]
    ubns = [s for s in lines if s.isdigit() and len(s) == 8]
    return ubns

def try_click_any(page, texts, timeout=2000) -> bool:
    for t in texts:
        try:
            el = page.get_by_text(t, exact=True)
            el.first.click(timeout=timeout)
            return True
        except Exception:
            continue
    return False

def try_fill_candidates(page, selectors, value, timeout=2000) -> bool:
    for sel in selectors:
        try:
            page.fill(sel, value, timeout=timeout)
            return True
        except Exception:
            try:
                page.locator(sel).first.click(timeout=timeout)
                page.fill(sel, value, timeout=timeout)
                return True
            except Exception:
                continue
    return False

def select_search_data_types(page) -> bool:
    """选择搜索资料类型：名稱或統一編號或工廠登記編號（默认选项）"""
    try:
        # 确保选择的是"名稱或統一編號或工廠登記編號"选项
        default_radio = page.locator("input[name='infoType'][value='D']")
        if not default_radio.is_checked():
            default_radio.check()
        return True
    except:
        return False

def export_pdf_from_printable(page, out_path: Path):
    page.wait_for_load_state("load", timeout=8000)
    time.sleep(0.5)
    page.pdf(
        path=str(out_path),
        format="A4",
        scale=1.0,
        margin={"top":"10mm", "bottom":"10mm", "left":"10mm", "right":"10mm"},
        print_background=True,
    )

def fetch_one(pw, ubn: str, headless: bool = True) -> Path:
    # 添加服务器环境必需的浏览器参数
    browser_args = [
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--disable-software-rasterizer",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-features=TranslateUI",
        "--disable-ipc-flooding-protection",
    ]
    
    browser = pw.chromium.launch(headless=headless, args=browser_args)
    context = browser.new_context(locale="zh-TW")
    page = context.new_page()

    # 1. 打开网页
    page.goto(FIND_BIZ_URL, wait_until="domcontentloaded", timeout=15000)
    
    # 处理可能的弹窗
    try_click_any(page, ["同意", "我知道了", "確定", "接受", "關閉"])
    
    # 2. 确保选择正确的搜索类型（名稱或統一編號）
    select_search_data_types(page)
    
    # 3. 输入统一编号到正确的输入框
    filled = try_fill_candidates(page,
        ["input[name='qryCond']",           # 主查询输入框
         "input[id='qryCond']",             # 通过ID查找  
         "input[type='text']:visible"],     # 可见的文本输入框
        ubn)
    
    if not filled:
        raise RuntimeError("找不到可輸入統一編號的欄位。")

    # 4. 点击查询按钮
    query_clicked = False
    try:
        # 优先点击主查询按钮
        page.click("button[onclick*='sendQueryList']", timeout=3000)
        query_clicked = True
    except:
        try:
            page.click("#qryBtn", timeout=3000)
            query_clicked = True
        except:
            if not try_click_any(page, ["查詢", "搜尋", "Search", "送出"]):
                page.keyboard.press("Enter")

    # 5. 等待查询结果页面
    page.wait_for_load_state("networkidle", timeout=10000)
    time.sleep(2)

    # 6. 查找并点击友善列印按钮
    try:
        page.wait_for_selector("text=友善列印", timeout=5000)
    except:
        pass

    with context.expect_page(timeout=10000) as newp:
        # 尝试多种方式点击友善列印
        print_clicked = False
        
        # 方法1: 直接点击文字
        try:
            page.click("text=友善列印", timeout=3000)
            print_clicked = True
        except:
            pass
        
        # 方法2: 点击链接
        if not print_clicked:
            try:
                page.click("a:has-text('友善列印')", timeout=3000)
                print_clicked = True
            except:
                pass
        
        # 方法3: 寻找包含友善列印的元素
        if not print_clicked:
            try:
                print_link = page.locator("a").filter(has_text="友善列印").first
                print_link.click(timeout=3000)
                print_clicked = True
            except:
                pass
        
        # 方法4: 使用通用方法
        if not print_clicked:
            if not try_click_any(page, ["友善列印", "列印", "Friendly Print", "Print"]):
                raise RuntimeError("找不到「友善列印」按钮。")
    
    ppage = newp.value
    out_path = OUTPUT_DIR / f"findbiz_{ubn}.pdf"
    export_pdf_from_printable(ppage, out_path)

    context.close()
    browser.close()
    return out_path

def make_zip(zip_name: str) -> Path:
    from shutil import make_archive
    archive_path = make_archive(zip_name, "zip", root_dir=OUTPUT_DIR)
    return Path(archive_path)

@app.route("/", methods=["GET"])
def index():
    return render_template_string(INDEX_HTML, zip_ready=False, logs="")

@app.route("/run", methods=["POST"])
def run_batch():
    raw = request.form.get("ubns", "")
    headed = request.form.get("headed") == "on"
    
    # 在生产环境中强制无头模式
    is_production = os.environ.get("PORT") is not None or os.environ.get("RENDER") is not None or os.environ.get("RAILWAY_ENVIRONMENT") is not None
    if is_production:
        headed = False  # 生产环境强制无头模式
    
    ubns = normalize_ubns(raw)
    if not ubns:
        flash("請至少輸入 1 個有效統編（8 碼數字）。", "error")
        return redirect(url_for("index"))

    # 清空舊輸出
    for p in OUTPUT_DIR.glob("*.pdf"):
        try:
            p.unlink()
        except Exception:
            pass

    errors: List[Tuple[str, str]] = []
    ok_count = 0
    logs = []

    with sync_playwright() as p:
        for ubn in ubns:
            try:
                logs.append(f"處理 {ubn} ...")
                path = fetch_one(p, ubn, headless=not headed)
                ok_count += 1
                logs.append(f"  成功 → {path}")
            except Exception as e:
                msg = f"{ubn} 失敗：{e}"
                errors.append((ubn, str(e)))
                logs.append("  " + msg)
                continue

    zip_basename = "findbiz_pdfs"
    zip_path = make_zip(zip_basename) if ok_count > 0 else None

    if errors:
        msg = "完成 {} 筆，{} 筆失敗。".format(ok_count, len(errors))
        flash(msg, "error" if ok_count == 0 else "info")
    else:
        flash(f"全部完成，共 {ok_count} 筆。", "success")

    log_text = "\n".join(logs)

    if zip_path:
        return render_template_string(INDEX_HTML, zip_ready=True, zip_name=zip_path.name, count=ok_count, logs=log_text)
    else:
        return render_template_string(INDEX_HTML, zip_ready=False, logs=log_text)

@app.route("/download/<path:zip_name>")
def download_zip(zip_name):
    if not zip_name.endswith(".zip"):
        return "invalid file", 400
    path = Path(zip_name)
    if not path.exists():
        return "file not found", 404
    return send_file(str(path), as_attachment=True)

if __name__ == "__main__":
    # 讀取 Render/Railway 的動態 PORT（若不存在，預設 5000）
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
