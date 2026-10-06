# -*- coding: utf-8 -*-
"""航次登记/结束与禁飞区核准复核航次的回归测试。

覆盖：
- 航次登记（POST /api/flights）：无发布 / 发布号已变 / 时刻无效 / 审查报错 /
  审查冲突均拒绝且不留记录；安全时落库航次编号、发布号、起飞时刻与航线快照
  （发布快照航线替换起飞时刻，与工作区已核准禁飞区重新审查）；
- 登记以发布快照航线为准：发布后工作区航线被继续编辑不影响航次航线快照；
- 航次查询（GET /api/flights）与结束（POST /api/flights/<id>/end）：
  结束后 open=false，重复结束 409，结束不存在 404；
- 禁飞区核准（POST /api/nfz-approval）除发布航线审查外，用候选禁飞区复核
  所有未结束航次（含旧发布的航次）：任一冲突返回航次及冲突明细，草稿、
  工作区与发布快照均不变；结束后该航次不再阻拦核准；
- 登记、结束与核准交错：已登记航次必被核准检查，核准生效的禁飞区必被
  后续登记检查，不漏检。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app  # noqa: E402

# 干净航线：4km 直线（y=100），200m 等高，08:00 起飞，全程约 9.6 分钟
ROUTE = {
    "departureTime": "2026-09-24T08:00",
    "unitScale": 100,
    "corridorHalfWidth": 0.3,
    "speeds": [25],
    "waypoints": [
        {"id": "w1", "name": "A", "x": 100, "y": 100, "alt": 200},
        {"id": "w2", "name": "B", "x": 500, "y": 100, "alt": 200},
    ],
}
# 覆盖航线中段、时窗 09:00–10:00 的禁飞区：08:00 起飞的发布航线不冲突，
# 09:00 起飞的航次必冲突（用于“航次阻拦核准、发布航线不阻拦”）
TIMED_NFZ = {
    "id": "nzT", "name": "时窗禁飞区", "enabled": True,
    "vertices": [[250, 50], [350, 50], [350, 150], [250, 150]],
    "minAlt": 0, "maxAlt": 400,
    "startTime": "2026-09-24T09:00", "endTime": "2026-09-24T10:00",
}
# 远离航线的合法禁飞区（与 ROUTE 任何起飞时刻都不冲突）
FAR_NFZ = {
    "id": "nzFar", "name": "远侧限制区", "enabled": True,
    "vertices": [[100, 500], [200, 500], [200, 600], [100, 600]],
    "minAlt": 0, "maxAlt": 400,
    "startTime": "", "endTime": "",
}
# 顶点少于 3 个的非法禁飞区（审查报错）
BAD_NFZ = {
    "id": "nzBad", "name": "非法区", "enabled": True,
    "vertices": [[1, 2], [3, 4]],
    "minAlt": 0, "maxAlt": 400,
    "startTime": "", "endTime": "",
}


def clone(obj):
    return json.loads(json.dumps(obj))


class FlightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old_path = app.DB_PATH
        app.DB_PATH = os.path.join(self.tmp, "test.db")
        app.init_db()
        self.client = app.app.test_client()

    def tearDown(self):
        app.DB_PATH = self.old_path
        shutil.rmtree(self.tmp, ignore_errors=True)

    # —— 辅助 ——
    def put_state(self, state):
        r = self.client.put("/api/state", json=state)
        self.assertEqual(r.status_code, 200)
        return r.get_json()["rev"]

    def save_version(self, state, name="v1"):
        r = self.client.post("/api/versions", json={"name": name, "state": state})
        self.assertEqual(r.status_code, 200)
        return r.get_json()["id"]

    def publish(self, vid, rev, seq):
        return self.client.post("/api/release", json={
            "versionId": vid, "workspaceRev": rev, "releaseSeq": seq})

    def publish_route(self):
        """发布 ROUTE（工作区禁飞区为空），返回 (versionId, 当前修订号)。"""
        rev = self.put_state({"route": {"waypoints": []}, "nfzs": []})
        vid = self.save_version({"route": ROUTE, "nfzs": []})
        r = self.publish(vid, rev, 0)
        self.assertEqual(r.status_code, 200, r.get_json())
        return vid, rev

    def register(self, dep, seq):
        return self.client.post("/api/flights", json={
            "departureTime": dep, "releaseSeq": seq})

    def register_ok(self, dep, seq):
        r = self.register(dep, seq)
        self.assertEqual(r.status_code, 200, r.get_json())
        return r.get_json()["flight"]

    def approve(self, nfzs, rev, seq):
        return self.client.post("/api/nfz-approval", json={
            "nfzs": nfzs, "workspaceRev": rev, "releaseSeq": seq})

    def flights(self):
        r = self.client.get("/api/flights")
        self.assertEqual(r.status_code, 200)
        return r.get_json()

    def ws_state(self):
        r = self.client.get("/api/state")
        self.assertEqual(r.status_code, 200)
        return r.get_json(), int(r.headers["X-Workspace-Rev"])

    def current_release(self):
        return self.client.get("/api/release").get_json()

    # —— 登记：无发布 ——
    def test_register_without_release_rejected(self):
        r = self.register("2026-09-24T09:00", 0)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["reason"], "no_release")
        self.assertEqual(self.flights(), [])  # 不留记录

    # —— 登记：字段与时刻校验 ——
    def test_register_missing_fields(self):
        self.publish_route()
        r = self.client.post("/api/flights", json={"departureTime": "2026-09-24T09:00"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["reason"], "missing_fields")
        r = self.client.post("/api/flights", json={
            "departureTime": "2026-09-24T09:00", "releaseSeq": "abc"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["reason"], "missing_fields")
        self.assertEqual(self.flights(), [])

    def test_register_invalid_time(self):
        self.publish_route()
        for bad in ("", "not-a-time", "2026-13-40T99:99", None, 12345):
            r = self.register(bad, 1)
            self.assertEqual(r.status_code, 400, bad)
            self.assertEqual(r.get_json()["reason"], "invalid_time", bad)
        self.assertEqual(self.flights(), [])  # 不留记录

    # —— 登记：发布号已变 ——
    def test_register_stale_release_seq(self):
        vid, rev = self.publish_route()
        r = self.publish(vid, rev, 1)  # 第二次发布，已发布号推进到 2
        self.assertEqual(r.status_code, 200, r.get_json())
        r = self.register("2026-09-24T09:00", 1)  # 提交旧发布号
        self.assertEqual(r.status_code, 409)
        body = r.get_json()
        self.assertEqual(body["reason"], "stale")
        self.assertEqual(body["details"]["releaseSeq"], {"expected": 1, "actual": 2})
        self.assertEqual(self.flights(), [])
        # 用当前发布号登记 → 通过
        self.register_ok("2026-09-24T09:00", 2)
        self.assertEqual(len(self.flights()), 1)

    # —— 登记：审查报错 / 冲突，不留记录 ——
    def test_register_analysis_errors_rejected(self):
        _, rev = self.publish_route()
        # 发布后工作区被写入非法禁飞区（直接 PUT，模拟异常工作区内容）
        self.put_state({"route": {"waypoints": []}, "nfzs": [clone(BAD_NFZ)]})
        r = self.register("2026-09-24T09:00", 1)
        self.assertEqual(r.status_code, 400)
        body = r.get_json()
        self.assertEqual(body["reason"], "analysis_errors")
        self.assertTrue(body["errors"])
        self.assertEqual(self.flights(), [])

    def test_register_conflict_rejected_then_ok(self):
        _, rev = self.publish_route()
        # 工作时窗禁飞区核准进工作区（08:00 起飞的发布航线不冲突 → 核准通过）
        r = self.approve([clone(TIMED_NFZ)], rev, 1)
        self.assertEqual(r.status_code, 200, r.get_json())
        # 09:00 起飞的航次穿过时窗 → 冲突，拒绝且不留记录
        r = self.register("2026-09-24T09:00", 1)
        self.assertEqual(r.status_code, 409)
        body = r.get_json()
        self.assertEqual(body["reason"], "analysis_conflicts")
        self.assertTrue(body["entries"])
        self.assertEqual(body["first"]["nfzId"], "nzT")
        self.assertEqual(self.flights(), [])
        # 改到 08:00 起飞（时窗未生效）→ 登记成功
        self.register_ok("2026-09-24T08:00", 1)
        self.assertEqual(len(self.flights()), 1)

    # —— 登记成功：落库内容与快照口径 ——
    def test_register_success_saves_snapshot(self):
        _, rev = self.publish_route()
        # 发布后工作区航线被继续编辑（不得影响航次快照）
        route2 = clone(ROUTE)
        route2["waypoints"][0]["name"] = "工作区改名"
        route2["departureTime"] = "2026-09-24T06:00"
        self.put_state({"route": route2, "nfzs": []})

        f = self.register_ok("2026-09-24 12:30", 1)  # 空格格式应被规范化
        self.assertEqual(f["id"], 1)
        self.assertEqual(f["releaseSeq"], 1)
        self.assertEqual(f["departureTime"], "2026-09-24T12:30")
        self.assertTrue(f["open"])
        self.assertIsNone(f["endedAt"])
        self.assertTrue(f["createdAt"])
        # 航线快照 = 发布快照航线替换起飞时刻（非工作区航线）
        self.assertEqual(f["route"]["departureTime"], "2026-09-24T12:30")
        self.assertEqual(f["route"]["waypoints"], ROUTE["waypoints"])
        # 查询接口返回同一航次（新的在前）
        lst = self.flights()
        self.assertEqual(len(lst), 1)
        self.assertEqual(lst[0]["id"], f["id"])
        self.assertEqual(lst[0]["route"]["waypoints"], ROUTE["waypoints"])
        # 发布快照与当前发布号不受登记影响
        cur = self.current_release()
        self.assertEqual(cur["releaseSeq"], 1)
        self.assertEqual(cur["release"]["state"]["route"]["departureTime"],
                         "2026-09-24T08:00")

    def test_register_uses_workspace_approved_nfzs(self):
        # 登记审查用的是工作区已核准禁飞区：核准 FAR_NFZ 后登记仍通过；
        # TIMED_NFZ 核准进工作区后，冲突时刻的登记被拒绝、时窗外的通过
        _, rev = self.publish_route()
        r = self.approve([clone(FAR_NFZ)], rev, 1)
        self.assertEqual(r.status_code, 200, r.get_json())
        rev = r.get_json()["rev"]
        f = self.register_ok("2026-09-24T09:00", 1)
        # 未结束的 09:00 航次阻拦 TIMED_NFZ 核准；结束后才放行
        r = self.approve([clone(FAR_NFZ), clone(TIMED_NFZ)], rev, 1)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["reason"], "flight_conflicts")
        self.client.post(f"/api/flights/{f['id']}/end")
        r = self.approve([clone(FAR_NFZ), clone(TIMED_NFZ)], rev, 1)
        self.assertEqual(r.status_code, 200, r.get_json())
        # 已核准禁飞区进入工作区后：时窗内的登记被拒，时窗外的通过
        r = self.register("2026-09-24T09:30", 1)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["reason"], "analysis_conflicts")
        self.register_ok("2026-09-24T11:00", 1)
        self.assertEqual(len(self.flights()), 2)

    # —— 结束航次 ——
    def test_end_flight(self):
        self.publish_route()
        f = self.register_ok("2026-09-24T09:00", 1)
        r = self.client.post(f"/api/flights/{f['id']}/end")
        self.assertEqual(r.status_code, 200, r.get_json())
        ended = r.get_json()["flight"]
        self.assertFalse(ended["open"])
        self.assertTrue(ended["endedAt"])
        # 重复结束 → 409；结束不存在 → 404
        r = self.client.post(f"/api/flights/{f['id']}/end")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["reason"], "already_ended")
        r = self.client.post("/api/flights/999/end")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.get_json()["reason"], "flight_not_found")
        # 查询可见已结束状态
        lst = self.flights()
        self.assertEqual(len(lst), 1)
        self.assertFalse(lst[0]["open"])

    # —— 核准复核未结束航次 ——
    def test_approval_rechecks_open_flights(self):
        _, rev = self.publish_route()
        f = self.register_ok("2026-09-24T09:00", 1)  # 09:00 起飞的航次
        rel_before = self.current_release()["release"]
        # 候选时窗禁飞区：发布航线（08:00 起飞）不冲突，航次（09:00 起飞）冲突
        r = self.approve([clone(TIMED_NFZ)], rev, 1)
        self.assertEqual(r.status_code, 409)
        body = r.get_json()
        self.assertEqual(body["reason"], "flight_conflicts")
        self.assertEqual(len(body["flights"]), 1)
        fc = body["flights"][0]
        self.assertEqual(fc["flight"]["id"], f["id"])
        self.assertEqual(fc["flight"]["releaseSeq"], 1)
        self.assertEqual(fc["flight"]["departureTime"], "2026-09-24T09:00")
        self.assertTrue(fc["entries"])
        self.assertEqual(fc["first"]["nfzId"], "nzT")
        # 工作区与发布快照均不变（草稿可改后再提）
        state, hdr_rev = self.ws_state()
        self.assertEqual(hdr_rev, rev)
        self.assertEqual(state["nfzs"], [])
        self.assertEqual(self.current_release()["release"], rel_before)
        # 结束航次后同一候选核准通过
        r = self.client.post(f"/api/flights/{f['id']}/end")
        self.assertEqual(r.status_code, 200)
        r = self.approve([clone(TIMED_NFZ)], rev, 1)
        self.assertEqual(r.status_code, 200, r.get_json())
        state, _ = self.ws_state()
        self.assertEqual(state["nfzs"], [TIMED_NFZ])

    def test_approval_rechecks_flights_of_old_releases(self):
        vid, rev = self.publish_route()
        f = self.register_ok("2026-09-24T09:00", 1)  # 挂在发布 #1 的航次
        # 第二次发布：已发布号推进到 2，航次属于旧发布
        r = self.publish(vid, rev, 1)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.current_release()["releaseSeq"], 2)
        # 旧发布的未结束航次仍被复核
        r = self.approve([clone(TIMED_NFZ)], rev, 2)
        self.assertEqual(r.status_code, 409)
        body = r.get_json()
        self.assertEqual(body["reason"], "flight_conflicts")
        self.assertEqual(body["flights"][0]["flight"]["id"], f["id"])
        self.assertEqual(body["flights"][0]["flight"]["releaseSeq"], 1)
        state, hdr_rev = self.ws_state()
        self.assertEqual(hdr_rev, rev)
        self.assertEqual(state["nfzs"], [])

    def test_approval_passes_when_no_flight_conflict(self):
        _, rev = self.publish_route()
        self.register_ok("2026-09-24T08:00", 1)  # 与发布同时刻，不冲突
        r = self.approve([clone(TIMED_NFZ)], rev, 1)
        self.assertEqual(r.status_code, 200, r.get_json())
        state, _ = self.ws_state()
        self.assertEqual(state["nfzs"], [TIMED_NFZ])

    # —— 登记 / 结束 / 核准交错：不漏检已登记航次 ——
    def test_interleaved_register_end_approve(self):
        _, rev = self.publish_route()
        # 登记两个航次：09:00（会撞时窗区）与 12:00（时窗外）
        f1 = self.register_ok("2026-09-24T09:00", 1)
        f2 = self.register_ok("2026-09-24T12:00", 1)
        # 核准被 09:00 航次阻拦（12:00 航次不冲突，不出现在明细里）
        r = self.approve([clone(TIMED_NFZ)], rev, 1)
        self.assertEqual(r.status_code, 409)
        body = r.get_json()
        self.assertEqual([fc["flight"]["id"] for fc in body["flights"]], [f1["id"]])
        # 结束 09:00 航次 → 核准通过（12:00 航次在时窗外不阻拦）
        self.client.post(f"/api/flights/{f1['id']}/end")
        r = self.approve([clone(TIMED_NFZ)], rev, 1)
        self.assertEqual(r.status_code, 200, r.get_json())
        rev = r.get_json()["rev"]
        # 核准生效后，新登记必须过已核准禁飞区：时窗内的登记被拒，时窗外通过
        r = self.register("2026-09-24T09:30", 1)
        self.assertEqual(r.status_code, 409)
        f3 = self.register_ok("2026-09-24T13:00", 1)
        # 未结束的 f2/f3 都在查询里（新的在前），f1 已结束
        lst = self.flights()
        self.assertEqual([f["id"] for f in lst], [f3["id"], f2["id"], f1["id"]])
        self.assertEqual([f["open"] for f in lst], [True, True, False])
        # 再核准一个时窗覆盖 12:00/13:00 航次、但不压发布航线（08:00 起飞）
        # 的禁飞区：f2/f3 未结束 → flight_conflicts；逐一结束后才放行
        noon = clone(TIMED_NFZ)
        noon["id"] = "nzNoon"
        noon["startTime"] = "2026-09-24T11:00"
        noon["endTime"] = "2026-09-24T14:00"
        r = self.approve([clone(noon)], rev, 1)
        self.assertEqual(r.status_code, 409)
        body = r.get_json()
        self.assertEqual(body["reason"], "flight_conflicts")
        self.assertEqual({fc["flight"]["id"] for fc in body["flights"]},
                         {f2["id"], f3["id"]})
        # f2 结束后 f3 仍阻拦；全部结束后核准通过
        self.client.post(f"/api/flights/{f2['id']}/end")
        r = self.approve([clone(noon)], rev, 1)
        self.assertEqual(r.status_code, 409)
        self.assertEqual([fc["flight"]["id"] for fc in r.get_json()["flights"]],
                         [f3["id"]])
        self.client.post(f"/api/flights/{f3['id']}/end")
        r = self.approve([clone(noon)], rev, 1)
        self.assertEqual(r.status_code, 200, r.get_json())
        state, _ = self.ws_state()
        self.assertEqual(state["nfzs"], [noon])

    # —— 既有行为不回归：核准失败修订号不增 ——
    def test_flight_conflict_bumps_nothing(self):
        _, rev = self.publish_route()
        self.register_ok("2026-09-24T09:00", 1)
        r = self.approve([clone(TIMED_NFZ)], rev, 1)
        self.assertEqual(r.status_code, 409)
        _, hdr_rev = self.ws_state()
        self.assertEqual(hdr_rev, rev)  # 修订号不变
        self.assertEqual(self.current_release()["releaseSeq"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
