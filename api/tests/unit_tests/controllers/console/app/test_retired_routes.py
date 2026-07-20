from controllers.console import console_ns


def test_unsafe_passport_extend_route_is_not_registered() -> None:
    urls = {url for _resource, resource_urls, _route_doc, _kwargs in console_ns.resources for url in resource_urls}

    assert "/passport-extend" not in urls
