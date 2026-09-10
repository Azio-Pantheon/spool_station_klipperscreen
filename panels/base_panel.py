# -*- coding: utf-8 -*-
import logging
import os
import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk, Pango
from jinja2 import Environment
from datetime import datetime
from math import log
from ks_includes.screen_panel import ScreenPanel


class BasePanel(ScreenPanel):
    def __init__(self, screen, title):
        super().__init__(screen, title)
        self.current_panel = None
        self.time_min = -1
        self.time_format = self._config.get_main_config().getboolean("24htime", True)
        self.time_update = None
        self.titlebar_items = []
        self.titlebar_name_type = None
        self.current_extruder = None
        self.last_usage_report = datetime.now()
        self.usage_report = 0
        self._cached_config = {}
        self._pending_title = None
        self._weight_timeout_id = None
        self._current_weight_info = None  # Track current weight to prevent flashing
        self._current_panel_title = None  # Track current panel to detect switches
        
        # Action bar buttons
        abscale = self.bts * 1.1
        self.control['back'] = self._gtk.Button('back', scale=abscale)
        self.control['back'].connect("clicked", self.back)
        self.control['home'] = self._gtk.Button('main', scale=abscale)
        self.control['home'].connect("clicked", self._screen._menu_go_back, True)
        for control in self.control:
            self.set_control_sensitive(False, control)

        self.shutdown = {
            "name": None,
            "panel": "shutdown",
            "icon": "shutdown",
        }
        self.control['shutdown'] = self._gtk.Button('shutdown', scale=abscale)
        self.control['shutdown'].connect("clicked", self.menu_item_clicked, self.shutdown)

        self.control['estop'] = self._gtk.Button('emergency', scale=abscale)
        self.control['estop'].connect("clicked", self.emergency_stop)

        self.control['printer_select'] = self._gtk.Button('shuffle', scale=abscale)
        self.control['printer_select'].connect("clicked", self._screen.show_printer_select)
        self.control['printer_select'].set_no_show_all(True)

        self.shorcut = {
            "name": "Macros",
            "panel": "gcode_macros",
            "icon": "custom-script",
        }
        self.control['shortcut'] = self._gtk.Button(self.shorcut['icon'], scale=abscale)
        self.control['shortcut'].connect("clicked", self.menu_item_clicked, self.shorcut)
        self.control['shortcut'].set_no_show_all(True)

        # Any action bar button should close the keyboard
        for item in self.control:
            self.control[item].connect("clicked", self._screen.remove_keyboard)

        # Action bar
        self.action_bar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        if self._screen.vertical_mode:
            self.action_bar.set_hexpand(True)
            self.action_bar.set_vexpand(False)
        else:
            self.action_bar.set_hexpand(False)
            self.action_bar.set_vexpand(True)
        self.action_bar.get_style_context().add_class('action_bar')
        self.action_bar.set_size_request(self._gtk.action_bar_width, self._gtk.action_bar_height)
        self.action_bar.add(self.control['back'])
        self.action_bar.add(self.control['home'])
        self.action_bar.add(self.control['printer_select'])
        #self.action_bar.add(self.control['shortcut'])
        self.action_bar.add(self.control['shutdown'])
        self.action_bar.add(self.control['estop'])
        self.show_printer_select(len(self._config.get_printers()) > 1)

        # Titlebar

        # This box will be populated by show_heaters
        self.control['temp_box'] = Gtk.Box(spacing=10)

        self.titlelbl = Gtk.Label(hexpand=True, halign=Gtk.Align.CENTER, ellipsize=Pango.EllipsizeMode.END)

        # Shown only while Moonraker reports machine_state.is_fleet_worker == 1
        self.fleet_worker_badge = Gtk.Label(label=_("FLEET WORKER"), valign=Gtk.Align.CENTER)
        self.fleet_worker_badge.get_style_context().add_class("fleet_worker_badge")
        self.fleet_worker_badge.set_no_show_all(True)
        self.fleet_worker_badge.hide()

        self.control['time'] = Gtk.Label(label="00:00 AM")
        self.control['time_box'] = Gtk.Box(halign=Gtk.Align.END)
        self.control['time_box'].pack_end(self.control['time'], True, True, 10)

        self.titlebar = Gtk.Box(spacing=5, valign=Gtk.Align.CENTER)
        self.titlebar.get_style_context().add_class("title_bar")
        self.titlebar.add(self.control['temp_box'])
        self.titlebar.add(self.titlelbl)
        self.titlebar.add(self.fleet_worker_badge)
        self.titlebar.add(self.control['time_box'])
        self.set_title(title)
        self.update_fleet_worker_badge()

        # Main layout
        self.main_grid = Gtk.Grid()

        if self._screen.vertical_mode:
            self.main_grid.attach(self.titlebar, 0, 0, 1, 1)
            self.main_grid.attach(self.content, 0, 1, 1, 1)
            self.main_grid.attach(self.action_bar, 0, 2, 1, 1)
            self.action_bar.set_orientation(orientation=Gtk.Orientation.HORIZONTAL)
        else:
            self.main_grid.attach(self.action_bar, 0, 0, 1, 2)
            self.action_bar.set_orientation(orientation=Gtk.Orientation.VERTICAL)
            self.main_grid.attach(self.titlebar, 1, 0, 1, 1)
            self.main_grid.attach(self.content, 1, 1, 1, 1)

        self.update_time()

    def show_heaters(self, show=True):
        try:
            for child in self.control['temp_box'].get_children():
                self.control['temp_box'].remove(child)
            devices = self._printer.get_temp_devices()
            if not show or not devices:
                return

            img_size = self._gtk.img_scale * self.bts
            for device in devices:
                self.labels[device] = Gtk.Label(ellipsize=Pango.EllipsizeMode.START)
                self.labels[f'{device}_box'] = Gtk.Box()
                icon = self.get_icon(device, img_size)
                if icon is not None:
                    self.labels[f'{device}_box'].pack_start(icon, False, False, 3)
                self.labels[f'{device}_box'].pack_start(self.labels[device], False, False, 0)

            # Limit the number of items according to resolution
            nlimit = int(round(log(self._screen.width, 10) * 5 - 10.5))
            n = 0
            if len(self._printer.get_tools()) > (nlimit - 1):
                self.current_extruder = self._printer.get_stat("toolhead", "extruder")
                if self.current_extruder and f"{self.current_extruder}_box" in self.labels:
                    self.control['temp_box'].add(self.labels[f"{self.current_extruder}_box"])
            else:
                self.current_extruder = False
            for device in devices:
                if n >= nlimit:
                    break
                if device.startswith("extruder") and self.current_extruder is False:
                    self.control['temp_box'].add(self.labels[f"{device}_box"])
                    n += 1
                elif device.startswith("heater"):
                    self.control['temp_box'].add(self.labels[f"{device}_box"])
                    n += 1
            for device in devices:
                # Users can fill the bar if they want
                if n >= nlimit + 1:
                    break
                name = device.split()[1] if len(device.split()) > 1 else device
                for item in self.titlebar_items:
                    if name == item:
                        self.control['temp_box'].add(self.labels[f"{device}_box"])
                        n += 1
                        break

            # Nozzle life tracker
            self.labels['nozzle_life'] = Gtk.Label(label="")
            self.labels['nozzle_life_box'] = Gtk.Box()
            # Stay hidden until nozzle life data arrives, even if the parent is show_all()'d
            self.labels['nozzle_life_box'].set_no_show_all(True)
            nozzle_icon = self._gtk.Image("extruder-health", img_size, img_size)
            self.labels['nozzle_life_box'].pack_start(nozzle_icon, False, False, 3)
            self.labels['nozzle_life_box'].pack_start(self.labels['nozzle_life'], False, False, 0)
            self.control['temp_box'].add(self.labels['nozzle_life_box'])

            self.control['temp_box'].show_all()
        except Exception as e:
            logging.debug(f"Couldn't create heaters box: {e}")

    def get_icon(self, device, img_size):
        if device.startswith("extruder"):
            if self._printer.extrudercount > 1:
                if device == "extruder":
                    device = "extruder0"
                return self._gtk.Image(f"extruder-{device[8:]}", img_size, img_size)
            return self._gtk.Image("extruder", img_size, img_size)
        elif device.startswith("heater_bed"):
            return self._gtk.Image("bed", img_size, img_size)
        # Extra items
        elif self.titlebar_name_type is not None:
            # The item has a name, do not use an icon
            return None
        elif device.startswith("temperature_fan"):
            return self._gtk.Image("fan", img_size, img_size)
        elif device.startswith("heater_generic"):
            return self._gtk.Image("heater", img_size, img_size)
        else:
            return self._gtk.Image("heat-up", img_size, img_size)

    def _update_nozzle_life_label(self, nozzle_life, remaining_life):
        if 'nozzle_life' not in self.labels or 'nozzle_life_box' not in self.labels:
            return
        try:
            life = float(nozzle_life) if nozzle_life else 0
            remaining = float(remaining_life) if remaining_life else 0
            if life > 0:
                pct = (remaining / life) * 100
                if pct < 0:
                    self.labels['nozzle_life'].set_markup(f'<span foreground="red">{pct:.0f}%</span>')
                else:
                    self.labels['nozzle_life'].set_label(f"{pct:.0f}% Health")
                # no_show_all is set on the box, so show children explicitly
                for child in self.labels['nozzle_life_box'].get_children():
                    child.show()
                self.labels['nozzle_life_box'].show()
            else:
                self.labels['nozzle_life_box'].hide()
        except Exception as e:
            logging.debug(f"Error updating nozzle life label: {e}")
            self.labels['nozzle_life_box'].hide()

    def activate(self):
        if self.time_update is None:
            self.time_update = GLib.timeout_add_seconds(1, self.update_time)

    def add_content(self, panel):
        printing = self._printer and self._printer.state in {"printing", "paused"}
        connected = self._printer and self._printer.state not in {'disconnected', 'startup', 'shutdown', 'error'}
        
        self.control['shutdown'].set_visible(not printing)
        self.control['estop'].set_visible(printing)
        self.show_shortcut(connected)
        self.show_heaters(connected)
        for control in ('back', 'home'):
            self.set_control_sensitive(len(self._screen._cur_panels) > 1, control=control)
        self.current_panel = panel
        self.set_title(panel.title)
        self.content.add(panel.content)

    def back(self, widget=None):
        if self.current_panel is None:
            return
        self._screen.remove_keyboard()
        if hasattr(self.current_panel, "back") \
                and not self.current_panel.back() \
                or not hasattr(self.current_panel, "back"):
            self._screen._menu_go_back()

    def process_update(self, action, data):
        if action == "notify_proc_stat_update":
            cpu = data["system_cpu_usage"]["cpu"]
            memory = (data["system_memory"]["used"] / data["system_memory"]["total"]) * 100
            error = "message_popup_error"
            ctx = self.titlebar.get_style_context()
            msg = f"CPU: {cpu:2.0f}%    RAM: {memory:2.0f}%"
            if cpu > 80 or memory > 85:
                if self.usage_report < 3:
                    self.usage_report += 1
                    return
                self.last_usage_report = datetime.now()
                if not ctx.has_class(error):
                    ctx.add_class(error)
                self._screen.log_notification(f"{os.uname().nodename}.local: {msg}", 2)
                self.titlelbl.set_label(msg)
            elif ctx.has_class(error):
                if (datetime.now() - self.last_usage_report).seconds < 5:
                    self.titlelbl.set_label(msg)
                    return
                self.usage_report = 0
                ctx.remove_class(error)
                self.titlelbl.set_label(f"{os.uname().nodename}.local")
            return

        if action == "notify_update_response":
            if self.update_dialog is None:
                self.show_update_dialog()
            if 'message' in data:
                self.labels['update_progress'].set_text(
                    f"{self.labels['update_progress'].get_text().strip()}\n"
                    f"{data['message']}\n")
            if 'complete' in data and data['complete']:
                logging.info("Update complete")
                if self.update_dialog is not None:
                    try:
                        self.update_dialog.set_response_sensitive(Gtk.ResponseType.OK, True)
                        self.update_dialog.get_widget_for_response(Gtk.ResponseType.OK).show()
                    except AttributeError:
                        logging.error("error trying to show the updater button the dialog might be closed")
                        self._screen.updating = False
                        for dialog in self._screen.dialogs:
                            self._gtk.remove_dialog(dialog)
            return

        if action != "notify_status_update" or self._screen.printer is None:
            return
        for device in self._printer.get_temp_devices():
            temp = self._printer.get_stat(device, "temperature")
            if temp and device in self.labels:
                name = ""
                if not (device.startswith("extruder") or device.startswith("heater_bed")):
                    if self.titlebar_name_type == "full":
                        name = device.split()[1] if len(device.split()) > 1 else device
                        name = f'{self.prettify(name)}: '
                    elif self.titlebar_name_type == "short":
                        name = device.split()[1] if len(device.split()) > 1 else device
                        name = f"{name[:1].upper()}: "
                self.labels[device].set_label(f"{name}{int(temp)}°")

        if (self.current_extruder and 'toolhead' in data and 'extruder' in data['toolhead']
                and data["toolhead"]["extruder"] != self.current_extruder):
            self.control['temp_box'].remove(self.labels[f"{self.current_extruder}_box"])
            self.current_extruder = data["toolhead"]["extruder"]
            self.control['temp_box'].pack_start(self.labels[f"{self.current_extruder}_box"], True, True, 3)
            self.control['temp_box'].reorder_child(self.labels[f"{self.current_extruder}_box"], 0)
            self.control['temp_box'].show_all()

        if 'toolhead' in data and any(k in data['toolhead'] for k in ('nozzle_life', 'remaining_nozzle_life')):
            th = data['toolhead']
            self._update_nozzle_life_label(
                th.get('nozzle_life', self._printer.get_stat('toolhead', 'nozzle_life')),
                th.get('remaining_nozzle_life', self._printer.get_stat('toolhead', 'remaining_nozzle_life'))
            )

        return False

    def remove(self, widget):
        self.content.remove(widget)

    def set_control_sensitive(self, value=True, control='shortcut'):
        self.control[control].set_sensitive(value)

    def show_shortcut(self, show=True):
        show = (
            show
            and self._config.get_main_config().getboolean('side_macro_shortcut', True)
            and self._printer.get_printer_status_data()["printer"]["gcode_macros"]["count"] > 0
        )
        self.control['shortcut'].set_visible(show)
        self.set_control_sensitive(self._screen._cur_panels[-1] != self.shorcut['panel'])
        self.set_control_sensitive(self._screen._cur_panels[-1] != self.shutdown['panel'], control='shutdown')

    def show_printer_select(self, show=True):
        self.control['printer_select'].set_visible(show)

    def update_fleet_worker_badge(self):
        # Badge visibility follows Moonraker's machine_state.is_fleet_worker.
        cfg = getattr(self._screen, "shared_printer_config", None)
        is_worker = getattr(cfg, "is_fleet_worker", 0)
        try:
            is_worker = int(is_worker)
        except (TypeError, ValueError):
            is_worker = 0
        if is_worker == 1:
            self.fleet_worker_badge.show()
        else:
            self.fleet_worker_badge.hide()

    def set_title(self, title):
        self.titlebar.get_style_context().remove_class("message_popup_error")
        
        # Check if this is a panel switch or just a refresh
        is_panel_switch = (self._current_panel_title != title)
        
        # Cancel any pending weight loading from previous panel switches
        if self._weight_timeout_id is not None:
            GLib.source_remove(self._weight_timeout_id)
            self._weight_timeout_id = None
        
        # Store title for lazy weight loading
        self._pending_title = title
        self._current_panel_title = title
        
        if is_panel_switch:
            # Clear weight info on panel switch
            self._current_weight_info = None
            # First, build and show title immediately WITHOUT weight (fast)
            self._build_title_without_weight(title)
        else:
            # For refreshes, use existing weight if available
            self._build_title_with_existing_weight(title)

    def _build_title_without_weight(self, title):
        """Build title immediately with config info but without spoolman weight"""
        
        def handle_config_response(response, method, params, *args):
            try:
                result = response.get("result", {})
                value = result.get("value", {})
                filament = value.get("filament_type", "")
                nozzle = value.get("nozzle_size", "")

                # Cache config for lazy weight loading
                self._cached_config = {'filament': filament, 'nozzle': nozzle}

                # Update nozzle life tracker
                self._update_nozzle_life_label(
                    value.get("nozzle_life", 0),
                    value.get("remaining_nozzle_life", 0)
                )

                # Build title immediately without weight
                self._build_final_title(title, filament, nozzle, None)

                # Schedule lazy weight loading (store ID to cancel if needed)
                self._weight_timeout_id = GLib.timeout_add_seconds(2, self._lazy_load_weight)

            except Exception as e:
                logging.debug(f"Error processing config response: {e}")
                self._set_title_fallback(title)

        # Get config data quickly (this is fast)
        try:
            if self._screen._ws is None:
                self._set_title_fallback(title)
                return

            self._screen._ws.send_method(
                "server.database.get_item",
                {"namespace": "HS3"},
                handle_config_response
            )
        except Exception as e:
            logging.debug(f"Error requesting config from Moonraker: {e}")
            self._set_title_fallback(title)

    def _build_title_with_existing_weight(self, title):
        """Build title with existing weight info to avoid flashing during refreshes"""
        
        def handle_config_response(response, method, params, *args):
            try:
                result = response.get("result", {})
                value = result.get("value", {})
                filament = value.get("filament_type", "")
                nozzle = value.get("nozzle_size", "")

                # Cache config for lazy weight loading
                self._cached_config = {'filament': filament, 'nozzle': nozzle}

                # Update nozzle life tracker
                self._update_nozzle_life_label(
                    value.get("nozzle_life", 0),
                    value.get("remaining_nozzle_life", 0)
                )

                # Build title with existing weight info to prevent flashing
                self._build_final_title(title, filament, nozzle, self._current_weight_info)

                # Still schedule weight update, but don't clear existing weight
                self._weight_timeout_id = GLib.timeout_add_seconds(2, self._lazy_load_weight)

            except Exception as e:
                logging.debug(f"Error processing config response: {e}")
                self._set_title_fallback(title)
        
        # Get config data quickly (this is fast)
        try:
            if self._screen._ws is None:
                self._set_title_fallback(title)
                return
                
            self._screen._ws.send_method(
                "server.database.get_item",
                {"namespace": "HS3"},
                handle_config_response
            )
        except Exception as e:
            logging.debug(f"Error requesting config from Moonraker: {e}")
            self._set_title_fallback(title)

    def _lazy_load_weight(self):
        """Fetch spoolman weight after a delay and update the title"""
        try:
            # Clear timeout ID since we're executing now
            self._weight_timeout_id = None
            
            # Use cached config to avoid duplicate API calls
            if not hasattr(self, '_cached_config') or not hasattr(self, '_pending_title'):
                return False
            
            filament = self._cached_config['filament']
            nozzle = self._cached_config['nozzle'] 
            title = self._pending_title
            
            # Only proceed if this title is still current (user hasn't switched panels)
            current_title = ""
            if (self.current_panel and hasattr(self.current_panel, 'title')):
                current_title = self.current_panel.title
            
            if title == current_title:
                # Now get spoolman weight with cached config
                self._get_spoolman_weight_and_build_title(title, filament, nozzle)
            else:
                logging.debug("Skipping weight load - panel changed")
            
        except Exception as e:
            logging.debug(f"Error in lazy weight loading: {e}")
        
        return False
                     
    def _get_spoolman_weight_and_build_title(self, title, filament, nozzle):
            """Get spool_tracker weight and build title, with fallback if spool_tracker unavailable"""
            
            try:
                # Check if apiclient is available
                if not hasattr(self._screen, 'apiclient') or self._screen.apiclient is None:
                    self._build_final_title(title, filament, nozzle, self._current_weight_info)
                    return
                
                # Get spool tracker status
                result = self._screen.apiclient.send_request("server/spool_tracker/status")
                if not result:
                    self._build_final_title(title, filament, nozzle, self._current_weight_info)
                    return
                
                tracker_data = result.get("result", {})
                
                # Check if tracking is enabled and we have valid data
                can_track = tracker_data.get("can_track", False)
                
                if not can_track:
                    # Tracking disabled or no weight available
                    weight_info = "weight untracked"
                    self._current_weight_info = weight_info
                    self._build_final_title(title, filament, nozzle, weight_info)
                else:
                    # Get remaining weight from tracker
                    weights = tracker_data.get("weights", {})
                    remaining_weight = weights.get("remaining_weight", 0)
                    
                    if remaining_weight > 0:
                        weight_text = f"{round(remaining_weight, 1)}g"
                    else:
                        weight_text = "weight untracked"
                        
                    self._current_weight_info = weight_text
                    self._build_final_title(title, filament, nozzle, weight_text)
                    
            except Exception as e:
                logging.debug(f"Error requesting spool_tracker data: {e}")
                # Build title with existing weight info
                self._build_final_title(title, filament, nozzle, self._current_weight_info)

    def _build_final_title(self, title, filament, nozzle, weight_info):
        """Build the final title - exclude config info for main menu to avoid duplication"""
        try:
            base_title = f"{os.uname().nodename}.local"
            
            # Check if this is the main menu panel or extrude panel - if so, skip config info since it's shown in buttons
            is_main_or_extrude_menu = (
                self.current_panel and 
                hasattr(self.current_panel, 'create_filament_info_panel')
            ) or (
                self.current_panel and 
                hasattr(self.current_panel, 'spoolman_filament_mapping')
            )
            
            # Add filament, nozzle, and weight only if NOT main menu
            if not is_main_or_extrude_menu:
                config_info = []
                if filament:
                    config_info.append(filament)
                if nozzle:
                    config_info.append(f"{nozzle}mm")
                if weight_info:
                    config_info.append(weight_info)
                
                if config_info:
                    base_title += f" | {' '.join(config_info)}"
            
            # Add the panel title if provided
            if title:
                try:
                    env = Environment(extensions=["jinja2.ext.i18n"], autoescape=True)
                    env.install_gettext_translations(self._config.get_lang())
                    j2_temp = env.from_string(title)
                    processed_title = j2_temp.render()
                    base_title += f" | {processed_title}"
                except Exception as e:
                    logging.debug(f"Error parsing jinja for title: {title}\n{e}")
                    base_title += f" | {title}"
            
            self.titlelbl.set_label(base_title)
            
        except Exception as e:
            logging.debug(f"Error building final title: {e}")
            # Final fallback
            self._set_title_fallback(title)

    def _set_title_fallback(self, title):
        """Fallback method that uses the original title setting logic"""
        if not title:
            self.titlelbl.set_label(f"{os.uname().nodename}.local")
            return
        try:
            env = Environment(extensions=["jinja2.ext.i18n"], autoescape=True)
            env.install_gettext_translations(self._config.get_lang())
            j2_temp = env.from_string(title)
            title = j2_temp.render()
        except Exception as e:
            logging.debug(f"Error parsing jinja for title: {title}\n{e}")

        self.titlelbl.set_label(f"{os.uname().nodename}.local | {title}")


    def update_time(self):
        now = datetime.now()
        confopt = self._config.get_main_config().getboolean("24htime", True)
        if now.minute != self.time_min or self.time_format != confopt:
            if confopt:
                self.control['time'].set_text(f'{now:%H:%M }')
            else:
                self.control['time'].set_text(f'{now:%I:%M %p}')
            self.time_min = now.minute
            self.time_format = confopt
        return True

    def set_ks_printer_cfg(self, printer):
        ScreenPanel.ks_printer_cfg = self._config.get_printer_config(printer)
        if self.ks_printer_cfg is not None:
            self.titlebar_name_type = self.ks_printer_cfg.get("titlebar_name_type", None)
            titlebar_items = self.ks_printer_cfg.get("titlebar_items", None)
            if titlebar_items is not None:
                self.titlebar_items = [str(i.strip()) for i in titlebar_items.split(',')]
                logging.info(f"Titlebar name type: {self.titlebar_name_type} items: {self.titlebar_items}")
            else:
                self.titlebar_items = []

    def show_update_dialog(self):
        if self.update_dialog is not None:
            return
        button = [{"name": _("Finish"), "response": Gtk.ResponseType.OK}]
        self.labels['update_progress'] = Gtk.Label(hexpand=True, vexpand=True, ellipsize=Pango.EllipsizeMode.END)
        self.labels['update_scroll'] = self._gtk.ScrolledWindow(steppers=False)
        self.labels['update_scroll'].set_property("overlay-scrolling", True)
        self.labels['update_scroll'].add(self.labels['update_progress'])
        self.labels['update_scroll'].connect("size-allocate", self._autoscroll)
        dialog = self._gtk.Dialog(_("Updating"), button, self.labels['update_scroll'], self.finish_updating)
        dialog.connect("delete-event", self.close_update_dialog)
        dialog.set_response_sensitive(Gtk.ResponseType.OK, False)
        dialog.get_widget_for_response(Gtk.ResponseType.OK).hide()
        self.update_dialog = dialog
        self._screen.updating = True

    def finish_updating(self, dialog, response_id):
        if response_id != Gtk.ResponseType.OK:
            return
        logging.info("Finishing update")
        self._screen.updating = False
        self._gtk.remove_dialog(dialog)
        self._screen._menu_go_back(home=True)

    def close_update_dialog(self, *args):
        logging.info("Closing update dialog")
        if self.update_dialog in self._screen.dialogs:
            self._screen.dialogs.remove(self.update_dialog)
        self.update_dialog = None
        self._screen._menu_go_back(home=True)