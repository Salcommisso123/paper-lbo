"""
Unit tests for the pure SEC-JSON parsing logic in edgar.py.

These run fully offline against a saved fixture (sample_data/sample_operating_income.json)
whose shape was verified against a real response from
data.sec.gov/api/xbrl/companyconcept/CIK0000320193/us-gaap/OperatingIncomeLoss.json
No network calls happen in this file.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from edgar import extract_annual_series  # noqa: E402

FIXTURE = Path(__file__).parent / "sample_data" / "sample_operating_income.json"


def load_fixture():
    return json.loads(FIXTURE.read_text())


def test_filters_out_quarterly_entries():
    data = load_fixture()
    series = extract_annual_series(data)
    fys = [e["fy"] for e in series]
    assert 2021 not in fys, "fy2021 only has a Q1 (10-Q) entry in the fixture and must be excluded"
    assert all(e["fp"] == "FY" for e in series)
    assert all(e["form"] in ("10-K", "10-K/A") for e in series)


def test_restatement_dedup_keeps_most_recently_filed():
    data = load_fixture()
    series = extract_annual_series(data)
    fy2020 = next(e for e in series if e["fy"] == 2020)
    # two competing FY2020 entries in the fixture: 10-K filed 2021-02-15 (val 1.9B)
    # and a 10-K/A filed later, 2021-06-01 (val 1.95B) — the later filing should win
    assert fy2020["val"] == 1_950_000_000
    assert fy2020["form"] == "10-K/A"


def test_results_sorted_ascending_by_fiscal_year():
    data = load_fixture()
    series = extract_annual_series(data)
    fys = [e["fy"] for e in series]
    assert fys == sorted(fys)
    assert fys == [2020, 2022, 2023, 2024]


def test_empty_or_missing_data_returns_empty_list():
    assert extract_annual_series(None) == []
    assert extract_annual_series({"units": {}}) == []
    assert extract_annual_series({"units": {"USD": []}}) == []


if __name__ == "__main__":
    test_filters_out_quarterly_entries()
    test_restatement_dedup_keeps_most_recently_filed()
    test_results_sorted_ascending_by_fiscal_year()
    test_empty_or_missing_data_returns_empty_list()
    print("All edgar parser tests passed.")
