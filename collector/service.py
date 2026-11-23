"""采集业务编排：把 Chromium 页面操作落到可恢复的本地状态上。

设计原则：
* 每个动作先落本地状态再推进，中断后重跑同一命令不会重复建包或重复补件；
* 阻断（过期委托/用途不符/旅客不在范围/明确拒绝）逐旅客、逐材料留痕，
  与“尚未领取”区分开；
* 受理结论以（旅客 × 批次）隔离，旧批次结论不自动套用到新批次；
* 共用原件按 SHA256 复用文件，但授权与受理归属逐旅客独立。
"""
from __future__ import annotations

import hashlib
import logging
from datetime import date
from pathlib import Path
from typing import Any

from .browser import BLOCKER_REASONS, PortalSession
from .config import Config
from .store import Store

log = logging.getLogger("collector.service")

# 受理台表单支持的文件字段；delegation（委托书）只校验有效性、不进入文件表单。
INTAKE_FILE_KINDS = ("passport", "guardian")


def _stable_ref(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("|".join(parts).encode()).hexdigest()[:12]
    return f"{prefix}-{digest}"


class CollectorService:
    def __init__(self, config: Config, store: Store):
        self.config = config
        self.store = store

    # ---- 扫描与领取 ---------------------------------------------------
    def scan(self, session: PortalSession) -> list[dict[str, Any]]:
        """读取门户当前展示的全部旅客及其当前批次契约并持久化。"""
        today = session.portal_today()
        self.store.set_meta("portal_today", today)
        travelers: list[dict[str, Any]] = []
        for entry in session.list_traveler_entries():
            traveler = session.open_traveler(entry["traveler_id"])
            self.store.upsert_traveler(traveler)
            self.store.replace_requirements(
                traveler["id"], traveler["trip"], traveler["required"])
            travelers.append(traveler)
            log.info("扫描到旅客 %s 批次 %s 委托至 %s",
                     traveler["id"], traveler["trip"], traveler["delegate_until"])
        return travelers

    def collect(
        self, session: PortalSession,
        traveler_id: str, trip: str | None = None,
    ) -> dict[str, Any]:
        """对一名旅客当前批次的所有所需材料执行浏览器领取。

        已成功领取且内容仍与门户材料号匹配的材料不重复下载（共用文件可复用）；
        每次领取无论允许与否都留下逐旅客授权记录。
        """
        trip = trip or self.store.latest_trip(traveler_id)
        if not trip:
            raise ValueError(f"本地无旅客 {traveler_id} 的批次记录，请先扫描")
        row = self.store.get_traveler(traveler_id, trip)
        # 库行列名 traveler_id，浏览器层使用门户契约键 id，这里做一次归一化。
        traveler = dict(row)
        traveler["id"] = traveler["traveler_id"]
        required = self.store.requirements(traveler_id, trip)
        result: dict[str, Any] = {"traveler": traveler_id, "trip": trip,
                                  "downloaded": [], "blocked": [], "skipped": []}

        if traveler["delegate_until"] < self._portal_today():
            # 委托过期是整批次阻断，仍逐材料记录原因，方便资料员看到缺什么。
            for kind, document_id in required.items():
                self.store.record_download(
                    traveler_id, trip, kind, document_id, False,
                    "delegation_expired", None)
                result["blocked"].append(
                    {"kind": kind, "document_id": document_id,
                     "reason": "delegation_expired"})
            log.warning("旅客 %s 批次 %s 委托已过期，全部材料暂不能领取",
                        traveler_id, trip)
            return result

        for kind, document_id in required.items():
            existing = self.store.latest_allowed_download(traveler_id, trip, kind)
            if existing and existing["document_id"] == document_id:
                result["skipped"].append({"kind": kind, "document_id": document_id})
                continue
            outcome = session.download_document(
                traveler, kind, document_id, self.config.downloads_dir)
            if not outcome.allowed:
                self.store.record_download(
                    traveler_id, trip, kind, document_id, False,
                    outcome.reason, None)
                result["blocked"].append(
                    {"kind": kind, "document_id": document_id,
                     "reason": outcome.reason})
                continue
            assert outcome.data is not None and outcome.stored_path is not None
            sha = self.store.register_document(
                document_id, outcome.file_name or document_id, outcome.data,
                outcome.stored_path, traveler["purpose"])
            self.store.record_download(
                traveler_id, trip, kind, document_id, True, None, sha)
            result["downloaded"].append(
                {"kind": kind, "document_id": document_id, "sha256": sha})
        return result

    # ---- 送审 ---------------------------------------------------------
    def _portal_today(self) -> str:
        value = self.store.get_meta("portal_today")
        if not value:
            raise RuntimeError("尚未扫描门户，缺少业务日期")
        return value

    def _accepted_kinds(self, traveler_id: str, trip: str) -> set[str]:
        """该旅客该批次下，已经受理台明确接受的材料类型（含补件接受）。"""
        accepted: set[str] = set()
        for sub in self.store.submissions_for(traveler_id, trip):
            if not sub["receipt"]:
                continue
            for kind, result in self.store.reviews_for(sub["receipt"]).items():
                if result == "accepted":
                    accepted.add(kind)
                elif result == "needs_replacement":
                    accepted.discard(kind)
        return accepted

    def _eligible_files(
        self, traveler_id: str, trip: str, required: dict[str, str],
    ) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        """挑出当前批次可交的文件；其余给出明确的暂不能交原因。"""
        files: list[dict[str, Any]] = []
        blockers: list[dict[str, str]] = []
        accepted = self._accepted_kinds(traveler_id, trip)
        for kind, document_id in required.items():
            if kind == "delegation":
                blockers.append({"kind": kind, "document_id": document_id,
                                 "reason": "delegation_not_a_file"})
                continue
            if kind in accepted:
                blockers.append({"kind": kind, "document_id": document_id,
                                 "reason": "already_accepted"})
                continue
            download = self.store.latest_allowed_download(traveler_id, trip, kind)
            if not download:
                blocker = self.store.latest_blocker(traveler_id, trip, kind)
                reason = blocker["reason"] if blocker else "not_collected"
                blockers.append({"kind": kind, "document_id": document_id,
                                 "reason": reason})
                continue
            if download["document_id"] != document_id:
                blockers.append({"kind": kind, "document_id": document_id,
                                 "reason": "superseded_document"})
                continue
            doc = self.store.document(download["document_id"])
            if not doc or not Path(doc["stored_path"]).is_file():
                blockers.append({"kind": kind, "document_id": document_id,
                                 "reason": "local_file_missing"})
                continue
            files.append({"kind": kind, "document_id": document_id,
                          "sha256": doc["sha256"], "file_name": doc["file_name"],
                          "stored_path": doc["stored_path"]})
        return files, blockers

    def submit(
        self, session: PortalSession, traveler_id: str,
        trip: str | None = None, lose_response: bool = False,
    ) -> dict[str, Any]:
        """组建并提交当前批次的送审包；已接受材料不重交。"""
        trip = trip or self.store.latest_trip(traveler_id)
        traveler = self.store.get_traveler(traveler_id, trip)
        required = self.store.requirements(traveler_id, trip)
        files, blockers = self._eligible_files(traveler_id, trip, required)
        if not files:
            log.info("旅客 %s 批次 %s 暂无可交文件", traveler_id, trip)
            return {"traveler": traveler_id, "trip": trip, "submitted": False,
                    "blockers": blockers}

        doc_map = {f["kind"]: f["document_id"] for f in files}
        package_ref = _stable_ref(
            "pkg", traveler_id, trip,
            *(f"{k}:{v}" for k, v in sorted(doc_map.items())))
        if self.store.submission_exists(package_ref):
            log.info("送审包 %s 已存在，不重复上传", package_ref)
            return {"traveler": traveler_id, "trip": trip, "submitted": False,
                    "package_ref": package_ref, "blockers": blockers,
                    "note": "duplicate_package_ref"}

        metadata = {
            "traveler": traveler_id, "trip": trip,
            "purpose": traveler["purpose"], "documents": doc_map,
        }
        self.store.create_submission(
            package_ref, traveler_id, trip, metadata, files)
        form_files = {f["kind"]: Path(f["stored_path"]) for f in files}
        response = session.submit_intake(
            package_ref, metadata, form_files, lose_response=lose_response)
        if response.get("lost"):
            log.warning("送审 %s 未取得确认，保留 pending_receive 待核对",
                        package_ref)
        return {"traveler": traveler_id, "trip": trip, "submitted": True,
                "package_ref": package_ref, "response": response,
                "blockers": blockers}

    # ---- 核对与结论 ---------------------------------------------------
    def reconcile(self, session: PortalSession) -> list[dict[str, Any]]:
        """用受理台历史页核对所有未确认送审，绝不因超时建立第二件。"""
        history = session.intake_history()
        reconciled = []
        for sub in self.store.pending_reconcile():
            ref = sub["package_ref"]
            if ref in history:
                self.store.mark_receipt(ref, history[ref], "received")
                log.info("送审 %s 核对到收件号 %s", ref, history[ref])
                reconciled.append({"package_ref": ref,
                                   "receipt": history[ref], "found": True})
            else:
                log.warning("送审 %s 受理台历史中仍无记录，保持待核对", ref)
                reconciled.append({"package_ref": ref, "found": False})
        return reconciled

    def poll_reviews(
        self, session: PortalSession,
        traveler_id: str | None = None, trip: str | None = None,
    ) -> list[dict[str, Any]]:
        """逐项拉取已有收件号的结论；空 results 表示仍待审。"""
        out = []
        subs = self._submissions_scope(traveler_id, trip)
        for sub in subs:
            if not sub["receipt"]:
                continue
            review = session.open_review(sub["package_ref"])
            results = review.get("results", {})
            for kind, result in results.items():
                self.store.upsert_review(sub["receipt"], kind, result)
            out.append({"package_ref": sub["package_ref"],
                        "receipt": sub["receipt"], "results": results})
            log.info("收件 %s 当前结论 %s", sub["receipt"], results or "待审")
        return out

    def _submissions_scope(
        self, traveler_id: str | None, trip: str | None,
    ) -> list[dict[str, Any]]:
        if traveler_id and trip:
            return self.store.submissions_for(traveler_id, trip)
        rows: list[dict[str, Any]] = []
        for t in self.store.known_travelers():
            rows.extend(self.store.submissions_for(t["traveler_id"], t["trip"]))
        # 去重（同旅客多观察行）
        seen, unique = set(), []
        for row in rows:
            if row["package_ref"] not in seen:
                seen.add(row["package_ref"])
                unique.append(row)
        return unique

    def supplement(
        self, session: PortalSession, receipt: str, kind: str,
        traveler_id: str, trip: str,
    ) -> dict[str, Any]:
        """针对某收件号的某项 needs_replacement 提交补件。

        护栏：
        * 该收件当前结论必须确实要求该类补件；
        * 补件必须是该旅客当前批次要求的材料号，且经该旅客授权成功领取；
        * 不能塞入与被审版本相同的原件，也不能使用其他旅客的文件。
        """
        reviews = self.store.reviews_for(receipt)
        if reviews.get(kind) != "needs_replacement":
            raise ValueError(f"收件 {receipt} 当前未要求 {kind} 补件")

        required = self.store.requirements(traveler_id, trip)
        document_id = required.get(kind)
        if not document_id:
            raise ValueError(f"旅客 {traveler_id} 批次 {trip} 无 {kind} 材料要求")
        download = self.store.latest_allowed_download(traveler_id, trip, kind)
        if not download or download["document_id"] != document_id:
            raise ValueError("补件原件尚未经该旅客授权领取，拒绝提交")
        doc = self.store.document(document_id)
        if not doc or not Path(doc["stored_path"]).is_file():
            raise ValueError("补件原件本地缺失")

        # 找到该收件对应的送审引用（补件必须回到同一 review 页）。
        review_ref = self._package_ref_for_receipt(receipt)
        prior_sha = self._reviewed_sha(receipt, kind)
        if prior_sha and doc["sha256"] == prior_sha:
            raise ValueError("补件与被审原件字节相同，受理台不会受理重复件")

        supp_ref = _stable_ref(
            "supp", receipt, kind, traveler_id, trip, document_id)
        if self.store.supplement_exists(supp_ref):
            log.info("补件 %s 已提交过，不重复上传", supp_ref)
            return {"supp_ref": supp_ref, "submitted": False,
                    "note": "duplicate_supplement_ref"}

        self.store.create_supplement(
            supp_ref, receipt, traveler_id, trip, kind, document_id,
            doc["sha256"], "pending_reconcile")
        response = session.submit_supplement(
            review_ref, supp_ref, kind, document_id, Path(doc["stored_path"]))
        if not response.get("lost"):
            self.store.create_supplement(
                supp_ref, receipt, traveler_id, trip, kind, document_id,
                doc["sha256"], "submitted")
        return {"supp_ref": supp_ref, "receipt": receipt, "kind": kind,
                "document_id": document_id, "submitted": True,
                "response": response}

    def _package_ref_for_receipt(self, receipt: str) -> str:
        for t in self.store.known_travelers():
            for sub in self.store.submissions_for(t["traveler_id"], t["trip"]):
                if sub["receipt"] == receipt:
                    return sub["package_ref"]
        raise ValueError(f"本地无收件号 {receipt} 的送审记录")

    def _reviewed_sha(self, receipt: str, kind: str) -> str | None:
        for t in self.store.known_travelers():
            for sub in self.store.submissions_for(t["traveler_id"], t["trip"]):
                if sub["receipt"] != receipt:
                    continue
                for f in self.store.submission_files(sub["package_ref"]):
                    if f["kind"] == kind:
                        return f["sha256"]
        return None

    # ---- 状态视图 -----------------------------------------------------
    def status(
        self, traveler_id: str | None = None, trip: str | None = None,
    ) -> list[dict[str, Any]]:
        """按旅客 × 批次汇总：缺什么、为何暂不能交、由哪个收件号受理。"""
        view: list[dict[str, Any]] = []
        travelers = self.store.known_travelers()
        if traveler_id:
            travelers = [t for t in travelers if t["traveler_id"] == traveler_id]
        for t in travelers:
            tid, tr = t["traveler_id"], t["trip"]
            if trip and tr != trip:
                continue
            required = self.store.requirements(tid, tr)
            items = []
            for kind, document_id in required.items():
                download = self.store.latest_allowed_download(tid, tr, kind)
                blocker = self.store.latest_blocker(tid, tr, kind)
                item = {"kind": kind, "document_id": document_id}
                if download and download["document_id"] == document_id:
                    item["material_state"] = "collected"
                    item["sha256"] = download["sha256"]
                elif blocker:
                    item["material_state"] = "blocked"
                    item["reason"] = blocker["reason"]
                else:
                    item["material_state"] = "missing"
                items.append(item)

            packages = []
            for sub in self.store.submissions_for(tid, tr):
                packages.append({
                    "package_ref": sub["package_ref"],
                    "receipt": sub["receipt"],
                    "state": sub["state"],
                    "files": [
                        {"kind": f["kind"], "document_id": f["document_id"],
                         "sha256": f["sha256"]}
                        for f in self.store.submission_files(sub["package_ref"])
                    ],
                    "reviews": (self.store.reviews_for(sub["receipt"])
                                if sub["receipt"] else {}),
                    "supplements": [
                        {"supp_ref": s["supp_ref"], "kind": s["kind"],
                         "document_id": s["document_id"], "state": s["state"]}
                        for s in self.store.supplements_for_receipt(
                            sub["receipt"] or "")
                        if s["traveler_id"] == tid and s["trip"] == tr
                    ],
                })
            view.append({
                "traveler": tid, "family": t["family"], "trip": tr,
                "purpose": t["purpose"], "delegate": t["delegate"],
                "delegate_until": t["delegate_until"],
                "delegation_active": t["delegate_until"] >= self._portal_today(),
                "requirements": items, "packages": packages,
            })
        return view

    @staticmethod
    def reason_text(code: str | None) -> str:
        return BLOCKER_REASONS.get(
            code or "",
            {
                "not_collected": "尚未领取",
                "already_accepted": "该材料已被受理台接受，不重交",
                "superseded_document": "门户已换版，本地原件不是当前有效版本",
                "local_file_missing": "本地原件缺失，需重新领取",
                "delegation_not_a_file": "委托书有效性按委托期限校验，不随文件上传",
                "download_unavailable": "下载未取得且门户未给出契约原因",
                "download_denied": "门户拒绝下载",
            }.get(code or "", code or "未知原因"),
        )
