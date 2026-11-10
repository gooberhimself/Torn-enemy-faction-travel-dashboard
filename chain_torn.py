"""Small v2 adapter, invoked only by the application's existing poll loop."""
import time

import requests


class TornUnavailable(Exception):
    pass


def get(api_key, path, params):
    try:
        response = requests.get('https://api.torn.com/v2/' + path,
                                headers={'Authorization': 'ApiKey ' + api_key}, params=params, timeout=20)
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError):
        # requests exceptions can contain credential-bearing URLs; never publish them.
        raise TornUnavailable('Torn API request failed; manual coordination remains available.') from None
    if not isinstance(data, dict):
        raise TornUnavailable('Unexpected Torn API response.')
    if 'error' in data:
        code = (data.get('error') or {}).get('code', 'unknown')
        guidance = {
            7: ('Faction data access denied. Ask a faction leader to check that the API key '
                "owner's faction position has API access permission. The key owner must "
                'belong to the configured friendly faction. Manual coordination remains available.'),
            16: ('API key access level is insufficient. Attack verification needs a Limited '
                 'access key or a custom key allowing faction attacks; the key owner also '
                 'needs faction API permission. Manual coordination remains available.'),
        }
        message = guidance.get(code, 'Torn rejected the request; manual coordination remains available.')
        raise TornUnavailable(f'Torn API error {code}. {message}')
    return data


def chain_observation(data, received_at):
    observed = data.get('timestamp')
    chain = data.get('chain')
    if type(observed) is not int or observed <= 0 or observed > received_at+5 or not isinstance(chain, dict):
        raise TornUnavailable('Chain observation has no valid authoritative timestamp.')
    if any(type(chain.get(k)) is not int or chain[k] < 0 for k in ('current', 'timeout', 'cooldown', 'start')):
        raise TornUnavailable('Chain observation contains invalid timer values.')
    if chain['timeout'] > 300:
        raise TornUnavailable('Unexpected chain timeout; countdown unavailable.')
    phase = ('cooldown' if chain['cooldown'] > observed else
             'inactive' if chain['current'] == 0 else
             'warmup' if chain['current'] < 10 else
             'active' if chain['timeout'] > 0 else 'awaiting_confirmation')
    return {'observed_at': observed, 'received_at': received_at, 'current': chain['current'],
            'phase': phase, 'deadline': observed+chain['timeout'] if chain['timeout'] > 0 else None,
            'cooldown_until': chain['cooldown'], 'start': chain['start']}


def poll(coordinator, api_key, friendly_id, verify=True):
    # Do not request data that cannot safely be applied to this database.
    with coordinator.db() as db:
        coordinator.queue(db)
    try:
        data = get(api_key, 'faction', {'selections': 'chain,timestamp', 'id': friendly_id})
        coordinator.observe('chain', chain_observation(data, time.time()))
    except TornUnavailable as exc:
        coordinator.observe('chain', error=str(exc))
    if not verify:
        coordinator.observe('attacks', error='Automatic verification disabled; use manual completion.')
        return
    try:
        data = get(api_key, 'faction', {'selections': 'attacks,timestamp', 'filters': 'outgoing', 'sort': 'DESC', 'limit': 100})
        observed = data.get('timestamp')
        if type(observed) is not int or observed <= 0 or observed > time.time()+5:
            raise TornUnavailable('Attack history has no valid authoritative timestamp.')
        attacks = data.get('attacks')
        if not isinstance(attacks, list):
            raise TornUnavailable('Unexpected attack history response.')
        factions = {str(((a.get('attacker') or {}).get('faction') or {}).get('id'))
                    for a in attacks if isinstance(a, dict) and a.get('attacker')}
        if factions and factions != {str(friendly_id)}:
            raise TornUnavailable('Attack API key does not belong to the configured friendly faction.')
        coordinator.reconcile(attacks)
        coordinator.observe('attacks', {'observed_at': observed, 'records': len(attacks)},
                            error='Latest 100 attacks only; older records may require manual review.' if len(attacks) == 100 else None)
    except TornUnavailable as exc:
        coordinator.observe('attacks', error=str(exc))
