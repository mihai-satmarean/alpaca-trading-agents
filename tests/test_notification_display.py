"""2026-09-03, Frank: the Notifications tab had no unread indicator and its
table didn't show a notification's actual content. Two independent fixes:
a per-session unread badge on the tab label, and a hand-authored details/
summary row list (theme.notification_rows_html) where hovering the summary
shows the full message via a native title= tooltip and clicking expands it
in place -- reusing the pattern already proven for "Currently on ntfy",
without going through Streamlit's own expander (and its icon font)."""

from __future__ import annotations

from dashboard.app import _unread_notification_count
from dashboard.theme import notification_rows_html


def _entry(ts, **kw):
    return {"ts": ts, "title": "t", "message": "m", "severity": "default",
            "delivered": True, **kw}


class TestUnreadCount:
    def test_a_fresh_session_opens_at_zero_even_with_history(self):
        """The whole journal existed before this viewer ever loaded the page;
        none of it is "unread" in any useful sense."""
        journal = [_entry("2026-09-03T09:45:00+00:00"), _entry("2026-09-03T09:30:00+00:00")]
        session = {}
        assert _unread_notification_count(journal, session) == 0

    def test_an_empty_journal_is_zero(self):
        assert _unread_notification_count([], {}) == 0

    def test_an_entry_that_arrives_after_the_baseline_counts(self):
        session = {}
        first = [_entry("2026-09-03T09:30:00+00:00")]
        assert _unread_notification_count(first, session) == 0        # sets baseline
        grown = [_entry("2026-09-03T09:45:00+00:00")] + first          # a new one, newest-first
        assert _unread_notification_count(grown, session) == 1

    def test_the_baseline_is_set_once_not_on_every_call(self):
        """A second call with an unchanged journal must not re-baseline to
        the newest entry and silently erase a real unread count."""
        session = {}
        journal = [_entry("2026-09-03T09:30:00+00:00")]
        _unread_notification_count(journal, session)
        grown = [_entry("2026-09-03T09:45:00+00:00")] + journal
        assert _unread_notification_count(grown, session) == 1
        assert _unread_notification_count(grown, session) == 1, "second call changed the count"

    def test_mark_all_as_read_matches_how_the_button_resets_the_baseline(self):
        session = {}
        journal = [_entry("2026-09-03T09:45:00+00:00"), _entry("2026-09-03T09:30:00+00:00")]
        assert _unread_notification_count(journal, session) == 0
        newer = [_entry("2026-09-03T10:00:00+00:00")] + journal
        assert _unread_notification_count(newer, session) == 1
        session["notif_seen_before"] = newer[0]["ts"]                  # the button's own action
        assert _unread_notification_count(newer, session) == 0

    def test_uses_the_real_streamlit_session_state_by_default(self):
        """The default parameter must be st.session_state, not a private
        module-level dict -- otherwise every viewer would share one counter."""
        import inspect
        sig = inspect.signature(_unread_notification_count)
        assert sig.parameters["session_state"].default is None


class TestNotificationRowsHtml:
    def test_empty_list_does_not_crash(self):
        assert "No notifications" in notification_rows_html([])

    def test_each_row_is_a_native_details_element(self):
        html = notification_rows_html([{"when": "09-03 09:45", "severity": "default",
                                        "title": "FIRST CYCLE", "message": "body text",
                                        "via": "ntfy", "delivered": True}])
        assert html.count("<details") == 1 and html.count("</details>") == 1
        assert "<summary" in html

    def test_the_hover_tooltip_carries_the_full_message(self):
        """The whole point: mouse-over shows content via a native title attr,
        no JS required, works even if the summary line itself is truncated."""
        html = notification_rows_html([{"when": "x", "severity": "default", "title": "Short",
                                        "message": "A much longer message body that would "
                                                   "never fit in a table cell.", "via": "ntfy",
                                        "delivered": True}])
        assert 'title="Short -- A much longer message' in html

    def test_the_expanded_body_carries_the_full_message_too(self):
        """Click, not just hover, must also reveal the content -- the summary
        line alone is not the fix; both paths the user asked for must work."""
        html = notification_rows_html([{"when": "x", "severity": "high", "title": "t",
                                        "message": "UNIQUE_BODY_MARKER_12345", "via": "ntfy",
                                        "delivered": False, "error": "timeout"}])
        assert "UNIQUE_BODY_MARKER_12345" in html
        assert "pa-notif-body" in html

    def test_severity_maps_to_a_distinct_css_class(self):
        high = notification_rows_html([{"when": "x", "severity": "high", "title": "t",
                                        "message": "m", "via": "ntfy", "delivered": True}])
        low = notification_rows_html([{"when": "x", "severity": "low", "title": "t",
                                       "message": "m", "via": "ntfy", "delivered": True}])
        assert "pa-chip--sev-high" in high
        assert "pa-chip--sev-low" in low

    def test_a_failed_delivery_says_so_in_the_row_and_the_body(self):
        html = notification_rows_html([{"when": "x", "severity": "high", "title": "t",
                                        "message": "m", "via": "ntfy", "delivered": False,
                                        "error": "connection reset"}])
        assert "connection reset" in html

    def test_html_in_a_message_is_escaped_not_injected(self):
        """A message body is operator-controlled today, but an untrusted or
        unusual message must never become a live tag in the page."""
        html = notification_rows_html([{"when": "x", "severity": "default", "title": "t",
                                        "message": "<script>alert(1)</script>", "via": "ntfy",
                                        "delivered": True}])
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_a_missing_message_body_says_so_rather_than_rendering_blank(self):
        html = notification_rows_html([{"when": "x", "severity": "default", "title": "t",
                                        "message": "", "via": "ntfy", "delivered": True}])
        assert "no message body" in html
