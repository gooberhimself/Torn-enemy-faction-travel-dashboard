/* Pure countdown helpers shared with the Node tests. All timestamps are UTC seconds. */
(function (root) {
  const valid = value => typeof value === 'number' && Number.isFinite(value) && value > 0;
  const remaining = (deadline, now) => valid(deadline) && valid(now) ? Math.max(0, Math.ceil(deadline - now)) : null;
  const duration = seconds => seconds === null ? '—' : `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
  const warning = (seconds, thresholds) => {
    if (seconds === null) return 'Unavailable';
    if (seconds === 0) return 'Expired estimate — awaiting confirmation';
    if (seconds <= thresholds.critical) return 'Critical';
    if (seconds <= thresholds.urgent) return 'Urgent';
    if (seconds <= thresholds.attack) return 'Attack window';
    if (seconds <= thresholds.prepare) return 'Prepare';
    return 'Normal';
  };
  const hospital = (member, now) => {
    if (!member || member.status !== 'Hospital') return '';
    const left = remaining(member.hospital_until, now);
    return left === null ? 'Hospital · release time unavailable' : left === 0 ?
      'Expected Release — Awaiting Confirmation' : `Hospital · estimated release in ${duration(left)}`;
  };
  const stale = (obs, now, limit) => !obs || !valid(obs.observed_at) || Boolean(obs.error) || now - obs.observed_at > limit;
  const api = {remaining, duration, warning, hospital, stale};
  if (typeof module !== 'undefined') module.exports = api;
  else root.ChainTime = api;
})(typeof window === 'undefined' ? globalThis : window);
