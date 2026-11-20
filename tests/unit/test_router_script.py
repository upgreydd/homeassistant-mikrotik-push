"""Generated RouterOS setup script."""

import pytest

from mikrotik_push import router_script as script


def build(**overrides):
    options = {"host": "172.20.40.1", "ha_address": "172.20.40.41", "password": "pw-123"}
    return script.build_setup_script(**{**options, **overrides})


def test_user_is_limited_to_the_ha_address():
    assert "address=172.20.40.41/32" in build()


def test_ipv6_ha_address_gets_a_host_prefix_of_128_bits():
    assert "address=fd00::5/128" in build(ha_address="fd00::5")


def test_no_address_clause_when_the_ha_address_is_unknown():
    assert "address=" not in build(ha_address=None)


def test_group_grants_exactly_read_and_api():
    text = build()
    assert text.count("policy=read,api") == 2  # add and update
    assert "policy=read,api," not in text and "!" not in text


def test_default_names_do_not_take_over_a_common_existing_account():
    assert script.DEFAULT_USERNAME not in ("home-assistant", "admin", "ha")


def test_rerunning_the_script_updates_the_existing_group_and_user():
    text = build()
    assert "else={ /user group set [/user group find" in text
    assert "else={ /user set [/user find" in text


def test_existing_api_ssl_certificate_is_only_replaced_when_our_own_is_created():
    lines = build().splitlines()
    certificate_line = next(line for line in lines if "/certificate sign" in line)
    assert "/ip service set api-ssl certificate=" in certificate_line  # inside the :if block
    assert not any(
        line.startswith("/ip service set api-ssl certificate") for line in lines
    )


def test_server_certificate_is_signed_by_a_local_ca_that_is_signed_first():
    line = next(l for l in build().splitlines() if "/certificate sign" in l)
    ca_signed = line.index('/certificate sign "mikrotik-push-ca"')
    leaf_signed = line.index('/certificate sign "mikrotik-push-api" ca="mikrotik-push-ca"')
    assert ca_signed < leaf_signed
    assert 'find name="mikrotik-push-api" private-key=yes' in line  # a failed attempt is redone


def test_password_appears_only_inside_the_user_commands():
    assert build().count("pw-123") == 2


@pytest.mark.parametrize(
    "host",
    ['a"b', "a b", "a;b", "a$b", "", "a\nb", "router\n", "192.168.88.1:8729", "-a", "a..b"],
)
def test_unsafe_host_is_refused(host):
    assert not script.is_safe_host(host)
    with pytest.raises(ValueError):
        build(host=host)


@pytest.mark.parametrize("host", ["172.20.40.1", "router.lan", "fe80::1", "my-router", "R1"])
def test_plain_hosts_are_accepted(host):
    assert script.is_safe_host(host)


@pytest.mark.parametrize("value", ['p"w', "p w", "p$w", "p[w", "p\\w", "", "pw\n"])
def test_password_and_username_with_special_characters_are_refused(value):
    with pytest.raises(ValueError):
        build(password=value)
    with pytest.raises(ValueError):
        build(username=value)


def test_generated_passwords_are_long_unique_and_script_safe():
    passwords = {script.generate_password() for _ in range(50)}
    assert len(passwords) == 50
    assert all(len(p) >= 20 and script.is_plain(p) for p in passwords)
