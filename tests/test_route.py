from shuttle.route import RouteMap, RoutePoint, snapshot_from_odom, speed_at_distance


def _route() -> RouteMap:
  return RouteMap(
    name="t",
    loop=True,
    points=[
      RoutePoint(0.0, 1.0),
      RoutePoint(50.0, 3.0),
      RoutePoint(100.0, 2.0),
    ],
  )


def test_speed_interpolation() -> None:
  r = _route()
  assert abs(speed_at_distance(r, 25.0) - 2.0) < 1e-6
  assert abs(speed_at_distance(r, 75.0) - 2.5) < 1e-6


def test_route_wrap_for_loop() -> None:
  r = _route()
  snap = snapshot_from_odom(r, wheel_odom_m=130.0)
  assert abs(snap.route_distance_m - 30.0) < 1e-6
  assert 0.29 < snap.route_percent < 0.31
