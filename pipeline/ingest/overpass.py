import time

import httpx

from pipeline.http import Fetcher, FetchResult

# Overpass là dịch vụ công cộng miễn phí, hay quá tải (504) hoặc bị giới hạn
# (429/502). Nhiều module ingest (osm, boundary) đều cần cùng cơ chế xoay
# vòng mirror + chờ rồi thử lại — sống ở một nơi duy nhất để tránh lệch nhau
# (osm.py từng có cơ chế này, boundary.py từng hard-code một URL không retry
# nên một lần 504 làm chết cả `all` ở bước ranh giới).
OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)
OVERPASS_URL = OVERPASS_ENDPOINTS[0]

_RETRYABLE_STATUSES = {429, 502, 504}
_RETRY_WAIT_SECONDS = 60
_RETRY_PASSES = 2


def fetch_overpass(fetcher: Fetcher, query: str, force: bool) -> FetchResult:
    """Thử lần lượt các endpoint Overpass, tối đa 2 lượt qua toàn bộ danh sách.

    Overpass là dịch vụ công cộng miễn phí, hay quá tải (504) hoặc bị giới
    hạn (429/502). Khi một endpoint lỗi theo kiểu tạm thời, đợi khoảng một
    phút rồi chuyển sang endpoint kế tiếp thay vì bỏ cuộc ngay.
    """
    last_error: Exception | None = None
    for lap in range(1, _RETRY_PASSES + 1):
        for url in OVERPASS_ENDPOINTS:
            print(f"overpass: đang thử endpoint {url} (lượt {lap}/{_RETRY_PASSES})")
            try:
                return fetcher.fetch(url, method="POST", data=query, force=force)
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status not in _RETRYABLE_STATUSES:
                    raise
                last_error = exc
                print(
                    f"overpass: {url} trả về {status}, đợi {_RETRY_WAIT_SECONDS}s"
                    " rồi thử endpoint kế tiếp"
                )
                time.sleep(_RETRY_WAIT_SECONDS)
            except httpx.TimeoutException as exc:
                last_error = exc
                print(
                    f"overpass: {url} timeout, đợi {_RETRY_WAIT_SECONDS}s"
                    " rồi thử endpoint kế tiếp"
                )
                time.sleep(_RETRY_WAIT_SECONDS)
    raise RuntimeError(
        f"Cả {len(OVERPASS_ENDPOINTS)} endpoint Overpass đều lỗi"
        f" sau {_RETRY_PASSES} lượt thử"
    ) from last_error
