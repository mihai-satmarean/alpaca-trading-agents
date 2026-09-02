"""Which Alpaca paper book a process is talking to."""

from src.core.account_identity import describe_broker_account


class TestDescribeBrokerAccount:
    def test_staging_key_is_the_isolated_laptop_book(self):
        d = describe_broker_account("staging", "PKPIWG", "PA310V54AWBY")
        assert d["book"] == "STAGING"
        assert d["tone"] == "ok"
        assert "PA310V54AWBY" in d["detail"]
        assert "PKPIWG" in d["detail"]
        assert "CONTEST" not in d["headline"]

    def test_contest_key_is_never_mistaken_for_staging(self):
        d = describe_broker_account("staging", "PK2UEW", "PA-CONTEST")
        assert d["book"] == "CONTEST"
        assert d["tone"] == "danger"
        assert "Stop" in d["detail"]

    def test_missing_account_number_still_names_the_book(self):
        d = describe_broker_account("staging", "PKPIWG", "")
        assert d["book"] == "STAGING"
        assert "--" in d["detail"]
