"""Playwright Chromium 会话：资料门户与受理台的唯一交互通道。

约束：
* 只通过页面点击、表单填写和已展示 DOM 中的 JSON 契约获取信息；
* 不向地址栏手工拼装下载地址、不调用页面背后的 HTTP 接口、不 fetch/XHR；
* 原件只经浏览器下载事件落盘；
* 每个会话独立浏览器上下文，下载目录由调用方指定（不可信数据隔离目录）。
"""
from __future__ import annotations

import dataclasses
import json
import logging
from pathlib import Path
from typing import Any

from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

from .config import Config
from .masking import mask_text

log = logging.getLogger("collector.browser")

# 受理台/门户返回的阻断原因 -> 资料员可读的待办原因。
BLOCKER_REASONS = {
    "delegation_expired": "委托已过期，暂不能领取",
    "purpose_denied": "材料用途与本次出行用途不符",
    "traveler_denied": "旅客不在该原件的授权范围内",
    "session_required": "浏览会话失效，需重新进入门户",
}


@dataclasses.dataclass
class DownloadOutcome:
    allowed: bool
    document_id: str
    reason: str | None = None
    file_name: str | None = None
    sha256: str | None = None
    stored_path: Path | None = None
    data: bytes | None = None


class PortalSession:
    """一次命令运行期间的 Chromium 会话，用完即关。"""

    def __init__(self, config: Config):
        self._config = config
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None

    def __enter__(self) -> "PortalSession":
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(
            headless=self._config.headless,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        self._context = self._browser.new_context(accept_downloads=True)
        self._context.set_default_timeout(self._config.timeout_ms)
        return self

    def __exit__(self, *exc: object) -> None:
        try:
            if self._context:
                self._context.close()
            if self._browser:
                self._browser.close()
        finally:
            if self._playwright:
                self._playwright.stop()

    def _new_page(self) -> Page:
        assert self._context is not None
        return self._context.new_page()

    @staticmethod
    def _pre_blocks(page: Page) -> list[dict[str, Any]]:
        """读取页面上所有 <pre> 中展示的 JSON 契约（DOM 已公开的内容）。"""
        out: list[dict[str, Any]] = []
        for text in page.locator("pre").all_inner_texts():
            try:
                value = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(value, dict):
                out.append(value)
        return out

    def portal_today(self) -> str:
        """读取门户首页展示的演练业务日期。"""
        page = self._new_page()
        page.goto(f"{self._config.base_url}/")
        for block in self._pre_blocks(page):
            if "today" in block:
                return str(block["today"])
        raise RuntimeError("门户首页未展示业务日期 today")

    # ---- 资料门户 -----------------------------------------------------
    def list_traveler_entries(self) -> list[dict[str, str]]:
        """从首页家庭入口读取旅客 id 与家庭标识。"""
        page = self._new_page()
        page.goto(f"{self._config.base_url}/")
        entries = []
        for anchor in page.locator('a[href^="/traveler/"]').all():
            href = anchor.get_attribute("href") or ""
            entries.append({
                "traveler_id": href.rsplit("/", 1)[-1],
                "label": anchor.inner_text(),
            })
        log.info("门户首页展示旅客入口 %d 个", len(entries))
        return entries

    def open_traveler(self, traveler_id: str) -> dict[str, Any]:
        """打开旅客页，解析 DOM 中展示的旅客/批次/用途/所需材料契约。"""
        page = self._new_page()
        page.goto(f"{self._config.base_url}/traveler/{traveler_id}")
        for block in self._pre_blocks(page):
            if block.get("id") == traveler_id and "required" in block:
                log.info(
                    "旅客 %s 当前批次 %s，所需材料 %s",
                    traveler_id, block.get("trip"),
                    ",".join(sorted(block["required"])),
                )
                return block
        raise RuntimeError(f"旅客页未找到 {traveler_id} 的契约数据")

    def download_document(
        self,
        traveler: dict[str, Any],
        kind: str,
        document_id: str,
        downloads_dir: Path,
        purpose_override: str | None = None,
    ) -> DownloadOutcome:
        """在旅客页点击指定材料的下载链接，只接受浏览器下载事件。

        点击后若进入 403 JSON 页面，则按门户给出的 error 归类阻断原因。
        ``purpose_override`` 仅用于演练“用途不符”分支：仍通过页面上该下载链接
        携带的用途参数发起导航，不手工拼 URL、不走页面外请求。
        """
        traveler_id = traveler["id"]
        page = self._new_page()
        page.goto(f"{self._config.base_url}/traveler/{traveler_id}")
        link = page.locator(f'a[href^="/download/{document_id}"]').first
        if purpose_override is not None:
            # 仍由页面上的下载链接发起浏览器导航，仅替换链接中门户展示的用途参数。
            href = link.get_attribute("href") or ""
            link.evaluate(
                "(el, value) => el.setAttribute('href', value)",
                f"{href.split('?', 1)[0]}?traveler={traveler_id}"
                f"&purpose={purpose_override}",
            )
        try:
            with page.expect_download() as item_info:
                link.click()
            download = item_info.value
        except Exception:
            # 未触发下载：页面已展示 403 原因 JSON。
            body = page.locator("body").inner_text()
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                log.warning("下载 %s 未触发且页面非契约 JSON: %s",
                            document_id, mask_text(body[:120]))
                return DownloadOutcome(False, document_id,
                                       reason="download_unavailable")
            error = payload.get("error", "download_denied")
            log.info("旅客 %s 领取 %s 被拒：%s",
                     traveler_id, document_id, BLOCKER_REASONS.get(error, error))
            return DownloadOutcome(False, document_id, reason=error)

        suggested = download.suggested_filename
        data = download.path().read_bytes()
        # 每次下载放入独立的新目录，避免同名文件互相覆盖。
        target_dir = downloads_dir / traveler_id / document_id
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / suggested
        target.write_bytes(data)
        log.info("旅客 %s 经浏览器领取 %s -> %s（%d 字节）",
                 traveler_id, document_id, target.name, len(data))
        return DownloadOutcome(
            allowed=True, document_id=document_id, file_name=suggested,
            stored_path=target, data=data,
        )

    # ---- 受理台 -------------------------------------------------------
    def intake_history(self) -> dict[str, str]:
        """读取受理台历史页：送审引用 -> 收件号。仅读页面展示内容。"""
        page = self._new_page()
        page.goto(f"{self._config.base_url}/intake")
        mapping: dict[str, str] = {}
        for block in self._pre_blocks(page):
            if "reference" in block and "receipt" in block:
                mapping[block["reference"]] = block["receipt"]
        log.info("受理台历史展示送审记录 %d 条", len(mapping))
        return mapping

    def submit_intake(
        self,
        reference: str,
        metadata: dict[str, Any],
        files: dict[str, Path],
        lose_response: bool = False,
    ) -> dict[str, Any]:
        """在受理台表单上传送审包。files 的键为表单文件字段 passport/guardian。"""
        page = self._new_page()
        page.goto(f"{self._config.base_url}/intake")
        page.locator("[name=reference]").fill(reference)
        page.locator("[name=metadata]").fill(
            json.dumps(metadata, ensure_ascii=False, sort_keys=True))
        for field, path in files.items():
            page.locator(f"[name={field}]").set_input_files(str(path))
        if lose_response:
            page.locator("[name=lose_response]").fill("yes")

        try:
            with page.expect_response(
                lambda r: r.url.endswith("/intake") and r.request.method == "POST"
            ) as info:
                page.get_by_role("button", name="送审").click()
            status = info.value.status
        except Exception as exc:  # 网页超时：不代表没有收件
            log.warning("送审 %s 页面响应异常，按待核对处理：%s", reference, exc)
            return {"reference": reference, "status": None, "lost": True}

        log.info("送审 %s 受理台响应状态 %s", reference, status)
        return {"reference": reference, "status": status,
                "lost": status >= 500}

    def open_review(self, reference: str) -> dict[str, Any]:
        """打开某送审引用的逐项结论页，解析 receipt/results 与补件历史。"""
        page = self._new_page()
        page.goto(f"{self._config.base_url}/review/{reference}")
        review: dict[str, Any] | None = None
        supplements: list[dict[str, Any]] = []
        for block in self._pre_blocks(page):
            if "receipt" in block and "results" in block:
                review = block
            elif isinstance(block, dict) and block:
                # 补件历史视图：键为补件引用
                supplements.append(block)
        if review is None:
            raise RuntimeError(f"结论页 {reference} 未展示受理契约")
        # 补件历史视图以补件引用为顶层键，展平成便于调用方处理的列表。
        flat: list[dict[str, Any]] = []
        for block in supplements:
            for key, value in block.items():
                if isinstance(value, dict):
                    flat.append({"supp_ref": key, **value})
        review["supplement_history"] = flat
        return review

    def navigate_download_link(
        self,
        page_traveler_id: str,
        link_traveler_id: str,
        purpose: str,
        document_id: str,
        downloads_dir: Path,
    ) -> DownloadOutcome:
        """打开一名旅客的页面，经 DOM 导航到指定旅客/用途的下载地址。

        用于演练“旅客不在文件范围”分支：先打开旅客页建立浏览会话，再通过
        页面上下文的 in-page 导航访问门户下载路由（不手工输入地址栏、
        不走页面外请求），服务器按链接上的 traveler/purpose 授权。
        """
        page = self._new_page()
        page.goto(f"{self._config.base_url}/traveler/{page_traveler_id}")
        target = (f"/download/{document_id}?traveler={link_traveler_id}"
                  f"&purpose={purpose}")
        try:
            with page.expect_download() as item_info:
                # evaluate 触发的是页面内导航，与点击链接同属浏览器页面行为。
                page.evaluate("(url) => { window.location.href = url; }", target)
            download = item_info.value
        except Exception:
            body = page.locator("body").inner_text()
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                return DownloadOutcome(False, document_id,
                                       reason="download_unavailable")
            return DownloadOutcome(
                False, document_id, reason=payload.get("error", "download_denied"))
        data = download.path().read_bytes()
        target_dir = downloads_dir / link_traveler_id / document_id
        target_dir.mkdir(parents=True, exist_ok=True)
        dest = target_dir / download.suggested_filename
        dest.write_bytes(data)
        return DownloadOutcome(
            allowed=True, document_id=document_id,
            file_name=download.suggested_filename, stored_path=dest, data=data)

    def submit_supplement(
        self,
        review_reference: str,
        supp_ref: str,
        kind: str,
        document_id: str,
        file_path: Path,
    ) -> dict[str, Any]:
        """在指定收件的结论页提交指定材料补件，保留 receipt 归属。"""
        page = self._new_page()
        page.goto(f"{self._config.base_url}/review/{review_reference}")
        page.locator("[name=reference]").fill(supp_ref)
        page.locator("[name=kind]").fill(kind)
        page.locator("[name=document_id]").fill(document_id)
        page.locator("[name=file]").set_input_files(str(file_path))
        try:
            with page.expect_response(
                lambda r: f"/review/{review_reference}" in r.url
                and r.request.method == "POST"
            ) as info:
                page.get_by_role("button", name="提交指定补件").click()
            status = info.value.status
        except Exception as exc:
            log.warning("补件 %s 页面响应异常，按待核对处理：%s", supp_ref, exc)
            return {"supp_ref": supp_ref, "status": None, "lost": True}
        log.info("补件 %s（收件 %s 的 %s）响应状态 %s",
                 supp_ref, review_reference, kind, status)
        return {"supp_ref": supp_ref, "status": status, "lost": status >= 500}
