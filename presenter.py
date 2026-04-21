from controller import HeatPumpController


def badge(controller: HeatPumpController) -> dict:
    """Badge label and CSS class for the current state. Called with state_lock held."""
    if controller.state == "DHW":
        return {"label": "Water",           "cls": "dhw",        "active": False}
    if controller.state == "DHW_WAIT":
        return {"label": "Water settling",  "cls": "dhw",        "active": False}
    if controller.state == "SUPPRESSED":
        return {"label": "Suppressed",      "cls": "suppressed", "active": False}
    if controller.state == "RUNNING":
        return {"label": "Heating",         "cls": "heating",    "active": True}
    if controller.state == "RESTING":
        return {"label": "Resting",         "cls": "resting",    "active": False}
    # IDLE — distinguish between pump dormant (heat pump shut itself off) and circulating
    if controller.pump == "dormant":
        return {"label": "Off",             "cls": "off",        "active": False}
    return     {"label": "Idle",            "cls": "idle",       "active": False}


def status_sentence(controller: HeatPumpController) -> str:
    """Plain-language explanation of the current state. Called with state_lock held."""
    if controller.current_temp() is None:
        return "Waking up, waiting for the first temperature reading."

    if controller.state == "DHW":
        return "Casually loading the hot water tank."

    elif controller.state == "DHW_WAIT":
        return "Hot water's done, letting things settle."

    elif controller.state == "SUPPRESSED":
        return "Pretty warm inside, so the heating is taking a break."

    elif controller.state == "IDLE" and controller.pump == "dormant":
        return "Outside is warm enough, no heating!"

    elif controller.state == "RESTING":
        if controller.temp_above_upper_band():
            return "Giving the floor a rest, it's warm enough!"
        elif controller.temp_below_lower_band():
            return f"Slightly cold, but your heat pump is taking a nap..."
        else:
            return "Heating done. I'll let it rest for now."

    elif controller.state == "IDLE":
        if controller.temp_above_upper_band():
            return "Pretty warm inside!"
        elif controller.temp_below_lower_band():
            return "Waiting for the pump to notice that it's a bit cold."
        else:
            return "Temperature is fine. Tuning up and down where needed."

    elif controller.state == "RUNNING":
        if controller.night_limit_reached():
            return "No heating anymore! Tomorrow's forecast is great."
        if controller.temp_above_upper_band():
            return "Heating the floor a little."
        elif controller.temp_below_lower_band():
            return f"Heating right now! Been at it for {controller.elapsed()/60:.0f} min."
        else:
            return "Heating a little to keep it nice and cosy."


def debug_status(controller: HeatPumpController) -> str:
    """Internal debug status string. Not for display — use for logging/back panel."""
    if controller.current_temp() is None:
        return "INACTIVE waiting for first room temperature"

    message = (
        f"{controller.state}: "
        f"{controller.elapsed()/60:.0f}min"
    )

    if controller.night_mode.is_active():
        message += f" night:{controller.night_mode.run_total()/60:.1f}/{controller.night_mode.limit_hours*60:.1f}min"
    if controller.night_mode.limit_reached():
        message += " night:LIMIT REACHED"
    if controller.is_extender_running():
        message += " extending run!"

    return message
