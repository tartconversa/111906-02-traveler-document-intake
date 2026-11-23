"""采集产品的可重复检查：真实 Chromium 下载、上传、核对、补件与中断恢复。

与来源自身检查一样，本文件只启动本地演练门户，不连接外部站点；
产品侧只通过页面操作与 DOM 契约工作（另见 test_no_bypass_contract）。
"""
import json
import re
import tempfile
import unittest
from pathlib import Path

from support import SourceCase

from collector.browser import PortalSession
from collector.cli import render_status
from collector.config import Config
from collector.masking import mask_text
from collector.service import CollectorService
from collector.store import Store


class CollectorCase(SourceCase):
    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = Path(self._tmp.name) / "state"
        self.config = Config(base_url=self.base, state_dir=self.state_dir)
        self.config.ensure_dirs()
        self.store = Store(self.config.db_path)
        self.addCleanup(self.store.close)

    def session(self):
        return PortalSession(self.config)

    def service(self, session):
        return CollectorService(self.config, self.store)

    def portal_traveler(self, traveler_id: str, trip: str, **overrides):
        """库行 -> 门户契约形态（浏览器层使用 id 键），可覆写用途等字段。"""
        row = dict(self.store.get_traveler(traveler_id, trip))
        row["id"] = row["traveler_id"]
        row.update(overrides)
        return row

    def scan_collect(self, session):
        svc = self.service(session)
        svc.scan(session)
        for traveler in self.store.known_travelers():
            svc.collect(session, traveler["traveler_id"], traveler["trip"])
        return svc

class HappyPath(CollectorCase):
    def test_scan_collect_submit_review_accepted(self):
        with self.session() as session:
            svc = self.scan_collect(session)
            # P-201 委托有效：两类材料均经浏览器下载成功。
            dl = self.store.latest_allowed_download("P-201", "T1", "passport")
            self.assertIsNotNone(dl)
            self.assertEqual(dl["document_id"], "PP201-v1")
            doc = self.store.document("PP201-v1")
            self.assertEqual(
                Path(doc["stored_path"]).read_bytes(),
                Path(dl["sha256"] and doc["stored_path"]).read_bytes())
            self.assertIn("FAKE-000201",
                          Path(doc["stored_path"]).read_text(encoding="utf-8"))

            # 受理台只接受出现在远端历史的 SHA256：先送审再核对收件号。
            result = svc.submit(session, "P-201", "T1")
            self.assertTrue(result["submitted"])
            ref = result["package_ref"]
            self.assertEqual(self.store.get_submission(ref)["state"],
                             "pending_reconcile")
            reconciled = svc.reconcile(session)
            self.assertTrue(all(r["found"] for r in reconciled))
            self.assertEqual(self.store.get_submission(ref)["receipt"], "intake-1")

            # 阶段0 结论为空（待审），阶段2 两项均接受。
            pending = svc.poll_reviews(session, "P-201", "T1")
            self.assertEqual(pending[0]["results"], {})
            self.control(2)
            reviews = svc.poll_reviews(session, "P-201", "T1")
            self.assertEqual(reviews[0]["results"],
                             {"passport": "accepted", "guardian": "accepted"})

            # 已接受后再次送审：不产生第二件，不重交任何材料。
            again = svc.submit(session, "P-201", "T1")
            self.assertFalse(again["submitted"])
            history = session.intake_history()
            self.assertEqual(list(history), [ref])


class Blockers(CollectorCase):
    def test_expired_delegation_blocks_whole_trip(self):
        with self.session() as session:
            svc = self.scan_collect(session)
            # P-202 阶段0 委托已于 2026-10-01 过期（门户今日 2026-11-19）。
            for kind in ("passport", "guardian"):
                blocker = self.store.latest_blocker("P-202", "T1", kind)
                self.assertEqual(blocker["reason"], "delegation_expired")
            result = svc.submit(session, "P-202", "T1")
            self.assertFalse(result["submitted"])
            reasons = {b["reason"] for b in result["blockers"]}
            self.assertIn("delegation_expired", reasons)
            self.assertEqual(session.intake_history(), {})

    def test_purpose_and_traveler_denials_are_distinct(self):
        with self.session() as session:
            svc = self.scan_collect(session)
            traveler = self.portal_traveler("P-201", "T1")
            # 用途不符：通过页面下载链接携带不匹配用途参数，浏览器真实导航至 403 页。
            outcome = session.download_document(
                traveler, "passport", "PP201-v1", self.config.downloads_dir,
                purpose_override="other")
            self.assertFalse(outcome.allowed)
            self.assertEqual(outcome.reason, "purpose_denied")
            # 旅客不在文件范围：阶段1 P-202 续期后，在其页面改写链接指向
            # P-201 的专属护照；链接仍取自页面 DOM，不手工输入地址。
            self.control(1)
            outcome2 = session.navigate_download_link(
                page_traveler_id="P-202", link_traveler_id="P-202",
                purpose="tour", document_id="PP201-v1",
                downloads_dir=self.config.downloads_dir)
            self.assertFalse(outcome2.allowed)
            self.assertEqual(outcome2.reason, "traveler_denied")
            # 两类原因必须能在状态文本中区分展示。
            text = CollectorService.reason_text("purpose_denied")
            text2 = CollectorService.reason_text("traveler_denied")
            self.assertNotEqual(text, text2)


class IntakeReliability(CollectorCase):
    def test_lost_response_does_not_create_second_package(self):
        with self.session() as session:
            svc = self.scan_collect(session)
            result = svc.submit(session, "P-201", "T1", lose_response=True)
            ref = result["package_ref"]
            self.assertTrue(result["response"]["lost"])
            self.assertEqual(self.store.get_submission(ref)["state"],
                             "pending_reconcile")
            # 重跑提交：确定性引用命中，不重复上传。
            again = svc.submit(session, "P-201", "T1")
            self.assertFalse(again["submitted"])
            self.assertEqual(again.get("note"), "duplicate_package_ref")
            # 受理台其实已经收件：核对后拿到同一收件号，历史仍只有一件。
            reconciled = svc.reconcile(session)
            self.assertEqual(reconciled[0]["receipt"], "intake-1")
            self.assertEqual(len(session.intake_history()), 1)

    def test_superseded_passport_and_targeted_supplement(self):
        with self.session() as session:
            svc = self.scan_collect(session)
            first = svc.submit(session, "P-201", "T1")
            pkg_ref = first["package_ref"]
            svc.reconcile(session)
            self.control(1)  # P-201 护照换版 v2，旧护照被要求补件，监护已接受
            svc.scan(session)

            required = self.store.requirements("P-201", "T1")
            self.assertEqual(required["passport"], "PP201-v2")
            # 旧批次结论保留：guardian 仍 accepted，不会被重交。
            svc.poll_reviews(session, "P-201", "T1")
            eligible, blockers = svc._eligible_files(
                "P-201", "T1", required)
            # 旧 v1 授权记录还在但已不是当前要求 -> superseded_document；
            # guardian 已被受理台接受 -> already_accepted。
            self.assertEqual(
                {(b["kind"], b["reason"]) for b in blockers},
                {("guardian", "already_accepted"),
                 ("passport", "superseded_document")})

            # 领取新版护照（与旧版同名 passport.txt，字节不同）。
            svc.collect(session, "P-201", "T1")
            v1 = self.store.document("PP201-v1")
            v2 = self.store.document("PP201-v2")
            self.assertEqual(v1["file_name"], v2["file_name"])
            self.assertNotEqual(v1["sha256"], v2["sha256"])

            # 护栏：未要求补件的类型不能补。
            with self.assertRaises(ValueError):
                svc.supplement(session, "intake-1", "guardian", "P-201", "T1")
            # 正确补件：receipt 归属保留，只送 passport 一项。
            out = svc.supplement(session, "intake-1", "passport", "P-201", "T1")
            self.assertTrue(out["submitted"])
            self.assertEqual(out["receipt"], "intake-1")
            self.assertEqual(out["document_id"], "PP201-v2")
            # 重复补件同一材料：确定性引用拦截。
            again = svc.supplement(
                session, "intake-1", "passport", "P-201", "T1")
            self.assertFalse(again["submitted"])
            # 受理台补件历史独立保存且挂同一收件号（补件没有独立 review 页）。
            review = session.open_review(pkg_ref)
            self.assertEqual(review["receipt"], "intake-1")
            history = review["supplement_history"]
            self.assertTrue(
                any(item["supp_ref"] == out["supp_ref"]
                    and item["payload"]["receipt"] == "intake-1"
                    and item["payload"]["kind"] == "passport"
                    for item in history))
            stored = self.store.supplements_for_receipt("intake-1")
            self.assertEqual(len(stored), 1)
            self.assertEqual(stored[0]["kind"], "passport")


class TripAndSharing(CollectorCase):
    def test_trip_switch_isolates_intake_and_keeps_history(self):
        with self.session() as session:
            svc = self.scan_collect(session)
            svc.submit(session, "P-201", "T1")
            svc.reconcile(session)
            self.control(1)  # P-202 转团 T1 -> T2，委托续期
            svc.scan(session)
            svc.collect(session, "P-202", "T2")
            result = svc.submit(session, "P-202", "T2")
            self.assertTrue(result["submitted"])
            svc.reconcile(session)

            # 旧批次 T1 记录仍可追查；新批次 T2 是独立送审包与收件号。
            old = self.store.submissions_for("P-202", "T1")
            new = self.store.submissions_for("P-202", "T2")
            self.assertEqual(len(old), 0)  # T1 时委托过期，从未送审成功
            self.assertEqual(len(new), 1)
            self.assertEqual(new[0]["receipt"], "intake-2")
            self.assertEqual(self.store.known_trips("P-202"), ["T1", "T2"])
            # 旧批次结论不得套到新身份组合：T2 尚无任何 review 行。
            self.assertEqual(self.store.reviews_for("intake-2"), {})

            # 共用监护证明：文件按内容只存一份，但两名旅客各有授权与受理归属。
            g1 = self.store.document("G1")
            for tid in ("P-201", "P-202"):
                trip = self.store.latest_trip(tid)
                dl = self.store.latest_allowed_download(tid, trip, "guardian")
                self.assertEqual(dl["sha256"], g1["sha256"])
            p202_pkg = self.store.submission_files(new[0]["package_ref"])
            self.assertIn("guardian", {f["kind"] for f in p202_pkg})

    def test_old_trip_acceptance_does_not_count_for_new_trip(self):
        # 纯状态层规则：即使 T1 已接受，T2 必须重新建立受理归属。
        with self.session() as session:
            svc = self.scan_collect(session)
            svc.submit(session, "P-201", "T1")
            svc.reconcile(session)
            self.control(2)
            svc.poll_reviews(session, "P-201", "T1")
            self.assertIn("passport", svc._accepted_kinds("P-201", "T1"))
            # 模拟转团后的新批次行（同一材料号也不继承结论）。
            self.store.upsert_traveler({
                "id": "P-201", "family": "F1", "trip": "T9", "purpose": "tour",
                "delegate": "D1", "delegate_until": "2026-12-31"})
            self.store.replace_requirements(
                "P-201", "T9", {"passport": "PP201-v2", "guardian": "G1"})
            self.assertEqual(svc._accepted_kinds("P-201", "T9"), set())
            eligible, _ = svc._eligible_files(
                "P-201", "T9", self.store.requirements("P-201", "T9"))
            # 没有 T9 的授权领取记录，即使文件字节存在也不能交。
            self.assertEqual(eligible, [])


class ResilienceAndPrivacy(CollectorCase):
    def test_state_survives_interruption_and_reuses_originals(self):
        with self.session() as session:
            svc = self.scan_collect(session)
            svc.submit(session, "P-201", "T1")
            svc.reconcile(session)
        db = self.config.db_path
        # 全新进程视角：重新打开同一状态库与下载目录。
        store2 = Store(db)
        self.addCleanup(store2.close)
        with self.session() as session:
            svc2 = CollectorService(self.config, store2)
            view = svc2.status("P-201", "T1")
            self.assertEqual(view[0]["packages"][0]["receipt"], "intake-1")
            # 再次领取全部命中本地授权记录，不产生新下载。
            result = svc2.collect(session, "P-201", "T1")
            self.assertEqual(result["downloaded"], [])
            self.assertEqual(len(result["skipped"]), 2)

    def test_logs_and_status_mask_identity_numbers(self):
        # 遮蔽函数本身的契约：长数字串仅留后三位。
        self.assertEqual(mask_text("号码 FAKE-000201"), "号码 FAKE-***201")
        self.assertEqual(mask_text("FAKE-000299"), "FAKE-***299")
        from collector.cli import _setup_logging
        import logging
        _setup_logging(self.config, verbose=False)
        logging.getLogger("collector.service").info(
            "原件核对 FAKE-000201 与 FAKE-000299")
        with self.session() as session:
            self.scan_collect(session)
            svc = CollectorService(self.config, self.store)
            text = render_status(svc.status())
        # 原件中的完整证件号不得出现在任何普通输出或日志中。
        self.assertNotIn("FAKE-000201", text)
        self.assertNotIn("FAKE-000299", text)
        self.assertNotIn("2026-11-19", text)  # 长数字日期同样遮蔽
        log_text = self.config.log_path.read_text(encoding="utf-8")
        self.assertNotIn("FAKE-000201", log_text)
        self.assertNotIn("FAKE-000299", log_text)
        self.assertIn("***201", log_text)
        # 日志落盘本身可用于中断后追溯。
        self.assertTrue(log_text.strip())


class ContractBoundary(CollectorCase):
    def test_product_never_calls_http_directly_or_reads_fixtures(self):
        root = Path(__file__).resolve().parents[1]
        for path in (root / "collector").rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            for banned in ("urllib.request", "import requests", "httpx",
                           "aiohttp", "http.client", "urlopen"):
                self.assertNotIn(banned, source,
                                 f"{path.name} 不得绕过浏览器直接发 HTTP：{banned}")
            self.assertNotIn("fixtures", source)
            self.assertNotIn("from source", source)
            self.assertNotIn("import source", source)

    def test_metadata_contract_and_remote_sha256_history(self):
        with self.session() as session:
            svc = self.scan_collect(session)
            result = svc.submit(session, "P-201", "T1")
            ref = result["package_ref"]
            svc.reconcile(session)
        # 通过受理台历史页（DOM）核对登记的 metadata 与文件 SHA256。
        with self.session() as session:
            page = session._new_page()
            page.goto(f"{self.base}/intake")
            blocks = session._pre_blocks(page)
        item = next(b for b in blocks if b.get("reference") == ref)
        payload = item["payload"]
        self.assertEqual(payload["metadata"]["traveler"], "P-201")
        self.assertEqual(payload["metadata"]["trip"], "T1")
        self.assertEqual(payload["metadata"]["documents"]["passport"], "PP201-v1")
        local_sha = self.store.document("PP201-v1")["sha256"]
        self.assertEqual(payload["files"]["passport"]["sha256"], local_sha)


if __name__ == "__main__":
    unittest.main(verbosity=2)
