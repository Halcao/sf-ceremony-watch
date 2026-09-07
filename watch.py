#!/usr/bin/env python3
"""
SF County Clerk 婚礼仪式 (Marriage Ceremony, roomID=195) 可用时段监控脚本。
设计为在 GitHub Actions 上定时跑，每次都是全新进程，靠仓库里的 state.json 去重通知。

全自动：每轮轮询都会自己走一遍
    Portal -> EventReservationPortalCommon -> EventReservationDetails(选Ceremony)
    -> ServiceInfo/Index -> ServiceInfoDetails(Next) -> SchedulerReservation
拿到全新的 __RequestVerificationToken + ASP.NET_SessionId，不需要手动复制 cookie。

用法：
    BARK_KEY=xxx python3 watch.py
"""

import json
import os
import re
import sys
import time
from datetime import datetime
from urllib.parse import quote

import requests

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

CONFIG = {
    "ROOM_ID": "195",
    "SERVICE_ID": "94",  # 94 = Schedule a Marriage Ceremony Appointment
    "YEAR": 2026,
    "MONTHS_TO_CHECK": [9, 10, 11],  # 顺带看看周边月份，方便判断预约窗口开放情况
    "TARGET_YEAR_MONTH_DAY": [(2026, 10, 2), (2026, 10, 3)],  # 10月2/3号
    "RELAXED_MONTH": 10,  # 测试用：这个月只要出现任意可用日就推送
    "BARK_BASE": f"https://api.day.app/{os.environ.get('BARK_KEY', '')}",
}

BASE = "https://countyclerk.sfgov.org"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)

TOKEN_RE = re.compile(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"')


def get_token(html):
    m = TOKEN_RE.search(html)
    if not m:
        raise RuntimeError("没找到 __RequestVerificationToken，页面结构可能变了")
    return m.group(1)


def new_session():
    s = requests.Session()
    s.headers.update({"user-agent": UA})
    s.trust_env = False
    return s


def establish_scheduler_session(s, cfg):
    """走完整的服务选择流程，拿到有效的 CSRF + ASP.NET_SessionId。"""
    r1 = s.get(
        f"{BASE}/SchedulerOnline/en/ServiceInfo/EventReservationPortalCommon",
        headers={"referer": f"{BASE}/Portal/"},
        timeout=20,
    )
    r1.raise_for_status()
    token1 = get_token(r1.text)

    r2 = s.post(
        f"{BASE}/SchedulerOnline/en/ServiceInfo/EventReservationDetails",
        data={
            "__RequestVerificationToken": token1,
            "reservationType": cfg["SERVICE_ID"],
        },
        headers={"referer": r1.url},
        timeout=20,
    )
    r2.raise_for_status()
    token2 = get_token(r2.text)

    r3 = s.post(
        f"{BASE}/SchedulerOnline/en/ServiceInfo/ServiceInfoDetails",
        data={
            "__RequestVerificationToken": token2,
            "ServiceID": cfg["SERVICE_ID"],
            "PreInit": "False",
            "MaxTabIndex": "0",
            "BtnNext": "Next",
        },
        headers={"referer": r2.url},
        timeout=20,
    )
    r3.raise_for_status()

    if "SchedulerReservation" not in r3.url:
        raise RuntimeError(f"登录流程失败，最终停在了: {r3.url}\n"
                            f"响应片段: {r3.text[:500]}")
    return r3.url  # 作为后续请求的 referer


def get_available_days(s, referer, cfg, month):
    payload = {
        "confNum": "",
        "roomID": cfg["ROOM_ID"],
        "year": cfg["YEAR"],
        "month": month,
        "slotNum": "",
    }
    r = s.post(
        f"{BASE}/SchedulerOnline/en/Home/GetAvailableDays",
        json=payload,
        headers={"referer": referer, "x-requested-with": "XMLHttpRequest"},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()  # 例: ["11/17/2026","11/18/2026",...]


def get_timetable(s, referer, cfg, date_str):
    payload = {
        "selectedDate": date_str,
        "roomID": cfg["ROOM_ID"],
        "confNum": "",
        "slotNum": "",
    }
    r = s.post(
        f"{BASE}/SchedulerOnline/en/Home/GetTimeTable",
        json=payload,
        headers={"referer": referer, "x-requested-with": "XMLHttpRequest"},
        timeout=20,
    )
    r.raise_for_status()
    return r.text


SLOT_RE = re.compile(
    r'List box\.\s*([\d:]+ [AP]M)\..*?eventContainerDiv">(.*?)</div>',
    re.S,
)


def parse_slots(html):
    slots = []
    for time_label, content in SLOT_RE.findall(html):
        slots.append((time_label.strip(), content.strip() == ""))
    return slots


def push_bark(cfg, title, body):
    if not os.environ.get("BARK_KEY"):
        print("    [bark] 未设置 BARK_KEY，跳过推送")
        return
    url = f"{cfg['BARK_BASE']}/{quote(title, safe='')}/{quote(body, safe='')}"
    try:
        r = requests.get(url, timeout=15)
        print(f"    [bark] status={r.status_code} body={r.text[:200]}")
    except Exception as e:
        print(f"    [bark] 推送失败: {e}")


def run_once(cfg):
    s = new_session()
    referer = establish_scheduler_session(s, cfg)

    targets = {(y, m, d) for (y, m, d) in cfg["TARGET_YEAR_MONTH_DAY"]}
    found = []
    month_raw_days = {}

    for month in cfg["MONTHS_TO_CHECK"]:
        raw_days = get_available_days(s, referer, cfg, month)
        month_raw_days[month] = raw_days
        parsed = []
        for d in raw_days:
            try:
                parsed.append(datetime.strptime(d, "%m/%d/%Y"))
            except ValueError:
                pass
        print(f"  {cfg['YEAR']}-{month:02d} 可用日: {[d.strftime('%m/%d') for d in parsed] or '无'}")

        for d in parsed:
            key = (d.year, d.month, d.day)
            if key in targets:
                orig_str = next(r for r in raw_days
                                 if datetime.strptime(r, "%m/%d/%Y") == d)
                html = get_timetable(s, referer, cfg, orig_str)
                slots = parse_slots(html)
                free = [t for t, is_free in slots if is_free]
                found.append((orig_str, slots, free))

    return found, month_raw_days


def run_once_resilient(cfg, max_attempts=4, gap_sec=15):
    last_err = None
    for i in range(max_attempts):
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            result = run_once(cfg)
            if i > 0:
                print(f"[{ts}] 第{i+1}次尝试成功。")
            return result
        except Exception as e:
            last_err = e
            print(f"[{ts}] 第{i+1}/{max_attempts}次尝试失败: {e}")
            if i < max_attempts - 1:
                time.sleep(gap_sec)
    raise last_err


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                data = json.load(f)
            return (
                set(data.get("notified_relaxed_dates", [])),
                set(data.get("notified_target_dates", [])),
            )
        except Exception:
            pass
    return set(), set()


def save_state(notified_relaxed_dates, notified_target_dates):
    with open(STATE_FILE, "w") as f:
        json.dump(
            {
                "notified_relaxed_dates": sorted(notified_relaxed_dates),
                "notified_target_dates": sorted(notified_target_dates),
            },
            f,
            indent=2,
        )
        f.write("\n")


def do_check(cfg, notified_relaxed_dates, notified_target_dates):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] 开始一轮检查...")
    try:
        found, month_raw_days = run_once_resilient(cfg)
    except Exception as e:
        print(f"[{ts}] 本轮失败: {e}")
        return notified_relaxed_dates, notified_target_dates

    if found:
        for date_str, slots, free in found:
            print(f"\a[{ts}] !!! {date_str} 命中目标日期 !!!")
            print(f"    全部时段: {slots}")
            print(f"    空闲时段: {free if free else '（该日在可用日列表里，但时段表里没查到空位，可能刚被订走）'}")
            if free and date_str not in notified_target_dates:
                push_bark(
                    cfg,
                    f"婚礼仪式可约：{date_str}",
                    f"空闲时段: {', '.join(free)}，赶紧去订！",
                )
                notified_target_dates.add(date_str)
    else:
        print(f"[{ts}] 目标日期(10/2、10/3)暂未开放/无可用位。")

    # 放松版（测试用）：目标月只要出现任意可用日就推一次，同一天期只推一次
    relaxed_month = cfg.get("RELAXED_MONTH")
    if relaxed_month:
        raw_days = month_raw_days.get(relaxed_month, [])
        new_dates = [d for d in raw_days if d not in notified_relaxed_dates]
        if new_dates:
            push_bark(
                cfg,
                f"{cfg['YEAR']}年{relaxed_month}月出号测试推送",
                f"检测到可预约日: {', '.join(new_dates)}",
            )
            notified_relaxed_dates.update(new_dates)

    return notified_relaxed_dates, notified_target_dates


def main():
    cfg = CONFIG
    notified_relaxed_dates, notified_target_dates = load_state()
    notified_relaxed_dates, notified_target_dates = do_check(
        cfg, notified_relaxed_dates, notified_target_dates
    )
    save_state(notified_relaxed_dates, notified_target_dates)


if __name__ == "__main__":
    main()
