"""Obstacle avoidance: drive forward, and turn in place while something is too close."""

from nervlynx import node


@node(inputs=["range.front"], outputs="cmd.drive", rate_hz=20, max_age_s=0.5)
def avoid(front, *, state, stop_m=0.4, clear_m=0.7, cruise=0.45, turn=0.6):
  distance = front["distance_m"]
  # Start turning below stop_m, keep turning until the way ahead is clear past clear_m.
  if state.get("turning"):
    state["turning"] = distance < clear_m
  else:
    state["turning"] = distance < stop_m
  if state["turning"]:
    return {"linear": 0.0, "angular": turn}
  return {"linear": cruise, "angular": 0.0}
