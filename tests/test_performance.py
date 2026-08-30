"""Performance-engine tests: win rate, profit factor, streaks, breakdowns (§21-22)."""
import pytest

from scanner.config import Config
from scanner.performance import compute_performance

from conftest import make_signal, T0, MS_15M


@pytest.fixture()
def cfg():
    return Config.load()


def _book():
    return [
        make_signal(id="SIG-1", symbol="BTCUSDT", status="WIN", rMultiple=3.0,
                    triggeredAt=T0, closedAt=T0 + 6 * MS_15M, generatedAt=T0),
        make_signal(id="SIG-2", symbol="ETHUSDT", direction="SHORT", status="WIN",
                    rMultiple=2.6, triggeredAt=T0 + MS_15M, closedAt=T0 + 5 * MS_15M,
                    generatedAt=T0),
        make_signal(id="SIG-3", symbol="BTCUSDT", status="LOSS", rMultiple=-1.0,
                    triggeredAt=T0 + 2 * MS_15M, closedAt=T0 + 4 * MS_15M,
                    generatedAt=T0),
        make_signal(id="SIG-4", symbol="SOLUSDT", status="LOSS", rMultiple=-1.0,
                    triggeredAt=T0 + 3 * MS_15M, closedAt=T0 + 4 * MS_15M,
                    generatedAt=T0),
        make_signal(id="SIG-5", symbol="SOLUSDT", status="EXPIRED", generatedAt=T0),
        make_signal(id="SIG-6", symbol="ADAUSDT", status="AMBIGUOUS",
                    triggeredAt=T0, closedAt=T0 + 2 * MS_15M, generatedAt=T0),
        make_signal(id="SIG-7", symbol="ADAUSDT", status="WAITING_TRIGGER",
                    generatedAt=T0),
    ]


class TestWinRate:
    def test_denominator_excludes_unresolved(self, cfg):
        perf = compute_performance(_book(), cfg, T0 + 100 * MS_15M)
        assert perf["wins"] == 2 and perf["losses"] == 2
        assert perf["expired"] == 1 and perf["ambiguous"] == 1
        assert perf["winRate"] == 50.0
        assert perf["resolvedTrades"] == 4

    def test_zero_resolved_gives_none_not_zero(self, cfg):
        perf = compute_performance([make_signal(id="SIG-x", status="WAITING_TRIGGER")],
                                   cfg, T0)
        assert perf["winRate"] is None

    def test_profit_factor(self, cfg):
        perf = compute_performance(_book(), cfg, T0 + 100 * MS_15M)
        gross_win = 3.0 + 2.6
        gross_loss = 2.0
        assert perf["profitFactor"] == pytest.approx(gross_win / gross_loss, abs=0.01)

    def test_streaks(self, cfg):
        perf = compute_performance(_book(), cfg, T0 + 100 * MS_15M)
        assert perf["maxWinningStreak"] == 2
        assert perf["maxLosingStreak"] == 2

    def test_avg_durations(self, cfg):
        perf = compute_performance(_book(), cfg, T0 + 100 * MS_15M)
        # wins: 6 candles and 4 candles; losses: 1 candle and 1 candle
        assert perf["avgTimeToTpMs"] == pytest.approx((6 + 4) / 2 * MS_15M)
        assert perf["avgTimeToSlMs"] == pytest.approx((2 + 1) / 2 * MS_15M)

    def test_breakdowns_exist(self, cfg):
        perf = compute_performance(_book(), cfg, T0 + 100 * MS_15M)
        for key in ("byDirection", "byQuality", "byRegime", "bySymbol", "byScoreRange"):
            assert key in perf and perf[key]
        assert perf["bySymbol"]["BTCUSDT"]["wins"] == 1
        assert perf["byDirection"]["SHORT"]["wins"] == 1

    def test_date_window_filters(self, cfg):
        book = _book()
        later = make_signal(id="SIG-8", symbol="XRPUSDT", status="LOSS", rMultiple=-1.0,
                            triggeredAt=T0 + 50 * MS_15M, closedAt=T0 + 51 * MS_15M,
                            generatedAt=T0 + 50 * MS_15M)
        perf = compute_performance(book + [later], cfg, T0 + 100 * MS_15M,
                                   since_ms=T0 + 10 * MS_15M)
        assert perf["total"] == 1 and perf["losses"] == 1

    def test_disclaimer_present(self, cfg):
        perf = compute_performance([], cfg, T0)
        assert "not a probability" in perf["disclaimer"]
