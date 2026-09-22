"""学校体育实课运行——领域规则内核。

设计原则
--------
* 纯内存账本 + 显式规则函数，不依赖 HTTP，便于单元测试与复核审计；
* 实际授课以"教师 / 场地 / 抽样学生"三类最小化事件交叉确认，重复签到不计时；
* 调课、降雨高温替代、伤病适配均须引用依据（通知 / 天气记录 / 医务建议）；
* 覆盖度按"教会、勤练、常赛"的可解释规则推导，只留技能等级观察，不采集体测分数与影像；
* 异常只生成教研复核条目，系统不产生任何自动处罚；
* 公众只见学校聚合，家长凭令牌只见自己孩子的适配与观察记录。
"""

from __future__ import annotations

import hashlib
import math
import threading
from collections import defaultdict
from datetime import date, datetime, timedelta
from itertools import combinations

# ---------------------------------------------------------------------------
# 领域常量（与 fixtures/domain.json 保持一致）
# ---------------------------------------------------------------------------

ACTIVITY_PE = "体育课"
ACTIVITY_BREAK = "大课间"
ACTIVITY_AFTER = "课后服务"
ACTIVITY_MATCH = "班级赛事"
ACTIVITY_TYPES = (ACTIVITY_PE, ACTIVITY_BREAK, ACTIVITY_AFTER, ACTIVITY_MATCH)

# 标准时段（分钟，按一日作息）
SLOTS = {
    "上午第一节": (8 * 60, 8 * 60 + 40),
    "上午第二节": (8 * 60 + 50, 9 * 60 + 30),
    "大课间": (9 * 60 + 50, 10 * 60 + 20),
    "上午第三节": (10 * 60 + 30, 11 * 60 + 10),
    "上午第四节": (11 * 60 + 20, 12 * 60),
    "下午第一节": (14 * 60, 14 * 60 + 40),
    "下午第二节": (14 * 60 + 50, 15 * 60 + 30),
    "下午第三节": (15 * 60 + 50, 16 * 60 + 30),
    "课后服务": (16 * 60 + 40, 17 * 60 + 20),
}

# 技能目录：类别 -> 技能点
SKILL_CATALOG = {
    "球类": ("篮球", "足球", "排球", "乒乓球"),
    "田径": ("快速跑", "耐久跑", "跳跃", "投掷"),
    "体操": ("技巧", "器械体操"),
    "体能": ("力量", "耐力", "柔韧", "室内体能"),
    "健康知识": ("安全规则", "营养与健康"),
}
SKILL_TO_CATEGORY = {skill: cat for cat, skills in SKILL_CATALOG.items() for skill in skills}
SKILL_LEVELS = ("优秀", "良好", "合格", "待提高")

EVENT_START = "开始"
EVENT_END = "结束"
EVENT_TEACHER_SIGN = "教师签到"
EVENT_VENUE = "场地状态"
EVENT_SAMPLE = "学生抽样"
EVENT_CONTENT = "授课内容"
EVENT_OCCUPY = "临时占课"
EVENT_OFFLINE = "离线补签"
EVENT_TYPES = (
    EVENT_TEACHER_SIGN,
    EVENT_VENUE,
    EVENT_SAMPLE,
    EVENT_START,
    EVENT_CONTENT,
    EVENT_END,
    EVENT_OCCUPY,
    EVENT_OFFLINE,
)

ADJUST_RESCHEDULE = "调课"
ADJUST_RAIN = "降雨替代"
ADJUST_HEAT = "高温替代"
ADJUST_OCCUPY = "临时占课"
ADJUST_MAKEUP = "补课"
ADJUST_TYPES = (ADJUST_RESCHEDULE, ADJUST_RAIN, ADJUST_HEAT, ADJUST_OCCUPY, ADJUST_MAKEUP)

ADAPT_NONE = "正常随班"
ADAPT_LOW = "降低强度"
ADAPT_WATCH = "见习观摩"
ADAPT_EXEMPT = "免修休养"
ADAPT_TYPES = (ADAPT_NONE, ADAPT_LOW, ADAPT_WATCH, ADAPT_EXEMPT)

STATUS_SCHEDULED = "已排课"
STATUS_TEACHING = "授课中"
STATUS_DONE = "已完成"
STATUS_PARTIAL = "部分完成"
STATUS_REPLACED = "已替代"
STATUS_MAKEUP = "待补课"
STATUS_REVIEW = "教研复核"
REVIEW_RESOLVED = ("已确认", "已排除", "已整改")

# 规则阈值（集中声明，供可解释输出引用）
RULE = {
    "每周体育课时": 5,
    "单次体育课时长_分钟": 40,
    "交叉确认来源数": 3,
    "抽样人数下限": 3,
    "抽样人数上限": 8,
    "高温阈值_摄氏度": 37,
    "教会_每技能最少新授复习次数": 2,
    "勤练_周体育时长_分钟": 5 * 40,
    "勤练_大课间出勤比例": 0.8,
    "常赛_每多少周一场": 9,
    "自由活动_异常场次": 3,
    "签到提前容忍_分钟": 30,
    "签到延后容忍_分钟": 60,
}

ROLE_RESEARCHER = "教研员"
ROLE_ADMIN = "学校管理员"
ROLE_TEACHER = "体育教师"
ROLE_PARENT = "学生家长"
ROLES = (ROLE_RESEARCHER, ROLE_ADMIN, ROLE_TEACHER, ROLE_PARENT)


# ---------------------------------------------------------------------------
# 异常类型
# ---------------------------------------------------------------------------


class DomainError(Exception):
    """所有可预期的领域错误，HTTP 层映射为状态码。"""

    status = 400


class ValidationError(DomainError):
    status = 422


class AccessDenied(DomainError):
    status = 403


class NotFound(DomainError):
    status = 404


class Conflict(DomainError):
    status = 409


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def parse_date(value) -> date:
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value), "%Y-%m-%d").date()


def parse_dt(value) -> datetime:
    if isinstance(value, datetime):
        return value
    text = str(value).replace("Z", "")
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ValidationError(f"时间格式无法解析: {value}")


def week_monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


def slot_minutes(slot: str):
    if slot not in SLOTS:
        raise ValidationError(f"未知作息时段: {slot}")
    return SLOTS[slot]


def overlap(a_start, a_end, b_start, b_end) -> bool:
    return a_start < b_end and b_start < a_end


def clamp(value, low, high):
    return max(low, min(high, value))


def evidence_hash(payload: dict) -> str:
    import json

    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def require_keys(obj: dict, keys, where: str):
    missing = [key for key in keys if key not in obj or obj[key] in (None, "")]
    if missing:
        raise ValidationError(f"{where} 缺少字段: {', '.join(missing)}")


# ---------------------------------------------------------------------------
# 账本
# ---------------------------------------------------------------------------


class Ledger:
    """线程安全的内存账本；所有写方法都加锁，方法名即业务动作。"""

    def __init__(self):
        self._lock = threading.RLock()
        self.schools: dict[str, dict] = {}
        self.classes: dict[str, dict] = {}
        self.students: dict[str, dict] = {}
        self.teachers: dict[str, dict] = {}
        self.venues: dict[str, dict] = {}
        self.parent_tokens: dict[str, str] = {}  # token -> student_id

        self.plans: dict[str, dict] = {}
        self.plan_by_class: dict[str, list[str]] = defaultdict(list)
        self.activities: dict[str, dict] = {}
        self.events: list[dict] = []
        self.adjustments: list[dict] = []
        self.observations: list[dict] = []
        self.weather: dict[str, dict] = {}  # (school, date) -> record
        self.reviews: dict[str, dict] = {}  # anomaly key -> review
        self._seq = 0
        self._seen_refs: set[tuple] = set()

    # -- 基础工具 -----------------------------------------------------------

    def _id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._seq:04d}"

    @staticmethod
    def _require_role(actor: dict, *allowed):
        role = actor.get("role")
        if role not in allowed:
            raise AccessDenied(f"需要角色 {'/'.join(allowed)}，当前为 {role or '未认证'}")

    def _get_class(self, class_id: str) -> dict:
        if class_id not in self.classes:
            raise NotFound(f"行政班不存在: {class_id}")
        return self.classes[class_id]

    def _get_student(self, student_id: str) -> dict:
        if student_id not in self.students:
            raise NotFound(f"学生不存在: {student_id}")
        return self.students[student_id]

    def _get_teacher(self, teacher_id: str) -> dict:
        if teacher_id not in self.teachers:
            raise NotFound(f"教师不存在: {teacher_id}")
        return self.teachers[teacher_id]

    def _get_venue(self, venue_id: str) -> dict:
        if venue_id not in self.venues:
            raise NotFound(f"场地不存在: {venue_id}")
        return self.venues[venue_id]

    def _dedup_ref(self, activity_id, kind, ref):
        """外部设备记录去重：同一活动同一类记录只接受一次。"""
        key = (activity_id, kind, ref)
        if key in self._seen_refs:
            raise Conflict("重复记录：该设备签到已提交过")
        self._seen_refs.add(key)

    # -- 主数据 -------------------------------------------------------------

    def seed_master_data(self, actor: dict, data: dict):
        """导入学校、班级、师生、场地主数据（可重复调用，按编号覆盖）。"""
        self._require_role(actor, ROLE_ADMIN, ROLE_RESEARCHER)
        with self._lock:
            for school in data.get("schools", []):
                require_keys(school, ("school_id", "name"), "学校")
                self.schools[school["school_id"]] = dict(school)
            for venue in data.get("venues", []):
                require_keys(venue, ("venue_id", "school_id", "name", "kind", "capacity"), "场地")
                if venue["kind"] not in ("室内", "室外"):
                    raise ValidationError("场地 kind 只能是 室内/室外")
                if int(venue["capacity"]) <= 0:
                    raise ValidationError("场地容量必须为正整数")
                self.venues[venue["venue_id"]] = {
                    "venue_id": venue["venue_id"],
                    "school_id": venue["school_id"],
                    "name": venue["name"],
                    "kind": venue["kind"],
                    "capacity": int(venue["capacity"]),
                }
            for teacher in data.get("teachers", []):
                require_keys(teacher, ("teacher_id", "school_id", "name"), "教师")
                skills = list(teacher.get("skills", []))
                bad = [s for s in skills if s not in SKILL_TO_CATEGORY]
                if bad:
                    raise ValidationError(f"教师资质含未知技能点: {bad}")
                self.teachers[teacher["teacher_id"]] = {
                    "teacher_id": teacher["teacher_id"],
                    "school_id": teacher["school_id"],
                    "name": teacher["name"],
                    "qualified": bool(teacher.get("qualified", True)),
                    "first_aid": bool(teacher.get("first_aid", False)),
                    "skills": skills,
                }
            for klass in data.get("classes", []):
                require_keys(klass, ("class_id", "school_id", "name"), "班级")
                self.classes[klass["class_id"]] = {
                    "class_id": klass["class_id"],
                    "school_id": klass["school_id"],
                    "name": klass["name"],
                    "student_ids": [],
                }
            for student in data.get("students", []):
                require_keys(student, ("student_id", "class_id", "student_no"), "学生")
                klass = self._get_class(student["class_id"])
                record = {
                    "student_id": student["student_id"],
                    "class_id": student["class_id"],
                    "student_no": student["student_no"],
                    "name": student.get("name", ""),
                    "enrolled": True,
                }
                self.students[student["student_id"]] = record
                if student["student_id"] not in klass["student_ids"]:
                    klass["student_ids"].append(student["student_id"])
            return {"schools": len(self.schools), "classes": len(self.classes),
                    "students": len(self.students), "teachers": len(self.teachers),
                    "venues": len(self.venues)}

    def issue_parent_token(self, actor: dict, student_id: str) -> dict:
        """家长令牌由学校管理员签发；令牌本身不含学生身份明文。"""
        self._require_role(actor, ROLE_ADMIN)
        with self._lock:
            student = self._get_student(student_id)
            token = hashlib.sha256(f"parent:{student_id}:{self._seq}".encode()).hexdigest()[:24]
            self._seq += 1
            self.parent_tokens[token] = student_id
            return {"token": token, "student_id": student["student_id"]}

    # -- 天气依据 -----------------------------------------------------------

    def record_weather(self, actor: dict, school_id: str, day: str, condition: str,
                       temp_c=None, source="") -> dict:
        self._require_role(actor, ROLE_ADMIN, ROLE_TEACHER)
        if school_id not in self.schools:
            raise NotFound(f"学校不存在: {school_id}")
        if condition not in ("晴", "多云", "阴", "雨", "高温"):
            raise ValidationError("天气状况取值非法")
        with self._lock:
            record = {
                "weather_id": self._id("wx"),
                "school_id": school_id,
                "date": str(parse_date(day)),
                "condition": condition,
                "temp_c": None if temp_c is None else float(temp_c),
                "source": source or "校级气象记录",
            }
            self.weather[(school_id, record["date"])] = record
            return record

    def _weather_on(self, school_id: str, day: str):
        return self.weather.get((school_id, str(day)))

    # -- 学期方案（带版本） --------------------------------------------------

    def active_plan(self, class_id: str):
        for plan_id in reversed(self.plan_by_class.get(class_id, [])):
            plan = self.plans[plan_id]
            if plan["status"] == "生效中":
                return plan
        return None

    def submit_plan(self, actor: dict, school_id: str, class_ids, week_start: str,
                    weeks: int, items: list[dict], note: str = "") -> dict:
        """学校提交学期方案；同班再次提交形成新版本，旧版本自动作废。"""
        self._require_role(actor, ROLE_ADMIN)
        if school_id not in self.schools:
            raise NotFound(f"学校不存在: {school_id}")
        class_ids = list(class_ids)
        if not class_ids:
            raise ValidationError("方案至少覆盖一个班级")
        for class_id in class_ids:
            klass = self._get_class(class_id)
            if klass["school_id"] != school_id:
                raise ValidationError(f"班级 {class_id} 不属于该校")
        start = parse_date(week_start)
        if start.weekday() != 0:
            raise ValidationError("学期起始周必须是周一")
        if not 1 <= int(weeks) <= 30:
            raise ValidationError("学期周数应在 1~30 之间")
        weeks = int(weeks)

        with self._lock:
            parsed = []
            errors, warnings = [], []
            for raw in items:
                require_keys(raw, ("type", "class_id", "weekday", "slot", "venue_id", "teacher_id"),
                             "方案条目")
                if raw["type"] not in ACTIVITY_TYPES:
                    raise ValidationError(f"未知活动类型: {raw['type']}")
                if raw["class_id"] not in class_ids:
                    raise ValidationError("方案条目班级与提交班级不一致")
                weekday_no = int(raw["weekday"])
                if not 1 <= weekday_no <= 7:
                    raise ValidationError("星期取值应为 1~7")
                start_min, end_min = slot_minutes(raw["slot"])
                venue = self._get_venue(raw["venue_id"])
                teacher = self._get_teacher(raw["teacher_id"])
                if venue["school_id"] != school_id:
                    errors.append(f"场地 {venue['name']} 不属于该校")
                skill = raw.get("skill")
                if skill and skill not in SKILL_TO_CATEGORY:
                    raise ValidationError(f"未知技能目标: {skill}")
                apply_weeks = raw.get("weeks") or list(range(1, weeks + 1))
                apply_weeks = sorted(int(w) for w in apply_weeks)
                if any(not 1 <= w <= weeks for w in apply_weeks):
                    raise ValidationError("方案条目周次超出学期范围")
                parsed.append({
                    "item_id": self._id("item"),
                    "type": raw["type"],
                    "class_id": raw["class_id"],
                    "weekday": weekday_no,
                    "slot": raw["slot"],
                    "start_min": start_min,
                    "end_min": end_min,
                    "venue_id": venue["venue_id"],
                    "teacher_id": teacher["teacher_id"],
                    "skill": skill,
                    "name": raw.get("name", ""),
                    "weeks": apply_weeks,
                })

            # 教师资质：体育课/班级赛事须持证，技能目标须在资质范围内
            for item in parsed:
                teacher = self._get_teacher(item["teacher_id"])
                if item["type"] in (ACTIVITY_PE, ACTIVITY_MATCH) and not teacher["qualified"]:
                    errors.append(
                        f"{item['type']}由未持教师资格的 {teacher['name']} 承担（星期{item['weekday']}{item['slot']}）")
                if item["skill"]:
                    flat = set()
                    for s in teacher["skills"]:
                        flat.add(s)
                        if s in SKILL_CATALOG:
                            flat.update(SKILL_CATALOG[s])
                    if item["skill"] not in flat:
                        errors.append(
                            f"教师 {teacher['name']} 无技能点 {item['skill']} 的资质，不能承担该目标")

            # 安全容量
            for item in parsed:
                klass = self._get_class(item["class_id"])
                venue = self._get_venue(item["venue_id"])
                if len(klass["student_ids"]) > venue["capacity"]:
                    errors.append(
                        f"{venue['name']} 安全容量 {venue['capacity']} 小于 {klass['name']} 人数 "
                        f"{len(klass['student_ids'])}（星期{item['weekday']}{item['slot']}）")

            # 每周每日一节体育课
            pe_grid = defaultdict(set)
            for item in parsed:
                if item["type"] == ACTIVITY_PE:
                    for w in item["weeks"]:
                        pe_grid[(item["class_id"], w)].add(item["weekday"])
            for class_id in class_ids:
                for w in range(1, weeks + 1):
                    days = pe_grid.get((class_id, w), set())
                    missing = [d for d in range(1, 6) if d not in days]
                    if missing:
                        errors.append(
                            f"{self._get_class(class_id)['name']} 第{w}周 缺少星期{'/'.join(map(str, missing))} 的体育课")

            # 同教师 / 同场地时间冲突
            occupied = []  # (label, weekday, start, end)
            for item in parsed:
                for w in item["weeks"]:
                    occupied.append((item, w))
            for (a, wa), (b, wb) in combinations(occupied, 2):
                if wa != wb or a["weekday"] != b["weekday"]:
                    continue
                if not overlap(a["start_min"], a["end_min"], b["start_min"], b["end_min"]):
                    continue
                if a["venue_id"] == b["venue_id"] and a["class_id"] != b["class_id"]:
                    errors.append(
                        f"第{wa}周星期{a['weekday']}{a['slot']} 场地 "
                        f"{self._get_venue(a['venue_id'])['name']} 同时安排给 "
                        f"{self._get_class(a['class_id'])['name']} 与 "
                        f"{self._get_class(b['class_id'])['name']}")
                if a["teacher_id"] == b["teacher_id"]:
                    errors.append(
                        f"第{wa}周星期{a['weekday']}{a['slot']} 教师 "
                        f"{self._get_teacher(a['teacher_id'])['name']} 同时承担两项活动")

            # 班级内部时间冲突（同班两项活动叠排）
            for (a, wa), (b, wb) in combinations(occupied, 2):
                if (a["class_id"] == b["class_id"] and wa == wb and a["weekday"] == b["weekday"]
                        and overlap(a["start_min"], a["end_min"], b["start_min"], b["end_min"])
                        and a["item_id"] != b["item_id"]):
                    errors.append(
                        f"{self._get_class(a['class_id'])['name']} 第{wa}周星期{a['weekday']} 时段重叠")

            if errors:
                raise ValidationError("学期方案校验未通过：" + "；".join(sorted(set(errors))))

            # 可解释提示：技能目标是否覆盖各技能类别
            for class_id in class_ids:
                planned = {i["skill"] for i in parsed if i["class_id"] == class_id and i["skill"]}
                cats = {SKILL_TO_CATEGORY[s] for s in planned}
                if "健康知识" not in cats:
                    warnings.append(f"{self._get_class(class_id)['name']} 未安排健康知识类目标，"
                                    f"雨天室内替代将缺少对应教学储备")

            # 旧版本作废、新版本入库
            superseded = []
            for class_id in class_ids:
                old = self.active_plan(class_id)
                if old:
                    old["status"] = "已作废"
                    superseded.append(old["plan_id"])

            plan_id = self._id("plan")
            version = max((len(self.plan_by_class[cid]) for cid in class_ids), default=0) + 1
            plan = {
                "plan_id": plan_id,
                "school_id": school_id,
                "class_ids": class_ids,
                "week_start": str(start),
                "weeks": weeks,
                "status": "生效中",
                "version": version,
                "note": note,
                "submitted_by": actor.get("name", actor.get("role")),
                "submitted_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "superseded": superseded,
                "items": parsed,
                "warnings": warnings,
            }
            self.plans[plan_id] = plan
            for class_id in class_ids:
                self.plan_by_class[class_id].append(plan_id)

            # 展开为逐周活动实例
            made = []
            for item in parsed:
                for w in item["weeks"]:
                    day = start + timedelta(weeks=w - 1, days=item["weekday"] - 1)
                    activity_id = self._id("act")
                    activity = {
                        "activity_id": activity_id,
                        "plan_id": plan_id,
                        "school_id": school_id,
                        "class_id": item["class_id"],
                        "type": item["type"],
                        "week": w,
                        "weekday": item["weekday"],
                        "date": str(day),
                        "slot": item["slot"],
                        "start_min": item["start_min"],
                        "end_min": item["end_min"],
                        "venue_id": item["venue_id"],
                        "teacher_id": item["teacher_id"],
                        "skill": item["skill"],
                        "name": item["name"],
                        "status": STATUS_SCHEDULED,
                        "events": [],
                        "replacement_id": None,
                        "origin_id": None,
                        "makeup_ids": [],
                    }
                    self.activities[activity_id] = activity
                    made.append(activity_id)
            return {"plan_id": plan_id, "version": plan["version"], "status": plan["status"],
                    "activities": len(made), "warnings": warnings, "superseded": superseded}

    def get_plan(self, actor: dict, class_id: str, version=None) -> dict:
        self._require_role(actor, ROLE_ADMIN, ROLE_RESEARCHER, ROLE_TEACHER)
        ids = self.plan_by_class.get(class_id, [])
        if not ids:
            raise NotFound("该班级尚无学期方案")
        plan = self.plans[ids[-1] if version is None else ids[int(version) - 1]]
        return self._public_plan(plan)

    def _public_plan(self, plan: dict) -> dict:
        return {k: v for k, v in plan.items()}

    # -- 活动定位 -----------------------------------------------------------

    def _find_activity(self, class_id: str, day, slot: str, active_only=True):
        day = str(parse_date(day))
        for activity in self.activities.values():
            if activity["class_id"] != class_id or activity["date"] != day or activity["slot"] != slot:
                continue
            if active_only and self.plans[activity["plan_id"]]["status"] != "生效中":
                continue
            return activity
        raise NotFound(f"{day} {slot} 无生效方案中的活动")

    def _activity_day_dt(self, activity: dict, minutes: int) -> datetime:
        return datetime.strptime(activity["date"], "%Y-%m-%d") + timedelta(minutes=minutes)

    # -- 调课 / 占课 / 天气替代 / 伤病适配 ----------------------------------

    def _basis_occupation(self, basis: dict):
        require_keys(basis, ("notice_id", "source", "reason"), "占课/调课依据")
        return dict(basis)

    def reschedule(self, actor: dict, activity_id: str, new_date: str, new_slot: str,
                   new_venue_id: str, basis: dict) -> dict:
        """调课：必须有占课通知或教务调课通知作为依据，且新时段无冲突。"""
        self._require_role(actor, ROLE_ADMIN, ROLE_TEACHER)
        with self._lock:
            origin = self.activities.get(activity_id)
            if not origin:
                raise NotFound("活动不存在")
            if origin["status"] in (STATUS_DONE, STATUS_REPLACED):
                raise Conflict("已完成或已替代的活动不能调课")
            basis = self._basis_occupation(basis)
            day = str(parse_date(new_date))
            start_min, end_min = slot_minutes(new_slot)
            venue = self._get_venue(new_venue_id)
            self._assert_venue_free(venue["venue_id"], day, start_min, end_min, exclude=activity_id)
            self._assert_teacher_free(origin["teacher_id"], day, start_min, end_min,
                                      exclude=activity_id)
            klass = self._get_class(origin["class_id"])
            if len(klass["student_ids"]) > venue["capacity"]:
                raise ValidationError(f"调入场地 {venue['name']} 容量不足")
            return self._make_replacement(origin, day, new_slot, start_min, end_min,
                                          venue["venue_id"], ADJUST_RESCHEDULE, basis, actor)

    def _assert_venue_free(self, venue_id, day, start_min, end_min, exclude=None):
        day = str(day)
        for other in self.activities.values():
            if other["activity_id"] == exclude or other["venue_id"] != venue_id:
                continue
            if other["date"] != day or other["status"] == STATUS_REPLACED:
                continue
            if overlap(start_min, end_min, other["start_min"], other["end_min"]):
                raise Conflict(f"场地 {self._get_venue(venue_id)['name']} 在该时段已安排给 "
                               f"{self._get_class(other['class_id'])['name']}")

    def _assert_teacher_free(self, teacher_id, day, start_min, end_min, exclude=None):
        day = str(day)
        for other in self.activities.values():
            if other["activity_id"] == exclude or other["teacher_id"] != teacher_id:
                continue
            if other["date"] != day or other["status"] == STATUS_REPLACED:
                continue
            if overlap(start_min, end_min, other["start_min"], other["end_min"]):
                raise Conflict(f"教师 {self._get_teacher(teacher_id)['name']} 该时段已有安排")

    def weather_substitution(self, actor: dict, activity_id: str, kind: str,
                             new_slot: str, new_venue_id: str, skill: str,
                             weather_id: str, content_name: str = "") -> dict:
        """降雨/高温替代：必须引用当日天气记录，场地必须为室内，教学仍须挂技能目标。"""
        self._require_role(actor, ROLE_ADMIN, ROLE_TEACHER)
        if kind not in (ADJUST_RAIN, ADJUST_HEAT):
            raise ValidationError("替代类型只能是 降雨替代/高温替代")
        if skill not in SKILL_TO_CATEGORY:
            raise ValidationError(f"替代课仍须明确技能目标，未知技能: {skill}")
        with self._lock:
            origin = self.activities.get(activity_id)
            if not origin:
                raise NotFound("活动不存在")
            weather = self._find_weather(weather_id)
            if weather["school_id"] != origin["school_id"] or weather["date"] != origin["date"]:
                raise ValidationError("天气依据必须是活动当日、本校的记录")
            if kind == ADJUST_RAIN and weather["condition"] != "雨":
                raise ValidationError("降雨替代须引用降雨天气记录")
            if kind == ADJUST_HEAT and not (
                    weather["condition"] == "高温"
                    or (weather["temp_c"] is not None and weather["temp_c"] >= RULE["高温阈值_摄氏度"])):
                raise ValidationError(f"高温替代须引用高温（≥{RULE['高温阈值_摄氏度']}℃）天气记录")
            venue = self._get_venue(new_venue_id)
            if venue["kind"] != "室内":
                raise ValidationError("天气异常时替代教学只能安排在室内场地")
            start_min, end_min = slot_minutes(new_slot)
            self._assert_venue_free(venue["venue_id"], origin["date"], start_min, end_min,
                                    exclude=activity_id)
            self._assert_teacher_free(origin["teacher_id"], origin["date"], start_min, end_min,
                                      exclude=activity_id)
            basis = {"weather_id": weather_id, "condition": weather["condition"],
                     "temp_c": weather["temp_c"], "source": weather["source"]}
            replacement = self._make_replacement(origin, origin["date"], new_slot, start_min,
                                                 end_min, venue["venue_id"], kind, basis, actor,
                                                 skill=skill, name=content_name)
            return replacement

    def _find_weather(self, weather_id: str) -> dict:
        for record in self.weather.values():
            if record["weather_id"] == weather_id:
                return record
        raise NotFound(f"天气依据不存在: {weather_id}")

    def _make_replacement(self, origin, day, slot, start_min, end_min, venue_id, kind, basis,
                          actor, skill=None, name="") -> dict:
        replacement_id = self._id("act")
        replacement = {
            "activity_id": replacement_id,
            "plan_id": origin["plan_id"],
            "school_id": origin["school_id"],
            "class_id": origin["class_id"],
            "type": origin["type"],
            "week": origin["week"],
            "weekday": origin["weekday"],
            "date": str(day),
            "slot": slot,
            "start_min": start_min,
            "end_min": end_min,
            "venue_id": venue_id,
            "teacher_id": origin["teacher_id"],
            "skill": skill if skill is not None else origin["skill"],
            "name": name or origin["name"],
            "status": STATUS_SCHEDULED,
            "events": [],
            "replacement_id": None,
            "origin_id": origin["activity_id"],
            "makeup_ids": [],
        }
        self.activities[replacement_id] = replacement
        origin["replacement_id"] = replacement_id
        origin["status"] = STATUS_REPLACED
        adjustment = {
            "adjustment_id": self._id("adj"),
            "kind": kind,
            "origin_activity_id": origin["activity_id"],
            "replacement_activity_id": replacement_id,
            "basis": basis,
            "by": actor.get("name", actor.get("role")),
            "at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
        self.adjustments.append(adjustment)
        replacement["adjustment_id"] = adjustment["adjustment_id"]
        return {"adjustment_id": adjustment["adjustment_id"], "kind": kind,
                "origin_activity_id": origin["activity_id"],
                "replacement_activity_id": replacement_id, "basis": basis}

    def occupy(self, actor: dict, activity_id: str, basis: dict) -> dict:
        """临时占课登记：必须引用通知依据；原课转为待补课，不自动销账。"""
        self._require_role(actor, ROLE_ADMIN, ROLE_TEACHER)
        with self._lock:
            origin = self.activities.get(activity_id)
            if not origin:
                raise NotFound("活动不存在")
            if origin["status"] in (STATUS_DONE, STATUS_REPLACED):
                raise Conflict("活动已结束或已替代，不能登记占课")
            basis = self._basis_occupation(basis)
            origin["status"] = STATUS_MAKEUP
            adjustment = {
                "adjustment_id": self._id("adj"),
                "kind": ADJUST_OCCUPY,
                "origin_activity_id": activity_id,
                "replacement_activity_id": None,
                "basis": basis,
                "by": actor.get("name", actor.get("role")),
                "at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            }
            self.adjustments.append(adjustment)
            self._log_event(origin, {
                "event_id": self._id("evt"),
                "type": EVENT_OCCUPY,
                "at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "by": actor.get("name", actor.get("role")),
                "payload": basis,
                "offline": False,
                "corroborated": True,
            })
            return {"adjustment_id": adjustment["adjustment_id"], "kind": ADJUST_OCCUPY,
                    "origin_activity_id": activity_id, "status": STATUS_MAKEUP}

    def student_adaptation(self, actor: dict, student_id: str, adaptation: str,
                           basis: dict, date_from: str, date_to: str, note: str = "") -> dict:
        """伤病适配：必须引用医务/家校依据，明确强度安排与起止日期。"""
        self._require_role(actor, ROLE_ADMIN, ROLE_TEACHER)
        if adaptation not in ADAPT_TYPES or adaptation == ADAPT_NONE:
            raise ValidationError("适配类型非法")
        require_keys(basis, ("medical_id", "source"), "伤病适配依据")
        d_from = parse_date(date_from)
        d_to = parse_date(date_to)
        if d_to < d_from:
            raise ValidationError("适配结束日期不能早于开始日期")
        with self._lock:
            student = self._get_student(student_id)
            record = {
                "adaptation_id": self._id("adp"),
                "student_id": student_id,
                "class_id": student["class_id"],
                "adaptation": adaptation,
                "basis": dict(basis),
                "date_from": str(d_from),
                "date_to": str(d_to),
                "note": note,
                "by": actor.get("name", actor.get("role")),
                "at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            }
            self.adjustments.append(record)
            return record

    def _adaptation_on(self, student_id: str, day) -> str:
        day = str(parse_date(day))
        current = ADAPT_NONE
        for record in self.adjustments:
            if record.get("student_id") != student_id:
                continue
            if record["date_from"] <= day <= record["date_to"]:
                current = record["adaptation"]
        return current

    # -- 最小化授课事件 -----------------------------------------------------

    def suggest_sample(self, actor: dict, activity_id: str, count: int = 6) -> dict:
        """按轮换抽取最小样本（学号，不含姓名影像），伤病免修者当日不入样。"""
        self._require_role(actor, ROLE_ADMIN, ROLE_TEACHER, ROLE_RESEARCHER)
        count = int(count)
        if not RULE["抽样人数下限"] <= count <= RULE["抽样人数上限"]:
            raise ValidationError(
                f"抽样人数应在 {RULE['抽样人数下限']}~{RULE['抽样人数上限']} 之间")
        with self._lock:
            activity = self.activities.get(activity_id)
            if not activity:
                raise NotFound("活动不存在")
            klass = self._get_class(activity["class_id"])
            pool = [sid for sid in klass["student_ids"]
                    if self._adaptation_on(sid, activity["date"]) != ADAPT_EXEMPT]
            # 确定性轮换：按 活动编号 哈希取起点，避免每班每次都是同样的学生
            ordered = sorted(pool, key=lambda sid: self.students[sid]["student_no"])
            if not ordered:
                raise ValidationError("当日无可抽样学生")
            seed = int(hashlib.sha256(activity_id.encode()).hexdigest(), 16)
            start = seed % len(ordered)
            picked = [ordered[(start + i) % len(ordered)] for i in range(min(count, len(ordered)))]
            return {"activity_id": activity_id, "sample_student_no":
                    [self.students[s]["student_no"] for s in picked],
                    "sample_student_ids": picked}

    def report_event(self, actor: dict, activity_id: str, event_type: str, occurred_at: str,
                     payload: dict = None, offline=False, offline_reason="", client_ref="") -> dict:
        """上报一类最小化事件；同类重复签到被拒绝，离线补签须带设备依据。"""
        self._require_role(actor, ROLE_TEACHER, ROLE_ADMIN)
        if event_type not in EVENT_TYPES or event_type in (EVENT_OCCUPY, EVENT_OFFLINE):
            raise ValidationError(f"事件类型不允许直接上报: {event_type}")
        payload = dict(payload or {})
        occurred = parse_dt(occurred_at)
        with self._lock:
            activity = self.activities.get(activity_id)
            if not activity:
                raise NotFound("活动不存在")
            if activity["status"] == STATUS_REPLACED:
                raise Conflict("该活动已被替代安排，请对替代活动上报")
            if client_ref:
                self._dedup_ref(activity_id, event_type, client_ref)

            self._validate_event_time(activity, occurred, offline, payload)
            event = {
                "event_id": self._id("evt"),
                "type": event_type,
                "at": occurred.strftime("%Y-%m-%d %H:%M"),
                "by": actor.get("name", actor.get("role")),
                "by_id": actor.get("teacher_id") or actor.get("actor_id"),
                "payload": payload,
                "offline": bool(offline),
                "offline_reason": offline_reason,
                "client_ref": client_ref,
                "corroborated": not offline,  # 离线事件先置待核对
            }

            if event_type == EVENT_TEACHER_SIGN:
                self._check_teacher_event(activity, event, actor)
            elif event_type == EVENT_VENUE:
                self._check_venue_event(activity, event, payload)
            elif event_type == EVENT_SAMPLE:
                self._check_sample_event(activity, event, payload, occurred)
            elif event_type in (EVENT_START, EVENT_END):
                self._check_phase_event(activity, event_type, event)
            elif event_type == EVENT_CONTENT:
                self._check_content_event(activity, event, payload)

            # 离线事件在其余两类来源齐备时转为已核
            if offline:
                event["corroborated"] = self._offline_corroboration(activity, event)
            self._log_event(activity, event)
            self._refresh_activity_status(activity)
            return {"event_id": event["event_id"], "activity_id": activity_id,
                    "type": event_type, "offline": event["offline"],
                    "corroborated": event["corroborated"],
                    "activity_status": activity["status"]}

    def _validate_event_time(self, activity, occurred: datetime, offline: bool, payload: dict):
        if occurred.strftime("%Y-%m-%d") != activity["date"]:
            raise ValidationError("事件日期与活动日期不一致")
        earliest = self._activity_day_dt(activity, activity["start_min"] - RULE["签到提前容忍_分钟"])
        latest = self._activity_day_dt(activity, activity["end_min"] + RULE["签到延后容忍_分钟"])
        if not earliest <= occurred <= latest:
            raise ValidationError("签到时间超出允许范围；如为离线情况请走离线补签")
        if offline:
            # 离线补签：实际发生时间在课内，设备恢复后提交；提交时间须在课后 7 日内
            submitted = parse_dt(payload["submitted_at"]) if payload.get("submitted_at") else occurred
            end_dt = self._activity_day_dt(activity, activity["end_min"])
            if submitted < end_dt:
                raise ValidationError("离线补签的提交时间不能早于活动结束")
            if submitted > end_dt + timedelta(days=7):
                raise ValidationError("离线补签超过 7 天追溯期")

    def _teacher_for(self, actor):
        teacher_id = actor.get("teacher_id")
        if teacher_id and teacher_id in self.teachers:
            return self.teachers[teacher_id]
        return None

    def _check_teacher_event(self, activity, event, actor):
        teacher = self._teacher_for(actor)
        if teacher is None:
            raise ValidationError("教师签到须绑定教师身份")
        if teacher["teacher_id"] != activity["teacher_id"] and actor.get("role") != ROLE_ADMIN:
            raise AccessDenied("只有排课教师本人可签到")
        prior = [e for e in activity["events"] if e["type"] == EVENT_TEACHER_SIGN]
        if prior:
            raise Conflict("重复签到：该教师本节课已签到，重复签到不计入运动时长")
        event["payload"] = {"teacher_id": teacher["teacher_id"], "role": "teacher"}

    def _check_venue_event(self, activity, event, payload):
        usable = payload.get("usable")
        if not isinstance(usable, bool):
            raise ValidationError("场地状态事件须给出 usable 布尔值")
        prior = [e for e in activity["events"] if e["type"] == EVENT_VENUE]
        if prior:
            raise Conflict("场地状态已上报，重复上报无效")
        event["payload"] = {"venue_id": activity["venue_id"], "usable": usable,
                            "note": str(payload.get("note", ""))[:80]}

    def _check_sample_event(self, activity, event, payload, occurred: datetime):
        ids = payload.get("student_ids") or []
        if not isinstance(ids, list) or not ids:
            raise ValidationError("抽样事件须提供 student_ids 列表")
        if not RULE["抽样人数下限"] <= len(ids) <= RULE["抽样人数上限"]:
            raise ValidationError(
                f"抽样人数应在 {RULE['抽样人数下限']}~{RULE['抽样人数上限']} 之间")
        klass = self._get_class(activity["class_id"])
        roster = set(klass["student_ids"])
        for sid in ids:
            if sid not in roster:
                raise ValidationError(f"抽样学生 {sid} 不在本班名册")
            if self._adaptation_on(sid, activity["date"]) == ADAPT_EXEMPT:
                raise ValidationError("当日免修休养学生不应纳入运动抽样")
        already = {s for e in activity["events"] if e["type"] == EVENT_SAMPLE
                   for s in e["payload"]["student_ids"]}
        dup = sorted(set(ids) & already)
        if dup:
            raise Conflict(f"重复抽样/签到：学生 {dup} 本节课已在样本中，不重复计时")
        # 只记录在样与强度，不采集影像
        event["payload"] = {"student_ids": sorted(ids),
                            "present": {sid: True for sid in ids},
                            "adaptation": {sid: self._adaptation_on(sid, activity["date"])
                                           for sid in ids}}

    def _check_phase_event(self, activity, event_type, event):
        prior = [e for e in activity["events"] if e["type"] == event_type]
        if prior:
            raise Conflict(f"{event_type}事件已存在，重复提交不改变运动时长")
        if event_type == EVENT_END:
            starts = [e for e in activity["events"] if e["type"] == EVENT_START]
            if not starts:
                raise ValidationError("缺少开始事件，不能先结束")
            if parse_dt(event["at"]) <= parse_dt(starts[0]["at"]):
                raise ValidationError("结束时间早于开始时间")

    def _check_content_event(self, activity, event, payload):
        mode = payload.get("mode", "技能教学")
        if mode not in ("技能教学", "体能练习", "自由活动", "健康知识讲授", "赛事组织"):
            raise ValidationError("授课模式取值非法")
        skills = payload.get("skills") or ([activity["skill"]] if activity["skill"] else [])
        for skill in skills:
            if skill not in SKILL_TO_CATEGORY:
                raise ValidationError(f"授课内容含未知技能点: {skill}")
        minutes = payload.get("minutes")
        if minutes is not None and not 0 <= int(minutes) <= activity["end_min"] - activity["start_min"] + 10:
            raise ValidationError("内容时长超出单课时长")
        event["payload"] = {"mode": mode, "skills": skills,
                            "minutes": None if minutes is None else int(minutes),
                            "phase": payload.get("phase", "新授") if mode == "技能教学" else None}

    def _offline_corroboration(self, activity, event) -> bool:
        """离线补签必须被其余最小化来源交叉印证，否则保持待核对、不计时长。"""
        types = {e["type"] for e in activity["events"] if e["corroborated"]}
        if event["type"] == EVENT_TEACHER_SIGN:
            return EVENT_VENUE in types and EVENT_SAMPLE in types
        if event["type"] in (EVENT_START, EVENT_END):
            return EVENT_TEACHER_SIGN in types and (EVENT_VENUE in types or EVENT_SAMPLE in types)
        return False

    def _log_event(self, activity, event: dict):
        activity["events"].append(event)
        self.events.append(event | {"activity_id": activity["activity_id"],
                                    "class_id": activity["class_id"],
                                    "school_id": activity["school_id"]})

    # -- 活动完成判定 / 时长 -------------------------------------------------

    def _effective_events(self, activity: dict):
        return [e for e in activity["events"] if e["corroborated"]]

    def _activity_minutes(self, activity: dict):
        """依据交叉确认后的开始/结束事件计算分钟，裁剪到计划时段，重复提交无效。"""
        events = self._effective_events(activity)
        starts = sorted((parse_dt(e["at"]) for e in events if e["type"] == EVENT_START))
        ends = sorted((parse_dt(e["at"]) for e in events if e["type"] == EVENT_END))
        if not starts or not ends:
            return 0, False
        end = ends[0]
        start = min(starts[0], end)
        raw = (end - start).total_seconds() / 60
        scheduled = activity["end_min"] - activity["start_min"]
        minutes = clamp(round(raw), 0, scheduled)  # 重复签到不会产生第二段区间
        pending_offline = any(not e["corroborated"] for e in activity["events"]
                              if e["type"] in (EVENT_START, EVENT_END, EVENT_TEACHER_SIGN))
        return minutes, pending_offline

    def _confirmation_sources(self, activity: dict) -> dict:
        events = self._effective_events(activity)
        types = {e["type"] for e in events}
        sources = {
            "教师": EVENT_TEACHER_SIGN in types or EVENT_START in types,
            "场地": EVENT_VENUE in types and any(
                e["type"] == EVENT_VENUE and e["payload"].get("usable") for e in events),
            "学生抽样": False,
        }
        samples = [e for e in events if e["type"] == EVENT_SAMPLE]
        if samples:
            total = sum(len(e["payload"]["student_ids"]) for e in samples)
            sources["学生抽样"] = total >= RULE["抽样人数下限"]
        return sources

    def _refresh_activity_status(self, activity: dict):
        if activity["status"] in (STATUS_REPLACED, STATUS_MAKEUP):
            return
        # 来源补齐后，先前待核的离线补签重新参与交叉印证
        for event in activity["events"]:
            if event["offline"] and not event["corroborated"]:
                event["corroborated"] = self._offline_corroboration(activity, event)
        events = self._effective_events(activity)
        types = [e["type"] for e in events]
        if EVENT_END not in types:
            if EVENT_START in types or EVENT_TEACHER_SIGN in types:
                activity["status"] = STATUS_TEACHING
            return
        sources = self._confirmation_sources(activity)
        if sum(sources.values()) >= RULE["交叉确认来源数"]:
            activity["status"] = STATUS_DONE
        else:
            activity["status"] = STATUS_PARTIAL

    def _free_play_only(self, activity) -> bool:
        contents = [e for e in self._effective_events(activity) if e["type"] == EVENT_CONTENT]
        return bool(contents) and all(e["payload"]["mode"] == "自由活动" for e in contents)

    # -- 补课 ---------------------------------------------------------------

    def arrange_makeup(self, actor: dict, origin_activity_id: str, new_date: str,
                       new_slot: str, new_venue_id: str, skill: str, basis_note: str = "") -> dict:
        """对缺失/占课/极端天气缺上的课安排补课，挂回原周次用于覆盖统计。"""
        self._require_role(actor, ROLE_ADMIN, ROLE_TEACHER)
        with self._lock:
            origin = self.activities.get(origin_activity_id)
            if not origin:
                raise NotFound("原活动不存在")
            if origin["type"] != ACTIVITY_PE:
                raise ValidationError("补课机制仅针对体育课")
            if origin["status"] == STATUS_DONE and not origin["origin_id"]:
                raise Conflict("该课已完成，无需补课")
            if origin["status"] == STATUS_REPLACED:
                raise Conflict("该课已有替代安排，请对替代活动追踪")
            if skill not in SKILL_TO_CATEGORY:
                raise ValidationError("补课须明确技能目标")
            day = str(parse_date(new_date))
            start_min, end_min = slot_minutes(new_slot)
            venue = self._get_venue(new_venue_id)
            self._assert_venue_free(venue["venue_id"], day, start_min, end_min)
            self._assert_teacher_free(origin["teacher_id"], day, start_min, end_min)
            replacement_id = self._id("act")
            makeup = {
                "activity_id": replacement_id,
                "plan_id": origin["plan_id"],
                "school_id": origin["school_id"],
                "class_id": origin["class_id"],
                "type": ACTIVITY_PE,
                "week": origin["week"],  # 归属原周
                "weekday": origin["weekday"],
                "date": day,
                "slot": new_slot,
                "start_min": start_min,
                "end_min": end_min,
                "venue_id": venue["venue_id"],
                "teacher_id": origin["teacher_id"],
                "skill": skill,
                "name": f"补课（第{origin['week']}周）",
                "status": STATUS_SCHEDULED,
                "events": [],
                "replacement_id": None,
                "origin_id": origin["activity_id"],
                "makeup_ids": [],
            }
            self.activities[replacement_id] = makeup
            origin["makeup_ids"].append(replacement_id)
            adjustment = {
                "adjustment_id": self._id("adj"),
                "kind": ADJUST_MAKEUP,
                "origin_activity_id": origin["activity_id"],
                "replacement_activity_id": replacement_id,
                "basis": {"note": basis_note or "依据缺课记录安排补课"},
                "by": actor.get("name", actor.get("role")),
                "at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            }
            self.adjustments.append(adjustment)
            return {"adjustment_id": adjustment["adjustment_id"],
                    "makeup_activity_id": replacement_id,
                    "origin_activity_id": origin["activity_id"], "week": origin["week"]}

    # -- 技能观察（非体测分数） ---------------------------------------------

    def record_observation(self, actor: dict, student_id: str, activity_id: str,
                           skill: str, level: str, note: str = "") -> dict:
        """只记录技能等级观察，拒绝任何分数/影像字段。"""
        self._require_role(actor, ROLE_TEACHER, ROLE_ADMIN)
        if skill not in SKILL_TO_CATEGORY:
            raise ValidationError(f"未知技能点: {skill}")
        if level not in SKILL_LEVELS:
            raise ValidationError(f"技能等级取值非法: {level}")
        with self._lock:
            student = self._get_student(student_id)
            activity = self.activities.get(activity_id)
            if not activity:
                raise NotFound("活动不存在")
            if student["class_id"] != activity["class_id"]:
                raise ValidationError("观察学生与活动班级不一致")
            record = {
                "observation_id": self._id("obs"),
                "student_id": student_id,
                "activity_id": activity_id,
                "skill": skill,
                "level": level,
                "note": str(note)[:120],
                "by": actor.get("name", actor.get("role")),
                "at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            }
            self.observations.append(record)
            return record

    # -- 覆盖度：教会、勤练、常赛 -------------------------------------------

    def _completed_chain(self, activity: dict):
        """一节排课活动实际算数的载体：自身（已完成/部分完成）或已完成的替代/补课。"""
        result = []
        if activity["status"] in (STATUS_DONE, STATUS_PARTIAL):
            result.append(activity)
        if activity["replacement_id"]:
            rep = self.activities[activity["replacement_id"]]
            if rep["status"] in (STATUS_DONE, STATUS_PARTIAL):
                result.append(rep)
        for mid in activity["makeup_ids"]:
            makeup = self.activities[mid]
            if makeup["status"] in (STATUS_DONE, STATUS_PARTIAL):
                result.append(makeup)
        return result

    def class_coverage(self, actor: dict, class_id: str, as_of: str = None) -> dict:
        """按可解释规则推导班级覆盖：返回每条规则的判定与证据。"""
        self._require_role(actor, ROLE_ADMIN, ROLE_RESEARCHER, ROLE_TEACHER)
        with self._lock:
            return self._coverage(class_id, as_of)

    def _coverage(self, class_id: str, as_of=None) -> dict:
        klass = self._get_class(class_id)
        plan = self.active_plan(class_id)
        if not plan:
            raise NotFound("该班级尚无生效方案")
        as_of = parse_date(as_of) if as_of else date.max
        start = parse_date(plan["week_start"])

        scheduled = [a for a in self.activities.values()
                     if a["class_id"] == class_id and a["plan_id"] == plan["plan_id"]
                     and a.get("origin_id") is None]
        elapsed_weeks = min(plan["weeks"],
                            max(0, math.ceil((as_of - start).days / 7 + 1e-9))
                            if as_of != date.max else plan["weeks"])
        elapsed_weeks = clamp(elapsed_weeks, 0, plan["weeks"])

        pe_acts = [a for a in scheduled if a["type"] == ACTIVITY_PE]
        break_acts = [a for a in scheduled if a["type"] == ACTIVITY_BREAK]
        match_acts = [a for a in scheduled if a["type"] == ACTIVITY_MATCH]

        # 周度统计
        weekly = {}
        gaps = []
        for a in scheduled:
            if parse_date(a["date"]) > as_of:
                continue
            chain = self._completed_chain(a)
            week = weekly.setdefault(a["week"], {"week": a["week"], "pe_minutes": 0,
                                                 "pe_done": 0, "pe_total": 0,
                                                 "break_minutes": 0, "break_scheduled": 0,
                                                 "match_done": 0})
            if a["type"] == ACTIVITY_PE:
                week["pe_total"] += 1
                if chain:
                    week["pe_done"] += 1
                    week["pe_minutes"] += sum(self._activity_minutes(c)[0] for c in chain)
                if not chain:
                    gaps.append(self._gap(a))
            elif a["type"] == ACTIVITY_BREAK:
                week["break_scheduled"] += a["end_min"] - a["start_min"]
                if chain:
                    sched = a["end_min"] - a["start_min"]
                    week["break_minutes"] += sum(
                        clamp(self._activity_minutes(c)[0], 0, sched) for c in chain)
            elif a["type"] == ACTIVITY_MATCH and chain:
                week["match_done"] += 1

        # 勤练
        qinlian_weeks = []
        for w in range(1, elapsed_weeks + 1):
            info = weekly.get(w, {"week": w, "pe_minutes": 0, "pe_done": 0, "pe_total": 0,
                                  "break_minutes": 0, "break_scheduled": 0,
                                  "match_done": 0})
            break_ratio = (info["break_minutes"] / info["break_scheduled"]
                           if info.get("break_scheduled") else 1.0)
            ok = (info["pe_minutes"] >= RULE["勤练_周体育时长_分钟"]
                  and break_ratio >= RULE["勤练_大课间出勤比例"])
            qinlian_weeks.append({"week": w, "pe_minutes": info["pe_minutes"],
                                  "pe_done": info["pe_done"], "pe_total": info["pe_total"],
                                  "break_ratio": round(break_ratio, 2), "pass": bool(ok)})
        qinlian_pass_weeks = sum(1 for w in qinlian_weeks if w["pass"])
        qinlian_pass = bool(qinlian_weeks) and qinlian_pass_weeks / len(qinlian_weeks) >= 0.8

        # 教会：计划技能目标 -> 授课次数 + 技能观察等级
        target_skills = sorted({a["skill"] for a in pe_acts if a["skill"]})
        jiaohui = []
        for skill in target_skills:
            sessions, free_sessions = [], []
            for a in pe_acts:
                for c in self._completed_chain(a):
                    for e in self._effective_events(c):
                        if e["type"] == EVENT_CONTENT and skill in e["payload"].get("skills", []):
                            sessions.append({"activity_id": c["activity_id"], "date": c["date"],
                                             "mode": e["payload"]["mode"]})
            obs = [o for o in self.observations
                   if o["skill"] == skill
                   and self.students[o["student_id"]]["class_id"] == class_id]
            levels = {lv: sum(1 for o in obs if o["level"] == lv) for lv in SKILL_LEVELS}
            taught = len(sessions)
            qualified = sum(levels[lv] for lv in ("合格", "良好", "优秀"))
            passed = (taught >= RULE["教会_每技能最少新授复习次数"] and qualified >= 1)
            jiaohui.append({"skill": skill, "category": SKILL_TO_CATEGORY[skill],
                            "taught_sessions": taught,
                            "rule_min_sessions": RULE["教会_每技能最少新授复习次数"],
                            "observation_levels": levels,
                            "evidence_sessions": sessions[-6:],
                            "pass": bool(passed)})
        jiaohui_pass = bool(jiaohui) and all(j["pass"] for j in jiaohui)

        # 常赛：每 9 个已历经周至少一场班级赛事实际完成
        match_done = sum(w.get("match_done", 0) for w in weekly.values())
        required_matches = math.ceil(elapsed_weeks / RULE["常赛_每多少周一场"]) if elapsed_weeks else 0
        changsai_pass = match_done >= required_matches

        return {
            "class_id": class_id,
            "class_name": klass["name"],
            "plan_id": plan["plan_id"],
            "version": plan["version"],
            "elapsed_weeks": elapsed_weeks,
            "as_of": str(as_of) if as_of != date.max else None,
            "教会": {"rule": f"每个计划技能点至少教授{RULE['教会_每技能最少新授复习次数']}次且有合格及以上观察",
                    "pass": jiaohui_pass, "skills": jiaohui},
            "勤练": {"rule": f"周体育时长≥{RULE['勤练_周体育时长_分钟']}分钟、大课间实际比例≥"
                             f"{int(RULE['勤练_大课间出勤比例'] * 100)}%（八成周达标即通过）",
                     "pass": qinlian_pass,
                     "pass_weeks": qinlian_pass_weeks, "total_weeks": len(qinlian_weeks),
                     "weekly": qinlian_weeks},
            "常赛": {"rule": f"每{RULE['常赛_每多少周一场']}个已历经周至少完成一场班级赛事",
                     "pass": bool(changsai_pass),
                     "matches_completed": match_done, "matches_required_by_now": required_matches,
                     "evidence": [{"activity_id": a["activity_id"], "date": a["date"]}
                                  for a in match_acts if self._completed_chain(a)]},
            "gaps": gaps,
        }

    def _gap(self, a: dict) -> dict:
        reason = {
            STATUS_MAKEUP: "占课/调课缺课待补",
            STATUS_REPLACED: "替代安排未完成",
            STATUS_SCHEDULED: "无授课记录",
            STATUS_TEACHING: "只有开始/签到，缺少结束确认",
            STATUS_PARTIAL: "交叉确认来源不足",
        }.get(a["status"], a["status"])
        return {"activity_id": a["activity_id"], "week": a["week"], "date": a["date"],
                "slot": a["slot"], "type": a["type"], "status": a["status"], "reason": reason,
                "planned_makeups": [self.activities[m]["date"] for m in a["makeup_ids"]]}

    # -- 异常发现与教研复核（不自动处罚） -----------------------------------

    def _activity_actual_mode(self, a: dict):
        chain = self._completed_chain(a) or [a]
        return chain

    def scan_anomalies(self, actor: dict, class_id: str, as_of: str = None) -> dict:
        """推导班级当前异常；异常进入教研复核清单，系统不做任何处罚。"""
        self._require_role(actor, ROLE_ADMIN, ROLE_RESEARCHER, ROLE_TEACHER)
        with self._lock:
            anomalies = self._derive_anomalies(class_id, as_of)
            return {"class_id": class_id, "open_reviews": self._open_review_count(class_id),
                    "anomalies": anomalies}

    def _derive_anomalies(self, class_id: str, as_of=None):
        plan = self.active_plan(class_id)
        if not plan:
            return []
        as_of = parse_date(as_of) if as_of else date.max
        anomalies = []

        def add(kind, severity, evidence, rule):
            key = evidence_hash({"class": class_id, "kind": kind, "e": evidence})
            anomalies.append({"key": key, "kind": kind, "severity": severity,
                              "evidence": evidence, "rule": rule,
                              "review_status": self.reviews.get(key, {}).get("status", "待复核")})

        pe_acts = [a for a in self.activities.values()
                   if a["class_id"] == class_id and a["plan_id"] == plan["plan_id"]
                   and a.get("origin_id") is None and a["type"] == ACTIVITY_PE
                   and parse_date(a["date"]) <= as_of]

        # 1) 阴阳课表：课表有体育、实际被占或全天无记录
        occupied = []
        no_record = []
        replaced_undone = []
        free_only = []
        weather_no_sub = []
        for a in pe_acts:
            chain = self._completed_chain(a)
            if not chain:
                if any(e["type"] == EVENT_OCCUPY for e in a["events"]):
                    occupied.append(a["activity_id"])
                elif a["status"] == STATUS_REPLACED:
                    replaced_undone.append(a["activity_id"])
                elif a["status"] in (STATUS_SCHEDULED, STATUS_TEACHING):
                    weather = self._weather_on(a["school_id"], a["date"])
                    if weather and (weather["condition"] == "雨" or weather["condition"] == "高温"
                                    or (weather["temp_c"] or 0) >= RULE["高温阈值_摄氏度"]):
                        weather_no_sub.append(a["activity_id"])
                    else:
                        no_record.append(a["activity_id"])
            if chain and all(self._free_play_only(c) for c in chain) and any(
                    e["type"] == EVENT_CONTENT for c in chain for e in self._effective_events(c)):
                free_only.append(a["activity_id"])

        if occupied:
            add("阴阳课表·实课被占", "高",
                {"activity_ids": occupied, "count": len(occupied)},
                "课表列体育课但出现占课依据且未销课，须教研复核")
        if no_record:
            add("阴阳课表·有课无记录", "高",
                {"activity_ids": no_record, "count": len(no_record)},
                "课表列体育课且无天气等依据，当日无任何授课确认记录")
        if replaced_undone:
            add("替代安排未落实", "中",
                {"activity_ids": replaced_undone, "count": len(replaced_undone)},
                "已有调课/天气替代安排，但替代课当日未完成")
        if weather_no_sub:
            add("极端天气缺替代", "中",
                {"activity_ids": weather_no_sub, "count": len(weather_no_sub)},
                "降雨/高温当日无替代教学完成记录")
        if len(free_only) >= RULE["自由活动_异常场次"]:
            add("整节自由活动", "中",
                {"activity_ids": free_only, "count": len(free_only)},
                f"连续/累计 {RULE['自由活动_异常场次']} 节整节自由活动")

        # 2) 只练考试项目：内容全部标注为应试倾向（教师上报模式=体能练习且无技能教学）
        exam_drill = []
        for a in pe_acts:
            for c in self._completed_chain(a):
                contents = [e for e in self._effective_events(c) if e["type"] == EVENT_CONTENT]
                if contents and all(e["payload"]["mode"] == "体能练习" for e in contents):
                    exam_drill.append(c["activity_id"])
        if len(exam_drill) >= RULE["自由活动_异常场次"]:
            add("只练考试项目倾向", "中", {"activity_ids": exam_drill, "count": len(exam_drill)},
                "多节体育课只有应试体能练习、无技能教学内容")

        # 3) 交叉确认失败 / 离线待核
        unverified, offline_pending = [], []
        for a in pe_acts:
            if a["status"] == STATUS_PARTIAL:
                unverified.append(a["activity_id"])
            _, pending = self._activity_minutes(a)
            if pending:
                offline_pending.append(a["activity_id"])
        if unverified:
            add("交叉确认不足", "高", {"activity_ids": unverified, "count": len(unverified)},
                f"教师/场地/抽样三类来源不足 {RULE['交叉确认来源数']} 类")
        if offline_pending:
            add("离线补签待核对", "中", {"activity_ids": offline_pending},
                "离线补签未被其他最小化来源印证，暂不计时")

        # 4) 覆盖度规则不达标
        coverage = self._coverage(class_id, str(as_of) if as_of != date.max else None)
        for pillar in ("教会", "勤练", "常赛"):
            if not coverage[pillar]["pass"] and coverage["elapsed_weeks"] >= 2:
                add(f"{pillar}覆盖不足", "中" if pillar != "教会" else "高",
                    {"pillar": pillar, "detail": self._coverage_brief(coverage, pillar)},
                    coverage[pillar]["rule"])
        return anomalies

    @staticmethod
    def _coverage_brief(coverage, pillar):
        if pillar == "教会":
            return [{"skill": j["skill"], "taught": j["taught_sessions"], "pass": j["pass"]}
                    for j in coverage["教会"]["skills"] if not j["pass"]]
        if pillar == "勤练":
            return {"pass_weeks": coverage["勤练"]["pass_weeks"],
                    "total_weeks": coverage["勤练"]["total_weeks"]}
        return {"matches_completed": coverage["常赛"]["matches_completed"],
                "required": coverage["常赛"]["matches_required_by_now"]}

    def anomaly_roster(self, actor: dict, school_id: str, as_of: str = None) -> dict:
        """教研员视角：异常班级清单（先复核，不自动处罚）。"""
        self._require_role(actor, ROLE_RESEARCHER, ROLE_ADMIN)
        with self._lock:
            rows = []
            for klass in self.classes.values():
                if klass["school_id"] != school_id:
                    continue
                if not self.active_plan(klass["class_id"]):
                    continue
                anomalies = self._derive_anomalies(klass["class_id"], as_of)
                open_items = [a for a in anomalies if a["review_status"] == "待复核"]
                if open_items:
                    rows.append({"class_id": klass["class_id"], "class_name": klass["name"],
                                 "open_count": len(open_items),
                                 "max_severity": "高" if any(a["severity"] == "高" for a in open_items) else "中",
                                 "items": open_items})
            return {"school_id": school_id, "note": "异常仅进入教研复核流程，不触发自动处罚",
                    "classes": rows}

    def resolve_review(self, actor: dict, class_id: str, key: str, decision: str,
                       note: str = "", as_of: str = None) -> dict:
        """教研员裁定：确认问题/排除异常/确认已整改（如已安排补课）。"""
        self._require_role(actor, ROLE_RESEARCHER)
        if decision not in REVIEW_RESOLVED:
            raise ValidationError(f"裁定取值须为 {'/'.join(REVIEW_RESOLVED)}")
        with self._lock:
            current = {a["key"]: a for a in self._derive_anomalies(class_id, as_of)}
            if key not in current:
                raise NotFound("该异常已不存在或编号有误")
            review = {
                "key": key, "class_id": class_id, "kind": current[key]["kind"],
                "evidence": current[key]["evidence"], "decision": decision, "note": note,
                "reviewer": actor.get("name", ROLE_RESEARCHER),
                "at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            }
            self.reviews[key] = {**review, "status": decision}
            return {"status": decision, "key": key, "kind": current[key]["kind"]}

    def _open_review_count(self, class_id: str) -> int:
        return sum(1 for r in self.reviews.values()
                   if r["class_id"] == class_id and r["status"] not in REVIEW_RESOLVED)

    # -- 班级事件链还原 -----------------------------------------------------

    def reconstruct_class(self, actor: dict, class_id: str, as_of: str = None) -> dict:
        """把 方案->调整->事件->每日状态->缺口->补课 串成可审计时间线。"""
        self._require_role(actor, ROLE_ADMIN, ROLE_RESEARCHER)
        with self._lock:
            plan = self.active_plan(class_id)
            if not plan:
                raise NotFound("该班级尚无生效方案")
            as_of = parse_date(as_of) if as_of else date.max
            timeline = []
            for a in sorted(self.activities.values(),
                            key=lambda x: (x["date"], x["start_min"], x["activity_id"])):
                if a["class_id"] != class_id or a["plan_id"] != plan["plan_id"]:
                    continue
                if parse_date(a["date"]) > as_of:
                    continue
                minutes, pending = self._activity_minutes(a)
                sources = self._confirmation_sources(a)
                timeline.append({
                    "activity_id": a["activity_id"],
                    "origin_activity_id": a.get("origin_id"),
                    "date": a["date"], "week": a["week"], "type": a["type"], "slot": a["slot"],
                    "venue": self._get_venue(a["venue_id"])["name"],
                    "teacher": self._get_teacher(a["teacher_id"])["name"],
                    "skill": a["skill"], "status": a["status"],
                    "minutes": minutes, "offline_pending": pending,
                    "confirmation_sources": sources,
                    "confirmed": sum(sources.values()) >= RULE["交叉确认来源数"],
                    "replacement_id": a["replacement_id"],
                    "makeup_ids": a["makeup_ids"],
                    "events": [{"type": e["type"], "at": e["at"], "offline": e["offline"],
                                "corroborated": e["corroborated"],
                                "sample_size": len(e["payload"].get("student_ids", []))
                                if e["type"] == EVENT_SAMPLE else None,
                                "mode": e["payload"].get("mode")
                                if e["type"] == EVENT_CONTENT else None}
                               for e in a["events"]],
                })
            def _adjustment_class(adj):
                if "adaptation" in adj:
                    return adj.get("class_id")
                origin = self.activities.get(adj.get("origin_activity_id"))
                return origin["class_id"] if origin else None

            adjustments = [a for a in self.adjustments if _adjustment_class(a) == class_id]
            coverage = self._coverage(class_id, str(as_of) if as_of != date.max else None)
            anomalies = self._derive_anomalies(class_id, str(as_of) if as_of != date.max else None)
            return {"class_id": class_id,
                    "class_name": self._get_class(class_id)["name"],
                    "plan": {"plan_id": plan["plan_id"], "version": plan["version"],
                             "week_start": plan["week_start"], "weeks": plan["weeks"]},
                    "adjustments": adjustments,
                    "timeline": timeline,
                    "coverage": coverage,
                    "anomalies": anomalies,
                    "reviews": [r for r in self.reviews.values() if r["class_id"] == class_id]}

    # -- 公众聚合 / 家长视图（隐私分级） ------------------------------------

    def public_school_summary(self, actor: dict, school_id: str, as_of: str = None) -> dict:
        """公众视图：仅学校级聚合指标，无任何师生明细。"""
        # 公众角色（含未认证）只允许读取聚合
        self._require_role(actor, ROLE_RESEARCHER, ROLE_ADMIN, ROLE_TEACHER, ROLE_PARENT, "公众")
        with self._lock:
            if school_id not in self.schools:
                raise NotFound("学校不存在")
            class_ids = [c["class_id"] for c in self.classes.values()
                         if c["school_id"] == school_id and self.active_plan(c["class_id"])]
            pe_scheduled = pe_done = 0
            makeup_done = 0
            week_minutes = defaultdict(int)
            pillar = {"教会": 0, "勤练": 0, "常赛": 0}
            open_anomalies = 0
            for class_id in class_ids:
                cov = self._coverage(class_id, as_of)
                for name in pillar:
                    pillar[name] += 1 if cov[name]["pass"] else 0
                for w in cov["勤练"]["weekly"]:
                    week_minutes[w["week"]] += w["pe_minutes"]
                for a in self.activities.values():
                    if a["class_id"] != class_id or a.get("origin_id"):
                        continue
                    if a["type"] == ACTIVITY_PE:
                        pe_scheduled += 1
                        if self._completed_chain(a):
                            pe_done += 1
                        if any(self.activities[m]["status"] == STATUS_DONE
                               for m in a["makeup_ids"]):
                            makeup_done += 1
                open_anomalies += len([x for x in self._derive_anomalies(class_id, as_of)
                                       if x["review_status"] == "待复核"])
            n = len(class_ids) or 1
            avg_weekly = round(sum(week_minutes.values()) /
                               max(1, sum(1 for _ in week_minutes)) / n) if week_minutes else 0
            return {
                "school_id": school_id,
                "school_name": self.schools[school_id]["name"],
                "classes_with_plan": len(class_ids),
                "pe_sessions_scheduled": pe_scheduled,
                "pe_sessions_completed": pe_done,
                "makeup_sessions_completed": makeup_done,
                "completion_rate": round(pe_done / pe_scheduled, 3) if pe_scheduled else None,
                "avg_weekly_pe_minutes_per_class": avg_weekly,
                "pillar_pass_classes": pillar,
                "open_review_items": open_anomalies,
                "note": "本结果为学校级聚合数据，不含任何学生个人信息",
            }

    def parent_view(self, token: str, as_of: str = None) -> dict:
        """家长视图：凭令牌只见自己孩子的适配、观察与出勤概况。"""
        student_id = self.parent_tokens.get(token)
        if not student_id:
            raise AccessDenied("令牌无效或已失效")
        with self._lock:
            student = self._get_student(student_id)
            adaptations = [a for a in self.adjustments
                           if a.get("student_id") == student_id and "adaptation" in a]
            observations = [o for o in self.observations if o["student_id"] == student_id]
            # 个人出勤：只统计抽样命中的活动（最小化），含伤病适配标记
            attendance = []
            for a in sorted(self.activities.values(), key=lambda x: x["date"]):
                if a["class_id"] != student["class_id"] or a.get("origin_id"):
                    continue
                hit = [e for e in a["events"]
                       if e["type"] == EVENT_SAMPLE and student_id in e["payload"]["student_ids"]]
                if not hit:
                    continue
                chain = self._completed_chain(a)
                attendance.append({
                    "date": a["date"], "type": a["type"], "slot": a["slot"],
                    "activity_status": (chain[0]["status"] if chain else a["status"]),
                    "adaptation": self._adaptation_on(student_id, a["date"]),
                    "minutes": sum(self._activity_minutes(c)[0] for c in chain) if chain else 0,
                })
            return {
                "student_no": student["student_no"],
                "class_name": self._get_class(student["class_id"])["name"],
                "adaptations": adaptations,
                "observations": observations,
                "sampled_attendance": attendance,
                "privacy": "仅含本人伤病适配与技能等级观察；无体测分数、无课堂影像、无他人数据",
            }
