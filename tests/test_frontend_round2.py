"""Round 2 front end and deploy checks (static: no database or browser).
   Static checks of the front-end files (CSS, JS, templates, logos, deploy scripts)."""
import os
import re
import struct
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
# the hosting files ship with the repository but not with the course submission zip
hosting = pytest.mark.skipif(not (ROOT / "deploy-vps.sh").exists(), reason="hosting files not in this copy")
CSS = (ROOT / "gui/static/app.css").read_text()
JS = (ROOT / "gui/static/app.js").read_text()


def png_size(p):
    with open(p, "rb") as f:
        head = f.read(24)
    return struct.unpack(">II", head[16:24])


@hosting
def test_l1_container_sends_secure_cookies_by_default():
    ep = (ROOT / "docker/entrypoint.sh").read_text()
    assert 'export VYTAL_SECURE_COOKIE="${VYTAL_SECURE_COOKIE:-1}"' in ep
    assert "VYTAL_SECURE_COOKIE=0" in (ROOT / "deploy-vps.sh").read_text()          # plain-HTTP first run only
    assert "-e VYTAL_SECURE_COOKIE=1" in (ROOT / "enable-https.sh").read_text()


@hosting
def test_l2_daily_reseed_at_four_toronto_time():
    d = (ROOT / "deploy-vps.sh").read_text()
    assert "OnCalendar=*-*-* 04:00:00 America/Toronto" in d and "docker restart -t 20 vytal" in d
    assert 'TZ=America/Toronto date +\\%H)" = 04' in d                               # cron fallback, % escaped for cron
    assert "vytal-reseed" in (ROOT / "DEMO.md").read_text()


@hosting
def test_shell_scripts_parse():
    for f in ["deploy-vps.sh", "enable-https.sh", "docker/entrypoint.sh"]:
        subprocess.run(["bash", "-n", str(ROOT / f)], check=True)


def test_l3_login_shortcut_uses_liams_real_code():
    t = (ROOT / "gui/templates/login.html").read_text()
    assert "D-1016" in t and "D-1001" not in t
    assert re.search(r"\(101, 'D-1016'", (ROOT / "sql/07_sample_data.sql").read_text())


def test_l4_l5_demo_table_stacks_and_never_splits_tokens():
    t = (ROOT / "gui/templates/_demo_accounts.html").read_text()
    assert "Password</th>" not in t and "<wbr>@" in t
    assert "@container (max-width:520px)" in CSS and "word-break:break-all" not in CSS


def test_rsp01_counts_keep_server_value():
    assert "count(el, !seen)" in JS and "if (reduce || end === 0 || still) { el.textContent = final; return; }" in JS
    assert "html:not(.js) [data-w]" in CSS and "html:not(.js) .ring .fg" in CSS


def test_rsp02_avatar_ink_follows_theme():
    rule = re.search(r"\.avatar\{[^}]*\}", CSS).group(0)
    assert "color:var(--on-red)" in rule and "#fff" not in rule


def test_rsp03_rsp04_tables():
    assert ".tablewrap:focus-visible" in CSS
    assert "@media(max-width:1279.98px){.col-opt{display:none!important}}" in CSS
    assert "position:sticky;right:0" in CSS and "more-r" in JS


def test_rsp09_theme_toggle_state():
    b = (ROOT / "gui/templates/base.html").read_text()
    assert 'id="theme"' in b and 'aria-pressed="false"' in b
    assert "tb.setAttribute('aria-pressed', String(d))" in JS


def test_rsp12_logos_are_small_two_x_assets():
    for name, max_bytes in [("vytal-logo-light.png", 12000), ("vytal-logo-dark.png", 16000)]:
        p = ROOT / "gui/static" / name
        w, h = png_size(p)
        assert p.stat().st_size < max_bytes and 160 <= w <= 300 and abs(w / h - 520 / 320) < .02
    m = (ROOT / "gui/templates/macros.html").read_text()
    assert 'width="168" height="103"' in m and 'width="280" height="172"' in m


def test_rsp15_table_controls_16px_on_small_screens():
    assert "@media(pointer:coarse),(max-width:1000px){input,select,textarea,table input,table select,table textarea{font-size:1rem}}" in CSS


def test_don13_ring_label_does_not_split_words():
    rule = re.search(r"\.ring \.in small\{[^}]*\}", CSS).group(0)
    assert "overflow-wrap:normal" in rule and "anywhere" not in rule


def test_stf03_filter_normalises_minus():
    assert "\\u2212" in JS and "norm(inp.value)" in JS
