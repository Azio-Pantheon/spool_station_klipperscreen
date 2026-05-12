import logging
import yaml
from typing import List, Optional


def check_config(gcode_config_yml: Optional[str], machine_config: Optional[dict]) -> List[str]:
    """Compare a gcode's embedded YAML config (PantheonSlicer "---...---" block)
    against the running printer's features.yml. Returns warning strings prefixed
    'Warning!' or 'Caution!' for print.py's prefix-based categorization.

    Returns [] if either input is missing or the gcode YAML is malformed —
    the caller falls through to the existing "Out of date PantheonSlicer"
    caution path in that case.
    """
    if not gcode_config_yml or not machine_config:
        return []

    cleaned = gcode_config_yml.replace('---', '').replace('...', '').replace(';', '\n').replace('\\n', '')
    try:
        header_data = yaml.safe_load(cleaned)
    except yaml.YAMLError:
        logging.exception("config_verifier: failed to parse gcode YAML")
        return []
    if not isinstance(header_data, dict):
        return []

    out: List[str] = []

    try:
        gcode_process = header_data['printer']['process']
        printer_process = machine_config['printer']['process']
        if gcode_process != printer_process:
            out.append(
                f"Warning! Process mismatch!\n\tExpected {gcode_process} got {printer_process}"
            )
    except (KeyError, TypeError):
        logging.exception("config_verifier: process compare failed")

    try:
        gcode_axes = header_data['printer']['axes-limits']
        printer_axes = machine_config['printer']['axes-limits']
        for axis, val in gcode_axes.items():
            printer_val = printer_axes.get(axis)
            if printer_val is None:
                continue
            if val > printer_val:
                out.append(
                    f"Caution! Slicer requested {val} mm {axis.upper()} axis, \n\t"
                    f"the printer has {printer_val} mm {axis.upper()} axis."
                )
    except (KeyError, TypeError):
        logging.exception("config_verifier: axes-limits compare failed")

    try:
        gcode_hw = header_data['printer']['hardware']
        printer_hw = machine_config['printer']['hardware']
        for key, val in gcode_hw.items():
            if key not in printer_hw:
                out.append(f"Caution! Slicer requested {key} which the printer does not have!")
            elif val != 'any' and val != printer_hw[key]:
                out.append(
                    f"Caution! Mismatched {key} found! \n\t"
                    f"Slicer requested {val}, the printer has {printer_hw[key]}"
                )
    except (KeyError, TypeError):
        logging.exception("config_verifier: hardware compare failed")

    return out
