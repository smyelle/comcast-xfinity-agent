"""One app, three journeys, reached by name instead of by remembered variables.

This delivery line says something different depending on what the upstream platform
already knew when the call arrived: a tracking id, and the state the parcel is in.
Both are `event_slot`s, so they are prefilled before the agent speaks and the caller
is never asked for either. That is the good version for the caller and the annoying
version for whoever has to TEST it — none of these journeys can be reached by
talking, only by seeding the session before the first turn.

`cujs.yaml` next to this module names the bundles, so a journey is a name:

    flows cujs                              # what is available
    flows chat --cuj parcel_delayed --app <APP_ID>

The bundles seed `event_data`, because that is where the engine reads event-sourced
slots from — not the top-level session variables. A CUJ variable holding a mapping
stays a mapping unless the file lists it under `querystring_variables`, which is why
`event_data` survives as the object the engine expects.

Nothing here is specific to delivery. Any app whose interesting paths depend on
upstream state has the same problem, and gets the same fix.

Build + validate offline:
    python -m examples.named_cujs           # emits ./named_cujs_app + its cujs.yaml

Drive it live, which is the only way to reach these paths:
    cxas push --app-dir ./named_cujs_app --display-name acme-delivery
    cd named_cujs_app && flows chat --cuj parcel_delayed --app <APP_ID>
"""

import os

import flows

CUJS = """\
version: 1

variable_aliases:
  parcel: [event_data]

cujs:
  parcel_on_time:
    description: On the van, arriving today. The everything-worked path.
    variables:
      parcel: {tracking_id: AC-40219, parcel_state: out_for_delivery}

  parcel_delayed:
    description: Held up in transit, with a new date to offer.
    variables:
      parcel: {tracking_id: AC-40219, parcel_state: delayed}

  parcel_missing:
    description: No scan for six days, so the agent opens a claim.
    variables:
      parcel: {tracking_id: AC-40219, parcel_state: missing}
"""

delivery = flows.Flow("delivery_status", root_agent="Delivery_Agent")

delivery.add(
    flows.event_slot("tracking_id"),
    flows.event_slot("parcel_state"),
    flows.announce(
        "on_time",
        texts=[
            "Parcel {tracking_id} is on the van and arrives today before 8 PM."
        ],
        preempt=True,
        requires=["tracking_id"],
        condition=flows.eq("parcel_state", "out_for_delivery"),
        end=True,
    ),
    flows.announce(
        "delayed",
        texts=[
            "Parcel {tracking_id} is held up in transit and now arrives"
            " Thursday. I'm sorry about that."
        ],
        preempt=True,
        requires=["tracking_id"],
        condition=flows.eq("parcel_state", "delayed"),
        end=True,
    ),
    flows.announce(
        "missing",
        texts=[
            "Parcel {tracking_id} hasn't scanned in six days, so I've opened a"
            " claim and you'll hear from us within two working days."
        ],
        preempt=True,
        requires=["tracking_id"],
        condition=flows.eq("parcel_state", "missing"),
        end=True,
    ),
)

app = flows.App(
    root_flow=delivery,
    app_display_name="Acme Delivery Status",
    variables=[
        {
            "name": "event_data",
            "description": "Upstream call event: tracking id and parcel state.",
            "schema": {"type": "OBJECT"},
        },
    ],
)


if __name__ == "__main__":
  errors, warnings = flows.validate_app(app)
  for w in warnings:
    print("warn:", w)
  for e in errors:
    print("ERROR:", e)
  print(f"validate: {len(errors)} errors, {len(warnings)} warnings")
  if not errors:
    out = "./named_cujs_app"
    flows.build_app(app, out)
    with open(os.path.join(out, "cujs.yaml"), "w") as fh:
      fh.write(CUJS)
    print(f"built: {out}")
    for cuj in flows.load_cujs(os.path.join(out, "cujs.yaml")):
      print(f"  {cuj.name:16} {cuj.variables}")
