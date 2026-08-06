"""CDP 验证：假设可验证性评分卡片渲染（fetch 覆写 mock /discoveries 走真实渲染流程）。

验证点：
1. Discovery 页正常加载
2. fetch 覆写 discoveries 返回带评分的假设 → renderDiscovery 真实渲染评分卡片
3. 评分条 + 综合分 + 降序
4. 材料页（覆盖度重抽后）正常显示
"""
import base64
import json
import os
import subprocess
import time
import urllib.request

import websocket

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PORT = 9229
BASE = "http://127.0.0.1:8001"
PROJECT = "proj_20260806_152947_31cdd6"

results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond), extra))
    print(("PASS" if cond else "FAIL"), "-", name, extra)


def wait_port(port, timeout=15):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1)
            return True
        except Exception:
            time.sleep(0.3)
    return False


MOCK_DISCOVERIES = {
    "project_id": PROJECT,
    "run_mode": "discovery",
    "discovery_summary": {
        "hypotheses": 3,
        "candidates": 6,
        "relationships": 2,
        "novel": 1,
        "hypothesis_list": [
            {
                "hypothesis": "掺杂浓度 x 对 ZT 的构效关系：低浓度区线性增益，存在最优掺杂窗口",
                "variables": ["doping_ratio", "temperature"], "target_property": "ZT",
                "rationale": "关联 Gap[G1]：掺杂最优浓度区间缺乏系统对比",
                "gap_ref": "[G1] 掺杂最优浓度区间",
                "novelty_score": 0.85, "feasibility_score": 0.72,
                "gap_relevance_score": 0.90, "overall_score": 0.83,
            },
            {
                "hypothesis": "晶粒尺寸细化可同时提升电导率与抑制热导率，存在最佳粒径窗口",
                "variables": ["grain_size"], "target_property": "ZT",
                "rationale": "关联 Gap[G2]：粒径-性能关系文献分散",
                "gap_ref": "[G2] 粒径窗口",
                "novelty_score": 0.62, "feasibility_score": 0.78,
                "gap_relevance_score": 0.70, "overall_score": 0.69,
            },
            {
                "hypothesis": "新型 Zintl 相化合物具有更低晶格热导率潜力",
                "variables": ["composition"], "target_property": "ZT",
                "rationale": "关联 Gap[G3]：Zintl 相数据稀疏",
                "gap_ref": "[G3] Zintl 相",
                "novelty_score": 0.45, "feasibility_score": 0.55,
                "gap_relevance_score": 0.60, "overall_score": 0.51,
            },
        ],
    },
    "relationships": [],
}

MOCK_JS = "window.__MOCK_DISCOVERIES__ = " + json.dumps(MOCK_DISCOVERIES) + """;
(() => {
    const origFetch = window.fetch;
    window.fetch = async (url, opts) => {
        const u = String(url);
        if (u.includes('/discoveries')) {
            window.__MOCK_HIT__ = (window.__MOCK_HIT__ || 0) + 1;
            return new Response(JSON.stringify(window.__MOCK_DISCOVERIES__), {
                status: 200, headers: { 'Content-Type': 'application/json' },
            });
        }
        return origFetch(url, opts);
    };
})();
"""


def main():
    subprocess.run(
        ["powershell", "-Command",
         "Get-Process msedge -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -match '9229' } | Stop-Process -Force -Confirm:$false"],
        capture_output=True)
    time.sleep(1)

    user_data = os.path.join(os.getcwd(), "_edge_hyp3_profile")
    proc = subprocess.Popen([
        EDGE, "--headless", "--disable-gpu",
        f"--remote-debugging-port={PORT}", "--remote-allow-origins=*",
        f"--user-data-dir={user_data}",
        "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    if not wait_port(PORT):
        print("Edge debug port not ready")
        return 1

    tabs = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json").read())
    target = tabs[0] if tabs else None
    if not target:
        print("No target tab")
        return 1
    ws = websocket.create_connection(target["webSocketDebuggerUrl"])
    mid = 0

    def send(method, params=None):
        nonlocal mid
        mid += 1
        ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == mid:
                return msg.get("result", {})

    def eval_js(expr):
        r = send("Runtime.evaluate", {"expression": expr, "returnByValue": True})
        return r.get("result", {}).get("value")

    send("Page.enable")
    send("Runtime.enable")

    # 1. 页面加载前注入 fetch 覆写（仅 mock discoveries 接口）
    send("Page.addScriptToEvaluateOnNewDocument", {"source": MOCK_JS})

    # 2. 导航到 discovery 页
    send("Page.navigate", {"url": f"{BASE}/?project={PROJECT}&page=discovery"})
    time.sleep(3.5)

    # 3. 验证
    title = eval_js("document.getElementById('topbar-title')?.textContent")
    check("深链 page=discovery 直达", title == "构效关系发现", f"title={title}")
    check("fetch mock 命中", (eval_js("window.__MOCK_HIT__") or 0) >= 1,
          f"hit={eval_js('window.__MOCK_HIT__')}")
    hy_items = eval_js("document.querySelectorAll('.hy-item').length")
    check("假设评分条目渲染数=3", hy_items == 3, f"count={hy_items}")
    bars = eval_js("document.querySelectorAll('.hy-bars .rec-popbar').length")
    check("评分条总数=9（3 条 × 3 项）", bars == 9, f"bars={bars}")
    overalls = eval_js("""
        (() => {
            const vals = [];
            document.querySelectorAll('.hy-overall-val').forEach(v => vals.push(v.textContent));
            return vals;
        })()
    """)
    check("综合分按降序显示", overalls == ["0.83", "0.69", "0.51"], f"vals={overalls}")
    widths = eval_js("""
        (() => {
            const w = [];
            document.querySelectorAll('.hy-item:first-child .rec-popbar-fill').forEach(f => w.push(f.style.width));
            return w;
        })()
    """)
    check("首条评分条宽度 85%/72%/90%", widths == ["85%", "72%", "90%"], f"w={widths}")

    # 4. 布局检查
    layout = eval_js("""
        (() => {
            const issues = [];
            const vw = window.innerWidth;
            document.querySelectorAll('.hy-item, .hy-head, .hy-bars, .rec-popbar').forEach(el => {
                const r = el.getBoundingClientRect();
                if (r.width <= 0 || r.height <= 0) return;
                if (r.right > vw + 2 || r.left < -2) issues.push('h-overflow:' + String(el.className).slice(0, 20));
            });
            return issues;
        })()
    """)
    check("评分卡片零横向溢出", layout is not None and len(layout) == 0, f"issues={layout}")

    # 5. 截图
    shot = send("Page.captureScreenshot", {"format": "png"})
    if shot and shot.get("data"):
        with open("_hyp_score_page.png", "wb") as f:
            f.write(base64.b64decode(shot["data"]))
        print("[shot] _hyp_score_page.png saved")

    # 6. 切到材料页验证真实数据（fetch 覆写只拦 discoveries，材料页走真实接口）
    send("Page.navigate", {"url": f"{BASE}/?project={PROJECT}&page=materials"})
    time.sleep(3.0)
    mat_title = eval_js("document.getElementById('topbar-title')?.textContent")
    check("材料页直达", mat_title == "材料知识", f"title={mat_title}")
    mat_count = eval_js("document.querySelectorAll('.list-item').length")
    check("材料列表已渲染", (mat_count or 0) > 0, f"count={mat_count}")
    has_full = eval_js("""
        (() => {
            const items = document.querySelectorAll('.list-item');
            for (const it of items) {
                const t = it.textContent || '';
                if (t.includes('Germanium selenide') && (t.includes('性能') || t.includes('合成'))) return true;
            }
            return false;
        })()
    """)
    check("Germanium selenide 显示性能/合成数据", has_full)

    ws.close()
    proc.terminate()

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n===== {passed}/{len(results)} PASS =====")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
