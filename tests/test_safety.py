from shuttle.safety import SafetySupervisor


def test_transitions_to_mrs_on_critical() -> None:
  sup = SafetySupervisor(caution_ttl_s=2.0, mrs_hold_s=5.0)
  now = 1_000_000_000
  sup.observe_fault("critical", "stale routeProgress", now)
  snap = sup.tick(now)
  assert snap.mode == "mrs"
  assert snap.emergency_stop is True


def test_recovers_to_normal_after_holds() -> None:
  sup = SafetySupervisor(caution_ttl_s=2.0, mrs_hold_s=5.0)
  t0 = 1_000_000_000
  sup.observe_fault("critical", "something", t0)
  _ = sup.tick(t0)

  # After MRS hold it should go to caution first.
  snap = sup.tick(t0 + 6_000_000_000)
  assert snap.mode == "caution"

  # After caution TTL and no new faults it returns to normal.
  snap = sup.tick(t0 + 8_500_000_000)
  assert snap.mode == "normal"
  assert snap.emergency_stop is False
