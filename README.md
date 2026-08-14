# Android Screen Mirror & ADB Control Hub

A high-performance Desktop application built using Python and PyQt5 for streaming, controlling, and managing Android devices over USB or Wi-Fi.

## Key Features

* **Wake & Sleep Resiliency:** Fixed screen capture bugs where mirroring failed while the phone screen was off or locked.
* **Dual Mirroring Engines:**
  * **Native scrcpy Engine:** High FPS (up to 60fps), low-latency audio/video streaming, and recording.
  * **Pure Python Engine:** Fully integrated zero-dependency fallback streaming mechanism using PIL and native Qt windows.
* **Wireless TCP/IP Connection:** Switch from USB mode to wireless Wi-Fi control seamlessly.
* **Full Remote Control:** Keyboard input mapping, drag-and-swipe touch events, home, back, and application switching controls.
* **ADB Suite Integration:** Take screenshots, batch install APKs, send desktop clipboard input, and trigger standard/bootloader system reboots.

## Prerequisites

1. **Python 3.8+** installed on your system.
2. **Android ADB Tools** added to system PATH (`adb`).
3. **USB Debugging** enabled on your Android target device (*Settings > Developer Options > USB Debugging*).
4. *(Optional)* `scrcpy` installed on your system for maximum framerate performance.

## Installation & Setup

1. **Clone Repository:**
   ```bash
   git clone [https://github.com/YOUR_USERNAME/android-screen-mirror-hub.git](https://github.com/YOUR_USERNAME/android-screen-mirror-hub.git)
   cd android-screen-mirror-hub
