#!/usr/bin/python3
"""Apple NZ 翻新机补货监控 → 飞书机器人通知。

用法:
  refurb_watch.py              检查一次，有新货就推飞书
  refurb_watch.py --dry-run    只打印匹配结果，不推送
  refurb_watch.py --test       自检 + 发一条测试消息到飞书

环境变量:
  FEISHU_WEBHOOK  飞书群自定义机器人 webhook（必填）
  FEISHU_SECRET   机器人开了"签名校验"才需要
"""
import base64, hashlib, hmac, json, os, sys, time, urllib.request

LIST_URL = "https://www.apple.com/nz/shop/refurbished/mac"  # 含全部 Mac 机型
STATE = os.environ.get("STATE_FILE") or os.path.expanduser("~/refurb-watch/state.json")

# 想盯的配置，改这里。chip 匹配标题（不分大小写），"m5 chip" 不会误中 "M5 Pro chip"
TARGETS = [
    {"chip": "m5 chip", "refurbClearModel": "macbookpro", "dimensionScreensize": "14inch",
     "tsMemorySize": "32gb", "dimensionCapacity": "1tb"},
    {"chip": "m5 pro chip", "refurbClearModel": "macbookpro", "tsMemorySize": "48gb"},  # 14/16 寸、任意硬盘
    {"chip": "m6 chip", "refurbClearModel": "macmini", "tsMemorySize": "32gb"},  # 任意硬盘
]

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129 Safari/537.36"
MARK = "window.REFURB_GRID_BOOTSTRAP = "
ALERT_AFTER = 3  # 连续失败几次才报警（每 5 分钟一次 ≈ 15 分钟）


def fetch_tiles():
    req = urllib.request.Request(LIST_URL, headers={"User-Agent": UA})
    for attempt in (1, 2):  # Apple 偶发 503，隔 10 秒重试一次
        try:
            html = urllib.request.urlopen(req, timeout=30).read().decode()
            break
        except OSError:  # HTTPError/URLError/超时 都是 OSError
            if attempt == 2:
                raise
            time.sleep(10)
    i = html.index(MARK) + len(MARK)  # 页面改版会在这里抛 ValueError
    return json.JSONDecoder().raw_decode(html[i:])[0]["tiles"]


def matches(tile):
    dims = tile.get("filters", {}).get("dimensions", {})
    title = tile["title"].lower()
    return any(t["chip"] in title and all(dims.get(k) == v for k, v in t.items() if k != "chip")
               for t in TARGETS)


def notify(text):
    hook = os.environ["FEISHU_WEBHOOK"]
    body = {"msg_type": "text", "content": {"text": text}}
    secret = os.environ.get("FEISHU_SECRET")
    if secret:
        ts = str(int(time.time()))
        key = f"{ts}\n{secret}".encode()
        body["timestamp"] = ts
        body["sign"] = base64.b64encode(hmac.new(key, b"", hashlib.sha256).digest()).decode()
    req = urllib.request.Request(hook, json.dumps(body).encode(), {"Content-Type": "application/json"})
    resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
    if resp.get("code", 0) != 0:
        raise RuntimeError(f"飞书返回错误: {resp}")


def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {"seen": [], "fails": 0}


def save_state(s):
    with open(STATE, "w") as f:
        json.dump(s, f)


def selftest():
    dims = {"refurbClearModel": "macbookpro", "dimensionScreensize": "14inch",
            "tsMemorySize": "32gb", "dimensionCapacity": "1tb"}
    tile = lambda title, **kw: {"title": title, "filters": {"dimensions": dict(dims, **kw)}}
    m5 = "Refurbished 14-inch MacBook Pro Apple M5 chip with 10‑Core CPU — Space Black"
    pro = "refurbished 16-inch macbook pro apple m5 pro chip with 18‑core cpu — silver"
    assert matches(tile(m5))
    assert not matches(tile(m5, tsMemorySize="24gb"))
    assert not matches(tile(pro))                                  # Pro 32GB 不要
    assert matches(tile(pro, tsMemorySize="48gb", dimensionScreensize="16inch", dimensionCapacity="2tb"))
    assert not matches(tile(m5.replace("M5", "M5 Max"), tsMemorySize="48gb"))
    mini = "Refurbished Mac mini Apple M6 Chip with 12‑Core CPU and 12‑Core GPU"
    mini_dims = {"refurbClearModel": "macmini", "tsMemorySize": "32gb", "dimensionCapacity": "512gb"}
    assert matches({"title": mini, "filters": {"dimensions": mini_dims}})
    assert not matches({"title": mini, "filters": {"dimensions": dict(mini_dims, tsMemorySize="24gb")}})
    assert not matches({"title": mini.replace("M6", "M5 Pro"), "filters": {"dimensions": mini_dims}})

    # 报警节奏：失败 1、2 次静默 exit 0，第 3 次报警并 exit 1，之后静默 exit 0，恢复发一次
    import tempfile, urllib.error
    g, sent, codes = globals(), [], []
    saved = {k: g[k] for k in ("STATE", "fetch_tiles", "notify")}
    def boom(): raise urllib.error.HTTPError(LIST_URL, 503, "Service Unavailable", None, None)
    g.update(STATE=tempfile.mktemp(), notify=sent.append, fetch_tiles=boom)
    sys.argv = ["x"]
    try:
        for _ in range(4):
            try: main()
            except SystemExit as e: codes.append(e.code)
        assert codes == [0, 0, 1, 0] and len(sent) == 1 and sent[0].startswith("⚠️"), (codes, sent)
        g["fetch_tiles"] = lambda: []
        main()
        assert len(sent) == 2 and sent[1].startswith("✅") and load_state()["fails"] == 0, sent
        main()
        assert len(sent) == 2  # 恢复后不再重复发
    finally:
        os.remove(g["STATE"]); g.update(saved)
    print("selftest ok")


def main():
    args = sys.argv[1:]
    if "--test" in args:
        selftest()
        notify("✅ 翻新机监控测试消息：飞书通知正常")
        print("测试消息已发送")
        return

    state = load_state()
    fails = state.get("fails", ALERT_AFTER if state.get("failing") else 0)  # 兼容旧 state 的 failing 布尔
    try:
        hits = [t for t in fetch_tiles() if matches(t)]
    except Exception as e:
        fails += 1
        print(f"检查失败 (连续第 {fails} 次): {e!r}", file=sys.stderr)
        if fails == ALERT_AFTER and "--dry-run" not in args:  # 偶发抖动不吵人；只在刚达到阈值时提醒一次
            notify(f"⚠️ 翻新机监控连续 {fails} 次失败（约 {fails * 5} 分钟），可能 Apple 维护或页面改版：{e!r}")
        state["fails"] = fails
        state.pop("failing", None)
        save_state(state)
        sys.exit(1 if fails == ALERT_AFTER else 0)  # 只在报警那次标红：维护几小时也只收一封 GitHub 失败邮件

    if fails >= ALERT_AFTER and "--dry-run" not in args:
        notify(f"✅ 翻新机监控已恢复（此前连续失败 {fails} 次）")

    if "--dry-run" in args:
        for t in hits:
            print(t["partNumber"], t["price"]["currentPrice"]["amount"], t["title"])
        print(f"{len(hits)} 个匹配")
        return

    new = [t for t in hits if t["partNumber"] not in state["seen"]]
    for t in new:
        url = "https://www.apple.com" + t["productDetailsUrl"].split("?")[0]
        notify(f"🍎 翻新机补货！\n{t['title']}\n{t['price']['currentPrice']['amount']}\n{url}")
    # 只记当前有货的：卖完再补货会再次提醒
    # day 每天变一次 → 云端每天至少一次提交，防止 GitHub 60 天无活动停掉定时任务
    state = {"seen": [t["partNumber"] for t in hits], "fails": 0, "day": time.strftime("%F")}
    save_state(state)
    print(time.strftime("%F %T"), f"匹配 {len(hits)}，新通知 {len(new)}")


if __name__ == "__main__":
    main()
