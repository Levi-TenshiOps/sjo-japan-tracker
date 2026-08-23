"""Config parsing and its refusal to accept unsafe settings."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import date
import pytest, yaml

from tracker.config import Config, ConfigError, load, upcoming_pairs

BASE = {
    "origins": ["SJO"], "destinations": ["NRT"],
    "date_pairs": [{"depart": "2027-02-10", "return": "2027-02-24"}],
}


def write(tmp_path, data):
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(data))
    return p


class TestLoading:
    def test_basic(self, tmp_path):
        cfg = load(write(tmp_path, BASE), use_env=False)
        assert cfg.origins == ["SJO"]
        assert cfg.date_pairs == [(date(2027, 2, 10), date(2027, 2, 24))]

    def test_defaults_hubs_when_absent(self, tmp_path):
        cfg = load(write(tmp_path, BASE), use_env=False)
        assert "MEX" in cfg.hubs and "ZRH" in cfg.hubs

    def test_free_tier_narrows_hubs(self, tmp_path):
        cfg = load(write(tmp_path, {**BASE, "hub_tier": "FREE"}), use_env=False)
        assert "LHR" not in cfg.hubs and "MEX" in cfg.hubs

    def test_list_form_date_pairs(self, tmp_path):
        cfg = load(write(tmp_path, {**BASE,
                   "date_pairs": [["2027-02-10", "2027-02-24"]]}), use_env=False)
        assert cfg.date_pairs[0][1] == date(2027, 2, 24)

    def test_codes_uppercased(self, tmp_path):
        cfg = load(write(tmp_path, {**BASE, "origins": ["sjo"],
                   "destinations": ["nrt"]}), use_env=False)
        assert cfg.origins == ["SJO"] and cfg.destinations == ["NRT"]

    def test_missing_file_falls_back_to_defaults(self, tmp_path):
        """Travel dates now come from preferences.json, so a bare config is
        valid on its own."""
        cfg = load(tmp_path / "absent.yaml", use_env=False)
        assert cfg.origins == ["SJO"] and cfg.hubs

    def test_new_coverage_and_alert_fields(self, tmp_path):
        cfg = load(write(tmp_path, BASE), use_env=False)
        assert cfg.hot_list_size > 0 and 0 < cfg.hot_share <= 1
        assert cfg.min_drop_usd > 0
        assert cfg.throttle_file and cfg.rotation_file


class TestValidation:
    def test_rejects_banned_hub(self, tmp_path):
        with pytest.raises(ConfigError, match="deny list"):
            load(write(tmp_path, {**BASE, "hubs": ["MEX", "DFW"]}), use_env=False)

    def test_rejects_banned_destination(self, tmp_path):
        with pytest.raises(ConfigError, match="deny list"):
            load(write(tmp_path, {**BASE, "destinations": ["LAX"]}), use_env=False)

    def test_rejects_great_above_good(self, tmp_path):
        with pytest.raises(ConfigError, match="must not exceed"):
            load(write(tmp_path, {**BASE, "good_price_usd": 1000,
                       "great_price_usd": 1500}), use_env=False)

    def test_rejects_return_before_departure(self, tmp_path):
        with pytest.raises(ConfigError, match="not after"):
            load(write(tmp_path, {**BASE,
                 "date_pairs": [{"depart": "2027-02-24", "return": "2027-02-10"}]}),
                 use_env=False)

    def test_rejects_bad_date(self, tmp_path):
        with pytest.raises(ConfigError, match="bad date"):
            load(write(tmp_path, {**BASE, "date_pairs": ["10/02/2027"]}),
                 use_env=False)

    def test_rejects_absurd_max_stops(self, tmp_path):
        with pytest.raises(ConfigError, match="max_stops"):
            load(write(tmp_path, {**BASE, "max_stops": 9}), use_env=False)


class TestEnvOverrides:
    def test_email_and_secrets(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ALERT_EMAIL", "me@example.com")
        monkeypatch.setenv("SMTP_USER", "bot@example.com")
        monkeypatch.setenv("SMTP_PASSWORD", "app-password")
        cfg = load(write(tmp_path, BASE))
        assert cfg.alert_email == "me@example.com"
        assert cfg.smtp_configured

    def test_threshold_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GOOD_PRICE_USD", "1200")
        assert load(write(tmp_path, BASE)).good_price_usd == 1200

    def test_not_configured_without_secrets(self, tmp_path, monkeypatch):
        for k in ("ALERT_EMAIL", "SMTP_USER", "SMTP_PASSWORD"):
            monkeypatch.delenv(k, raising=False)
        assert not load(write(tmp_path, BASE)).smtp_configured


class TestUpcoming:
    def test_filters_past(self):
        pairs = [(date(2026, 1, 1), date(2026, 1, 10)),
                 (date(2027, 2, 10), date(2027, 2, 24))]
        assert len(upcoming_pairs(pairs, date(2026, 8, 22))) == 1
