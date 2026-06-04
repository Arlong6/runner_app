# runner_app — 個人跑步賽事提醒工具

抓運動筆記賽事頁(已聚合全台/全球路跑),產生 `races.ics`。
在 Google Calendar 訂閱一次,賽事日 + 報名截止前 7 天自動提醒,不再錯過報名。

**定位:純自己用的 personal tool。** 不上線、不給別人、不盈利。
若哪天想「擴出去給別人用」→ 商業規則回歸,先重跑 /product-gate。

## 用法

```bash
python3 runner_calendar.py            # 抓全部 → races.ics
python3 runner_calendar.py --taiwan   # 只要台灣賽事
```

## 在 Google Calendar 訂閱(只需做一次)

最省事的方式是讓 .ics 有個固定網址,Google 會定期自動同步:

1. 把 `races.ics` 放到一個有公開網址的地方(GitHub raw / Vercel / Gist)。
2. Google Calendar → 左側「其他日曆」+ →「以網址新增日曆」→ 貼上 .ics 網址。
3. 完成。之後重跑腳本更新該檔,行事曆自動跟著更新。

> 想先看效果,也可以直接「匯入」`races.ics`(設定 → 匯入),但匯入是一次性快照、不會自動更新。

## 自動更新(之後要做再做)

把腳本掛 cron / launchd 每天跑一次,push 更新後的 `races.ics`。
**注意 R7 護欄:這工具 ≤ 5hr/週,Nephilim 客戶案永遠優先。**

## 架構

```
runner_calendar.py
  fetch()      抓運動筆記賽事頁(靜態 HTML,純 stdlib)
  parse()      → Race 物件(名稱/日期/地點/距離/報名截止/連結)
  build_ics()  → races.ics(賽事 VEVENT + 報名截止 VEVENT 含 VALARM)
```

**可插拔來源**:目前一個來源(運動筆記)已聚合全台賽事。要加新來源,
只需寫一個回傳 `list[Race]` 的 parser,管線其餘不動。
新來源只在「真的要跑那場」時才加,不投機性地全抓(避免變成爬蟲農場)。

## 已知限制

- 報名截止只解析「MM-DD截止」格式;「報名時間未定」「已截止報名」「改期」不產生提醒。
- 運動筆記改版面會讓 parser 失效(personal tool,壞了有空再修)。
