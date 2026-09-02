"""The design-system builders are pure; these pin what they promise."""
from __future__ import annotations
from dashboard.theme import (hero_html, money, pct, positions_table_html, sleeve_card_html,
                             sleeve_cards_html, sparkline_svg)


class TestFormatting:
    def test_money_and_pct_use_a_real_minus_sign(self):
        assert money(-1234.5, signed=True) == "−$1,234.50"
        assert money(1234.5, signed=True) == "+$1,234.50"
        assert pct(-0.43) == "−0.43%" and pct(0.5) == "+0.50%"


class TestSparkline:
    def test_fewer_than_two_points_renders_a_message_not_a_broken_path(self):
        assert "Session data arrives" in sparkline_svg([]) and "<path" not in sparkline_svg([1.0])

    def test_a_series_renders_one_line_and_a_prior_close_hairline(self):
        svg = sparkline_svg([100.0, 101.0, 99.5, 102.0], baseline=100.5)
        assert svg.count("<path") == 2 and "PRIOR CLOSE" in svg

    def test_colour_follows_last_point_versus_baseline_not_first_point(self):
        assert "#6fd39a" in sparkline_svg([100, 90, 101], baseline=100.5)
        assert "#f08b86" in sparkline_svg([100, 110, 100.2], baseline=100.5)


class TestHero:
    def test_hero_carries_both_figures_and_the_status(self):
        h = hero_html(equity=99589.63, today=572.07, today_pct=0.58, since=-410.37, since_pct=-0.41,
                      market_open=True, status_text="Market open", clock_text="15:48 ET",
                      spark_svg="<svg></svg>", session_low=99100.0, session_high=99700.0)
        assert "$99,589.63" in h and "+$572.07" in h and "−$410.37" in h
        assert "Since $100,000 start" in h and "pa-dot" in h and "pa-dot--off" not in h
        assert "Low $99,100.00" in h

    def test_status_text_is_escaped(self):
        assert "<script>" not in hero_html(equity=1, today=0, today_pct=0, since=0, since_pct=0, market_open=False,
                                           status_text="<script>x</script>", clock_text="", spark_svg="", session_low=None, session_high=None)


class TestSleeveCards:
    def test_over_budget_is_flagged_and_the_bar_is_capped(self):
        c = sleeve_card_html("CSP", 0.15, 14865.0, 20250.0, "active")
        assert "Over by $5,385.00" in c and "pa-bar--over" in c and "width:100%" in c

    def test_retired_and_armed_statuses(self):
        assert "Retired" in sleeve_card_html("Vampire", 0.0, 0.0, 0.0, "retired")
        assert "Armed, no signal yet" in sleeve_card_html("Pendulum", 0.15, 14865.0, 0.0, "armed")

    def test_four_cards_in_a_band(self):
        html = sleeve_cards_html([dict(name="A", target_pct=0.5, budget=1.0, used=0.5, status="active")] * 4)
        assert html.count('class="pa-card"') == 4 and "Where the money is" in html


class TestLedger:
    ROWS = [
        {"sleeve": "SixFold", "symbol": "AAPL", "qty": 15, "entry": 315.2, "last": 325.0, "pl": 147.0, "plpc": 3.1, "mv": 4875.0},
        {"sleeve": "SixFold", "symbol": "MSFT", "qty": 9, "entry": 511.8, "last": 500.0, "pl": -106.0, "plpc": -2.3, "mv": 4500.0},
        {"sleeve": "CSP", "symbol": "NIO260911P00004000", "qty": -2, "entry": 0.12, "last": 0.10, "pl": 4.0, "plpc": 16.0, "mv": -20.0},
    ]

    def test_groups_by_sleeve_sorted_by_pnl_with_totals(self):
        h = positions_table_html(self.ROWS)
        assert h.index("AAPL") < h.index("MSFT") and h.index("SixFold") < h.index("CSP")
        assert "+$45.00" in h and "$9,355.00" in h   # totals

    def test_symbols_are_escaped_and_empty_is_handled(self):
        assert "&lt;b&gt;" in positions_table_html([dict(self.ROWS[0], symbol="<b>")])
        assert "No open positions" in positions_table_html([])


class TestDashboardAllocatorUsesTheConfiguredSplit:
    def test_get_allocator_reads_strategies_yml_not_the_dataclass_defaults(self):
        """The sleeve cards showed CSP 'of $79,644' (the legacy 80% default)
        and SIXFOLD 'no budget' because the dashboard built
        AllocationConfig() bare. from_config() is the only correct call."""
        import inspect, sys
        sys.argv = ["streamlit", "run"]
        import dashboard.app as app
        assert "AllocationConfig.from_config()" in inspect.getsource(app.get_allocator)
