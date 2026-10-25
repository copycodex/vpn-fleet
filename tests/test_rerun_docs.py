"""Keep operator instructions aligned with wizard-generated identities."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_identity_documentation():
    for name in ("GUIDE.md", "GUIDE_RU.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "node_<node>_reality_public_key" in text
        assert 'reality_private_key: "REPLACE"' not in text
        assert 'reality_short_id: "REPLACE"' not in text
