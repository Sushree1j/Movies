# Desktop Listener

This is the desktop component of the Camera Streamer application. It receives and displays the camera stream from the Android app.

## Requirements

- Python 3.8 or higher
- Pillow library

## Installation

```bash
pip install -r requirements.txt
```

## Usage

```bash
python main.py
```

The application will start listening for camera stream data from the Android app. Make sure to configure the correct IP address and port in the Android app to match your desktop's network settings.

## Features

- Real-time video display
- Network statistics (FPS, data rate)
- Configurable listening port
- Simple GUI interface