"""Per-request capability for the bounded writer recovery adapters."""

WAIT_CAPABILITY = "writer-wait-budget-v1"


def confirms_not_executed(response):
    """Only the new explicit contract settles zero writes; old 503s do not."""
    if response is None or response.status_code != 503:
        return False
    try:
        body = response.json()
    except ValueError:
        return False
    return (isinstance(body, dict) and body.get('code') == 'mutation_busy'
            and body.get('write_outcome') == 'not_executed')


def headers(api):
    """Advertise only on adapters whose whole-request timeout covers the cap."""
    result = api._client.headers.copy()
    values = {value.strip() for value in result.get("X-DRadar-Capabilities", "").split(",") if value.strip()}
    values.add(WAIT_CAPABILITY)
    result["X-DRadar-Capabilities"] = ",".join(sorted(values))
    return result
