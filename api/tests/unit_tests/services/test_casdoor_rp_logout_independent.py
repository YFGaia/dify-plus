"""Independent RP logout regressions; synthetic offline wires only."""

from test_casdoor_rp_logout_service_extend import source_owner

pytest_plugins = ("test_casdoor_rp_logout_service_extend",)


def test_each_retry_keeps_the_original_expiry_and_invalidates_the_previous_handoff(rp):
    owner, account, cookie = source_owner(rp)
    prepared = owner.prepare_logout(
        account_id=str(account), refresh_token="original-request", opaque=cookie.value
    )
    assert prepared is not None
    first = owner.complete_local_logout(prepared)
    assert first is not None
    grant_id, retry_browser = first.cookies[1].value, first.cookies[2].value
    previous = first

    for elapsed in (1, 2, 3):
        rp.clock[0] += elapsed
        current = rp.service.retry(opaque=grant_id, browser_cookie=retry_browser)
        assert current is not None
        assert current.cookies[0].max_age < previous.cookies[0].max_age
        assert (
            rp.service.navigate(
                opaque=previous.handoff_path.rsplit("/", 1)[1],
                browser_cookie=previous.cookies[0].value,
            )
            is None
        )
        previous = current

    rp.clock[0] += 1000
    assert rp.service.retry(opaque=grant_id, browser_cookie=retry_browser) is None
