#!/usr/bin/env python3
import sys
import os
import subprocess
import threading
import time
import re
import io
import queue
from PyQt5 import QtWidgets, QtCore, QtGui
from PIL import Image

ADB_BIN = "adb"

class ADBWorker(QtCore.QObject):
    log_signal = QtCore.pyqtSignal(str)
    command_finished = QtCore.pyqtSignal(str, str, int)

    def run_command(self, cmd_name, args):
        def _target():
            try:
                full_cmd = [ADB_BIN] + args
                self.log_signal.emit(f"Running: {' '.join(full_cmd)}")
                result = subprocess.run(full_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
                self.command_finished.emit(cmd_name, result.stdout, result.returncode)
            except subprocess.TimeoutExpired:
                self.log_signal.emit(f"Command timed out: {cmd_name}")
                self.command_finished.emit(cmd_name, "TIMEOUT", -1)
            except Exception as e:
                self.log_signal.emit(f"Error running command: {str(e)}")
                self.command_finished.emit(cmd_name, str(e), -1)

        threading.Thread(target=_target, daemon=True).start()

class AsyncInputQueue(QtCore.QThread):
    def __init__(self, device_id):
        super().__init__()
        self.device_id = device_id
        self.queue = queue.Queue()
        self.running = True

    def run(self):
        while self.running:
            try:
                cmd = self.queue.get(timeout=0.1)
                full_cmd = [ADB_BIN, "-s", self.device_id] + cmd
                subprocess.run(full_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.queue.task_done()
            except queue.Empty:
                continue
            except Exception:
                pass

    def add_command(self, cmd):
        self.queue.put(cmd)

    def stop(self):
        self.running = False
        self.wait()

class ScreenCaptureThread(QtCore.QThread):
    frame_received = QtCore.pyqtSignal(QtGui.QImage)
    error_signal = QtCore.pyqtSignal(str)

    def __init__(self, device_id, fps_limit=15):
        super().__init__()
        self.device_id = device_id
        self.running = True
        self.fps_limit = fps_limit
        self.frame_delay = 1.0 / fps_limit

    def wake_screen_if_off(self):
        """Ensures display is awake prior to capturing screencap frames"""
        try:
            # Send KEYCODE_WAKEUP (224) to wake screen if sleeping
            subprocess.run([ADB_BIN, "-s", self.device_id, "shell", "input", "keyevent", "224"], 
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2)
        except Exception:
            pass

    def run(self):
        # Automatically send wake signal before initializing stream
        self.wake_screen_if_off()
        cmd = [ADB_BIN, "-s", self.device_id, "shell", "screencap", "-p"]
        
        consecutive_errors = 0
        while self.running:
            start_time = time.time()
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                stdout_data, stderr_data = proc.communicate(timeout=3.0)
                
                if proc.returncode != 0:
                    err_msg = stderr_data.decode('utf-8', errors='ignore').strip()
                    consecutive_errors += 1
                    if consecutive_errors > 3:
                        self.error_signal.emit(f"Capture failed: {err_msg}")
                        break
                    time.sleep(0.5)
                    continue

                if not stdout_data:
                    consecutive_errors += 1
                    if consecutive_errors > 5:
                        # Attempt display wake recovery
                        self.wake_screen_if_off()
                    continue

                consecutive_errors = 0
                image = Image.open(io.BytesIO(stdout_data))
                image_rgba = image.convert("RGBA")
                width, height = image_rgba.size
                raw_bytes = image_rgba.tobytes()
                
                qimage = QtGui.QImage(
                    raw_bytes,
                    width,
                    height,
                    QtGui.QImage.Format_RGBA8888
                )
                self.frame_received.emit(qimage.copy())

            except subprocess.TimeoutExpired:
                consecutive_errors += 1
                if consecutive_errors > 3:
                    self.wake_screen_if_off()
                continue
            except Exception as e:
                self.error_signal.emit(f"Stream error: {str(e)}")
                break

            elapsed = time.time() - start_time
            sleep_time = self.frame_delay - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)

    def stop(self):
        self.running = False
        self.wait()

class PurePythonViewerWindow(QtWidgets.QDialog):
    def __init__(self, device_id, device_name, parent=None):
        super().__init__(parent)
        self.device_id = device_id
        self.device_name = device_name
        self.original_width = 1080
        self.original_height = 2400
        self.aspect_ratio = self.original_width / self.original_height
        
        self.setWindowTitle(f"Screen Mirror - {device_name} ({device_id})")
        self.setMinimumSize(380, 680)
        self.resize(450, 820)
        
        self.setStyleSheet("""
            QDialog {
                background-color: #0f172a;
            }
            QLabel#screen_label {
                background-color: #020617;
                border: 2px solid #1e293b;
                border-radius: 8px;
            }
        """)

        self.layout = QtWidgets.QVBoxLayout(self)
        self.layout.setContentsMargins(10, 10, 10, 10)
        
        self.screen_label = QtWidgets.QLabel(self)
        self.screen_label.setObjectName("screen_label")
        self.screen_label.setAlignment(QtCore.Qt.AlignCenter)
        self.layout.addWidget(self.screen_label)
        
        # Bottom Navigation Controls Overlay
        nav_box = QtWidgets.QHBoxLayout()
        self.btn_wake = QtWidgets.QPushButton(" Wake Screen", self)
        self.btn_back = QtWidgets.QPushButton(" Back", self)
        self.btn_home = QtWidgets.QPushButton(" Home", self)
        self.btn_apps = QtWidgets.QPushButton(" Apps", self)

        for btn in [self.btn_wake, self.btn_back, self.btn_home, self.btn_apps]:
            btn.setStyleSheet("""
                QPushButton {
                    background-color: #1e293b;
                    color: #f8fafc;
                    border: 1px solid #334155;
                    border-radius: 6px;
                    padding: 6px;
                    font-weight: bold;
                }
                QPushButton:hover {
                    background-color: #3b82f6;
                    color: #ffffff;
                }
            """)
            nav_box.addWidget(btn)

        self.btn_wake.clicked.connect(lambda: self.input_queue.add_command(["shell", "input", "keyevent", "224"]))
        self.btn_back.clicked.connect(lambda: self.input_queue.add_command(["shell", "input", "keyevent", "4"]))
        self.btn_home.clicked.connect(lambda: self.input_queue.add_command(["shell", "input", "keyevent", "3"]))
        self.btn_apps.clicked.connect(lambda: self.input_queue.add_command(["shell", "input", "keyevent", "187"]))
        self.layout.addLayout(nav_box)

        self.status_bar = QtWidgets.QLabel("Connecting stream...", self)
        self.status_bar.setStyleSheet("background-color: #1e293b; color: #38bdf8; padding: 6px; font-weight: bold; border-radius: 4px;")
        self.status_bar.setAlignment(QtCore.Qt.AlignCenter)
        self.layout.addWidget(self.status_bar)

        self.drag_start_pos = None
        self.drag_start_time = None
        self.current_pixmap = None

        self.input_queue = AsyncInputQueue(self.device_id)
        self.input_queue.start()

        self.capture_thread = ScreenCaptureThread(self.device_id, fps_limit=15)
        self.capture_thread.frame_received.connect(self.on_frame_received)
        self.capture_thread.error_signal.connect(self.on_stream_error)
        self.capture_thread.start()

        self.screen_label.setMouseTracking(True)
        self.setFocusPolicy(QtCore.Qt.StrongFocus)

    def on_frame_received(self, qimage):
        self.status_bar.setText("Stream Active | Interactive Control Ready")
        self.original_width = qimage.width()
        self.original_height = qimage.height()
        self.aspect_ratio = self.original_width / self.original_height
        
        self.current_pixmap = QtGui.QPixmap.fromImage(qimage)
        self.update_display()

    def on_stream_error(self, err_msg):
        self.status_bar.setText(f"Error: {err_msg}")
        QtWidgets.QMessageBox.critical(self, "Streaming Error", f"Failed to mirror screen: {err_msg}")
        self.close()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.update_display()

    def update_display(self):
        if not self.current_pixmap:
            return
            
        scaled = self.current_pixmap.scaled(
            self.screen_label.width(),
            self.screen_label.height(),
            QtCore.Qt.KeepAspectRatio,
            QtCore.Qt.SmoothTransformation
        )
        self.screen_label.setPixmap(scaled)

    def get_image_display_geometry(self):
        if not self.current_pixmap:
            return None
            
        lbl_w = self.screen_label.width()
        lbl_h = self.screen_label.height()
        
        if lbl_w / lbl_h > self.aspect_ratio:
            scaled_h = lbl_h
            scaled_w = int(lbl_h * self.aspect_ratio)
            x_offset = int((lbl_w - scaled_w) / 2)
            y_offset = 0
        else:
            scaled_w = lbl_w
            scaled_h = int(lbl_w / self.aspect_ratio)
            x_offset = 0
            y_offset = int((lbl_h - scaled_h) / 2)
            
        return x_offset, y_offset, scaled_w, scaled_h

    def map_to_device_coords(self, local_pos):
        geom = self.get_image_display_geometry()
        if not geom:
            return None
            
        x_offset, y_offset, scaled_w, scaled_h = geom
        click_x = local_pos.x() - x_offset
        click_y = local_pos.y() - y_offset
        
        if 0 <= click_x <= scaled_w and 0 <= click_y <= scaled_h:
            dev_x = int(click_x * self.original_width / scaled_w)
            dev_y = int(click_y * self.original_height / scaled_h)
            return dev_x, dev_y
        return None

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton:
            dev_coords = self.map_to_device_coords(event.pos())
            if dev_coords:
                self.drag_start_pos = dev_coords
                self.drag_start_time = time.time()

    def mouseReleaseEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton and self.drag_start_pos:
            dev_coords = self.map_to_device_coords(event.pos())
            if dev_coords:
                end_x, end_y = dev_coords
                start_x, start_y = self.drag_start_pos
                duration = int((time.time() - self.drag_start_time) * 1000)
                
                dist = ((end_x - start_x)**2 + (end_y - start_y)**2)**0.5
                if dist < 8 and duration < 250:
                    self.input_queue.add_command(["shell", "input", "tap", str(start_x), str(start_y)])
                else:
                    if duration < 100:
                        duration = 100
                    self.input_queue.add_command(["shell", "input", "swipe", str(start_x), str(start_y), str(end_x), str(end_y), str(duration)])
            
            self.drag_start_pos = None
            self.drag_start_time = None

    def keyPressEvent(self, event):
        key = event.key()
        KEY_MAP = {
            QtCore.Qt.Key_Back: 4,
            QtCore.Qt.Key_Escape: 4,
            QtCore.Qt.Key_Home: 3,
            QtCore.Qt.Key_Return: 66,
            QtCore.Qt.Key_Enter: 66,
            QtCore.Qt.Key_Backspace: 67,
            QtCore.Qt.Key_Delete: 112,
            QtCore.Qt.Key_Tab: 61,
            QtCore.Qt.Key_Space: 62,
            QtCore.Qt.Key_Menu: 82,
            QtCore.Qt.Key_VolumeUp: 24,
            QtCore.Qt.Key_VolumeDown: 25,
            QtCore.Qt.Key_Power: 26,
        }
        
        if key in KEY_MAP:
            self.input_queue.add_command(["shell", "input", "keyevent", str(KEY_MAP[key])])
            event.accept()
        else:
            text = event.text()
            if text and text.isprintable():
                sanitized = ""
                for char in text:
                    if char == " ":
                        sanitized += "%s"
                    elif char in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.,:;?_+-*/=()[]{}!@#$^&*":
                        sanitized += char
                if sanitized:
                    self.input_queue.add_command(["shell", "input", "text", sanitized])
                event.accept()

    def closeEvent(self, event):
        self.capture_thread.stop()
        self.input_queue.stop()
        super().closeEvent(event)


class AndroidControllerMainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Android Control Hub Pro")
        self.resize(850, 720)
        self.setup_modern_theme()
        
        self.devices = []
        self.scrcpy_process = None
        
        self.adb_worker = ADBWorker()
        self.adb_worker.log_signal.connect(self.log_message)
        self.adb_worker.command_finished.connect(self.on_adb_command_completed)

        central_widget = QtWidgets.QWidget(self)
        self.setCentralWidget(central_widget)
        
        main_layout = QtWidgets.QHBoxLayout(central_widget)
        
        # Left Panel
        left_widget = QtWidgets.QWidget(self)
        left_layout = QtWidgets.QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 0, 0)
        
        # 1. Device Selection
        device_group = QtWidgets.QGroupBox("1. Connected Devices", self)
        device_group_layout = QtWidgets.QVBoxLayout(device_group)
        
        self.device_list_widget = QtWidgets.QListWidget(self)
        self.device_list_widget.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.device_list_widget.itemSelectionChanged.connect(self.on_device_selection_changed)
        device_group_layout.addWidget(self.device_list_widget)
        
        btn_refresh_layout = QtWidgets.QHBoxLayout()
        self.btn_refresh = QtWidgets.QPushButton("Scan Devices", self)
        self.btn_refresh.clicked.connect(self.scan_devices)
        btn_refresh_layout.addWidget(self.btn_refresh)
        
        self.btn_wake_dev = QtWidgets.QPushButton("Wake Screen", self)
        self.btn_wake_dev.clicked.connect(self.wake_selected_device)
        btn_refresh_layout.addWidget(self.btn_wake_dev)

        self.btn_disconnect = QtWidgets.QPushButton("Disconnect WiFi", self)
        self.btn_disconnect.clicked.connect(self.disconnect_wireless)
        btn_refresh_layout.addWidget(self.btn_disconnect)
        
        device_group_layout.addLayout(btn_refresh_layout)
        left_layout.addWidget(device_group)
        
        # 2. Mirror Engine Settings
        settings_group = QtWidgets.QGroupBox("2. Streaming Configuration", self)
        settings_layout = QtWidgets.QGridLayout(settings_group)
        
        settings_layout.addWidget(QtWidgets.QLabel("Mirror Engine:"), 0, 0)
        self.combo_engine = QtWidgets.QComboBox(self)
        self.combo_engine.addItems(["Native (scrcpy - High Performance)", "Pure Python (Built-in Fallback)"])
        self.combo_engine.currentIndexChanged.connect(self.on_engine_changed)
        settings_layout.addWidget(self.combo_engine, 0, 1)
        
        settings_layout.addWidget(QtWidgets.QLabel("Max Resolution:"), 1, 0)
        self.combo_res = QtWidgets.QComboBox(self)
        self.combo_res.addItems(["Auto", "1920", "1440", "1024", "800", "640"])
        settings_layout.addWidget(self.combo_res, 1, 1)
        
        settings_layout.addWidget(QtWidgets.QLabel("Bitrate (Mbps):"), 2, 0)
        self.spin_bitrate = QtWidgets.QSpinBox(self)
        self.spin_bitrate.setRange(1, 30)
        self.spin_bitrate.setValue(8)
        settings_layout.addWidget(self.spin_bitrate, 2, 1)
        
        settings_layout.addWidget(QtWidgets.QLabel("Max FPS:"), 3, 0)
        self.combo_fps = QtWidgets.QComboBox(self)
        self.combo_fps.addItems(["No Limit", "60", "30", "15"])
        settings_layout.addWidget(self.combo_fps, 3, 1)
        
        self.chk_screen_off = QtWidgets.QCheckBox("Keep physical phone display off", self)
        settings_layout.addWidget(self.chk_screen_off, 4, 0, 1, 2)
        
        self.chk_always_on_top = QtWidgets.QCheckBox("Window Always On Top", self)
        self.chk_always_on_top.setChecked(True)
        settings_layout.addWidget(self.chk_always_on_top, 5, 0, 1, 2)
        
        self.chk_no_audio = QtWidgets.QCheckBox("Disable Audio Forwarding", self)
        settings_layout.addWidget(self.chk_no_audio, 6, 0, 1, 2)
        
        self.chk_read_only = QtWidgets.QCheckBox("Read-only Mode (Disable control)", self)
        settings_layout.addWidget(self.chk_read_only, 7, 0, 1, 2)
        
        self.chk_record = QtWidgets.QCheckBox("Record Session to MP4", self)
        settings_layout.addWidget(self.chk_record, 8, 0, 1, 2)
        
        left_layout.addWidget(settings_group)
        
        self.btn_start = QtWidgets.QPushButton("LAUNCH MIRRORING SESSION", self)
        self.btn_start.setObjectName("btn_start")
        self.btn_start.clicked.connect(self.launch_mirroring)
        left_layout.addWidget(self.btn_start)
        
        main_layout.addWidget(left_widget, stretch=4)
        
        # Right Panel
        right_widget = QtWidgets.QWidget(self)
        right_layout = QtWidgets.QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)
        
        # 3. Wi-Fi Wireless Tools
        wifi_group = QtWidgets.QGroupBox("3. Wireless Pair Center", self)
        wifi_layout = QtWidgets.QVBoxLayout(wifi_group)
        
        wifi_info = QtWidgets.QLabel("1. Connect phone via USB.\n2. Click 'Enable TCP Mode'.\n3. Enter IP address & pair wirelessly.", self)
        wifi_info.setStyleSheet("color: #94a3b8; font-size: 11px;")
        wifi_layout.addWidget(wifi_info)
        
        self.btn_prep_tcpip = QtWidgets.QPushButton("Enable TCP Mode (Port 5555)", self)
        self.btn_prep_tcpip.clicked.connect(self.prep_wireless_mode)
        wifi_layout.addWidget(self.btn_prep_tcpip)
        
        ip_layout = QtWidgets.QHBoxLayout()
        ip_layout.addWidget(QtWidgets.QLabel("Device IP:"))
        self.txt_ip = QtWidgets.QLineEdit(self)
        self.txt_ip.setPlaceholderText("e.g. 192.168.1.50")
        ip_layout.addWidget(self.txt_ip)
        wifi_layout.addLayout(ip_layout)
        
        self.btn_connect_wifi = QtWidgets.QPushButton("Connect Wirelessly", self)
        self.btn_connect_wifi.clicked.connect(self.connect_wireless)
        wifi_layout.addWidget(self.btn_connect_wifi)
        
        right_layout.addWidget(wifi_group)
        
        # 4. ADB Management Tools
        tools_group = QtWidgets.QGroupBox("4. Quick ADB Utilities", self)
        tools_layout = QtWidgets.QVBoxLayout(tools_group)
        
        type_layout = QtWidgets.QHBoxLayout()
        self.txt_send_text = QtWidgets.QLineEdit(self)
        self.txt_send_text.setPlaceholderText("Send text to target device...")
        type_layout.addWidget(self.txt_send_text)
        
        self.btn_send_text = QtWidgets.QPushButton("Send Text", self)
        self.btn_send_text.clicked.connect(self.send_text_to_device)
        type_layout.addWidget(self.btn_send_text)
        tools_layout.addLayout(type_layout)
        
        btn_grid = QtWidgets.QGridLayout()
        self.btn_screenshot = QtWidgets.QPushButton("Capture Screen", self)
        self.btn_screenshot.clicked.connect(self.take_screenshot)
        btn_grid.addWidget(self.btn_screenshot, 0, 0)
        
        self.btn_install_apk = QtWidgets.QPushButton("Install APK", self)
        self.btn_install_apk.clicked.connect(self.install_apk)
        btn_grid.addWidget(self.btn_install_apk, 0, 1)
        
        self.btn_reboot = QtWidgets.QPushButton("Reboot System", self)
        self.btn_reboot.clicked.connect(lambda: self.reboot_device("normal"))
        btn_grid.addWidget(self.btn_reboot, 1, 0)
        
        self.btn_reboot_loader = QtWidgets.QPushButton("Reboot Bootloader", self)
        self.btn_reboot_loader.clicked.connect(lambda: self.reboot_device("bootloader"))
        btn_grid.addWidget(self.btn_reboot_loader, 1, 1)
        
        tools_layout.addLayout(btn_grid)
        right_layout.addWidget(tools_group)
        
        # 5. Event Logs
        log_group = QtWidgets.QGroupBox("Execution Console", self)
        log_layout = QtWidgets.QVBoxLayout(log_group)
        self.txt_log = QtWidgets.QPlainTextEdit(self)
        self.txt_log.setReadOnly(True)
        self.txt_log.setStyleSheet("background-color: #020617; color: #38bdf8; font-family: monospace; font-size: 11px; border-radius: 4px;")
        log_layout.addWidget(self.txt_log)
        right_layout.addWidget(log_group)
        
        main_layout.addWidget(right_widget, stretch=5)
        
        self.scan_timer = QtCore.QTimer(self)
        self.scan_timer.setInterval(3000)
        self.scan_timer.timeout.connect(self.scan_devices)
        self.scan_timer.start()
        
        self.scan_devices()
        self.log_message("Control Hub Initialized. Ready for device connection.")

    def setup_modern_theme(self):
        stylesheet = """
            QMainWindow {
                background-color: #0f172a;
            }
            QWidget {
                background-color: #0f172a;
                color: #f8fafc;
                font-family: 'Segoe UI', system-ui, sans-serif;
                font-size: 12px;
            }
            QGroupBox {
                border: 1px solid #334155;
                border-radius: 8px;
                margin-top: 12px;
                font-weight: bold;
                color: #38bdf8;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 4px;
            }
            QPushButton {
                background-color: #1e293b;
                color: #ffffff;
                border: 1px solid #334155;
                border-radius: 6px;
                padding: 6px 12px;
                font-weight: 600;
            }
            QPushButton:hover {
                background-color: #2563eb;
                border-color: #3b82f6;
            }
            QPushButton:pressed {
                background-color: #1d4ed8;
            }
            QPushButton#btn_start {
                background-color: #0284c7;
                color: #ffffff;
                font-size: 13px;
                padding: 10px;
                border: none;
            }
            QPushButton#btn_start:hover {
                background-color: #0369a1;
            }
            QListWidget {
                background-color: #020617;
                border: 1px solid #334155;
                border-radius: 6px;
                padding: 4px;
            }
            QListWidget::item {
                padding: 6px;
                border-bottom: 1px solid #1e293b;
                border-radius: 4px;
            }
            QListWidget::item:selected {
                background-color: #0284c7;
                color: #ffffff;
            }
            QLineEdit, QComboBox, QSpinBox {
                background-color: #020617;
                border: 1px solid #334155;
                border-radius: 6px;
                padding: 5px;
                color: #f8fafc;
            }
        """
        self.setStyleSheet(stylesheet)

    def log_message(self, message):
        timestamp = time.strftime("[%H:%M:%S]")
        self.txt_log.appendPlainText(f"{timestamp} {message}")

    def get_selected_device(self):
        selected_items = self.device_list_widget.selectedItems()
        if not selected_items:
            return None
        index = self.device_list_widget.row(selected_items[0])
        if 0 <= index < len(self.devices):
            return self.devices[index]
        return None

    def wake_selected_device(self):
        dev = self.get_selected_device()
        if dev:
            self.adb_worker.run_command("WAKE", ["-s", dev['id'], "shell", "input", "keyevent", "224"])
            self.log_message(f"Sent wake command to {dev['id']}")

    def on_device_selection_changed(self):
        dev = self.get_selected_device()
        if dev:
            self.log_message(f"Selected: {dev['name']} ({dev['id']})")

    def scan_devices(self):
        try:
            proc = subprocess.Popen([ADB_BIN, "devices", "-l"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            stdout_data, _ = proc.communicate(timeout=2.0)
            
            lines = stdout_data.strip().split('\n')
            new_devices = []
            
            for line in lines[1:]:
                line = line.strip()
                if not line:
                    continue
                
                parts = re.split(r'\s+', line)
                if len(parts) >= 2:
                    device_id = parts[0]
                    state = parts[1]
                    
                    model_match = re.search(r'model:(\S+)', line)
                    model = model_match.group(1) if model_match else "Android Device"
                    model = model.replace('_', ' ')
                    
                    new_devices.append({
                        "id": device_id,
                        "state": state,
                        "name": f"{model} [{state}]"
                    })
            
            if new_devices != self.devices:
                self.devices = new_devices
                self.device_list_widget.clear()
                for dev in self.devices:
                    item_text = f"{dev['name']}\nID: {dev['id']}"
                    self.device_list_widget.addItem(item_text)
                    
                if self.devices:
                    self.device_list_widget.setCurrentRow(0)
                    self.log_message(f"Scanned: {len(self.devices)} active device(s).")
                else:
                    self.log_message("Scan: Searching for connected Android devices...")
                    
        except Exception:
            pass

    def on_engine_changed(self, index):
        if index == 1:
            self.chk_no_audio.setEnabled(False)
            self.chk_record.setEnabled(False)
            self.log_message("Switched to Built-in Pure Python Mirror Engine.")
        else:
            self.chk_no_audio.setEnabled(True)
            self.chk_record.setEnabled(True)
            self.log_message("Switched to Native scrcpy Performance Engine.")

    def launch_mirroring(self):
        dev = self.get_selected_device()
        if not dev:
            QtWidgets.QMessageBox.warning(self, "No Device", "Please select a connected device.")
            return

        if dev['state'] == 'unauthorized':
            QtWidgets.QMessageBox.warning(self, "Unauthorized", "Device unauthorized. Allow USB debugging prompt on phone screen.")
            return

        # Ensure phone screen is woken up first before streaming
        self.adb_worker.run_command("WAKE", ["-s", dev['id'], "shell", "input", "keyevent", "224"])

        engine = self.combo_engine.currentText()
        if "Pure Python" in engine:
            self.log_message(f"Launching integrated viewer for {dev['id']}...")
            viewer = PurePythonViewerWindow(dev['id'], dev['name'], self)
            viewer.exec_()
        else:
            self.log_message(f"Configuring Native scrcpy engine for {dev['id']}...")
            cmd = ["scrcpy", "-s", dev['id']]
            
            res = self.combo_res.currentText()
            if res != "Auto":
                cmd += ["-m", res]
                
            cmd += ["-b", f"{self.spin_bitrate.value()}M"]
            
            fps = self.combo_fps.currentText()
            if fps != "No Limit":
                cmd += ["--max-fps", fps]
                
            if self.chk_screen_off.isChecked():
                cmd += ["--turn-screen-off"]
                
            if self.chk_always_on_top.isChecked():
                cmd += ["--always-on-top"]
                
            if self.chk_no_audio.isChecked():
                cmd += ["--no-audio"]
                
            if self.chk_read_only.isChecked():
                cmd += ["--no-control"]
                
            if self.chk_record.isChecked():
                record_file = f"capture_{dev['id']}_{int(time.time())}.mp4"
                cmd += ["--record", record_file]
                
            try:
                self.scrcpy_process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                
                def check_scrcpy_error():
                    time.sleep(1.5)
                    poll = self.scrcpy_process.poll()
                    if poll is not None:
                        _, stderr_data = self.scrcpy_process.communicate()
                        self.log_message(f"Scrcpy output code {poll}: {stderr_data.strip()}")
                        QtCore.QMetaObject.invokeMethod(self, "suggest_fallback", QtCore.Qt.QueuedConnection)
                    else:
                        self.log_message("Native Mirror stream active.")

                threading.Thread(target=check_scrcpy_error, daemon=True).start()
            except Exception as e:
                self.log_message(f"Failed to execute scrcpy: {str(e)}")
                QtWidgets.QMessageBox.critical(self, "Error", f"Could not launch scrcpy: {str(e)}")

    @QtCore.pyqtSlot()
    def suggest_fallback(self):
        reply = QtWidgets.QMessageBox.question(
            self, 
            "Fallback Required", 
            "Native scrcpy launcher failed. Fall back to integrated Pure Python engine?",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No
        )
        if reply == QtWidgets.QMessageBox.Yes:
            self.combo_engine.setCurrentIndex(1)
            self.launch_mirroring()

    def prep_wireless_mode(self):
        dev = self.get_selected_device()
        if not dev:
            QtWidgets.QMessageBox.warning(self, "No Device", "Select a USB connected device.")
            return
            
        self.adb_worker.run_command("PREP_TCP", ["-s", dev['id'], "tcpip", "5555"])

    def connect_wireless(self):
        ip = self.txt_ip.text().strip()
        if not ip:
            QtWidgets.QMessageBox.warning(self, "IP Required", "Enter target device IP address.")
            return
            
        self.adb_worker.run_command("CONNECT_WIFI", ["connect", f"{ip}:5555"])

    def disconnect_wireless(self):
        dev = self.get_selected_device()
        target = dev['id'] if dev else ""
        if not target or ":" not in target:
            self.adb_worker.run_command("DISCONNECT", ["disconnect"])
        else:
            self.adb_worker.run_command("DISCONNECT", ["disconnect", target])

    def send_text_to_device(self):
        dev = self.get_selected_device()
        if not dev:
            return
        text = self.txt_send_text.text()
        if not text:
            return
            
        sanitized = ""
        for char in text:
            if char == " ":
                sanitized += "%s"
            elif char in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.,:;?_+-*/=()[]{}!@#$^&*":
                sanitized += char
                
        if sanitized:
            self.adb_worker.run_command("SEND_TEXT", ["-s", dev['id'], "shell", "input", "text", sanitized])
            self.txt_send_text.clear()

    def take_screenshot(self):
        dev = self.get_selected_device()
        if not dev:
            return
            
        file_path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save Screenshot", os.path.expanduser(f"~/screencap_{dev['id']}.png"), "PNG Images (*.png)"
        )
        if not file_path:
            return
            
        def _capture():
            try:
                cmd = [ADB_BIN, "-s", dev['id'], "shell", "screencap", "-p"]
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                stdout_data, _ = proc.communicate(timeout=8.0)
                if proc.returncode == 0 and stdout_data:
                    with open(file_path, "wb") as f:
                        f.write(stdout_data)
                    self.adb_worker.log_signal.emit(f"Saved: {file_path}")
            except Exception as e:
                self.adb_worker.log_signal.emit(f"Capture error: {str(e)}")

        threading.Thread(target=_capture, daemon=True).start()

    def install_apk(self):
        dev = self.get_selected_device()
        if not dev:
            return
        apk_path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Select APK", os.path.expanduser("~"), "APK Package (*.apk)")
        if apk_path:
            self.adb_worker.run_command("INSTALL_APK", ["-s", dev['id'], "install", "-r", apk_path])

    def reboot_device(self, mode="normal"):
        dev = self.get_selected_device()
        if not dev:
            return
        cmd_args = ["-s", dev['id'], "reboot"]
        if mode == "bootloader":
            cmd_args.append("bootloader")
        self.adb_worker.run_command("REBOOT", cmd_args)

    def on_adb_command_completed(self, cmd_name, output, exit_code):
        if cmd_name == "PREP_TCP":
            if exit_code == 0:
                QtWidgets.QMessageBox.information(self, "Success", "Wireless TCP mode enabled on port 5555. Unplug USB and connect over Wi-Fi IP.")
        elif cmd_name == "CONNECT_WIFI":
            self.scan_devices()

if __name__ == "__main__":
    app = QtWidgets.QApplication(sys.argv)
    window = AndroidControllerMainWindow()
    window.show()
    sys.exit(app.exec_())