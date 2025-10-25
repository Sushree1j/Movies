# Camera Streamer 📹

A modern Android application that streams your device's camera feed to a desktop application in real-time. Perfect for remote monitoring, presentations, or computer vision applications.

## ✨ Features

- **Real-time Camera Streaming**: Stream live camera feed from your Android device
- **Dual Camera Support**: Choose between front and rear cameras
- **Multiple Resolution Options**: Low (640x480), Medium (1280x720), High (1920x1080)
- **Modern UI**: Beautiful Material Design 3 interface with card-based layout
- **Network Streaming**: Stream over WiFi to desktop applications
- **Easy Setup**: Simple IP address and port configuration

## 🏗️ Architecture

This project consists of two main components:

### Android Client (`android-client/`)
- Built with Kotlin and Android Camera2 API
- Material Design 3 UI with modern card-based interface
- Supports both front and rear camera streaming
- Configurable resolution and network settings

### Desktop Listener (`desktop-listener/`)
- Python-based desktop application
- Receives and displays the camera stream
- Simple setup and configuration

## 🚀 Quick Start

### Prerequisites

- **Android**: Minimum SDK 26 (Android 8.0)
- **Java**: JDK 17 or higher
- **Android SDK**: API level 34
- **Python**: 3.8+ (for desktop listener)

### Building the Android App

1. **Clone the repository:**
   ```bash
   git clone https://github.com/yourusername/camera-streamer.git
   cd camera-streamer
   ```

2. **Set up Android SDK:**
   - Download and install Android SDK
   - Set `ANDROID_HOME` environment variable
   - Ensure `build-tools` and `platforms` are installed

3. **Build the app:**
   ```bash
   cd android-client
   ./gradlew build
   ```

4. **Install on device:**
   - Transfer `app/build/outputs/apk/debug/app-debug.apk` to your Android device
   - Install the APK (you may need to enable "Install unknown apps")

### Running the Desktop Listener

1. **Install dependencies:**
   ```bash
   cd desktop-listener
   pip install -r requirements.txt
   ```

2. **Run the listener:**
   ```bash
   python main.py
   ```

## 📱 Usage

1. **Launch the app** on your Android device
2. **Enter connection details:**
   - IP address of your desktop computer
   - Port number (default: usually 8080 or similar)
3. **Select camera** (front or rear)
4. **Choose resolution** based on your needs
5. **Start streaming** by tapping the "Start Streaming" button
6. **View the stream** on your desktop application

## 🔧 Configuration

### Network Settings
- **IP Address**: Enter the IP address of the machine running the desktop listener
- **Port**: Specify the port number for streaming (must match desktop listener)

### Camera Settings
- **Camera Selection**: Toggle between front and rear cameras
- **Resolution**: Choose appropriate resolution based on network speed and quality needs

## 🛠️ Development

### Android Development Setup

1. **Import project** in Android Studio
2. **Sync Gradle** files
3. **Run on device/emulator**

### Key Technologies Used

- **Kotlin**: Primary programming language
- **Android Camera2 API**: Camera access and streaming
- **Material Design 3**: Modern UI components
- **ConstraintLayout**: Responsive layouts
- **Coroutines**: Asynchronous operations
- **CameraX**: Camera lifecycle management

### Project Structure

```
android-client/
├── app/
│   ├── src/main/
│   │   ├── java/com/example/chessassiststreamer/
│   │   │   └── MainActivity.kt          # Main activity
│   │   ├── res/                         # Resources
│   │   │   ├── drawable/                # Icons and graphics
│   │   │   ├── layout/                  # UI layouts
│   │   │   ├── values/                  # Colors, strings, themes
│   │   │   └── xml/                     # Configuration files
│   │   └── AndroidManifest.xml          # App manifest
│   └── build.gradle                     # App build configuration
├── build.gradle                         # Project build configuration
├── gradle.properties                    # Gradle properties
└── settings.gradle                      # Project settings

desktop-listener/
├── main.py                              # Desktop streaming receiver
└── requirements.txt                     # Python dependencies
```

## 📋 Requirements

### Android App
- **Min SDK**: 26 (Android 8.0)
- **Target SDK**: 34 (Android 14)
- **Permissions**:
  - `CAMERA`: For camera access
  - `INTERNET`: For network streaming
  - `ACCESS_WIFI_STATE`: For WiFi information
  - `ACCESS_NETWORK_STATE`: For network state

### Desktop Listener
- **Python**: 3.8 or higher
- **Libraries**: See `desktop-listener/requirements.txt`

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## 🐛 Issues & Support

- **Bug Reports**: [GitHub Issues](https://github.com/yourusername/camera-streamer/issues)
- **Feature Requests**: [GitHub Issues](https://github.com/yourusername/camera-streamer/issues)
- **Discussions**: [GitHub Discussions](https://github.com/yourusername/camera-streamer/discussions)

## 🙏 Acknowledgments

- Built with [Android Camera2 API](https://developer.android.com/reference/android/hardware/camera2/package-summary)
- UI designed with [Material Design 3](https://material.io/design)
- Icons from Android Studio and custom vector graphics

---

**Made with ❤️ for computer vision and remote monitoring applications**
