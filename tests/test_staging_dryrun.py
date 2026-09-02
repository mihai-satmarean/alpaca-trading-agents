"""Tests for staging environment and dry-run mode (alpaca_client.py)."""

from __future__ import annotations

import os
from unittest.mock import patch, MagicMock

import pytest

from src.core.alpaca_client import (
    AlpacaClient,
    AlpacaConfig,
    load_config,
    _dry_run_order,
)
from alpaca.trading.enums import OrderSide


class TestLoadConfig:
    def test_production_default(self):
        with patch.dict(os.environ, {
            "ALPACA_API_KEY": "PROD_KEY",
            "ALPACA_SECRET_KEY": "PROD_SECRET",
        }, clear=False):
            cfg = load_config()
            assert cfg.api_key == "PROD_KEY"
            assert cfg.environment == "production"

    def test_staging_via_arg(self):
        with patch.dict(os.environ, {
            "ALPACA_STAGING_API_KEY": "STG_KEY",
            "ALPACA_STAGING_SECRET_KEY": "STG_SECRET",
        }, clear=False):
            cfg = load_config(staging=True)
            assert cfg.api_key == "STG_KEY"
            assert cfg.environment == "staging"

    def test_staging_via_env_var(self):
        with patch.dict(os.environ, {
            "ALPACA_ENV": "staging",
            "ALPACA_STAGING_API_KEY": "STG_KEY2",
            "ALPACA_STAGING_SECRET_KEY": "STG_SECRET2",
        }, clear=False):
            cfg = load_config()
            assert cfg.api_key == "STG_KEY2"
            assert cfg.environment == "staging"

    def test_staging_missing_keys_raises(self):
        env = {k: v for k, v in os.environ.items()
               if "ALPACA_STAGING" not in k}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(ValueError, match="Staging requested"):
                load_config(staging=True)


class TestDryRunOrder:
    def test_returns_dict_with_dry_run_status(self):
        result = _dry_run_order("SPY", 10.0, OrderSide.BUY, "market")
        assert result["symbol"] == "SPY"
        assert result["status"] == "dry_run"
        assert result["qty"] == "10.0"
        assert "id" in result

    def test_includes_limit_price_when_provided(self):
        result = _dry_run_order("AAPL", 5.0, OrderSide.SELL, "limit", 150.0)
        assert result["limit_price"] == "150.0"


class TestDryRunClient:
    @patch("src.core.alpaca_client.TradingClient")
    @patch("src.core.alpaca_client.StockHistoricalDataClient")
    @patch("src.core.alpaca_client.StockDataStream")
    def test_market_order_not_submitted(self, mock_stream, mock_data, mock_trading):
        cfg = AlpacaConfig(api_key="K", secret_key="S", paper=True)
        client = AlpacaClient(config=cfg, dry_run=True)
        assert client.is_dry_run

        result = client.market_order("SPY", 10, OrderSide.BUY)
        assert result["status"] == "dry_run"
        mock_trading.return_value.submit_order.assert_not_called()

    @patch("src.core.alpaca_client.TradingClient")
    @patch("src.core.alpaca_client.StockHistoricalDataClient")
    @patch("src.core.alpaca_client.StockDataStream")
    def test_limit_order_not_submitted(self, mock_stream, mock_data, mock_trading):
        cfg = AlpacaConfig(api_key="K", secret_key="S", paper=True)
        client = AlpacaClient(config=cfg, dry_run=True)

        result = client.limit_order("AAPL", 5, OrderSide.BUY, 150.0)
        assert result["status"] == "dry_run"
        mock_trading.return_value.submit_order.assert_not_called()

    @patch("src.core.alpaca_client.TradingClient")
    @patch("src.core.alpaca_client.StockHistoricalDataClient")
    @patch("src.core.alpaca_client.StockDataStream")
    def test_submit_order_not_submitted(self, mock_stream, mock_data, mock_trading):
        from alpaca.trading.requests import LimitOrderRequest
        from alpaca.trading.enums import TimeInForce

        cfg = AlpacaConfig(api_key="K", secret_key="S", paper=True)
        client = AlpacaClient(config=cfg, dry_run=True)
        req = LimitOrderRequest(
            symbol="JPM", qty=2, side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY, limit_price=200.0,
        )
        result = client.submit_order(req)
        assert result["status"] == "dry_run"
        mock_trading.return_value.submit_order.assert_not_called()

    @patch("src.core.alpaca_client.TradingClient")
    @patch("src.core.alpaca_client.StockHistoricalDataClient")
    @patch("src.core.alpaca_client.StockDataStream")
    def test_close_position_not_called(self, mock_stream, mock_data, mock_trading):
        cfg = AlpacaConfig(api_key="K", secret_key="S", paper=True)
        client = AlpacaClient(config=cfg, dry_run=True)

        result = client.close_position("SPY")
        assert result is None
        mock_trading.return_value.close_position.assert_not_called()

    @patch("src.core.alpaca_client.TradingClient")
    @patch("src.core.alpaca_client.StockHistoricalDataClient")
    @patch("src.core.alpaca_client.StockDataStream")
    def test_read_operations_work_normally(self, mock_stream, mock_data, mock_trading):
        cfg = AlpacaConfig(api_key="K", secret_key="S", paper=True)
        client = AlpacaClient(config=cfg, dry_run=True)

        client.get_account()
        mock_trading.return_value.get_account.assert_called_once()

        client.get_positions()
        mock_trading.return_value.get_all_positions.assert_called_once()


class TestEnvironmentProperty:
    @patch("src.core.alpaca_client.TradingClient")
    @patch("src.core.alpaca_client.StockHistoricalDataClient")
    @patch("src.core.alpaca_client.StockDataStream")
    def test_environment_from_config(self, mock_stream, mock_data, mock_trading):
        cfg = AlpacaConfig(api_key="K", secret_key="S", paper=True, environment="staging")
        client = AlpacaClient(config=cfg)
        assert client.environment == "staging"


class TestCoordinatorWiresStaging:
    def test_passes_staging_and_dry_run_into_alpaca_client(self):
        cfg = AlpacaConfig(
            api_key="STGKEY", secret_key="STGSEC", paper=True, environment="staging"
        )
        config_obj = MagicMock()
        config_obj.validate.return_value = []
        config_obj.vampire_symbols = ["SPY"]
        config_obj.vampire_paused_until = None
        config_obj.pendulum_pct = 0
        config_obj.pendulum_symbol = "TLT"
        config_obj.pendulum = {}

        with (
            patch("src.agents.coordinator.load_config", return_value=cfg) as mock_load,
            patch("src.agents.coordinator.AlpacaClient") as mock_client,
            patch("src.agents.coordinator.MarketDataService"),
            patch("src.agents.coordinator.PositionTracker"),
            patch("src.agents.coordinator.get_config", return_value=config_obj),
            patch("src.agents.coordinator.CircuitBreaker"),
            patch("src.agents.coordinator.AllocationManager"),
            patch("src.agents.coordinator.OptionsIncomeAgent"),
            patch("src.agents.coordinator.VampireAgent"),
            patch("src.agents.coordinator.PendulumAgent"),
            patch("src.agents.coordinator.RiskManagerAgent"),
            patch("src.agents.coordinator.SixfoldAnalystAgent"),
            patch("src.agents.coordinator.SixfoldExecutor"),
        ):
            mock_client.return_value.environment = "staging"
            mock_client.return_value.is_dry_run = True
            from src.agents.coordinator import Coordinator

            Coordinator(staging=True, dry_run=True)
            mock_load.assert_called_once_with(staging=True)
            mock_client.assert_called_once()
            assert mock_client.call_args.kwargs["dry_run"] is True
            assert mock_client.call_args.kwargs["config"] is cfg


class TestRunLiveFlags:
    def _load_run_live(self):
        import importlib.util
        from pathlib import Path

        path = Path("scripts/run_live.py")
        spec = importlib.util.spec_from_file_location("run_live_under_test", path)
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(mod)
        return mod

    def test_parse_args_defaults_to_contest_account(self):
        args = self._load_run_live().parse_args([])
        assert args.staging is False
        assert args.dry_run is False

    def test_parse_args_staging_and_dry_run(self):
        args = self._load_run_live().parse_args(["--staging", "--dry-run"])
        assert args.staging is True
        assert args.dry_run is True


class TestRunStagingScript:
    def test_refuses_contest_fallback_and_requires_confirm(self):
        from pathlib import Path

        src = Path("scripts/run_staging.sh").read_text()
        assert "ALPACA_ENV=staging" in src
        assert "unset SNS_TOPIC_ARN" in src
        assert "CONFIRM_STAGING_LIVE" in src
        assert "ALPACA_STAGING_API_KEY" in src
        assert "will not fall back to the contest" in src
        assert "100.101.239.56:30400" in src
        assert "OPENAI_BASE_URL" in src
        assert "DECISION_LOG" in src
        assert "observe" in src
