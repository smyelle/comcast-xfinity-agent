# A routed call, seeded on arrival — driven live on `gemini-composite-v1`

Everything below is a real transcript from a deployed app. Offline tests prove the router
gets a table entry; only a live drive proves the caller is not asked for something the
call arrived with, because the failure is not an error — it is one extra, perfectly
reasonable question.

## The two apps

```
cd packages/flows
PYTHONPATH=src python -m examples.variable_map_routed            # treatment
PYTHONPATH=src python -m examples.variable_map_routed --control  # A/B control
```

Both validate clean (`0 errors, 2 warnings` — the shared retry counter, unrelated). The
arms are the SAME app: same flows, same map, same cues, same model. The control has the
router's entry stripped back out of the emitted table, which is exactly the table every
routed app used to ship.

```
cxas push --app-dir variable_map_routed_app --project-id ces-deployment-dev \
          --location us --display-name "Variable map routed TREATMENT"
cxas push --app-dir variable_map_routed_control_app --project-id ces-deployment-dev \
          --location us --display-name "Variable map routed CONTROL"
```

| arm | app |
|---|---|
| treatment | `8d507bbb-ad4d-4ce6-8e7d-cb4ddd5f03b6` |
| control | `71ea7bc1-39c7-4bb1-a31a-ff72a6aa81e9` |

The build prints which configs the table reaches, so the arms are distinguishable before
either is pushed:

```
  arm: TREATMENT                          arm: CONTROL
  billing      ['by_account']             billing      ['by_account']
  front_desk   ['by_account']  <- router  tracking     ['by_account']
  tracking     ['by_account']             front_desk   (no entry)
```

## The A/B — one turn, one question apart

Both arms open the session with `accountNumber = "A-4471"` and the same line.

**Treatment** — session `9f330cc2-945c-4595-a05e-95abe0d006bb`

```
caller: my parcel hasn't turned up          [arrives with accountNumber=A-4471]
agent : Could you please provide your tracking number?
```

The router inherited `by_account`, ingress fired before routing, `account_number` was
already filled when `tracking` went live — so the flow asked for the only thing it still
needed.

**Control** — session `c19254f4-c8b4-45e3-b000-12e1c1b77879`

```
caller: my parcel hasn't turned up          [arrives with accountNumber=A-4471]
agent : Could you please provide your account number?
```

The number is in the session. The ingress ran, saw `front_desk` as the active config,
found no entry for it, and returned. The map for `tracking` was sitting in the same table
the whole time.

## Why the router needed the entry

Ingress runs as `before_agent_callbacks_00pre` — deliberately ahead of config resolution
— and keys the table on `state["_active_config_id"] or state["default_config_id"]`. On a
router app that is the ROUTER, and a router holds no user slots, so projection kept
nothing for it and it got no entry at all.

The maps were never unreachable forever: `_active_config_id` names a real flow once the
route lands, so the ingress would fire a turn or more later. That is the quiet part — the
value does arrive, just after the flow has already asked for it. Which is the one thing a
variable map exists to prevent.

A router is now given the shapes its flows declare: each authored map once, at its
authored position, carrying the widest projection any flow gave it. The router cannot
know which flow will run, and a binding for a slot that flow turns out not to hold is
inert.

`HostRouter` apps were never affected — a host is not a config, so `default_config_id`
already named a real flow.

## Offline

```
PYTHONPATH=src pytest tests/test_variable_map.py -q
```

62 tests, three of them new: a single-agent router inherits its flows' shapes, a
single-flow app is unchanged, and a router holding a slot of its own keeps the projection
it earned. The first fails without the fix.
