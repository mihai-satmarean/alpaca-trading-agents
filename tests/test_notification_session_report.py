"""2026-09-03: two failures in the notification detail view, found by Frank
live, in order.

First, every row rendered as a bare "Details" with no visible content at
all. Root cause: st.markdown(..., unsafe_allow_html=True) still runs its
content through markdown's HTML-block parser first, and that parser treats
a BLANK LINE as the end of a raw HTML block -- even one sitting inside an
attribute or a <div> we are still in the middle of writing. Every real
notify() message is multi-paragraph, so every row broke at its first blank
line, and everything after it (including our own closing markup for that
row) rendered as literal text instead of a real DOM element. The bug never
showed up in the original tests because they used single-line synthetic
messages; a blank line was never exercised. TestNoBlankLineSurvivesRendering
below pins the actual mechanism, not just a symptom, against real fixtures.

Second, even with the corruption gone, the raw content was notify()'s own
markdown built for a phone notification -- **bold** markers, indented plain-
text symbol lists, an em-dash-joined "8 position(s)" line. Frank: "the
details ... are all cryptic text (JSON like format)". parse_session_report
+ session_report_html typeset that shape instead of dumping it: stat tiles
for Equity/Cash/Day P&L, a card per sleeve with its used/budget/pct chip and
position symbols as pills, working orders as a list, any trailing sentence
as plain prose. Fixtures below are verbatim messages read from the box's own
logs/notify.jsonl on 2026-09-03, not invented examples.
"""

from __future__ import annotations

from dashboard.theme import (
    notification_rows_html, parse_session_report, session_report_html,
)

FIRST_CYCLE = (
    "_Fifteen minutes in: what the first scan did, and what it refused._\n\n"
    "**Equity** $99,907.49  |  **Cash** $29,839.54\n**Day P&L** +$344.40\n\n"
    "**CSP (options)** $20,250 / $14,986 (135%)\n  unrealized -$110.00 \xb7 8 position(s)\n"
    "    CLF260918P00011500\n    KO261002C00093000\n    MARA260911P00010000\n    MARA260918P00010000\n"
    "    NIO260911P00004000\n    NIO260918P00004000\n**Vampire** $0 / $4,995 (0%)\n"
    "  unrealized +$0.00 \xb7 0 position(s)\n**SixFold (Tashi)** $63,980 / $74,931 (85%)\n"
    "  unrealized +$843.49 \xb7 13 position(s)\n    AAPL\n    ABBV\n    AMZN\n    COST\n    GOOGL\n    HD\n"
    "**Unattributed** $7,025\n  unrealized +$7.60 \xb7 2 position(s)\n    HRB\n    UTHR\n\n"
    "**Working orders** (3)\n  FHI buy 60 @ 61.87 \xb7 new\n  FHI buy 61 @ 61.18 \xb7 new\n  HRB buy 74 @ 50.60 \xb7 new"
)

OPEN = (
    "_Market open. Agents live._\n\n**Equity** $99,446.14  |  **Cash** $36,856.68\n**Day P&L** -$116.95\n\n"
    "**CSP (options)** $20,250 / $14,917 (136%)\n  unrealized -$453.00 \xb7 8 position(s)\n"
    "    CLF260918P00011500\n    KO261002C00093000\n    MARA260911P00010000\n    MARA260918P00010000\n"
    "    NIO260911P00004000\n    NIO260918P00004000\n**Vampire** $0 / $4,972 (0%)\n"
    "  unrealized +$0.00 \xb7 0 position(s)\n**SixFold (Tashi)** $63,870 / $74,585 (86%)\n"
    "  unrealized +$733.72 \xb7 13 position(s)\n    AAPL\n    ABBV\n    AMZN\n    COST\n    GOOGL\n    HD\n\n"
    "---\nThe system placed no new trades this cycle, resulting in a daily loss of $116.95."
)

SESSION_STARTING = "SixFold $74,728 \xb7 CSP $14,946 \xb7 Vampire $4,982 \xb7 Buffer $0\n\nEquity $99,636.68"

WATCHDOG_ALERT = ("Vampire above its sleeve\n\nExposure $12,340 against a $4,995 sleeve "
                  "(2.5x). Containment at 1.5x.\n\nHalted TQQQ, cancelled resting orders.")


class TestNoBlankLineSurvivesRendering:
    """The actual root cause, pinned directly: nothing this function emits
    may contain a blank line, on pain of exactly the corruption Frank saw."""

    def test_real_multiparagraph_messages_produce_no_blank_line(self):
        rows = [
            {"when": "x", "severity": "default", "title": "FIRST CYCLE", "message": FIRST_CYCLE,
             "via": "ntfy", "delivered": True},
            {"when": "x", "severity": "default", "title": "OPEN", "message": OPEN,
             "via": "ntfy", "delivered": False, "error": "timeout"},
            {"when": "x", "severity": "high", "title": "WATCHDOG", "message": WATCHDOG_ALERT,
             "via": "ntfy", "delivered": True},
        ]
        html = notification_rows_html(rows)
        assert "\n\n" not in html, "a blank line in the output is what broke every row live"
        assert html.count("<details") == html.count("</details") == 3

    def test_a_title_attribute_never_contains_a_raw_newline(self):
        """A single embedded \\n would not itself break the CommonMark rule
        (that needs a blank line), but it is still not a real single-line
        tooltip, and a stray one is worth catching on its own."""
        import re
        rows = [{"when": "x", "severity": "default", "title": "t", "message": FIRST_CYCLE,
                "via": "ntfy", "delivered": True}]
        html = notification_rows_html(rows)
        m = re.search(r'title="([^"]*)"', html)
        assert m and "\n" not in m.group(1)


class TestParseSessionReport:
    def test_the_lead_sentence_is_extracted_without_its_underscores(self):
        p = parse_session_report(FIRST_CYCLE)
        assert p["lead"] == "Fifteen minutes in: what the first scan did, and what it refused."

    def test_the_three_headline_stats(self):
        p = parse_session_report(OPEN)
        assert p["stats"] == {"Equity": "$99,446.14", "Cash": "$36,856.68", "Day P&L": "-$116.95"}

    def test_every_sleeve_is_found_in_order(self):
        p = parse_session_report(FIRST_CYCLE)
        assert [s["name"] for s in p["sleeves"]] == ["CSP (options)", "Vampire", "SixFold (Tashi)", "Unattributed"]

    def test_a_sleeves_symbol_list_stops_at_the_next_sleeve(self):
        """The bug this guards: SixFold's block used to run all the way into
        Unattributed's own lines because Unattributed has no budget/pct and
        the splitter had nothing to recognize as its boundary."""
        p = parse_session_report(FIRST_CYCLE)
        by_name = {s["name"]: s for s in p["sleeves"]}
        assert by_name["SixFold (Tashi)"]["symbols"] == ["AAPL", "ABBV", "AMZN", "COST", "GOOGL", "HD"]
        assert by_name["Unattributed"]["symbols"] == ["HRB", "UTHR"]

    def test_a_sleeve_without_a_budget_has_no_percentage(self):
        p = parse_session_report(FIRST_CYCLE)
        unattributed = next(s for s in p["sleeves"] if s["name"] == "Unattributed")
        assert unattributed["budget"] is None and unattributed["pct"] is None
        assert unattributed["used"] == "7,025"

    def test_a_sleeve_with_a_budget_has_its_percentage_as_an_int(self):
        p = parse_session_report(FIRST_CYCLE)
        csp = next(s for s in p["sleeves"] if s["name"] == "CSP (options)")
        assert csp["pct"] == 135 and csp["used"] == "20,250" and csp["budget"] == "14,986"

    def test_zero_position_sleeves_have_an_empty_symbol_list_not_a_crash(self):
        p = parse_session_report(FIRST_CYCLE)
        vampire = next(s for s in p["sleeves"] if s["name"] == "Vampire")
        assert vampire["symbols"] == [] and vampire["position_count"] == 0

    def test_working_orders_are_extracted_with_their_count(self):
        p = parse_session_report(FIRST_CYCLE)
        assert len(p["working_orders"]) == 3
        assert p["working_orders"][0] == "FHI buy 60 @ 61.87 \xb7 new"

    def test_a_message_with_no_working_orders_section_has_none(self):
        p = parse_session_report(OPEN)
        assert p["working_orders"] is None

    def test_the_trailing_sentence_after_the_rule_is_captured(self):
        p = parse_session_report(OPEN)
        assert p["trailer"] == "The system placed no new trades this cycle, resulting in a daily loss of $116.95."

    def test_a_message_with_no_trailer_has_none(self):
        p = parse_session_report(FIRST_CYCLE)
        assert p["trailer"] is None

    def test_a_message_without_equity_does_not_match(self):
        """The fallback contract: anything that is not this shape returns
        None so the caller renders it plainly instead of forcing a parse."""
        assert parse_session_report(WATCHDOG_ALERT) is None
        assert parse_session_report(SESSION_STARTING) is None
        assert parse_session_report("") is None


class TestSessionReportHtml:
    def test_renders_a_stat_tile_per_headline_number(self):
        html = session_report_html(parse_session_report(FIRST_CYCLE))
        assert html.count("pa-notif-stat__v") == 3
        assert "$99,907.49" in html and "+$344.40" in html

    def test_day_pl_gets_a_color_class_matching_its_sign(self):
        pos = session_report_html(parse_session_report(FIRST_CYCLE))   # +$344.40
        neg = session_report_html(parse_session_report(OPEN))          # -$116.95
        assert '"pa-notif-stat__v pos"' in pos
        assert '"pa-notif-stat__v neg"' in neg

    def test_an_over_cap_sleeve_gets_the_high_severity_chip(self):
        html = session_report_html(parse_session_report(OPEN))         # CSP at 136%
        assert "pa-chip--sev-high" in html

    def test_position_symbols_render_as_individual_pills_not_a_text_blob(self):
        html = session_report_html(parse_session_report(FIRST_CYCLE))
        assert html.count('class="pa-notif-sym"') == 6 + 6 + 2   # CSP + SixFold + Unattributed
        assert '<span class="pa-notif-sym">AAPL</span>' in html

    def test_working_orders_render_as_a_list(self):
        html = session_report_html(parse_session_report(FIRST_CYCLE))
        assert "<li>FHI buy 60 @ 61.87 \xb7 new</li>" in html

    def test_the_trailer_renders_as_plain_prose(self):
        html = session_report_html(parse_session_report(OPEN))
        assert "pa-notif-trailer" in html and "daily loss of $116.95" in html

    def test_no_markdown_asterisks_or_underscores_survive_into_the_output(self):
        """The literal complaint: the old render showed **Equity** and
        _lead text_ with the markup characters still visible."""
        html = session_report_html(parse_session_report(FIRST_CYCLE))
        assert "**" not in html
        assert not html.startswith("_") and "_Fifteen" not in html


class TestTheRowEntryPointTypesetsSessionReports:
    """Through notification_rows_html itself, not the renderer in isolation:
    the mutation that skipped the parser in _notification_body_html survived
    every other test here because none of them crossed that dispatch."""

    def test_a_session_report_row_expands_to_stat_tiles_not_markdown(self):
        rows = [{"when": "x", "severity": "default", "title": "FIRST CYCLE", "message": FIRST_CYCLE,
                "via": "ntfy", "delivered": True}]
        html = notification_rows_html(rows)
        assert "pa-notif-stat__v" in html and "pa-notif-sleeve" in html
        body = html.split('class="pa-notif-body">', 1)[1]
        assert "**" not in body and "_Fifteen" not in body


class TestFallbackForNonSessionReportMessages:
    def test_a_watchdog_alert_still_renders_readable_plain_text(self):
        rows = [{"when": "x", "severity": "high", "title": "WATCHDOG", "message": WATCHDOG_ALERT,
                "via": "ntfy", "delivered": True}]
        html = notification_rows_html(rows)
        assert "Halted TQQQ" in html
        assert "\n\n" not in html

    def test_the_short_session_starting_ping_still_renders(self):
        rows = [{"when": "x", "severity": "default", "title": "session starting",
                "message": SESSION_STARTING, "via": "ntfy", "delivered": True}]
        html = notification_rows_html(rows)
        assert "Equity $99,636.68" in html
