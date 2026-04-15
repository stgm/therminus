# Thermostat control modes

Four actors are relevant:

- **The heat pump** itself is installed outside. It does
  quite a bit of control: based on the requested flow temperatures for heating
  and the hot water temperature, it can decide to start heating the water tank.
  If no thermostat is connected, it also provides a heat curve and it will
  accept a desired room temperature. But it does not have access to actual
  room temperature measurements, so there is no feedback loop. It can work
  well in a very limited heating system and a well-tuned heat curve setting.
  Other than that, the pump also does frost protection and circulation pump
  management.
- **The controller** is either built into a full unit that switches
  heating and hot water circuits, or it is a small standalone that contains
  a lot of eletrical connections to control valves and read temperatures.
  So it provides a connection to indoor hydraulics for the heat pump.
  It can be used to set basic operating parameters, for example at which
  outside temperature the heating mode will be switched off entirely.
  When no wall thermostat is connected, this unit also exposes parameters
  such as the desired hot water tank temperature or the room temperature
  target. Although it is then the main interface to the owner, it is still
  the heat pump outside that performs most of the control
  logic, like converting the target temperature + measured outside temperature
  to a fitting circuit flow temperature (using heat curve tables).
- **The thermostat** is an optional wall-mounted room controller. It
  measures room temperature, can maintain a schedule, raise the hot water
  temperature to control legionella growth. When active, it directly controls
  by setting circuit water temperature targets (for example, somewhere around
  30º when using underfloor heating) and the heatpump only modulates to
  reach that temperature. As it has a built-in temperature sensor, it can
  control the resulting room temperature more precisely than just through
  a theoretical heating curve. However, because the heat pump can take over
  the most basic functionality, the thermostat is not required.
- **Your own application** runs on a computer connected to the ebus interface.
  It can read state from both the pump and the controller, and
  write control values directly, bypassing or supplementing the thermostat.

Each control mode assigns these actors different responsibilities.

## 1. Vaillant thermostat is fully in control

Hardware present: pump + controller + wall thermostat.

This is the "normal" way of using the heat pump. The thermostat owns the room
control loop: it reads the room, applies the heat curve, sends the desired
circuit flow temperature to the pump. The pump then modulates the compressor
to get there.

Your application can intervene at the margins by setting base parameters
that are normally set using system menus:

- Raise MinFlowTemp to extend a run beyond what the thermostat would allow,
  e.g. to restore floor warmth after a long rest period.

So the application really only nudges. Notably, if you try to set parameters
that the thermostat owns, it will overwrite these again in short notice.

## 2. No thermostat, your app controls virtual room target

Hardware present: pump + controller.

When not using a room thermostat, the controller allows setting a basic
room temperature, which defaults to 20ºC. This is theoretical, because
there is no sensor in the room the measure the effects of the heating
system. However, with a carefully tuned heat curve, one might find that
the heating systems works reasonably well.

Often, the installer sets the heat curve in the advanced menus of
the controller unit. The owner can nudge by setting the desired
room temperature, which directly results in changes in the heating
circuit temperature. The owner can also set the desired temperature
for the hot water storage tank. The heat pump switches to making hot
water automatically as soon as the tank drops below a reasonable
temperature. However, there is no automated legonella program without
the thermostat, so the manual strongly encourages to keep the water
tank at 60ºC.

The setting of a virtual room temperature also provides a first point of
control for applications that are connected via eBus: you can set this
target temperature by sending a `TargetTempHc` message. The pump then
runs the heating curve, computes the flow temperature, and manages the
compressor accordingly.

But there is a lot more an application can do when the thermostat is not present:

- **Proportional control**: use the same algorithm as the manufacturer room
  thermostat by writing `setpoint + (setpoint - room_temp)` as
  the target. The pump's curve translates this nudged setpoint into the right
  flow temperature automatically: no knowledge of water temperatures needed.
- **Schedule**: keep different setpoints at different times of day.
- **Suppression**: set a low target (e.g. 15°C) to idle the compressor
  during intentional rest periods.
- **Run extension**: raise `MinFlowTemp` to keep the compressor running beyond
  what the room setpoint alone would sustain.

This is a form of external control that reuses algorithms provided by the
manufacturer: the heat pump handles the physics, the application handles room
comfort logic.

## 3. Direct flow temperature

Hardware present: pump + controller.

The application sends SetModeOverride with a flowtempdesired value. The
pump disables its own heating curve and runs directly to the commanded flow
temperature. The relevant ebusd message definition:

    wi,,,SetModeOverride,Operation mode,,08,b510,00,
      hcmode,,UCH,,,,
      flowtempdesired,,D1C,,,,
      hwctempdesired,,D1C,,,,
      hwcflowtempdesired,,UCH,,,,
      setmode1,,UCH,,,,
        disablehc,,BI0,,,,
        disablehwctapping,,BI1,,,,
        disablehwcload,,BI2,,,,
      setmode2,,UCH,,,,
        remoteControlHcPump,,BI0,,,,
        releaseBackup,,BI1,,,,
        releaseCooling,,BI2

Once `SetModeOverride` is sent, it is no longer possible to use or set the
heat curve, target room temperature, or target HWC. In other words, the pump's
own thermostat logic is no longer running when you start controlling the
circuit temperature directly.

What an application could do:

- Set any flow temperature at any time, for both HC and HWC independently.
- Force a switch to making hot water, or disabling it.
- Implement its own weather compensation curve from scratch.
- Run a full PID loop on room temperature with flow temperature as the output.

This mode is also a little bit dangerous: if your application crashes, the
pump would stay in the last known mode. However, to mitigate this, the pump
will reset to an autonomous state if it does not receive any more
`SetModeOverride` messages within ~15 minutes.

## Choosing a mode

| Mode                | Application role         | Pump control from app |
| ------------------- | ------------------------ | --------------------- |
| Vaillant thermostat | nudges only              | basic parameters      |
| Virtual room target | basic control            | target temperature    |
| SetModeOverride     | full algorithmic control | circuit temperature   |
