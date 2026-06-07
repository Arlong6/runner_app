#!/usr/bin/env python3
"""
runner_calendar — 個人用跑步賽事提醒工具

抓運動筆記賽事頁(已聚合全台/全球路跑),產生 races.ics。
在 Google Calendar「訂閱」這個 .ics 一次,之後:
  - 每場賽事當天出現在行事曆
  - 報名截止日 + 截止前 N 天自動提醒,不再錯過報名

純標準函式庫,零 pip 依賴。資料源是運動筆記的公開靜態頁面。

用法:
  python3 runner_calendar.py                 # 抓全部,輸出 races.ics
  python3 runner_calendar.py --taiwan        # 只要台灣賽事
  python3 runner_calendar.py --out my.ics     # 自訂輸出檔名
"""
from __future__ import annotations

import argparse
import glob
import html as html_lib
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, timedelta

SOURCE_URL = "https://running.biji.co/?q=competition"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
)
# 報名截止前幾天提醒(可調)
SIGNUP_REMIND_DAYS = 7


@dataclass
class Race:
    cid: str
    name: str
    start: date
    end: date
    place: str = ""
    distances: list[str] = field(default_factory=list)
    signup_start: date | None = None
    signup_deadline: date | None = None
    signup_status: str = ""
    organizer: str = ""
    url: str = ""


# ---------- 抓取 ----------

def fetch(url: str = SOURCE_URL) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


# ---------- 解析 ----------

_ROW_SPLIT = re.compile(r'class="competition-list-row"')
_CID = re.compile(r"act=info&cid=(\d+)")
# 運動筆記內嵌的 Google Calendar 連結帶乾淨日期: dates=YYYYMMDD/YYYYMMDD
_GCAL_DATES = re.compile(r"dates=(\d{8})/(\d{8})")
_NAME = re.compile(
    r'class="competition-name">\s*<a[^>]*>([^<]+)</a>', re.S
)
_PLACE = re.compile(r'class="competition-place"><span>([^<]*)</span>')
_DISTANCE = re.compile(r'class="event-item event_item"[^>]*>([^<]+)</div>')
_STATUS = re.compile(
    r'class="competition-status"[^>]*>\s*<span>\s*([^<]+?)\s*</span>', re.S
)
_DEADLINE = re.compile(r"(\d{2})-(\d{2})截止")


def _parse_date8(s: str) -> date:
    return date(int(s[0:4]), int(s[4:6]), int(s[6:8]))


def _parse_deadline(status: str, race_start: date) -> date | None:
    """status 形如 '06-17截止' → 推回賽事所屬年份的截止日。"""
    m = _DEADLINE.search(status)
    if not m:
        return None
    mm, dd = int(m.group(1)), int(m.group(2))
    # 截止日在賽事年份;若月份大於賽事月份(跨年報名),退一年
    year = race_start.year
    try:
        d = date(year, mm, dd)
    except ValueError:
        return None
    if d > race_start:
        try:
            d = date(year - 1, mm, dd)
        except ValueError:
            return None
    return d


def parse(html: str) -> list[Race]:
    rows = _ROW_SPLIT.split(html)[1:]  # 第一段是表頭前綴
    races: list[Race] = []
    for row in rows:
        cid_m = _CID.search(row)
        dates_m = _GCAL_DATES.search(row)
        name_m = _NAME.search(row)
        if not (cid_m and dates_m and name_m):
            continue
        start = _parse_date8(dates_m.group(1))
        end = _parse_date8(dates_m.group(2))
        name = html_lib.unescape(name_m.group(1)).strip()
        place = ""
        if (pm := _PLACE.search(row)):
            place = html_lib.unescape(pm.group(1)).strip()
        distances = [
            html_lib.unescape(d).strip()
            for d in _DISTANCE.findall(row)
            if d.strip()
        ]
        status = ""
        if (sm := _STATUS.search(row)):
            status = html_lib.unescape(sm.group(1)).strip()
        deadline = _parse_deadline(status, start)
        cid = cid_m.group(1)
        url = (
            "https://running.biji.co/index.php"
            f"?q=competition&act=info&cid={cid}"
        )
        races.append(
            Race(
                cid=cid,
                name=name,
                start=start,
                end=end,
                place=place,
                distances=distances,
                signup_deadline=deadline,
                signup_status=status,
                url=url,
            )
        )
    return races


# ---------- 詳情頁補抓報名開始/截止 ----------

# 詳情頁格式: 報名日期】2026/02/26 11:00 ~ 2026/05/05 23:59
_SIGNUP_RANGE = re.compile(
    r"報名日期】\s*(\d{4})/(\d{2})/(\d{2})[^~]*~[^\d]*(\d{4})/(\d{2})/(\d{2})"
)
_ORGANIZER = re.compile(
    r'主辦單位</div>\s*<div class="data-content">\s*(.*?)\s*</div>', re.S
)


def _fetch_detail(r: Race) -> None:
    """抓單場詳情頁,補上報名開始日 + 更精確截止日 + 主辦單位。失敗就保留原樣。"""
    try:
        html = fetch(r.url)
    except Exception:
        return
    m = _SIGNUP_RANGE.search(html)
    if m:
        try:
            r.signup_start = date(int(m[1]), int(m[2]), int(m[3]))
            r.signup_deadline = date(int(m[4]), int(m[5]), int(m[6]))
        except ValueError:
            pass
    om = _ORGANIZER.search(html)
    if om:
        org = html_lib.unescape(re.sub(r"<[^>]+>", "", om.group(1))).strip()
        if org:
            r.organizer = org


def enrich_signup_dates(races: list[Race], workers: int = 8) -> None:
    """多線程補抓詳情頁的報名開始~截止日(只處理運動筆記來源的場次)。"""
    targets = [r for r in races if "running.biji.co" in r.url]
    if not targets:
        return
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(_fetch_detail, targets))


# ---------- 產生 ICS ----------

def _esc(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def _fold(line: str) -> str:
    """ICS 規範:每行 <=75 octets,超過要折行。"""
    out = []
    raw = line.encode("utf-8")
    while len(raw) > 73:
        cut = 73
        # 不要切在多位元組字元中間
        while cut > 0 and (raw[cut] & 0xC0) == 0x80:
            cut -= 1
        out.append(raw[:cut].decode("utf-8"))
        raw = b" " + raw[cut:]
    out.append(raw.decode("utf-8"))
    return "\r\n".join(out)


def build_ics(races: list[Race]) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//runner_app//biji//ZH-TW",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:跑步賽事提醒",
        "X-WR-TIMEZONE:Asia/Taipei",
    ]
    for r in races:
        dist = " / ".join(r.distances) if r.distances else "—"
        desc_parts = [f"距離: {dist}", f"地點: {r.place or '—'}"]
        if r.signup_start:
            desc_parts.append(f"報名開始: {r.signup_start.strftime('%Y/%m/%d')}")
        if r.signup_deadline:
            desc_parts.append(f"報名截止: {r.signup_deadline.strftime('%Y/%m/%d')}")
        if r.signup_status and not r.signup_deadline:
            desc_parts.append(f"報名: {r.signup_status}")
        desc_parts.append(r.url)
        desc = "\\n".join(_esc(p) for p in desc_parts)

        # 賽事當天(全天事件,DTEND 為隔天)
        lines += [
            "BEGIN:VEVENT",
            f"UID:race-{r.cid}@runner_app",
            f"DTSTART;VALUE=DATE:{r.start.strftime('%Y%m%d')}",
            f"DTEND;VALUE=DATE:{(r.end + timedelta(days=1)).strftime('%Y%m%d')}",
            f"SUMMARY:{_esc('🏃 ' + r.name)}",
            f"DESCRIPTION:{desc}",
        ]
        if r.place:
            lines.append(f"LOCATION:{_esc(r.place)}")
        lines += ["TRANSP:TRANSPARENT", "END:VEVENT"]

        # 報名截止提醒(全天 + 截止前 N 天 alarm)
        if r.signup_deadline:
            dl = r.signup_deadline
            lines += [
                "BEGIN:VEVENT",
                f"UID:signup-{r.cid}@runner_app",
                f"DTSTART;VALUE=DATE:{dl.strftime('%Y%m%d')}",
                f"DTEND;VALUE=DATE:{(dl + timedelta(days=1)).strftime('%Y%m%d')}",
                f"SUMMARY:{_esc('⏰ 報名截止: ' + r.name)}",
                f"DESCRIPTION:{desc}",
                "TRANSP:TRANSPARENT",
                "BEGIN:VALARM",
                "ACTION:DISPLAY",
                f"DESCRIPTION:{_esc('報名即將截止: ' + r.name)}",
                f"TRIGGER:-P{SIGNUP_REMIND_DAYS}D",
                "END:VALARM",
                "END:VEVENT",
            ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(ln) for ln in lines) + "\r\n"


def load_manual_races(path: str = "manual_races.json") -> list[Race]:
    """手動加入運動筆記沒有的賽事(例如日本富士山馬拉松)。
    格式見 manual_races.json:每筆含 name/start/end/place/distances/deadline/status/url。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    out: list[Race] = []
    for d in data:
        dl = d.get("deadline")
        ss = d.get("signup_start")
        out.append(
            Race(
                cid=str(d["cid"]),
                name=d["name"],
                start=date.fromisoformat(d["start"]),
                end=date.fromisoformat(d.get("end") or d["start"]),
                place=d.get("place", ""),
                distances=d.get("distances", []),
                signup_start=date.fromisoformat(ss) if ss else None,
                signup_deadline=date.fromisoformat(dl) if dl else None,
                signup_status=d.get("status", ""),
                url=d.get("url", ""),
            )
        )
    return out


def races_to_json(races: list[Race]) -> str:
    """全部賽事 → JSON,給靜態網頁讀取(瀏覽/勾選用)。"""
    data = [
        {
            "cid": r.cid,
            "name": r.name,
            "start": r.start.isoformat(),
            "end": r.end.isoformat(),
            "place": r.place,
            "distances": r.distances,
            "signup_start": r.signup_start.isoformat() if r.signup_start else None,
            "deadline": r.signup_deadline.isoformat() if r.signup_deadline else None,
            "status": r.signup_status,
            "organizer": r.organizer,
            "url": r.url,
        }
        for r in sorted(races, key=lambda x: x.start)
    ]
    return json.dumps(data, ensure_ascii=False, indent=0)


# ---------- 主程式 ----------

def load_watchlist(path: str) -> list[tuple[str, str]]:
    """讀 watchlist.txt。每行一個關鍵字(子字串比對),或 cid:12345 精確比對。
    # 開頭與空行忽略。回傳 [(kind, value), ...],kind 為 'cid' 或 'kw'。"""
    terms: list[tuple[str, str]] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                if s.lower().startswith("cid:"):
                    terms.append(("cid", s[4:].strip()))
                else:
                    terms.append(("kw", s.casefold()))
    except FileNotFoundError:
        return []
    return terms


def filter_by_watchlist(
    races: list[Race], terms: list[tuple[str, str]]
) -> tuple[list[Race], list[str]]:
    """只留下符合 watchlist 任一關鍵字的賽事。回傳 (篩選後賽事, 0 命中的關鍵字)。"""
    kept: list[Race] = []
    hits = {t: 0 for t in terms}
    for r in races:
        name_cf = r.name.casefold()
        matched = False
        for t in terms:
            kind, val = t
            if (kind == "cid" and r.cid == val) or (
                kind == "kw" and val in name_cf
            ):
                hits[t] += 1
                matched = True
        if matched:
            kept.append(r)
    unmatched = [val for (kind, val), n in hits.items() if n == 0]
    return kept, unmatched


def is_taiwan(r: Race) -> bool:
    tw = ("台", "臺", "新北", "桃園", "高雄", "台中", "台南", "新竹",
          "基隆", "宜蘭", "花蓮", "台東", "臺東", "屏東", "南投",
          "嘉義", "雲林", "彰化", "苗栗", "金門", "澎湖", "馬祖")
    return any(k in r.place for k in tw)


def generate_one(
    all_races: list[Race], watchlist_path: str, out_path: str, taiwan: bool
) -> None:
    """依單一 watchlist 篩選 all_races,寫出一個 .ics。"""
    label = os.path.basename(out_path)
    terms = load_watchlist(watchlist_path)
    races = all_races
    if terms:
        races, unmatched = filter_by_watchlist(races, terms)
        msg = f"[{label}] watchlist {len(terms)} 條 → {len(races)} 場"
        if unmatched:
            msg += "(沒對到: " + ", ".join(unmatched) + ")"
        print(msg, file=sys.stderr)
    else:
        print(f"[{label}] 無關鍵字 → 收錄全部 {len(races)} 場", file=sys.stderr)
    if taiwan:
        races = [r for r in races if is_taiwan(r)]
    with_deadline = sum(1 for r in races if r.signup_deadline)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(build_ics(races))
    print(
        f"[{label}] 已寫出({len(races)} 場,{with_deadline} 場有報名截止提醒)",
        file=sys.stderr,
    )


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="跑步賽事提醒 → .ics")
    ap.add_argument("--taiwan", action="store_true", help="只要台灣賽事")
    ap.add_argument(
        "--watchlist",
        default="watchlist.txt",
        help="單一追蹤清單檔(預設 watchlist.txt)",
    )
    ap.add_argument(
        "--all", action="store_true", help="忽略 watchlist,收錄全部賽事"
    )
    ap.add_argument("--out", default="races.ics", help="單檔模式輸出檔名")
    ap.add_argument("--url", default=SOURCE_URL, help="來源頁(預設運動筆記賽事頁)")
    ap.add_argument(
        "--batch",
        action="store_true",
        help="多清單模式:掃 watchlists/*.txt,各產生一個同名 .ics(只抓一次)",
    )
    ap.add_argument(
        "--web",
        action="store_true",
        help="只產生 docs/races.json(給靜態網頁瀏覽/勾選用)",
    )
    ap.add_argument(
        "--fast",
        action="store_true",
        help="跳過詳情頁補抓(不取報名開始日,僅供快速測試)",
    )
    args = ap.parse_args(argv)

    print(f"抓取: {args.url}", file=sys.stderr)
    all_races = parse(fetch(args.url))
    print(f"解析到 {len(all_races)} 場賽事", file=sys.stderr)

    if not args.fast:
        print("補抓詳情頁報名開始/截止日…", file=sys.stderr)
        enrich_signup_dates(all_races)
        got = sum(1 for r in all_races if r.signup_start)
        print(f"  已補上報名開始日 {got}/{len(all_races)} 場", file=sys.stderr)

    manual = load_manual_races()
    if manual:
        all_races += manual
        print(f"加入手動賽事 {len(manual)} 場", file=sys.stderr)

    # 給靜態網頁用的全賽事資料(瀏覽→勾選→前端自己生成 .ics)
    if args.batch or args.web:
        os.makedirs("docs", exist_ok=True)
        with open("docs/races.json", "w", encoding="utf-8") as f:
            f.write(races_to_json(all_races))
        print(f"已寫出 docs/races.json({len(all_races)} 場)", file=sys.stderr)
        if args.web:
            return 0

    # 多清單模式:你自己 watchlist.txt → races.ics,每位朋友 watchlists/<name>.txt → <name>.ics
    if args.batch or os.path.isdir("watchlists"):
        jobs: list[tuple[str, str]] = []
        if os.path.isfile("watchlist.txt"):
            jobs.append(("watchlist.txt", "races.ics"))
        for wl in sorted(glob.glob(os.path.join("watchlists", "*.txt"))):
            name = os.path.splitext(os.path.basename(wl))[0]
            if name.startswith("_"):  # _ 開頭是範本,跳過
                continue
            jobs.append((wl, f"{name}.ics"))
        if not jobs:
            print("沒有任何清單檔(watchlist.txt 或 watchlists/*.txt)", file=sys.stderr)
            return 1
        for wl, out in jobs:
            generate_one(all_races, wl, out, args.taiwan)
        return 0

    # 單檔模式
    if args.all:
        generate_one(all_races, os.devnull, args.out, args.taiwan)
    else:
        generate_one(all_races, args.watchlist, args.out, args.taiwan)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
