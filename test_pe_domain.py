"""按真实业务叙事组织的领域规则测试。

每个测试类对应场景中的一个关切：
方案版本与资源校验、最小化交叉确认、依据链、教会勤练常赛、
异常先复核、连续意外后的还原、未成年人隐私分级。
"""

import unittest
from datetime import datetime

from pe_domain import (
    ADAPT_EXEMPT,
    ADJUST_HEAT,
    ADJUST_RAIN,
    Conflict,
    EVENT_CONTENT,
    EVENT_END,
    EVENT_SAMPLE,
    EVENT_START,
    EVENT_TEACHER_SIGN,
    EVENT_VENUE,
    Ledger,
    NotFound,
    ROLE_ADMIN,
    ROLE_PARENT,
    ROLE_RESEARCHER,
    ROLE_TEACHER,
    AccessDenied,
    SLOTS,
    STATUS_DONE,
    STATUS_MAKEUP,
    STATUS_PARTIAL,
    STATUS_REPLACED,
    ValidationError,
)

WEEK_START = "2026-09-07"  # 周一
SCHOOL = "S1"
CLASS = "C1"
CLASS2 = "C2"
TEACHER = "T1"
TEACHER2 = "T2"
V_FIELD = "V-field"    # 操场（室外）
V_GYM = "V-gym"        # 体育馆（室内）
V_RAIN = "V-rain"      # 风雨操场（室内）
V_TT = "V-tt"          # 乒乓房（室内）

A_admin = {"role": ROLE_ADMIN, "name": "李校长"}
A_researcher = {"role": ROLE_RESEARCHER, "name": "周教研"}
A_teacher = {"role": ROLE_TEACHER, "name": "王老师", "teacher_id": TEACHER}
A_teacher2 = {"role": ROLE_TEACHER, "name": "赵老师", "teacher_id": TEACHER2}
A_parent = {"role": ROLE_PARENT, "name": "某家长"}

PE_SKILLS = ["篮球", "快速跑", "技巧", "耐久跑", "足球"]


def build_world(ledger: Ledger, class_count=40, classes=(CLASS,), weeks=2):
    """搭建一所小学：两个室外/室内场地、两名教师、若干 12 人班级。"""
    students = []
    class_payload = []
    for cid in classes:
        class_payload.append({"class_id": cid, "school_id": SCHOOL, "name": f"{cid}班"})
        for i in range(1, class_count + 1):
            students.append({"student_id": f"{cid}-s{i:02d}", "class_id": cid,
                             "student_no": f"{cid[-1]}{i:02d}"})
    ledger.seed_master_data(A_admin, {
        "schools": [{"school_id": SCHOOL, "name": "实验小学"}],
        "venues": [
            {"venue_id": V_FIELD, "school_id": SCHOOL, "name": "操场", "kind": "室外",
             "capacity": 60},
            {"venue_id": V_GYM, "school_id": SCHOOL, "name": "体育馆", "kind": "室内",
             "capacity": 45},
            {"venue_id": V_RAIN, "school_id": SCHOOL, "name": "风雨操场", "kind": "室内",
             "capacity": 45},
            {"venue_id": V_TT, "school_id": SCHOOL, "name": "乒乓房", "kind": "室内",
             "capacity": 40},
        ],
        "teachers": [
            {"teacher_id": TEACHER, "school_id": SCHOOL, "name": "王老师",
             "qualified": True, "first_aid": True,
             "skills": ["篮球", "足球", "快速跑", "耐久跑", "技巧", "室内体能", "安全规则"]},
            {"teacher_id": TEACHER2, "school_id": SCHOOL, "name": "赵老师",
             "qualified": True, "first_aid": False, "skills": ["乒乓球"]},
        ],
        "classes": class_payload,
        "students": students,
    })
    return students


def standard_plan_items(classes=(CLASS,), teacher=TEACHER):
    """每日一节体育课（技能轮换）+ 每日大课间 + 周五班级赛事 + 两次课后服务。"""
    items = []
    for cid in classes:
        for weekday, skill in enumerate(PE_SKILLS, start=1):
            items.append({"type": "体育课", "class_id": cid, "weekday": weekday,
                          "slot": "上午第一节", "venue_id": V_FIELD,
                          "teacher_id": teacher, "skill": skill})
        for weekday in range(1, 6):
            items.append({"type": "大课间", "class_id": cid, "weekday": weekday,
                          "slot": "大课间", "venue_id": V_FIELD, "teacher_id": teacher})
        items.append({"type": "班级赛事", "class_id": cid, "weekday": 5,
                      "slot": "下午第三节", "venue_id": V_FIELD, "teacher_id": teacher,
                      "skill": "篮球", "name": "班级篮球联赛"})
        for weekday in (2, 4):
            items.append({"type": "课后服务", "class_id": cid, "weekday": weekday,
                          "slot": "课后服务", "venue_id": V_TT, "teacher_id": TEACHER2,
                          "skill": "乒乓球"})
    return items


def submit_standard_plan(ledger: Ledger, classes=(CLASS,), weeks=2, teacher=TEACHER):
    return ledger.submit_plan(A_admin, SCHOOL, list(classes), WEEK_START, weeks,
                              standard_plan_items(classes, teacher))


def class_activities(ledger: Ledger, class_id=CLASS, plan_id=None, atype=None):
    out = [a for a in ledger.activities.values() if a["class_id"] == class_id
           and a.get("origin_id") is None]
    if plan_id:
        out = [a for a in out if a["plan_id"] == plan_id]
    if atype:
        out = [a for a in out if a["type"] == atype]
    return out


def day_minutes(activity_date: str, slot: str, delta_minutes: int):
    start_h, start_m = SLOTS[slot][0] // 60, SLOTS[slot][0] % 60
    base = datetime.strptime(activity_date, "%Y-%m-%d").replace(hour=start_h, minute=start_m)
    from datetime import timedelta
    return (base + timedelta(minutes=delta_minutes)).strftime("%Y-%m-%d %H:%M")


def complete_activity(ledger: Ledger, activity: dict, sample_ids=None, actor=None,
                      content_mode=None, offline_teacher=False, submitted_at=None,
                      skills=None, observe_student=None):
    """按 场地→教师→抽样→开始→内容→结束 上报完整事件链。"""
    if actor is None:
        actor = A_teacher2 if activity["teacher_id"] == TEACHER2 else A_teacher
    slot = activity["slot"]
    day = activity["date"]
    sample_ids = sample_ids or ledger.suggest_sample(actor, activity["activity_id"])["sample_student_ids"]

    ledger.report_event(actor, activity["activity_id"], EVENT_VENUE,
                        day_minutes(day, slot, -5), {"usable": True})
    if offline_teacher:
        ledger.report_event(actor, activity["activity_id"], EVENT_TEACHER_SIGN,
                            day_minutes(day, slot, -4),
                            {"submitted_at": submitted_at or day_minutes(day, slot, 240)},
                            offline=True, offline_reason="操场考勤设备断网",
                            client_ref="dev-1-offline")
    else:
        ledger.report_event(actor, activity["activity_id"], EVENT_TEACHER_SIGN,
                            day_minutes(day, slot, -4))
    ledger.report_event(actor, activity["activity_id"], EVENT_SAMPLE,
                        day_minutes(day, slot, -2), {"student_ids": sample_ids})
    ledger.report_event(actor, activity["activity_id"], EVENT_START,
                        day_minutes(day, slot, 0))
    mode = content_mode or {"体育课": "技能教学", "大课间": "体能练习",
                            "课后服务": "技能教学", "班级赛事": "赛事组织"}[activity["type"]]
    use_skills = skills if skills is not None else ([activity["skill"]] if activity["skill"] else [])
    ledger.report_event(actor, activity["activity_id"], EVENT_CONTENT,
                        day_minutes(day, slot, 10),
                        {"mode": mode, "skills": use_skills, "minutes": 40,
                         "phase": "新授" if mode == "技能教学" else None})
    ledger.report_event(actor, activity["activity_id"], EVENT_END,
                        day_minutes(day, slot, 40))
    if observe_student and activity["skill"] and activity["type"] == "体育课":
        ledger.record_observation(actor, observe_student, activity["activity_id"],
                                  activity["skill"], "合格")


def complete_week(ledger: Ledger, week: int, class_id=CLASS, observe_student=None, **kwargs):
    for a in class_activities(ledger, class_id):
        if a["week"] != week:
            continue
        complete_activity(ledger, ledger.activities[a["activity_id"]],
                          observe_student=observe_student, **kwargs)


# ---------------------------------------------------------------------------


class PlanValidationTest(unittest.TestCase):
    def setUp(self):
        self.ledger = Ledger()
        build_world(self.ledger)

    def test_missing_daily_pe_is_rejected(self):
        items = [i for i in standard_plan_items() if not (
            i["type"] == "体育课" and i["class_id"] == CLASS and i["weekday"] == 3)]
        with self.assertRaisesRegex(ValidationError, "星期3"):
            self.ledger.submit_plan(A_admin, SCHOOL, [CLASS], WEEK_START, 2, items)

    def test_venue_double_booking_is_rejected(self):
        # 二班与一班使用完全相同的方案 → 操场与教师均冲突
        self.ledger.seed_master_data(A_admin, {
            "classes": [{"class_id": CLASS2, "school_id": SCHOOL, "name": "C2班"}],
            "students": [{"student_id": f"{CLASS2}-s{i:02d}", "class_id": CLASS2,
                          "student_no": f"2{i:02d}"} for i in range(1, 13)],
        })
        items = standard_plan_items([CLASS, CLASS2])
        with self.assertRaisesRegex(ValidationError, "操场"):
            self.ledger.submit_plan(A_admin, SCHOOL, [CLASS, CLASS2], WEEK_START, 2, items)

    def test_teacher_double_booking_is_rejected(self):
        items = standard_plan_items([CLASS])
        items.append({"type": "体育课", "class_id": CLASS, "weekday": 1,
                      "slot": "上午第一节", "venue_id": V_GYM, "teacher_id": TEACHER,
                      "skill": "室内体能"})  # 与周一第一节同班同时段
        with self.assertRaisesRegex(ValidationError, "时段重叠|王老师"):
            self.ledger.submit_plan(A_admin, SCHOOL, [CLASS], WEEK_START, 2, items)

    def test_unqualified_teacher_and_skill_mismatch_are_rejected(self):
        self.ledger.seed_master_data(A_admin, {
            "teachers": [{"teacher_id": "T3", "school_id": SCHOOL, "name": "临时工",
                          "qualified": False, "skills": []}]})
        items = [dict(i, teacher_id="T3") if i["type"] == "体育课" and i["weekday"] == 1 else i
                 for i in standard_plan_items()]
        with self.assertRaisesRegex(ValidationError, "教师资格"):
            self.ledger.submit_plan(A_admin, SCHOOL, [CLASS], WEEK_START, 2, items)

        self.ledger.seed_master_data(A_admin, {
            "teachers": [{"teacher_id": "T4", "school_id": SCHOOL, "name": "钱老师",
                          "qualified": True, "skills": ["乒乓球"]}]})
        items2 = [dict(i, teacher_id="T4") if i["type"] == "体育课" and i["weekday"] == 2 else i
                  for i in standard_plan_items()]
        with self.assertRaisesRegex(ValidationError, "资质"):
            self.ledger.submit_plan(A_admin, SCHOOL, [CLASS], WEEK_START, 2, items2)

    def test_safety_capacity_is_enforced(self):
        small = Ledger()
        build_world(small, class_count=12)
        small.seed_master_data(A_admin, {"venues": [
            {"venue_id": "V-tiny", "school_id": SCHOOL, "name": "器材间", "kind": "室内",
             "capacity": 5}]})
        items = [dict(i, venue_id="V-tiny") for i in standard_plan_items()]
        with self.assertRaisesRegex(ValidationError, "安全容量"):
            small.submit_plan(A_admin, SCHOOL, [CLASS], WEEK_START, 1, items)

    def test_week_start_must_be_monday(self):
        with self.assertRaisesRegex(ValidationError, "周一"):
            self.ledger.submit_plan(A_admin, SCHOOL, [CLASS], "2026-09-08", 2,
                                    standard_plan_items())

    def test_plan_versions_supersede_old(self):
        first = submit_standard_plan(self.ledger)
        second = submit_standard_plan(self.ledger)
        self.assertEqual(second["version"], 2)
        self.assertEqual(first["plan_id"], second["superseded"][0])
        self.assertEqual(self.ledger.plans[first["plan_id"]]["status"], "已作废")
        # 旧版活动不再参与核对
        active = self.ledger.active_plan(CLASS)
        self.assertEqual(active["plan_id"], second["plan_id"])

    def test_only_admin_submits_plan(self):
        with self.assertRaises(AccessDenied):
            self.ledger.submit_plan(A_teacher, SCHOOL, [CLASS], WEEK_START, 2,
                                    standard_plan_items())


class CrossConfirmationTest(unittest.TestCase):
    def setUp(self):
        self.ledger = Ledger()
        build_world(self.ledger, class_count=12)
        submit_standard_plan(self.ledger)
        self.pe1 = class_activities(self.ledger, CLASS, atype="体育课")[0]
        self.sample = [f"{CLASS}-s{i:02d}" for i in range(1, 7)]

    def test_three_sources_complete_the_class(self):
        complete_activity(self.ledger, self.pe1, self.sample)
        self.assertEqual(self.pe1["status"], STATUS_DONE)
        sources = self.ledger._confirmation_sources(self.pe1)
        self.assertEqual(sum(sources.values()), 3)

    def test_missing_student_sample_is_only_partial(self):
        day, slot = self.pe1["date"], self.pe1["slot"]
        self.ledger.report_event(A_teacher, self.pe1["activity_id"], EVENT_VENUE,
                                 day_minutes(day, slot, -5), {"usable": True})
        self.ledger.report_event(A_teacher, self.pe1["activity_id"], EVENT_TEACHER_SIGN,
                                 day_minutes(day, slot, -4))
        self.ledger.report_event(A_teacher, self.pe1["activity_id"], EVENT_START,
                                 day_minutes(day, slot, 0))
        self.ledger.report_event(A_teacher, self.pe1["activity_id"], EVENT_END,
                                 day_minutes(day, slot, 40))
        self.assertEqual(self.pe1["status"], STATUS_PARTIAL)

    def test_duplicate_teacher_sign_does_not_inflate_minutes(self):
        complete_activity(self.ledger, self.pe1, self.sample)
        with self.assertRaisesRegex(Conflict, "重复签到"):
            self.ledger.report_event(A_teacher, self.pe1["activity_id"], EVENT_TEACHER_SIGN,
                                     day_minutes(self.pe1["date"], self.pe1["slot"], -1))
        minutes, _ = self.ledger._activity_minutes(self.pe1)
        self.assertEqual(minutes, 40)

    def test_duplicate_sample_student_rejected(self):
        day, slot = self.pe1["date"], self.pe1["slot"]
        self.ledger.report_event(A_teacher, self.pe1["activity_id"], EVENT_SAMPLE,
                                 day_minutes(day, slot, -2),
                                 {"student_ids": self.sample})
        with self.assertRaisesRegex(Conflict, "重复抽样"):
            self.ledger.report_event(A_teacher, self.pe1["activity_id"], EVENT_SAMPLE,
                                     day_minutes(day, slot, -1),
                                     {"student_ids": self.sample[:3]})

    def test_client_ref_dedup_for_devices(self):
        day, slot = self.pe1["date"], self.pe1["slot"]
        self.ledger.report_event(A_teacher, self.pe1["activity_id"], EVENT_VENUE,
                                 day_minutes(day, slot, -5), {"usable": True},
                                 client_ref="gate-001")
        with self.assertRaises(Conflict):
            self.ledger.report_event(A_teacher, self.pe1["activity_id"], EVENT_VENUE,
                                     day_minutes(day, slot, -4), {"usable": True},
                                     client_ref="gate-001")

    def test_offline_sign_pending_then_corroborated(self):
        day, slot = self.pe1["date"], self.pe1["slot"]
        result = self.ledger.report_event(
            A_teacher, self.pe1["activity_id"], EVENT_TEACHER_SIGN,
            day_minutes(day, slot, -4),
            {"submitted_at": day_minutes(day, slot, 200)},
            offline=True, offline_reason="设备断网", client_ref="offline-1")
        self.assertFalse(result["corroborated"])  # 单来源离线，不计时
        self.ledger.report_event(A_teacher, self.pe1["activity_id"], EVENT_VENUE,
                                 day_minutes(day, slot, -5), {"usable": True})
        self.ledger.report_event(A_teacher, self.pe1["activity_id"], EVENT_SAMPLE,
                                 day_minutes(day, slot, -2), {"student_ids": self.sample})
        self.ledger.report_event(A_teacher, self.pe1["activity_id"], EVENT_START,
                                 day_minutes(day, slot, 0))
        self.ledger.report_event(A_teacher, self.pe1["activity_id"], EVENT_END,
                                 day_minutes(day, slot, 40))
        # 三类来源齐备后，离线补签自动转为已核
        self.assertEqual(self.pe1["status"], STATUS_DONE)
        minutes, pending = self.ledger._activity_minutes(self.pe1)
        self.assertEqual(minutes, 40)
        self.assertFalse(pending)

    def test_offline_sign_beyond_seven_days_rejected(self):
        day, slot = self.pe1["date"], self.pe1["slot"]
        with self.assertRaisesRegex(ValidationError, "7 天"):
            self.ledger.report_event(
                A_teacher, self.pe1["activity_id"], EVENT_TEACHER_SIGN,
                day_minutes(day, slot, -4),
                {"submitted_at": day_minutes(day, slot, 8 * 24 * 60)},
                offline=True)

    def test_minutes_clamped_to_scheduled_length(self):
        day, slot = self.pe1["date"], self.pe1["slot"]
        self.ledger.report_event(A_teacher, self.pe1["activity_id"], EVENT_VENUE,
                                 day_minutes(day, slot, -5), {"usable": True})
        self.ledger.report_event(A_teacher, self.pe1["activity_id"], EVENT_TEACHER_SIGN,
                                 day_minutes(day, slot, -4))
        self.ledger.report_event(A_teacher, self.pe1["activity_id"], EVENT_SAMPLE,
                                 day_minutes(day, slot, -2), {"student_ids": self.sample})
        self.ledger.report_event(A_teacher, self.pe1["activity_id"], EVENT_START,
                                 day_minutes(day, slot, 0))
        # 设备时钟漂移导致结束晚了 55 分钟，仍裁剪到 40 分钟
        self.ledger.report_event(A_teacher, self.pe1["activity_id"], EVENT_END,
                                 day_minutes(day, slot, 55))
        minutes, _ = self.ledger._activity_minutes(self.pe1)
        self.assertEqual(minutes, 40)

    def test_only_assigned_teacher_signs(self):
        other = dict(A_teacher, teacher_id=TEACHER2)
        with self.assertRaises(AccessDenied):
            self.ledger.report_event(other, self.pe1["activity_id"], EVENT_TEACHER_SIGN,
                                     day_minutes(self.pe1["date"], self.pe1["slot"], -4))


class AdjustmentBasisTest(unittest.TestCase):
    def setUp(self):
        self.ledger = Ledger()
        build_world(self.ledger, class_count=12)
        submit_standard_plan(self.ledger)
        self.pe = class_activities(self.ledger, CLASS, atype="体育课")

    def test_occupy_requires_written_basis(self):
        with self.assertRaisesRegex(ValidationError, "依据"):
            self.ledger.occupy(A_teacher, self.pe[1]["activity_id"], {"reason": "数学要用"})

    def test_occupy_creates_makeup_gap_not_silence(self):
        target = self.pe[1]
        result = self.ledger.occupy(A_admin, target["activity_id"],
                                    {"notice_id": "JWC-2026-015", "source": "教务处",
                                     "reason": "片区数学监测临时占用"})
        self.assertEqual(result["status"], STATUS_MAKEUP)
        scan = self.ledger.scan_anomalies(A_researcher, CLASS, target["date"])
        kinds = {a["kind"] for a in scan["anomalies"]}
        self.assertIn("阴阳课表·实课被占", kinds)

    def test_rain_substitution_needs_weather_record(self):
        target = self.pe[2]
        with self.assertRaises(NotFound):
            self.ledger.weather_substitution(A_teacher, target["activity_id"], ADJUST_RAIN,
                                             "上午第一节", V_RAIN, "室内体能", "WX-404")

    def _setup_rain_substitution(self):
        target = self.pe[2]
        wx = self.ledger.record_weather(A_admin, SCHOOL, target["date"], "雨", source="气象台")
        with self.assertRaisesRegex(ValidationError, "室内"):
            self.ledger.weather_substitution(A_teacher, target["activity_id"], ADJUST_RAIN,
                                             "上午第一节", V_FIELD, "耐久跑", wx["weather_id"])
        # 引用晴天记录同样不成立
        sunny_day = self.pe[3]
        wx_sun = self.ledger.record_weather(A_admin, SCHOOL, sunny_day["date"], "晴")
        with self.assertRaisesRegex(ValidationError, "降雨"):
            self.ledger.weather_substitution(A_teacher, sunny_day["activity_id"], ADJUST_RAIN,
                                             "上午第一节", V_RAIN, "室内体能",
                                             wx_sun["weather_id"])
        result = self.ledger.weather_substitution(
            A_teacher, target["activity_id"], ADJUST_RAIN, "上午第一节", V_RAIN,
            "室内体能", wx["weather_id"], content_name="雨天体能循环练习")
        self.assertEqual(target["status"], STATUS_REPLACED)
        replacement = self.ledger.activities[result["replacement_activity_id"]]
        self.assertEqual(replacement["skill"], "室内体能")
        return target, replacement

    def test_rain_substitution_must_match_rainy_day_and_indoor_venue(self):
        self._setup_rain_substitution()

    def test_rainy_day_with_qualified_substitution_has_no_gap(self):
        target, replacement = self._setup_rain_substitution()
        sample = [f"{CLASS}-s{i:02d}" for i in range(1, 7)]
        complete_activity(self.ledger, replacement, sample)
        scan = self.ledger.scan_anomalies(A_researcher, CLASS, target["date"])
        ids = [aid for a in scan["anomalies"] for aid in a["evidence"].get("activity_ids", [])]
        self.assertNotIn(target["activity_id"], ids)

    def test_heat_threshold_37(self):
        target = self.pe[3]
        wx = self.ledger.record_weather(A_admin, SCHOOL, target["date"], "晴", temp_c=37.5)
        result = self.ledger.weather_substitution(
            A_teacher, target["activity_id"], ADJUST_HEAT, "上午第一节", V_GYM,
            "安全规则", wx["weather_id"], content_name="高温运动安全")
        self.assertEqual(result["basis"]["temp_c"], 37.5)
        self.assertEqual(result["basis"]["condition"], "晴")  # 达温度阈值即使未标"高温"也成立

    def test_reschedule_requires_basis_and_free_slot(self):
        target = self.pe[0]
        with self.assertRaises(ValidationError):
            self.ledger.reschedule(A_teacher, target["activity_id"], "2026-09-13",
                                   "上午第一节", V_GYM, {"reason": "操场维修"})
        result = self.ledger.reschedule(
            A_admin, target["activity_id"], "2026-09-13", "上午第一节", V_GYM,
            {"notice_id": "JWC-2026-021", "source": "总务处", "reason": "操场划线维护"})
        self.assertEqual(target["status"], STATUS_REPLACED)
        # 再次调进王老师同时段必然冲突
        other = self.pe[1]
        with self.assertRaises(Conflict):
            self.ledger.reschedule(A_admin, other["activity_id"], "2026-09-13",
                                   "上午第一节", V_RAIN,
                                   {"notice_id": "JWC-2026-022", "source": "总务处",
                                    "reason": "二次调课"})
        self.assertTrue(result["adjustment_id"])

    def test_injury_adaptation_requires_medical_basis(self):
        sid = f"{CLASS}-s01"
        with self.assertRaisesRegex(ValidationError, "伤病适配依据"):
            self.ledger.student_adaptation(A_teacher, sid, "见习观摩",
                                           {"source": "家长口头"}, "2026-09-07", "2026-09-20")
        record = self.ledger.student_adaptation(
            A_teacher, sid, "见习观摩",
            {"medical_id": "MED-901", "source": "区二院骨科", "advice": "避免跑跳"},
            "2026-09-07", "2026-09-20", note="踝韧带损伤恢复期")
        self.assertEqual(record["adaptation"], "见习观摩")

    def test_exempt_student_cannot_be_sampled(self):
        sid = f"{CLASS}-s01"
        self.ledger.student_adaptation(
            A_teacher, sid, ADAPT_EXEMPT,
            {"medical_id": "MED-902", "source": "区二院骨科"}, "2026-09-07", "2026-09-30")
        target = self.pe[0]
        with self.assertRaisesRegex(ValidationError, "免修"):
            self.ledger.report_event(
                A_teacher, target["activity_id"], EVENT_SAMPLE,
                day_minutes(target["date"], target["slot"], -2),
                {"student_ids": [f"{CLASS}-s{i:02d}" for i in range(1, 7)]})
        # 轮换建议也应自动避开免修学生
        suggested = self.ledger.suggest_sample(A_teacher, target["activity_id"])
        self.assertNotIn(sid, suggested["sample_student_ids"])


class CoveragePillarsTest(unittest.TestCase):
    def setUp(self):
        self.ledger = Ledger()
        build_world(self.ledger, class_count=12)
        submit_standard_plan(self.ledger, weeks=2)
        self.sample = [f"{CLASS}-s{i:02d}" for i in range(1, 7)]
        # 完整完成两周全部活动，每个技能两次新授、一次合格观察
        for week in (1, 2):
            complete_week(self.ledger, week, observe_student=f"{CLASS}-s01")

    def test_three_pillars_pass_with_explained_rules(self):
        cov = self.ledger.class_coverage(A_researcher, CLASS)
        self.assertTrue(cov["教会"]["pass"], cov["教会"])
        self.assertTrue(cov["勤练"]["pass"])
        self.assertTrue(cov["常赛"]["pass"])
        # 可解释：每条结论都挂着规则文字与证据
        self.assertIn("2", cov["教会"]["rule"])
        self.assertEqual(cov["勤练"]["weekly"][0]["pe_minutes"], 200)
        self.assertGreaterEqual(cov["常赛"]["matches_completed"],
                                 cov["常赛"]["matches_required_by_now"])
        taught = {j["skill"]: j["taught_sessions"] for j in cov["教会"]["skills"]}
        self.assertEqual(taught["篮球"], 2)

    def test_no_single_test_score_used_for_student(self):
        # 系统只接受等级观察，不接受分数，载荷中也不含影像字段
        with self.assertRaises(ValidationError):
            self.ledger.record_observation(A_teacher, f"{CLASS}-s02",
                                           class_activities(self.ledger)[0]["activity_id"],
                                           "篮球", "85 分")
        activity = class_activities(self.ledger, atype="体育课")[0]
        rec = self.ledger.record_observation(A_teacher, f"{CLASS}-s02",
                                             activity["activity_id"], "篮球", "待提高")
        self.assertNotIn("score", rec)
        self.assertNotIn("video", rec)

    def test_free_play_three_sessions_flags_anomaly(self):
        ledger = Ledger()
        build_world(ledger, class_count=12)
        ledger.submit_plan(A_admin, SCHOOL, [CLASS], WEEK_START, 2, standard_plan_items())
        count = 0
        for a in class_activities(ledger, atype="体育课"):
            sample = [f"{CLASS}-s{i:02d}" for i in range(1, 7)]
            mode = "自由活动" if count < 3 else "技能教学"
            complete_activity(ledger, a, sample, content_mode=mode,
                              skills=[a["skill"]] if mode != "自由活动" else [])
            count += 1
        scan = ledger.scan_anomalies(A_researcher, CLASS)
        self.assertIn("整节自由活动", {a["kind"] for a in scan["anomalies"]})

    def test_exam_only_drills_flag_anomaly(self):
        ledger = Ledger()
        build_world(ledger, class_count=12)
        ledger.submit_plan(A_admin, SCHOOL, [CLASS], WEEK_START, 2, standard_plan_items())
        for i, a in enumerate(class_activities(ledger, atype="体育课")):
            sample = [f"{CLASS}-s{k:02d}" for k in range(1, 7)]
            complete_activity(ledger, a, sample, content_mode="体能练习",
                              skills=["耐力"] if i < 3 else [a["skill"]])
        scan = ledger.scan_anomalies(A_researcher, CLASS)
        self.assertIn("只练考试项目倾向", {a["kind"] for a in scan["anomalies"]})


class ReviewAndReconstructionTest(unittest.TestCase):
    def test_conflict_occupy_offline_makeup_chain_is_reconstructed(self):
        # 叙事：第2周周二操场被区里临时征用 → 占课登记（有通知）→
        # 教研员看到异常先复核（不处罚）→ 学校周六在体育馆补课 →
        # 补课当天考勤设备断网，教师离线补签，场地+抽样交叉印证 → 缺口销账。
        ledger = Ledger()
        build_world(ledger, class_count=12)
        submit_standard_plan(ledger, weeks=2)
        for a in class_activities(ledger, CLASS):
            if a["week"] == 1:
                complete_activity(ledger, a, [f"{CLASS}-s{i:02d}" for i in range(1, 7)])
        tuesday = [a for a in class_activities(ledger, atype="体育课") if a["week"] == 2][1]

        ledger.occupy(A_admin, tuesday["activity_id"],
                      {"notice_id": "QJ-2026-033", "source": "区教育局体卫艺科",
                       "reason": "操场临时征用做核酸采样点"})

        roster = ledger.anomaly_roster(A_researcher, SCHOOL, "2026-09-15")
        self.assertEqual(roster["note"], "异常仅进入教研复核流程，不触发自动处罚")
        row = next(c for c in roster["classes"] if c["class_id"] == CLASS)
        anomaly = next(a for a in row["items"] if a["kind"] == "阴阳课表·实课被占")
        self.assertEqual(anomaly["severity"], "高")

        # 教师不能裁定，只有教研员可以
        with self.assertRaises(AccessDenied):
            ledger.resolve_review(A_teacher, CLASS, anomaly["key"], "已确认")
        ledger.resolve_review(A_researcher, CLASS, anomaly["key"], "已确认",
                              note="通知属实，要求一周内补课")

        # 学校安排周六上午体育馆补课（归属第2周）
        makeup = ledger.arrange_makeup(
            A_admin, tuesday["activity_id"], "2026-09-19", "上午第一节", V_GYM,
            "快速跑", basis_note="执行区教研复核意见 QJ-2026-033")
        makeup_activity = ledger.activities[makeup["makeup_activity_id"]]
        sample = [f"{CLASS}-s{i:02d}" for i in range(1, 7)]
        complete_activity(ledger, makeup_activity, sample, offline_teacher=True)

        # 还原：缺口列表不再含周二；时间线保留完整依据链
        result = ledger.reconstruct_class(A_researcher, CLASS)
        gap_ids = {g["activity_id"] for g in result["coverage"]["gaps"]}
        self.assertNotIn(tuesday["activity_id"], gap_ids)
        kinds = {a["kind"] for a in result["anomalies"]}
        self.assertNotIn("阴阳课表·实课被占", kinds)
        adjustments = {(a["kind"], a["basis"].get("notice_id")) for a in result["adjustments"]}
        self.assertIn(("临时占课", "QJ-2026-033"), adjustments)
        makeup_rows = [t for t in result["timeline"]
                       if t["activity_id"] == makeup_activity["activity_id"]]
        self.assertEqual(makeup_rows[0]["status"], STATUS_DONE)
        self.assertTrue(makeup_rows[0]["minutes"] == 40)
        offline_events = [e for t in result["timeline"] for e in t["events"] if e["offline"]]
        self.assertTrue(offline_events and offline_events[0]["corroborated"])

    def test_no_record_anomaly_clears_after_class_completed(self):
        ledger = Ledger()
        build_world(ledger, class_count=12)
        submit_standard_plan(ledger, weeks=1)
        target = class_activities(ledger, atype="体育课")[0]
        scan_before = ledger.scan_anomalies(A_researcher, CLASS, target["date"])
        self.assertIn("阴阳课表·有课无记录",
                      {a["kind"] for a in scan_before["anomalies"]})
        complete_activity(ledger, target, [f"{CLASS}-s{i:02d}" for i in range(1, 7)])
        scan_after = ledger.scan_anomalies(A_researcher, CLASS, target["date"])
        ids = [aid for a in scan_after["anomalies"]
               for aid in a["evidence"].get("activity_ids", [])]
        self.assertNotIn(target["activity_id"], ids)


class PrivacyTest(unittest.TestCase):
    def setUp(self):
        self.ledger = Ledger()
        build_world(self.ledger, class_count=12)
        submit_standard_plan(self.ledger, weeks=2)
        for week in (1, 2):
            complete_week(self.ledger, week, observe_student=f"{CLASS}-s01")
        # s02 有一段伤病适配
        self.ledger.student_adaptation(
            A_teacher, f"{CLASS}-s02", "见习观摩",
            {"medical_id": "MED-910", "source": "区二院骨科"}, "2026-09-07", "2026-09-14")
        self.token1 = self.ledger.issue_parent_token(A_admin, f"{CLASS}-s01")["token"]
        self.token2 = self.ledger.issue_parent_token(A_admin, f"{CLASS}-s02")["token"]

    def test_public_sees_school_aggregate_only(self):
        summary = self.ledger.public_school_summary({"role": "公众", "name": "市民"}, SCHOOL)
        allowed = {"school_id", "school_name", "classes_with_plan", "pe_sessions_scheduled",
                   "pe_sessions_completed", "makeup_sessions_completed", "completion_rate",
                   "avg_weekly_pe_minutes_per_class", "pillar_pass_classes",
                   "open_review_items", "note"}
        self.assertEqual(set(summary), allowed)
        self.assertEqual(summary["pe_sessions_scheduled"], 10)  # 5 天 × 2 周
        self.assertEqual(summary["completion_rate"], 1.0)

    def test_parent_only_sees_own_child(self):
        view = self.ledger.parent_view(self.token2)
        self.assertTrue(all(a["student_id"] == f"{CLASS}-s02" for a in view["adaptations"]))
        self.assertTrue(all(o["student_id"] == f"{CLASS}-s02" for o in view["observations"]))
        self.assertTrue(all(r["adaptation"] in ("正常随班", "见习观摩") for r in view["sampled_attendance"]))
        serialized = str(view)
        self.assertNotIn(f"{CLASS}-s01", serialized)  # 无其他学生标识
        self.assertNotIn("score", serialized)
        self.assertIn("无体测分数", view["privacy"])

    def test_invalid_parent_token_denied(self):
        with self.assertRaises(AccessDenied):
            self.ledger.parent_view("not-a-real-token")

    def test_parent_cannot_access_class_details(self):
        with self.assertRaises(AccessDenied):
            self.ledger.reconstruct_class(A_parent, CLASS)
        with self.assertRaises(AccessDenied):
            self.ledger.anomaly_roster(A_parent, SCHOOL)

    def test_only_admin_issues_parent_token(self):
        with self.assertRaises(AccessDenied):
            self.ledger.issue_parent_token(A_researcher, f"{CLASS}-s01")

    def test_sample_events_keep_no_images(self):
        # 抽样事件载荷只保留在样布尔值与适配类型
        any_pe = class_activities(self.ledger, atype="体育课")[0]
        sample_events = [e for e in any_pe["events"] if e["type"] == EVENT_SAMPLE]
        payload = sample_events[0]["payload"]
        self.assertEqual(set(payload), {"student_ids", "present", "adaptation"})
        self.assertNotIn("video", payload)


if __name__ == "__main__":
    unittest.main()
