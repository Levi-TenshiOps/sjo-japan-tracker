"""Reading .env, which for a long time nothing did.

setup_tracker.py wrote the file, .env.example documented it, the README
explained it - and config.py only ever read os.environ. Every run ended at
"SMTP credentials missing" regardless of what the file said.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from tracker.config import load_dotenv


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ("SMTP_USER", "SMTP_PASSWORD", "SMTP_HOST", "SMTP_PORT",
              "NTFY_TOPIC", "ALERT_EMAIL", "QUOTED", "SPACED"):
        monkeypatch.delenv(k, raising=False)


def write(tmp_path, text):
    p = tmp_path / ".env"
    p.write_text(text, encoding="utf-8")
    return p


class TestLoading:
    def test_simple_pairs_reach_the_environment(self, tmp_path):
        n = load_dotenv(write(tmp_path, "SMTP_USER=a@b.com\nSMTP_PORT=587\n"))
        assert n == 2
        assert os.environ["SMTP_USER"] == "a@b.com"
        assert os.environ["SMTP_PORT"] == "587"

    def test_missing_file_is_zero_not_an_error(self, tmp_path):
        assert load_dotenv(tmp_path / "absent") == 0

    def test_comments_and_blank_lines_are_skipped(self, tmp_path):
        load_dotenv(write(tmp_path, "# a note\n\nSMTP_USER=x@y.com\n\n# end\n"))
        assert os.environ["SMTP_USER"] == "x@y.com"

    def test_a_trailing_comment_is_stripped(self, tmp_path):
        """.env.example ships exactly this shape."""
        load_dotenv(write(tmp_path, "SMTP_PORT=587      # STARTTLS\n"))
        assert os.environ["SMTP_PORT"] == "587"

    def test_quotes_are_removed(self, tmp_path):
        load_dotenv(write(tmp_path, 'QUOTED="hello world"\n'))
        assert os.environ["QUOTED"] == "hello world"

    def test_a_hash_inside_quotes_survives(self, tmp_path):
        """App Passwords are alphanumeric, but a real password may not be."""
        load_dotenv(write(tmp_path, 'SMTP_PASSWORD="pa#ss word"\n'))
        assert os.environ["SMTP_PASSWORD"] == "pa#ss word"

    def test_export_prefix_is_tolerated(self, tmp_path):
        load_dotenv(write(tmp_path, "export SMTP_USER=z@z.com\n"))
        assert os.environ["SMTP_USER"] == "z@z.com"

    def test_surrounding_whitespace_is_trimmed(self, tmp_path):
        load_dotenv(write(tmp_path, "  SPACED  =   value   \n"))
        assert os.environ["SPACED"] == "value"

    def test_lines_without_an_equals_are_ignored(self, tmp_path):
        assert load_dotenv(write(tmp_path, "nonsense\nSMTP_USER=a@b.com\n")) == 1

    def test_a_real_env_var_outranks_the_file(self, tmp_path, monkeypatch):
        """A scheduled job or shell export must still be able to override."""
        monkeypatch.setenv("SMTP_USER", "from-shell@x.com")
        load_dotenv(write(tmp_path, "SMTP_USER=from-file@x.com\n"))
        assert os.environ["SMTP_USER"] == "from-shell@x.com"

    def test_a_password_containing_equals_is_kept_whole(self, tmp_path):
        load_dotenv(write(tmp_path, "SMTP_PASSWORD=ab=cd=ef\n"))
        assert os.environ["SMTP_PASSWORD"] == "ab=cd=ef"


class TestConfigActuallyPicksItUp:
    def test_credentials_flow_through_to_the_config(self, tmp_path, monkeypatch):
        """The end-to-end failure this was written for."""
        from tracker.config import load
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text(
            "SMTP_USER=me@gmail.com\nSMTP_PASSWORD=abcdefghijklmnop\n"
            "SMTP_HOST=smtp.gmail.com\nSMTP_PORT=587\n", encoding="utf-8")
        (tmp_path / "config.yaml").write_text("origins: [SJO]\n", encoding="utf-8")
        cfg = load("config.yaml")
        assert cfg.smtp_user == "me@gmail.com"
        assert cfg.smtp_password == "abcdefghijklmnop"
        assert cfg.smtp_port == 587
