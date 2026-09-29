from datetime import datetime

import pytest

from jarvis.policy import PolicyContext, Principal, in_time_window

from conftest import ALEX, GUEST

CTX = PolicyContext(now=datetime(2026, 9, 26, 19, 0))


def test_r0_r1_allowed_for_household(policy):
    assert policy.evaluate(ALEX, "home.get_state", "home", "R0", {}, CTX).effect == "allow"
    assert policy.evaluate(ALEX, "home.set_light", "home", "R1", {}, CTX).effect == "allow"


def test_r2_allowed_without_taint_but_confirm_with_taint(policy):
    assert policy.evaluate(ALEX, "home.set_climate", "home", "R2", {}, CTX).effect == "allow"
    tainted = PolicyContext(tainted=True, now=CTX.now)
    decision = policy.evaluate(ALEX, "home.set_climate", "home", "R2", {}, tainted)
    assert (decision.effect, decision.rule_id, decision.method) == ("confirm", "risk.R2.tainted", "voice")


def test_r2_confirm_in_away_mode(policy):
    away = PolicyContext(mode="away", now=CTX.now)
    assert policy.evaluate(ALEX, "home.set_climate", "home", "R2", {}, away).effect == "confirm"


def test_r3_always_requires_strong_confirmation(policy):
    decision = policy.evaluate(ALEX, "home.lock", "home", "R3", {"state": "unlocked"}, CTX)
    assert decision.effect == "confirm"
    assert decision.method == "app_biometric"


def test_r4_is_always_denied_even_for_admin(policy):
    admin = Principal(actor="user:root", role="admin", trust="trusted_user")
    assert policy.evaluate(admin, "payment.transfer", "finance", "R4", {}, CTX).effect == "deny"


def test_low_voice_confidence_lowers_ceiling(policy):
    unsure = Principal(actor="user:alex", role="adult", trust="household", voice_confidence=0.6)
    assert policy.evaluate(unsure, "home.set_light", "home", "R1", {}, CTX).effect == "allow"
    decision = policy.evaluate(unsure, "home.set_climate", "home", "R2", {}, CTX)
    assert (decision.effect, decision.rule_id) == ("deny", "ceiling.household")


def test_guest_limited_to_allowlist_and_areas(policy):
    assert policy.evaluate(GUEST, "home.set_light", "home", "R1", {}, CTX).effect == "allow"
    assert policy.evaluate(GUEST, "home.lock", "home", "R1", {"state": "locked"}, CTX).rule_id == \
        "role.guest.not_allowed"
    in_office = Principal(actor="guest:unknown", role="guest", trust="guest", area="buero")
    assert policy.evaluate(in_office, "home.set_light", "home", "R1", {}, CTX).rule_id == "role.guest.area"


def test_external_untrusted_can_only_read(policy):
    hook = Principal(actor="service:whk_doorbell", role="service", trust="external_untrusted")
    assert policy.evaluate(hook, "home.get_state", "home", "R0", {}, CTX).effect == "allow"
    assert policy.evaluate(hook, "home.set_light", "home", "R1", {}, CTX).effect == "deny"


def test_intent_binding_blocks_foreign_domains(policy):
    research = PolicyContext(allowed_domains=frozenset({"web", "info"}), now=CTX.now)
    decision = policy.evaluate(ALEX, "home.set_light", "home", "R1", {}, research)
    assert (decision.effect, decision.rule_id) == ("deny", "intent_binding")


def test_child_night_rule(policy):
    child = Principal(actor="user:kim", role="child", trust="household", voice_confidence=0.95)
    night = PolicyContext(now=datetime(2026, 9, 26, 21, 15))
    day = PolicyContext(now=datetime(2026, 9, 26, 16, 0))
    assert policy.evaluate(child, "home.media_control", "home", "R1", {}, night).rule_id == \
        "child.no_media_at_night"
    assert policy.evaluate(child, "home.media_control", "home", "R1", {}, day).effect == "allow"


def test_explicit_confirm_rule_for_new_mail_recipient(policy):
    decision = policy.evaluate(ALEX, "mail.send", "comms", "R2", {"recipient_known": False}, CTX)
    assert (decision.effect, decision.rule_id, decision.method) == ("confirm", "mail.new_recipient", "app")


def test_explicit_rule_cannot_exceed_role_or_trust(policy):
    # Regel "automation.lock_at_night" (allow) greift, aber nur innerhalb der Obergrenzen
    automation = Principal(actor="automation:aut_night", role="adult", trust="system")
    decision = policy.evaluate(automation, "home.lock", "home", "R1", {"state": "locked"}, CTX)
    assert (decision.effect, decision.rule_id) == ("allow", "automation.lock_at_night")
    decision = policy.evaluate(automation, "home.lock", "home", "R3", {"state": "unlocked"}, CTX)
    assert decision.effect == "deny"  # system-Trust endet bei R2


@pytest.mark.parametrize(("hhmm", "window", "expected"), [
    ("23:00", "22:00-07:00", True),
    ("06:59", "22:00-07:00", True),
    ("07:00", "22:00-07:00", False),
    ("12:00", "09:00-17:00", True),
    ("17:00", "09:00-17:00", False),
])
def test_time_windows(hhmm, window, expected):
    assert in_time_window(datetime.strptime(hhmm, "%H:%M").time(), window) is expected
