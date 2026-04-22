"""勤怠管理アプリ：打刻・申請・承認・勤務時間計算（社員/本社AB/現場AB）"""
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from datetime import datetime, date, time, timedelta
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo
import csv
import io
import calendar

import jpholiday
import database as db

JST = ZoneInfo("Asia/Tokyo")


def now_jst() -> datetime:
    """日本時間の現在時刻（naive）"""
    return datetime.now(JST).replace(tzinfo=None)


def today_jst() -> date:
    return datetime.now(JST).date()

BASE_DIR = Path(__file__).parent

# 会社統一ルール
OT_ROUND_UNIT = 30         # 残業を30分単位で切り捨て
OT_MIN_MINUTES = 30        # 30分未満は残業カウントなし
GENBA_ROUND_UNIT = 30      # 現場アルバイトの勤務時間を30分単位切り捨て

# 昼休憩と午後休憩の時間帯（社員/本社AB用・自動控除のため）
LUNCH_START = time(12, 0)
LUNCH_END   = time(13, 0)
AFTERNOON_BREAK_START = time(15, 0)
AFTERNOON_BREAK_END   = time(15, 15)

app = FastAPI(title="勤怠管理アプリ")
app.add_middleware(SessionMiddleware, secret_key="kintai-secret-change-me")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

db.init_db()


def current_user(request: Request):
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return db.get_user(user_id)


def require_admin(request: Request):
    user = current_user(request)
    if not user or user["role"] != "admin":
        return None
    return user


def fmt_hm(minutes: int) -> str:
    if minutes <= 0:
        return "0時間00分"
    return f"{minutes // 60}時間{minutes % 60:02d}分"


def parse_hhmm(s: str):
    """'HH:MM' → time オブジェクト。None なら None"""
    if not s:
        return None
    h, m = s.split(":")
    return time(int(h), int(m))


def combine(d: date, t: time) -> datetime:
    return datetime.combine(d, t)


def is_legal_holiday(d: date) -> bool:
    """法定休日：日曜・祝日・特別休日（お正月/お盆）"""
    if d.weekday() == 6:  # 日曜
        return True
    if jpholiday.is_holiday(d):
        return True
    mm_dd = d.strftime("%m-%d")
    for sh in db.get_special_holidays():
        if sh["start_date"] <= mm_dd <= sh["end_date"]:
            return True
    return False


def is_regular_holiday(d: date) -> bool:
    """法定外休日：土曜"""
    return d.weekday() == 5


def overlap_minutes(a_start: datetime, a_end: datetime,
                    b_start: datetime, b_end: datetime) -> int:
    """2区間の重なり分数"""
    s = max(a_start, b_start)
    e = min(a_end, b_end)
    if e <= s:
        return 0
    return int((e - s).total_seconds() / 60)


def floor_to(minutes: int, unit: int) -> int:
    return (minutes // unit) * unit


def collect_punches(punches_of_day):
    """打刻から出勤・退勤・休憩区間を取り出す"""
    clock_in = None
    clock_out = None
    breaks = []
    break_start = None
    for p in punches_of_day:
        dt = datetime.fromisoformat(p["punched_at"])
        t = p["punch_type"]
        if t == "in" and clock_in is None:
            clock_in = dt
        elif t == "out":
            clock_out = dt
        elif t == "break_in":
            break_start = dt
        elif t == "break_out" and break_start is not None:
            breaks.append((break_start, dt))
            break_start = None
    return clock_in, clock_out, breaks, break_start is not None


def calc_day_summary(punches_of_day, user=None, day_date: date = None):
    """
    日別の勤務集計。
    user が None の場合は旧仕様（社員デフォルトで計算）で動作。
    戻り値：
      clock_in / clock_out : datetime or None
      break_minutes : int（表示用：自動控除分 + 打刻休憩）
      break_ongoing : bool
      worked_minutes : 勤務合計（所定 + 所定外）
      overtime_minutes : 所定外（残業）
      shotei_minutes : 所定
      holiday_work_minutes : 法定休日労働（現場AB向け）
      worked_on_holiday : bool（休日に勤務があったか）
      emp_type : 従業員タイプ
    """
    clock_in, clock_out, breaks, break_ongoing = collect_punches(punches_of_day)

    punched_break = sum(int((e - s).total_seconds() / 60) for s, e in breaks)

    # デフォルトは社員
    emp_type = user["emp_type"] if user else "seishain"
    sched_start = parse_hhmm(user["scheduled_start"] if user else "09:00") or time(9, 0)
    sched_end   = parse_hhmm(user["scheduled_end"]   if user else "18:15") or time(18, 15)
    auto_break  = (user["auto_break_minutes"] if user else 75) or 0

    result = {
        "clock_in": clock_in, "clock_out": clock_out,
        "break_minutes": 0, "break_ongoing": break_ongoing,
        "worked_minutes": 0, "overtime_minutes": 0,
        "shotei_minutes": 0, "holiday_work_minutes": 0,
        "worked_on_holiday": False,
        "emp_type": emp_type,
    }

    if not (clock_in and clock_out):
        return result

    dd = day_date or clock_in.date()
    holiday = is_legal_holiday(dd) or is_regular_holiday(dd)

    if emp_type == "genba_arubaito":
        # 現場アルバイト：出退勤ベース、実休憩控除、30分単位切捨
        raw = int((clock_out - clock_in).total_seconds() / 60)
        net = max(0, raw - punched_break)
        worked = floor_to(net, GENBA_ROUND_UNIT)
        result["break_minutes"] = punched_break
        result["worked_minutes"] = worked
        result["shotei_minutes"] = worked
        result["worked_on_holiday"] = holiday and worked > 0
        if is_legal_holiday(dd):
            result["holiday_work_minutes"] = worked
        return result

    # 社員 / 本社アルバイト：定時ベース、早出切捨、自動休憩控除
    sched_start_dt = combine(dd, sched_start)
    sched_end_dt   = combine(dd, sched_end)
    scheduled_duration = int((sched_end_dt - sched_start_dt).total_seconds() / 60) - auto_break

    # 早出は定時扱い
    effective_start = max(clock_in, sched_start_dt)
    effective_end = clock_out
    if effective_end <= effective_start:
        return result

    raw = int((effective_end - effective_start).total_seconds() / 60)

    # 自動休憩：勤務区間と昼休憩/午後休憩の重なり分を控除
    lunch = (combine(dd, LUNCH_START), combine(dd, LUNCH_END))
    pm    = (combine(dd, AFTERNOON_BREAK_START), combine(dd, AFTERNOON_BREAK_END))
    auto_deducted = overlap_minutes(effective_start, effective_end, *lunch) \
                  + overlap_minutes(effective_start, effective_end, *pm)
    total_break = auto_deducted + punched_break
    result["break_minutes"] = total_break

    net = max(0, raw - auto_deducted - punched_break)

    if holiday:
        # 休日出勤は1日カウント＋時間もwork_minutesとして表示
        result["worked_minutes"] = net
        result["shotei_minutes"] = net
        result["worked_on_holiday"] = True
        if is_legal_holiday(dd):
            result["holiday_work_minutes"] = net
        return result

    # 平日
    shotei = min(net, scheduled_duration)
    ot_raw = max(0, net - scheduled_duration)
    ot_final = floor_to(ot_raw, OT_ROUND_UNIT) if ot_raw >= OT_MIN_MINUTES else 0

    result["shotei_minutes"] = shotei
    result["overtime_minutes"] = ot_final
    result["worked_minutes"] = shotei + ot_final
    return result


def build_monthly_rows(user_id, year, month):
    """月次の行データと合計を返す（申請の反映含む）"""
    user_row = db.get_user(user_id)
    punches = db.get_monthly_punches(user_id, year, month)
    reqs = db.get_approved_requests_in_month(user_id, year, month)

    # 申請を日付ごとに分類
    req_by_day = defaultdict(list)
    for r in reqs:
        req_by_day[r["target_date"]].append(r)

    by_day = defaultdict(list)
    for p in punches:
        day_key = datetime.fromisoformat(p["punched_at"]).strftime("%Y-%m-%d")
        by_day[day_key].append(p)

    # 月の全日付を生成（freeeスタイル：打刻がなくても全日行を出す）
    last_day = calendar.monthrange(year, month)[1]
    all_days = [f"{year:04d}-{month:02d}-{d:02d}" for d in range(1, last_day + 1)]

    rows = []
    total_work = 0
    total_ot = 0
    total_break = 0
    total_holiday_minutes = 0
    leave_days = 0.0
    holiday_work_days = 0
    workday_count = 0  # 平日出勤日数

    weekday_jp = ["月", "火", "水", "木", "金", "土", "日"]

    for day_key in all_days:
        day_reqs = req_by_day.get(day_key, [])
        day_punches = list(by_day.get(day_key, []))
        day_date_obj = datetime.strptime(day_key, "%Y-%m-%d").date()

        # 承認済み「臨時出勤」→ 打刻として加算
        for r in day_reqs:
            if r["req_type"] == "extra_work" and r["start_time"] and r["end_time"]:
                day_punches.append({
                    "punch_type": "in",
                    "punched_at": f"{day_key}T{r['start_time']}:00",
                })
                day_punches.append({
                    "punch_type": "out",
                    "punched_at": f"{day_key}T{r['end_time']}:00",
                })
        day_punches.sort(key=lambda p: p["punched_at"])

        # 承認済み「打刻修正」→ 打刻を上書き
        punch_fix = next((r for r in day_reqs if r["req_type"] == "punch_fix"), None)
        if punch_fix:
            day_punches = []
            for kind, col in [("in","fix_clock_in"),("break_in","fix_break_in"),
                              ("break_out","fix_break_out"),("out","fix_clock_out")]:
                if punch_fix[col]:
                    day_punches.append({
                        "punch_type": kind,
                        "punched_at": f"{day_key}T{punch_fix[col]}:00",
                    })

        s = calc_day_summary(day_punches, user=user_row, day_date=day_date_obj)

        # 有給・半休の日数カウント（leave_kindが'paid'のみ消化）
        is_full_leave = any(r["req_type"] == "leave" for r in day_reqs)
        half = next((r for r in day_reqs if r["req_type"] == "half_leave"), None)
        if is_full_leave:
            leave_days += 1.0
        elif half and (half["leave_kind"] or "paid") == "paid":
            leave_days += 0.5

        # 休日出勤カウント
        if s["worked_on_holiday"]:
            holiday_work_days += 1
        total_holiday_minutes += s["holiday_work_minutes"]

        # 交通費（承認済み）
        day_transport = sum((r["transport_amount"] or 0)
                            for r in day_reqs if r["req_type"] == "transport")
        transport_note = "／".join(
            (r["transport_route"] or "") for r in day_reqs if r["req_type"] == "transport"
        )

        # 表示用タグ
        tags = []
        for r in day_reqs:
            label = db.REQ_TYPES.get(r["req_type"], r["req_type"])
            if r["req_type"] == "half_leave":
                label = f"半休({r['half_period'] or ''})"
            elif r["req_type"] == "delay" and r["delay_minutes"]:
                label = f"遅延{r['delay_minutes']}分"
            tags.append(label)

        total_work += s["worked_minutes"]
        total_ot += s["overtime_minutes"]
        total_break += s["break_minutes"]

        # 平日出勤日数（平日に出勤打刻あり）
        is_sun = day_date_obj.weekday() == 6
        is_sat = day_date_obj.weekday() == 5
        is_jp_holiday = jpholiday.is_holiday(day_date_obj)
        is_weekday = not (is_sun or is_sat) and not is_jp_holiday \
                     and not is_legal_holiday(day_date_obj)
        if is_weekday and s["clock_in"]:
            workday_count += 1

        # 備考（その他／打刻修正／振替系申請のメモ）
        day_note = " / ".join(r["note"] for r in day_reqs if r["note"])

        # 曜日・休日フラグ（行色分け用）
        wd_idx = day_date_obj.weekday()
        day_type = "weekday"
        if is_legal_holiday(day_date_obj) or is_jp_holiday or is_sun:
            day_type = "sun"  # 赤字系（法定休日）
        elif is_sat:
            day_type = "sat"  # 青字系（法定外休日）

        rows.append({
            "date": day_key,
            "day_num": day_date_obj.day,
            "weekday": weekday_jp[wd_idx],
            "day": day_date_obj.strftime("%m/%d") + f" ({weekday_jp[wd_idx]})",
            "day_type": day_type,
            "clock_in": s["clock_in"].strftime("%H:%M") if s["clock_in"] else "",
            "clock_out": s["clock_out"].strftime("%H:%M") if s["clock_out"] else "",
            "break": fmt_hm(s["break_minutes"]) if s["break_minutes"] else "",
            "worked": fmt_hm(s["worked_minutes"]) if s["worked_minutes"] else "",
            "overtime": fmt_hm(s["overtime_minutes"]) if s["overtime_minutes"] else "",
            "is_leave": is_full_leave or (half is not None),
            "tags": tags,
            "transport": day_transport,
            "transport_note": transport_note,
            "note": day_note,
            "is_holiday_work": s["worked_on_holiday"],
            "holiday_minutes": s["holiday_work_minutes"],
            "has_data": bool(s["clock_in"] or day_reqs),
        })

    return {
        "rows": rows,
        "total_work": total_work,
        "total_ot": total_ot,
        "total_break": total_break,
        "leave_count": leave_days,
        "holiday_work_days": holiday_work_days,
        "total_holiday_minutes": total_holiday_minutes,
        "total_transport": sum(r["transport"] for r in rows),
        "workday_count": workday_count,
        "emp_type": user_row["emp_type"],
    }


# --- ログイン ---
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    user = current_user(request)
    if user:
        return RedirectResponse("/admin" if user["role"] == "admin" else "/dashboard", 303)
    return RedirectResponse("/login", 303)


@app.get("/login", response_class=HTMLResponse)
def login_get(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login", response_class=HTMLResponse)
def login_post(request: Request, login_id: str = Form(...), password: str = Form(...)):
    user = db.find_user(login_id, password)
    if not user:
        return templates.TemplateResponse(
            request, "login.html", {"error": "IDまたはパスワードが違います"}
        )
    request.session["user_id"] = user["id"]
    return RedirectResponse("/admin" if user["role"] == "admin" else "/dashboard", 303)


@app.get("/settings", response_class=HTMLResponse)
def settings_get(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", 303)
    flash = request.session.pop("flash", None)
    error = request.session.pop("settings_error", None)
    return templates.TemplateResponse(request, "settings.html", {
        "user": user, "flash": flash, "error": error,
    })


@app.post("/settings/password")
def settings_password(request: Request,
                      current_password: str = Form(...),
                      new_password: str = Form(...),
                      confirm_password: str = Form(...)):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", 303)
    if not db.verify_password(user["id"], current_password):
        request.session["settings_error"] = "現在のパスワードが違います"
    elif new_password != confirm_password:
        request.session["settings_error"] = "新しいパスワードと確認が一致しません"
    elif len(new_password) < 4:
        request.session["settings_error"] = "パスワードは4文字以上にしてください"
    else:
        db.update_password(user["id"], new_password)
        request.session["flash"] = "パスワードを変更しました"
    return RedirectResponse("/settings", 303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", 303)


# --- 従業員：打刻 ---
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", 303)

    today_punches = db.get_today_punches(user["id"])
    s = calc_day_summary(today_punches, user=user, day_date=today_jst())

    return templates.TemplateResponse(request, "dashboard.html", {
        "user": user,
        "today": today_jst().strftime("%Y年%m月%d日"),
        "now": now_jst().strftime("%H:%M"),
        "clock_in": s["clock_in"].strftime("%H:%M") if s["clock_in"] else None,
        "clock_out": s["clock_out"].strftime("%H:%M") if s["clock_out"] else None,
        "break_text": fmt_hm(s["break_minutes"]),
        "break_ongoing": s["break_ongoing"],
    })


@app.post("/punch")
def punch(request: Request, punch_type: str = Form(...)):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", 303)
    if punch_type not in ("in", "out", "break_in", "break_out"):
        raise HTTPException(400, "不正な打刻種別")
    db.add_punch(user["id"], punch_type)
    return RedirectResponse("/dashboard", 303)


# --- 従業員：月次 ---
@app.get("/monthly", response_class=HTMLResponse)
def monthly(request: Request, year: int = None, month: int = None):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", 303)

    today = today_jst()
    year = year or today.year
    month = month or today.month
    data = build_monthly_rows(user["id"], year, month)

    prev_y, prev_m = (year, month - 1) if month > 1 else (year - 1, 12)
    next_y, next_m = (year, month + 1) if month < 12 else (year + 1, 1)

    return templates.TemplateResponse(request, "monthly.html", {
        "user": user, "year": year, "month": month,
        "rows": data["rows"],
        "total_work": fmt_hm(data["total_work"]),
        "total_overtime": fmt_hm(data["total_ot"]),
        "total_break": fmt_hm(data["total_break"]),
        "leave_count": data["leave_count"],
        "holiday_work_days": data["holiday_work_days"],
        "total_holiday_minutes": fmt_hm(data["total_holiday_minutes"]),
        "total_transport": f"{data['total_transport']:,}",
        "workday_count": data["workday_count"],
        "emp_type": data["emp_type"],
        "prev_y": prev_y, "prev_m": prev_m,
        "next_y": next_y, "next_m": next_m,
    })


# --- 従業員：申請（7種類統合） ---
@app.get("/requests", response_class=HTMLResponse)
def requests_page(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", 303)

    my_requests = db.get_user_requests(user["id"])
    used = db.get_approved_leave_days(user["id"])
    remaining = user["leave_days"] - used

    return templates.TemplateResponse(request, "requests.html", {
        "user": user,
        "my_requests": my_requests,
        "type_labels": db.REQ_TYPES,
        "granted": user["leave_days"],
        "used": used,
        "remaining": remaining,
        "today": today_jst().isoformat(),
    })


@app.post("/requests/confirm", response_class=HTMLResponse)
def requests_confirm(request: Request,
                     req_type: str = Form(...),
                     target_date: str = Form(...),
                     note: str = Form(""),
                     half_period: str = Form(""),
                     leave_kind: str = Form(""),
                     delay_minutes: str = Form(""),
                     start_time: str = Form(""),
                     end_time: str = Form(""),
                     fix_clock_in: str = Form(""),
                     fix_clock_out: str = Form(""),
                     fix_break_in: str = Form(""),
                     fix_break_out: str = Form(""),
                     transport_route: str = Form(""),
                     transport_amount: str = Form("")):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", 303)
    if req_type not in db.REQ_TYPES:
        raise HTTPException(400, "不正な申請種別")

    form_data = {
        "req_type": req_type, "target_date": target_date, "note": note,
        "half_period": half_period, "leave_kind": leave_kind,
        "delay_minutes": delay_minutes,
        "start_time": start_time, "end_time": end_time,
        "fix_clock_in": fix_clock_in, "fix_clock_out": fix_clock_out,
        "fix_break_in": fix_break_in, "fix_break_out": fix_break_out,
        "transport_route": transport_route, "transport_amount": transport_amount,
    }
    return templates.TemplateResponse(request, "request_confirm.html", {
        "user": user, "form": form_data,
        "type_label": db.REQ_TYPES[req_type],
    })


@app.post("/requests/create")
def requests_create(request: Request,
                    req_type: str = Form(...),
                    target_date: str = Form(...),
                    note: str = Form(""),
                    half_period: str = Form(""),
                    leave_kind: str = Form(""),
                    delay_minutes: str = Form(""),
                    start_time: str = Form(""),
                    end_time: str = Form(""),
                    fix_clock_in: str = Form(""),
                    fix_clock_out: str = Form(""),
                    fix_break_in: str = Form(""),
                    fix_break_out: str = Form(""),
                    transport_route: str = Form(""),
                    transport_amount: str = Form("")):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", 303)
    if req_type not in db.REQ_TYPES:
        raise HTTPException(400, "不正な申請種別")

    db.create_request(
        user_id=user["id"], req_type=req_type, target_date=target_date, note=note,
        half_period=half_period or None,
        delay_minutes=int(delay_minutes) if delay_minutes else None,
        start_time=start_time or None,
        end_time=end_time or None,
        fix_clock_in=fix_clock_in or None,
        fix_clock_out=fix_clock_out or None,
        fix_break_in=fix_break_in or None,
        fix_break_out=fix_break_out or None,
        transport_route=transport_route or None,
        transport_amount=int(transport_amount) if transport_amount else None,
        leave_kind=leave_kind or None,
    )
    return RedirectResponse("/requests", 303)


@app.post("/requests/cancel")
def requests_cancel(request: Request, req_id: int = Form(...)):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", 303)
    db.delete_request(req_id, user["id"])
    return RedirectResponse("/requests", 303)


# --- CSV出力（従業員本人） ---
@app.get("/monthly/csv")
def monthly_csv(request: Request, year: int = None, month: int = None):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", 303)

    today = today_jst()
    year = year or today.year
    month = month or today.month
    data = build_monthly_rows(user["id"], year, month)

    return _csv_response(
        f"kintai_{user['login_id']}_{year}{month:02d}.csv",
        [["日付", "出勤", "退勤", "休憩", "勤務", "残業", "有給", "交通費", "区間", "備考"]] +
        [[r["day"], r["clock_in"], r["clock_out"], r["break"], r["worked"], r["overtime"],
          "○" if r["is_leave"] else "",
          r["transport"] if r["transport"] else "",
          r["transport_note"], r["note"]] for r in data["rows"] if r["has_data"]] +
        [[], ["出勤日数", f"{data['workday_count']}日"],
         ["勤務合計", fmt_hm(data["total_work"])],
         ["残業合計", fmt_hm(data["total_ot"])],
         ["休憩合計", fmt_hm(data["total_break"])],
         ["有給取得日数", f"{data['leave_count']}日"],
         ["休日出勤", f"{data['holiday_work_days']}日"],
         ["交通費合計", f"¥{data['total_transport']:,}"]]
    )


def _csv_response(filename: str, rows):
    buf = io.StringIO()
    buf.write("\ufeff")  # ExcelでUTF-8を正しく開くためのBOM
    writer = csv.writer(buf)
    for r in rows:
        writer.writerow(r)
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --- 管理者：ダッシュボード ---
@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request):
    user = require_admin(request)
    if not user:
        return RedirectResponse("/login", 303)

    users = db.get_all_employees()
    rows = []
    for u in users:
        punches = db.get_today_punches(u["id"])
        s = calc_day_summary(punches, user=u, day_date=today_jst())
        if s["clock_in"] and s["clock_out"]:
            status, sc = "退勤済", "status-done"
        elif s["clock_in"] and s["break_ongoing"]:
            status, sc = "休憩中", "status-break"
        elif s["clock_in"]:
            status, sc = "勤務中", "status-working"
        else:
            status, sc = "未出勤", "status-off"
        rows.append({
            "id": u["id"], "name": u["name"], "login_id": u["login_id"],
            "status": status, "status_class": sc,
            "clock_in": s["clock_in"].strftime("%H:%M") if s["clock_in"] else "-",
            "clock_out": s["clock_out"].strftime("%H:%M") if s["clock_out"] else "-",
            "worked": fmt_hm(s["worked_minutes"]) if s["worked_minutes"] else "-",
            "overtime": fmt_hm(s["overtime_minutes"]) if s["overtime_minutes"] else "-",
        })

    pending_count = len(db.get_all_pending_requests())

    # 対象月セレクター：今月・先月・先々月
    td = today_jst()
    def _shift(y, m, delta):
        total = y * 12 + (m - 1) + delta
        return total // 12, total % 12 + 1
    cy, cm = td.year, td.month
    py, pm = _shift(cy, cm, -1)
    ppy, ppm = _shift(cy, cm, -2)
    month_options = [
        {"y": py,  "m": pm,  "label": f"先月（{py}年{pm:02d}月）",     "selected": True},
        {"y": cy,  "m": cm,  "label": f"今月（{cy}年{cm:02d}月）",     "selected": False},
        {"y": ppy, "m": ppm, "label": f"先々月（{ppy}年{ppm:02d}月）", "selected": False},
    ]

    return templates.TemplateResponse(request, "admin.html", {
        "user": user,
        "today": td.strftime("%Y年%m月%d日"),
        "rows": rows,
        "pending_count": pending_count,
        "month_options": month_options,
    })


@app.get("/admin/user/{user_id}", response_class=HTMLResponse)
def admin_user_monthly(request: Request, user_id: int, year: int = None, month: int = None):
    user = require_admin(request)
    if not user:
        return RedirectResponse("/login", 303)

    target = db.get_user(user_id)
    if not target:
        raise HTTPException(404)

    today = today_jst()
    year = year or today.year
    month = month or today.month
    data = build_monthly_rows(user_id, year, month)

    prev_y, prev_m = (year, month - 1) if month > 1 else (year - 1, 12)
    next_y, next_m = (year, month + 1) if month < 12 else (year + 1, 1)

    return templates.TemplateResponse(request, "admin_user.html", {
        "user": user, "target": target,
        "year": year, "month": month,
        "rows": data["rows"],
        "total_work": fmt_hm(data["total_work"]),
        "total_overtime": fmt_hm(data["total_ot"]),
        "total_break": fmt_hm(data["total_break"]),
        "leave_count": data["leave_count"],
        "holiday_work_days": data["holiday_work_days"],
        "total_holiday_minutes": fmt_hm(data["total_holiday_minutes"]),
        "total_transport": f"{data['total_transport']:,}",
        "workday_count": data["workday_count"],
        "emp_type": data["emp_type"],
        "prev_y": prev_y, "prev_m": prev_m,
        "next_y": next_y, "next_m": next_m,
    })


def _build_print_sheet(target, year, month):
    """印刷用：1人分の集計データを組み立てる"""
    data = build_monthly_rows(target["id"], year, month)
    dep_name = "（未所属）"
    if target["department_id"]:
        d = db.get_department(target["department_id"])
        if d:
            dep_name = d["name"]
    emp_label = db.EMP_TYPES.get(target["emp_type"], "-")
    scheduled = ""
    if target["scheduled_start"] and target["scheduled_end"]:
        scheduled = f"{target['scheduled_start']}〜{target['scheduled_end']}"
    return {
        "target": target,
        "rows": data["rows"],
        "total_work": fmt_hm(data["total_work"]),
        "total_overtime": fmt_hm(data["total_ot"]),
        "total_break": fmt_hm(data["total_break"]),
        "leave_count": data["leave_count"],
        "holiday_work_days": data["holiday_work_days"],
        "total_holiday_minutes": fmt_hm(data["total_holiday_minutes"]),
        "total_transport": f"{data['total_transport']:,}",
        "workday_count": data["workday_count"],
        "emp_type": data["emp_type"],
        "emp_label": emp_label,
        "scheduled": scheduled,
        "dep_name": dep_name,
    }


# --- 管理者：印刷用タイムカード（A4縦・PDF保存用・1人分） ---
@app.get("/admin/user/{user_id}/print", response_class=HTMLResponse)
def admin_user_print(request: Request, user_id: int,
                     year: int = None, month: int = None):
    user = require_admin(request)
    if not user:
        return RedirectResponse("/login", 303)
    target = db.get_user(user_id)
    if not target:
        raise HTTPException(404)

    today = today_jst()
    year = year or today.year
    month = month or today.month
    sheet = _build_print_sheet(target, year, month)

    return templates.TemplateResponse(request, "admin_user_print.html", {
        "user": user,
        "year": year, "month": month,
        "sheets": [sheet],
        "printed_at": now_jst().strftime("%Y/%m/%d %H:%M"),
        "back_url": f"/admin/user/{user_id}?year={year}&month={month}",
    })


# --- 管理者：印刷用タイムカード（複数人・全員） ---
@app.get("/admin/print", response_class=HTMLResponse)
def admin_print_multi(request: Request,
                      year: int = None, month: int = None,
                      user_ids: str = None):
    """user_ids=カンマ区切りのIDリスト。未指定なら全員"""
    user = require_admin(request)
    if not user:
        return RedirectResponse("/login", 303)

    today = today_jst()
    year = year or today.year
    month = month or today.month

    all_emps = db.get_all_employees()
    if user_ids:
        wanted = set(int(x) for x in user_ids.split(",") if x.strip().isdigit())
        target_emps = [u for u in all_emps if u["id"] in wanted]
    else:
        target_emps = all_emps

    sheets = [_build_print_sheet(u, year, month) for u in target_emps]

    return templates.TemplateResponse(request, "admin_user_print.html", {
        "user": user,
        "year": year, "month": month,
        "sheets": sheets,
        "printed_at": now_jst().strftime("%Y/%m/%d %H:%M"),
        "back_url": "/admin",
    })


# --- 管理者：打刻修正 ---
@app.get("/admin/edit/{user_id}/{day}", response_class=HTMLResponse)
def admin_edit_day(request: Request, user_id: int, day: str):
    user = require_admin(request)
    if not user:
        return RedirectResponse("/login", 303)
    target = db.get_user(user_id)
    if not target:
        raise HTTPException(404)

    punches = db.get_day_punches(user_id, day)
    punch_rows = []
    for p in punches:
        dt = datetime.fromisoformat(p["punched_at"])
        punch_rows.append({
            "type": p["punch_type"],
            "type_label": {"in": "出勤", "out": "退勤",
                           "break_in": "休憩開始", "break_out": "休憩終了"}[p["punch_type"]],
            "time": dt.strftime("%H:%M"),
        })

    flash = request.session.pop("flash", None)
    return templates.TemplateResponse(request, "admin_edit_day.html", {
        "user": user, "target": target, "day": day,
        "punches": punch_rows, "flash": flash,
    })


@app.post("/admin/edit/{user_id}/{day}")
def admin_edit_day_save(request: Request, user_id: int, day: str,
                        clock_in: str = Form(""),
                        clock_out: str = Form(""),
                        break_in: str = Form(""),
                        break_out: str = Form("")):
    user = require_admin(request)
    if not user:
        return RedirectResponse("/login", 303)

    # 既存の打刻を全削除してから再投入
    db.delete_day_punches(user_id, day)

    def save(kind: str, hhmm: str):
        if hhmm:
            iso = f"{day}T{hhmm}:00"
            db.add_punch_at(user_id, kind, iso)

    save("in", clock_in)
    save("break_in", break_in)
    save("break_out", break_out)
    save("out", clock_out)

    request.session["flash"] = f"{day} の打刻を更新しました"
    return RedirectResponse(f"/admin/user/{user_id}", 303)


# --- 承認（部署承認者 or admin） ---
@app.get("/approvals", response_class=HTMLResponse)
def approvals_page(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", 303)

    is_admin = user["role"] == "admin"
    is_approver = bool(user["is_approver"])
    if not (is_admin or is_approver):
        return RedirectResponse("/dashboard", 303)

    if is_admin:
        pending = db.get_all_pending_requests()
    else:
        pending = db.get_pending_requests_for_department(user["department_id"])

    return templates.TemplateResponse(request, "approvals.html", {
        "user": user, "pending": pending,
        "type_labels": db.REQ_TYPES,
        "is_admin": is_admin,
    })


@app.post("/approvals/review")
def approvals_review(request: Request,
                     req_id: int = Form(...),
                     action: str = Form(...)):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", 303)

    req = db.get_request(req_id)
    if not req:
        raise HTTPException(404)

    is_admin = user["role"] == "admin"
    is_approver = bool(user["is_approver"])
    if not (is_admin or is_approver):
        raise HTTPException(403)

    # 承認者は自部署のみ
    if is_approver and not is_admin:
        requester = db.get_user(req["user_id"])
        if requester["department_id"] != user["department_id"]:
            raise HTTPException(403, "他部署の申請は承認できません")

    if action not in ("approve", "reject"):
        raise HTTPException(400)
    db.review_request(req_id, user["id"], "approved" if action == "approve" else "rejected")
    return RedirectResponse("/approvals", 303)


# --- 管理者：交通費一覧 ---
@app.get("/admin/transport/csv")
def admin_transport_csv(request: Request, year: int = None, month: int = None):
    user = require_admin(request)
    if not user:
        return RedirectResponse("/login", 303)
    today = today_jst()
    year = year or today.year
    month = month or today.month
    rows = db.get_transport_expenses_in_month(year, month)
    total = sum((r["transport_amount"] or 0) for r in rows)

    out = [["日付", "氏名", "部署", "区間", "金額", "メモ"]]
    for r in rows:
        out.append([r["target_date"], r["user_name"],
                    r["department_name"] or "", r["transport_route"] or "",
                    r["transport_amount"] or 0, r["note"] or ""])
    out.append([])
    out.append(["合計", "", "", "", total, ""])
    return _csv_response(f"transport_{year}{month:02d}.csv", out)


@app.get("/admin/transport", response_class=HTMLResponse)
def admin_transport(request: Request, year: int = None, month: int = None):
    user = require_admin(request)
    if not user:
        return RedirectResponse("/login", 303)

    today = today_jst()
    year = year or today.year
    month = month or today.month
    rows = db.get_transport_expenses_in_month(year, month)
    total = sum((r["transport_amount"] or 0) for r in rows)
    formatted_total = f"{total:,}"

    prev_y, prev_m = (year, month - 1) if month > 1 else (year - 1, 12)
    next_y, next_m = (year, month + 1) if month < 12 else (year + 1, 1)

    return templates.TemplateResponse(request, "admin_transport.html", {
        "user": user, "rows": rows, "total": total,
        "formatted_total": formatted_total,
        "year": year, "month": month,
        "prev_y": prev_y, "prev_m": prev_m,
        "next_y": next_y, "next_m": next_m,
    })


# --- 管理者：有給日数の設定 ---
@app.get("/admin/users", response_class=HTMLResponse)
def admin_users(request: Request):
    user = require_admin(request)
    if not user:
        return RedirectResponse("/login", 303)

    users = db.get_all_employees()
    deps = db.get_departments()
    dep_name = {d["id"]: d["name"] for d in deps}
    rows = []
    for u in users:
        used = db.count_approved_leaves(u["id"])
        # hire_date/email は後方互換（古いDBには存在しない可能性）
        try:
            hire_date = u["hire_date"] or ""
            email = u["email"] or ""
        except (IndexError, KeyError):
            hire_date = ""
            email = ""
        rows.append({
            "id": u["id"], "name": u["name"], "login_id": u["login_id"],
            "granted": u["leave_days"], "used": used,
            "remaining": u["leave_days"] - used,
            "department_id": u["department_id"],
            "department_name": dep_name.get(u["department_id"], "（未所属）"),
            "is_approver": bool(u["is_approver"]),
            "emp_type": u["emp_type"],
            "emp_type_label": db.EMP_TYPES.get(u["emp_type"], "-"),
            "scheduled_start": u["scheduled_start"] or "",
            "scheduled_end": u["scheduled_end"] or "",
            "auto_break_minutes": u["auto_break_minutes"],
            "hire_date": hire_date,
            "email": email,
        })
    admin_rows = [r for r in rows if db.get_user(r["id"])["role"] == "admin"]
    employee_rows = [r for r in rows if db.get_user(r["id"])["role"] != "admin"]
    flash = request.session.pop("flash", None)
    return templates.TemplateResponse(request, "admin_users.html", {
        "user": user, "rows": rows,
        "admin_rows": admin_rows, "employee_rows": employee_rows,
        "flash": flash,
        "departments": deps, "emp_types": db.EMP_TYPES,
    })


@app.post("/admin/users/create")
def admin_users_create(request: Request,
                       login_id: str = Form(...),
                       password: str = Form(...),
                       name: str = Form(...),
                       role: str = Form("employee"),
                       leave_days: float = Form(10),
                       hire_date: str = Form(""),
                       email: str = Form("")):
    user = require_admin(request)
    if not user:
        return RedirectResponse("/login", 303)
    ok, err = db.create_user(login_id.strip(), password, name.strip(), role, leave_days,
                             hire_date=hire_date.strip() or None,
                             email=email.strip() or None)
    request.session["flash"] = err or f"{name} を追加しました"
    return RedirectResponse("/admin/users", 303)


@app.post("/admin/users/contact")
def admin_users_contact(request: Request,
                        user_id: int = Form(...),
                        hire_date: str = Form(""),
                        email: str = Form("")):
    u = require_admin(request)
    if not u:
        return RedirectResponse("/login", 303)
    db.update_user_contact(user_id,
                           hire_date=hire_date.strip() or None,
                           email=email.strip() or None)
    request.session["flash"] = "入社日・メールアドレスを更新しました"
    return RedirectResponse("/admin/users", 303)


@app.post("/admin/users/delete")
def admin_users_delete(request: Request, user_id: int = Form(...)):
    user = require_admin(request)
    if not user:
        return RedirectResponse("/login", 303)
    db.delete_user(user_id)
    request.session["flash"] = "削除しました"
    return RedirectResponse("/admin/users", 303)


# --- 管理者：部署管理 ---
@app.get("/admin/departments", response_class=HTMLResponse)
def admin_departments(request: Request):
    user = require_admin(request)
    if not user:
        return RedirectResponse("/login", 303)

    deps = db.get_departments()
    rows = []
    for d in deps:
        approver = db.get_approver_of(d["id"])
        rows.append({
            "id": d["id"], "name": d["name"],
            "approver_name": approver["name"] if approver else "（未設定）",
        })
    flash = request.session.pop("flash", None)
    return templates.TemplateResponse(request, "admin_departments.html", {
        "user": user, "rows": rows, "flash": flash,
    })


@app.post("/admin/departments/create")
def admin_departments_create(request: Request, name: str = Form(...)):
    user = require_admin(request)
    if not user:
        return RedirectResponse("/login", 303)
    ok, err = db.create_department(name.strip())
    request.session["flash"] = err or f"{name} を追加しました"
    return RedirectResponse("/admin/departments", 303)


@app.post("/admin/departments/rename")
def admin_departments_rename(request: Request,
                              dep_id: int = Form(...),
                              name: str = Form(...)):
    user = require_admin(request)
    if not user:
        return RedirectResponse("/login", 303)
    ok, err = db.rename_department(dep_id, name.strip())
    request.session["flash"] = err or "部署名を変更しました"
    return RedirectResponse("/admin/departments", 303)


@app.post("/admin/departments/delete")
def admin_departments_delete(request: Request, dep_id: int = Form(...)):
    user = require_admin(request)
    if not user:
        return RedirectResponse("/login", 303)
    db.delete_department(dep_id)
    request.session["flash"] = "部署を削除しました"
    return RedirectResponse("/admin/departments", 303)


@app.post("/admin/users/profile")
def admin_users_profile(request: Request,
                        user_id: int = Form(...),
                        emp_type: str = Form(...),
                        scheduled_start: str = Form(""),
                        scheduled_end: str = Form(""),
                        auto_break_minutes: int = Form(0)):
    u = require_admin(request)
    if not u:
        return RedirectResponse("/login", 303)
    if emp_type not in db.EMP_TYPES:
        raise HTTPException(400)
    db.update_employee_profile(user_id, emp_type,
                                scheduled_start, scheduled_end,
                                auto_break_minutes)
    request.session["flash"] = "勤務区分・定時を更新しました"
    return RedirectResponse("/admin/users", 303)


# --- 特別休日（お正月・お盆）管理 ---
@app.get("/admin/holidays", response_class=HTMLResponse)
def admin_holidays(request: Request):
    u = require_admin(request)
    if not u:
        return RedirectResponse("/login", 303)
    rows = db.get_special_holidays()
    flash = request.session.pop("flash", None)
    return templates.TemplateResponse(request, "admin_holidays.html", {
        "user": u, "rows": rows, "flash": flash,
    })


@app.post("/admin/holidays/create")
def admin_holidays_create(request: Request,
                          name: str = Form(...),
                          start_date: str = Form(...),
                          end_date: str = Form(...)):
    u = require_admin(request)
    if not u:
        return RedirectResponse("/login", 303)
    db.create_special_holiday(name.strip(), start_date.strip(), end_date.strip())
    request.session["flash"] = f"{name} を追加しました"
    return RedirectResponse("/admin/holidays", 303)


@app.post("/admin/holidays/delete")
def admin_holidays_delete(request: Request, h_id: int = Form(...)):
    u = require_admin(request)
    if not u:
        return RedirectResponse("/login", 303)
    db.delete_special_holiday(h_id)
    request.session["flash"] = "削除しました"
    return RedirectResponse("/admin/holidays", 303)


@app.post("/admin/users/assign")
def admin_users_assign(request: Request,
                       user_id: int = Form(...),
                       department_id: str = Form(""),
                       is_approver: str = Form("")):
    user = require_admin(request)
    if not user:
        return RedirectResponse("/login", 303)
    dep = int(department_id) if department_id else None
    db.update_user_department(user_id, dep, is_approver == "on")
    request.session["flash"] = "部署・承認者を更新しました"
    return RedirectResponse("/admin/users", 303)


@app.post("/admin/users/update")
def admin_users_update(request: Request,
                       user_id: int = Form(...),
                       leave_days: float = Form(...)):
    user = require_admin(request)
    if not user:
        return RedirectResponse("/login", 303)
    db.update_leave_days(user_id, leave_days)
    return RedirectResponse("/admin/users", 303)


# --- 管理者：全員CSV ---
@app.get("/admin/csv")
def admin_csv(request: Request, year: int = None, month: int = None, user_ids: str = None):
    """user_ids=カンマ区切りのIDリスト。未指定なら全員"""
    user = require_admin(request)
    if not user:
        return RedirectResponse("/login", 303)

    today = today_jst()
    year = year or today.year
    month = month or today.month

    all_emps = db.get_all_employees()
    if user_ids:
        wanted = set(int(x) for x in user_ids.split(",") if x.strip().isdigit())
        target_emps = [u for u in all_emps if u["id"] in wanted]
    else:
        target_emps = all_emps

    rows = [["氏名", "ログインID", "日付", "出勤", "退勤", "休憩", "勤務", "残業", "有給", "交通費", "区間", "備考"]]
    for u in target_emps:
        data = build_monthly_rows(u["id"], year, month)
        for r in data["rows"]:
            if not r["has_data"]:
                continue
            rows.append([u["name"], u["login_id"], r["day"],
                         r["clock_in"], r["clock_out"], r["break"],
                         r["worked"], r["overtime"],
                         "○" if r["is_leave"] else "",
                         r["transport"] if r["transport"] else "",
                         r["transport_note"], r["note"]])
        rows.append([u["name"], "合計",
                     f"出勤{data['workday_count']}日",
                     "", "", fmt_hm(data["total_break"]),
                     fmt_hm(data["total_work"]),
                     fmt_hm(data["total_ot"]),
                     f"{data['leave_count']}日",
                     f"¥{data['total_transport']:,}",
                     f"休日出勤{data['holiday_work_days']}日", ""])
        rows.append([])

    return _csv_response(f"kintai_all_{year}{month:02d}.csv", rows)
