# HeaterMeter + Home Assistant cookbook

Enable MQTT in **Settings -> Home Assistant (MQTT)** (broker host + credentials,
Save). The HeaterMeter device auto-appears in Home Assistant via MQTT discovery -
no YAML needed for the entities themselves. Everything is local; no cloud.

## Entities

| Entity | What it is |
|---|---|
| `sensor.heatermeter_pit_temp` (and Food 1/2, Ambient) | Live temperatures, in the board's unit |
| `sensor.heatermeter_fan_output` | Blower duty % |
| `number.heatermeter_setpoint` | Pit setpoint - **writable** from HA |
| `number.heatermeter_food_1_target` (food 2, ambient) | Food targets - **writable** |
| `binary_sensor.heatermeter_lid_open` | Lid-open detection |
| `binary_sensor.heatermeter_cook_stalled` | A food probe is in the evaporative stall |
| `binary_sensor.heatermeter_fuel_low` | Blower near its limit: add charcoal |
| `sensor.heatermeter_predicted_done` | Timestamp the soonest targeted food is predicted done |

Entity ids vary with your node id; check the HeaterMeter device page in HA.

## Recipes

Announce on speakers when the food reaches its target:

```yaml
automation:
  - alias: BBQ food done announcement
    trigger:
      - platform: numeric_state
        entity_id: sensor.heatermeter_food_1_temp
        above: input_number.bbq_target   # or a fixed value
    action:
      - service: tts.speak
        target: { entity_id: tts.home_assistant_cloud }
        data:
          media_player_entity_id: media_player.kitchen
          message: "The food is up to temperature."
```

Tell the house when the brisket stalls (and when it breaks):

```yaml
  - alias: BBQ stall started
    trigger:
      - platform: state
        entity_id: binary_sensor.heatermeter_cook_stalled
        to: "on"
    action:
      - service: notify.mobile_app_phone
        data: { message: "The cook has hit the stall. Wrap it or wait it out." }
```

Flash a light when fuel runs low:

```yaml
  - alias: BBQ fuel low
    trigger:
      - platform: state
        entity_id: binary_sensor.heatermeter_fuel_low
        to: "on"
    action:
      - service: light.turn_on
        target: { entity_id: light.porch }
        data: { flash: long }
```

Dinner-time heads-up from the prediction:

```yaml
  - alias: BBQ done in 30 minutes
    trigger:
      - platform: template
        value_template: >
          {{ states('sensor.heatermeter_predicted_done') not in ('unknown','unavailable')
             and (as_timestamp(states('sensor.heatermeter_predicted_done')) - now().timestamp()) < 1800 }}
    action:
      - service: notify.family
        data: { message: "Dinner in about 30 minutes." }
```

Drop the pit to keep-warm from a dashboard button (HA writes the setpoint):

```yaml
  - alias: BBQ keep warm
    trigger:
      - platform: state
        entity_id: input_boolean.bbq_keep_warm
        to: "on"
    action:
      - service: number.set_value
        target: { entity_id: number.heatermeter_setpoint }
        data: { value: 150 }
```

## Notes

- The temperature sensors intentionally carry no `device_class: temperature`, so
  HA displays the board's unit as-is instead of auto-converting it.
- `predicted_done` updates while a food probe has a target and is climbing; it
  reads `unknown` otherwise. Predictions during a stall are flagged
  low-confidence in the app and may hold steady until the stall breaks.
- All of these work offline on your LAN. If HA is down, the cook is unaffected.
