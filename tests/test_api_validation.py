"""API / normalize tests driven by ``tests/golden/import_validation.yaml``."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from backend.app.schemas.portfolio_import import RawImportRow
from backend.app.services.portfolio_normalize import (
    ImportValidationError,
    normalize_import_rows,
)
from tests.golden.loader import case_ids, load_golden

_CASES = load_golden("import_validation")


@pytest.mark.parametrize("case", _CASES, ids=case_ids(_CASES))
def test_golden_import_validation(
    case: dict,
    api_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expect = case["expect"]
    inp = case.get("input") or {}

    # FastAPI contract cases (empty / missing payload).
    if "payload" in inp:
        resp = api_client.post(
            "/api/v1/portfolios/validate-import",
            json=inp["payload"],
        )
        assert resp.status_code == int(expect["http_status"])
        return

    rows_raw = inp.get("rows") or []
    rows = [RawImportRow(**r) for r in rows_raw]

    if expect.get("ok"):
        result = normalize_import_rows(rows)
        min_pos = int(expect.get("min_positions") or 1)
        assert len(result.positions) >= min_pos
        return

    if expect.get("raises") == "ImportValidationError":
        with pytest.raises(ImportValidationError) as exc_info:
            normalize_import_rows(rows)
        needle = expect.get("error_substring")
        if needle:
            assert needle in str(exc_info.value)

        # Optional API mapping check (domain error → 400).
        if expect.get("http_status") == 400:

            async def _boom(_payload):  # noqa: ANN001
                raise ImportValidationError(str(exc_info.value))

            monkeypatch.setattr(
                "backend.app.api.v1.portfolios.validate_import",
                _boom,
            )
            monkeypatch.setattr(
                "backend.app.api.v1.portfolios.write_application_event",
                AsyncMock(return_value=None),
            )
            resp = api_client.post(
                "/api/v1/portfolios/validate-import",
                json={"rows": rows_raw},
            )
            assert resp.status_code == 400
            detail = resp.json().get("detail") or resp.json()
            if needle:
                assert needle in str(detail)
        return

    pytest.fail(f"Case {case['id']}: unrecognized expect keys {sorted(expect)}")
