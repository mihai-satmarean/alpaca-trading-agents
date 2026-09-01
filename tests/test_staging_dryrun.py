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
