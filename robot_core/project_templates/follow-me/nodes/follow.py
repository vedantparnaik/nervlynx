"""Follow a person: turn towards them, and drive closer until they fill enough of the view."""

from nervlynx import node


@node(inputs=["detections.front"], outputs="cmd.drive", rate_hz=20, max_age_s=0.5)
def follow(seen, *, ctx, state, target_height=0.6, cruise=0.5, turn_gain=1.8, max_turn=0.8, search_turn=0.4, search_s=6.0):
  now = ctx.now_ns / 1e9
  people = [d for d in seen["detections"] if d["label"] == "person"]
  if not people:
    # Lost them: for a few seconds, turn towards the side they were last seen on.
    if "last_x" in state and now - state["seen_at"] < search_s:
      return {"linear": 0.0, "angular": search_turn if state["last_x"] < 0.5 else -search_turn}
    return {"linear": 0.0, "angular": 0.0}
  person = max(people, key=lambda d: d["size"][1])  # the tallest box is the closest person
  x, height = person["center"][0], person["size"][1]
  state["last_x"], state["seen_at"] = x, now
  # Positive angular turns left; a person left of centre has x below 0.5.
  angular = max(-max_turn, min(max_turn, turn_gain * (0.5 - x)))
  # Full speed when they look small, slowing to a stop as they reach target_height.
  gap = max(0.0, target_height - height) / target_height
  linear = cruise * min(1.0, 2.0 * gap)
  if abs(0.5 - x) > 0.25:
    linear *= 0.3  # mostly turn when they are far off to one side
  return {"linear": linear, "angular": angular}
