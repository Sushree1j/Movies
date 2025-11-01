import argparse
import io
import queue
import socket
import struct
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from tkinter import ttk
from typing import Optional, Dict, List
import json

from PIL import Image, ImageTk, ImageFilter, ImageEnhance

try:
    RESAMPLE_LANCZOS = Image.Resampling.LANCZOS
except AttributeError:  # Pillow < 9.1 fallback
    RESAMPLE_LANCZOS = Image.LANCZOS


class ToolTip:
    """Simple tooltip class for Tkinter widgets"""
    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.tooltip_window = None
        self.widget.bind("<Enter>", self.show_tooltip)
        self.widget.bind("<Leave>", self.hide_tooltip)

    def show_tooltip(self, event=None):
        x, y, _, _ = self.widget.bbox("insert")
        x += self.widget.winfo_rootx() + 25
        y += self.widget.winfo_rooty() + 25

        self.tooltip_window = tk.Toplevel(self.widget)
        self.tooltip_window.wm_overrideredirect(True)
        self.tooltip_window.wm_geometry(f"+{x}+{y}")

        label = tk.Label(self.tooltip_window, text=self.text, background="#ffffe0", 
                        relief="solid", borderwidth=1, font=("Segoe UI", 8))
        label.pack()

    def hide_tooltip(self, event=None):
        if self.tooltip_window:
            self.tooltip_window.destroy()
            self.tooltip_window = None


@dataclass
class FrameStats:
    fps: float = 0.0
    latency_ms: float = 0.0
    last_updated: float = time.time()
    camera_id: str = "default"
    
@dataclass
class ImageSettings:
    """Settings for image processing"""
    brightness: float = 1.0  # 0.0 to 2.0
    contrast: float = 1.0    # 0.0 to 2.0
    saturation: float = 1.0  # 0.0 to 2.0
    filter_type: str = "none"  # none, grayscale, blur, sharpen, edge_enhance


class VideoServer:
    def __init__(self, host: str, port: int, frame_queue: queue.Queue, stats: FrameStats, camera_id: str = "default"):
        self.host = host
        self.port = port
        self.frame_queue = frame_queue
        self.stats = stats
        self.camera_id = camera_id
        self._should_run = threading.Event()
        self._server_thread: Optional[threading.Thread] = None
        self._client_socket: Optional[socket.socket] = None
        self._client_output_stream: Optional[socket.socket] = None

    def start(self) -> None:
        if self._server_thread and self._server_thread.is_alive():
            return
        self._should_run.set()
        self._server_thread = threading.Thread(target=self._run_server, daemon=True)
        self._server_thread.start()

    def stop(self) -> None:
        self._should_run.clear()
        if self._client_socket:
            try:
                self._client_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self._client_socket.close()
            except OSError:
                pass
            self._client_socket = None

    def _run_server(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_socket:
            server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server_socket.bind((self.host, self.port))
            server_socket.listen(1)
            server_socket.settimeout(1.0)

            while self._should_run.is_set():
                try:
                    client_socket, address = server_socket.accept()
                    self._client_socket = client_socket
                    self._handle_client(client_socket, address)
                except socket.timeout:
                    continue
                except OSError:
                    break

    def _handle_client(self, client_socket: socket.socket, address) -> None:
        with client_socket:
            client_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            client_socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 512 * 1024)  # 512KB receive buffer
            client_socket.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 64 * 1024)  # 64KB send buffer
            client_socket.settimeout(5.0)
            self._client_output_stream = client_socket
            frame_count = 0
            window_start = time.time()

            # Try to read camera metadata if available
            try:
                # Read metadata header (optional)
                metadata_header = self._recvall(client_socket, 4)
                if metadata_header:
                    (metadata_length,) = struct.unpack('>I', metadata_header)
                    if 0 < metadata_length < 1024:  # Reasonable metadata size
                        metadata_bytes = self._recvall(client_socket, metadata_length)
                        if metadata_bytes:
                            try:
                                metadata = json.loads(metadata_bytes.decode('utf-8'))
                                self.camera_id = metadata.get('camera_id', self.camera_id)
                                self.stats.camera_id = self.camera_id
                            except (json.JSONDecodeError, UnicodeDecodeError):
                                pass
            except:
                pass

            while self._should_run.is_set():
                try:
                    header = self._recvall(client_socket, 4)
                    if not header:
                        break
                    (frame_length,) = struct.unpack('>I', header)
                    if frame_length <= 0 or frame_length > 5 * 1024 * 1024:
                        continue
                    frame_data = self._recvall(client_socket, frame_length)
                    if not frame_data:
                        break

                    now = time.time()
                    self._push_frame(frame_data, now)

                    frame_count += 1
                    elapsed = now - window_start
                    if elapsed >= 1.0:
                        self.stats.fps = frame_count / elapsed
                        frame_count = 0
                        window_start = now
                except (socket.timeout, ConnectionError, struct.error):
                    break
                except OSError:
                    break
            
            self._client_output_stream = None

    def _push_frame(self, frame_data: bytes, timestamp: float) -> None:
        # Drop old frames immediately for minimal latency - only keep latest frame
        # Clear queue completely to ensure we always show the latest frame
        while not self.frame_queue.empty():
            try:
                self.frame_queue.get_nowait()
            except queue.Empty:
                break
        # Add the new frame
        try:
            self.frame_queue.put_nowait((frame_data, timestamp))
        except queue.Full:
            # Should not happen since we just cleared the queue, but handle gracefully
            pass
        self.stats.last_updated = timestamp

    def _recvall(self, client_socket: socket.socket, length: int) -> Optional[bytes]:
        data = bytearray(length)
        view = memoryview(data)
        received = 0
        while received < length:
            try:
                nbytes = client_socket.recv_into(view[received:])
            except socket.timeout:
                return None
            if nbytes == 0:
                return None
            received += nbytes
        return bytes(data)
    
    def send_control_command(self, command: str) -> None:
        """Send control command to the Android client"""
        if self._client_output_stream:
            try:
                # Send command as UTF-8 string with length prefix
                self._client_output_stream.sendall(command.encode('utf-8') + b'\n')
            except Exception as e:
                print(f"Failed to send control command: {e}")



class ViewerApp(tk.Tk):
    def __init__(self, args: argparse.Namespace):
        super().__init__()
        self.title("🎥 Camera Streamer - Multi-Camera Viewer")
        self.geometry("1600x900")
        self.resizable(True, True)
        self.configure(bg='#f5f7fa')

        # Enable high DPI scaling
        try:
            from ctypes import windll
            windll.shcore.SetProcessDpiAwareness(1)
        except:
            pass

        # Try to set window icon (optional)
        try:
            self.iconphoto(True, tk.PhotoImage(width=1, height=1))
        except:
            pass

        # Set up styles
        self._setup_styles()

        # Multi-camera support
        self.cameras: Dict[str, Dict] = {}  # camera_id -> {server, stats, queue, etc.}
        self.active_camera_id: Optional[str] = None
        self.image_settings = ImageSettings()
        
        # Setup first camera (default)
        self._add_camera("Camera 1", args.host, args.port)
        self.active_camera_id = "Camera 1"

        self._photo_image: Optional[ImageTk.PhotoImage] = None
        self._current_image_ts: float = 0.0
        self._cached_display_size: Optional[tuple[int, int]] = None
        self._last_label_size: tuple[int, int] = (0, 0)

        self._build_ui(args)
        
        # Start all camera servers
        for camera in self.cameras.values():
            camera['server'].start()
            
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(8, self._poll_frames)  # ~8ms for 120Hz polling (2x 60fps for smooth handling)
        self.after(500, self._refresh_stats)
    
    def _add_camera(self, camera_id: str, host: str, port: int) -> None:
        """Add a new camera stream"""
        frame_queue = queue.Queue(maxsize=1)
        stats = FrameStats(camera_id=camera_id)
        server = VideoServer(host, port, frame_queue, stats, camera_id)
        
        self.cameras[camera_id] = {
            'server': server,
            'stats': stats,
            'queue': frame_queue,
            'host': host,
            'port': port
        }
    
    def _switch_camera(self, camera_id: str) -> None:
        """Switch active camera view"""
        if camera_id in self.cameras:
            self.active_camera_id = camera_id
            self._update_camera_label()

    def _setup_styles(self) -> None:
        """Set up clean, modern styles for the application"""
        style = ttk.Style()
        style.theme_use('clam')  # Use clam theme for better customization

        # Light modern theme with great contrast
        BG_MAIN = '#f5f7fa'
        BG_CARD = '#ffffff'
        BG_HEADER = '#1e293b'
        BG_CONTROL = '#f8fafc'
        
        TEXT_PRIMARY = '#0f172a'
        TEXT_SECONDARY = '#64748b'
        TEXT_LIGHT = '#ffffff'
        
        ACCENT_BLUE = '#3b82f6'
        ACCENT_GREEN = '#10b981'
        ACCENT_RED = '#ef4444'
        ACCENT_ORANGE = '#f59e0b'

        # Main frame styling
        style.configure('TFrame', background=BG_MAIN)
        style.configure('Card.TFrame', background=BG_CARD, relief='flat')
        style.configure('Header.TFrame', background=BG_HEADER, relief='flat')
        style.configure('Control.TFrame', background=BG_CONTROL)

        # LabelFrame styling - clean cards with borders
        style.configure('TLabelFrame', background=BG_CARD, relief='solid', borderwidth=1,
                       bordercolor='#e2e8f0')
        style.configure('TLabelFrame.Label', background=BG_CARD, foreground=ACCENT_BLUE,
                       font=('Segoe UI', 11, 'bold'))

        # Label styling
        style.configure('TLabel', background=BG_MAIN, foreground=TEXT_PRIMARY, 
                       font=('Segoe UI', 10))
        style.configure('Header.TLabel', background=BG_HEADER, foreground=TEXT_LIGHT,
                       font=('Segoe UI', 18, 'bold'))
        style.configure('Info.TLabel', background=BG_HEADER, foreground='#94a3b8',
                       font=('Segoe UI', 9))
        style.configure('Value.TLabel', background=BG_HEADER, foreground=ACCENT_GREEN,
                       font=('Segoe UI', 9, 'bold'))
        style.configure('Control.TLabel', background=BG_CARD, foreground=TEXT_PRIMARY,
                       font=('Segoe UI', 10, 'bold'))
        style.configure('ControlValue.TLabel', background=BG_CARD, foreground=ACCENT_BLUE,
                       font=('Segoe UI', 10, 'bold'))

        # Button styling - modern with gradients feel
        style.configure('TButton', font=('Segoe UI', 10, 'bold'), padding=(20, 10),
                       relief='flat', background=ACCENT_BLUE, foreground=TEXT_LIGHT,
                       borderwidth=0, focuscolor='none')
        style.configure('Reset.TButton', font=('Segoe UI', 10, 'bold'), padding=(16, 8),
                       background=ACCENT_RED, foreground=TEXT_LIGHT, borderwidth=0)

        # Button hover effects
        style.map('TButton',
                 background=[('active', '#2563eb'), ('pressed', '#1d4ed8')],
                 foreground=[('active', TEXT_LIGHT), ('pressed', TEXT_LIGHT)])
        style.map('Reset.TButton',
                 background=[('active', '#dc2626'), ('pressed', '#b91c1c')],
                 foreground=[('active', TEXT_LIGHT), ('pressed', TEXT_LIGHT)])

        # Scale styling - modern sliders
        style.configure('Horizontal.TScale', background=BG_CARD, troughcolor='#e2e8f0',
                       borderwidth=0, lightcolor=ACCENT_BLUE, darkcolor=ACCENT_BLUE)

        # Status indicator styles
        style.configure('Status.TLabel', font=('Segoe UI', 10, 'bold'), background=BG_CARD)
        style.configure('Connected.TLabel', foreground=ACCENT_GREEN, background=BG_CARD,
                       font=('Segoe UI', 10))
        style.configure('Disconnected.TLabel', foreground=ACCENT_RED, background=BG_CARD,
                       font=('Segoe UI', 10))
        style.configure('Streaming.TLabel', foreground=ACCENT_BLUE, background=BG_CARD,
                       font=('Segoe UI', 10))

    def _build_ui(self, args: argparse.Namespace) -> None:
        # Main container with light, airy design
        main_container = ttk.Frame(self, padding="15", style='TFrame')
        main_container.pack(fill=tk.BOTH, expand=True)

        # Configure grid weights for proper layout
        main_container.grid_rowconfigure(1, weight=1)  # Video area gets most space
        main_container.grid_columnconfigure(0, weight=1)

        # Header section - modern design
        self._build_header(args, main_container)

        # Video display section - premium look
        self._build_video_display(main_container)

        # Camera controls section - sleek controls
        self._build_camera_controls(main_container)

        # Status and controls section - professional status bar
        self._build_status_bar(main_container)

    def _build_header(self, args: argparse.Namespace, parent: ttk.Frame) -> None:
        """Build the clean, modern header with connection info"""
        header_frame = ttk.Frame(parent, style='Header.TFrame', padding=(25, 18))
        header_frame.grid(row=0, column=0, sticky='ew', pady=(0, 15))

        # Title row
        title_row = ttk.Frame(header_frame, style='Header.TFrame')
        title_row.pack(fill=tk.X, pady=(0, 12))
        
        title_label = ttk.Label(title_row, text="🎥 ChessAssist Pro",
                               style='Header.TLabel')
        title_label.pack(side=tk.LEFT)

        # Connection info row
        info_row = ttk.Frame(header_frame, style='Header.TFrame')
        info_row.pack(fill=tk.X)

        # Server info
        server_frame = ttk.Frame(info_row, style='Header.TFrame')
        server_frame.pack(side=tk.LEFT)
        ttk.Label(server_frame, text="📡 Server:", style='Info.TLabel').pack(side=tk.LEFT, padx=(0, 5))
        ttk.Label(server_frame, text=f"{args.host}:{args.port}", style='Value.TLabel').pack(side=tk.LEFT, padx=(0, 25))

        # Local IPs
        ip_frame = ttk.Frame(info_row, style='Header.TFrame')
        ip_frame.pack(side=tk.LEFT)
        ttk.Label(ip_frame, text="� Local IPs:", style='Info.TLabel').pack(side=tk.LEFT, padx=(0, 5))
        ips_text = ', '.join(get_local_ip_addresses())
        ttk.Label(ip_frame, text=ips_text, style='Value.TLabel').pack(side=tk.LEFT)

    def _build_camera_controls(self, parent: ttk.Frame) -> None:
        """Build the clean camera controls panel"""
        controls_frame = ttk.LabelFrame(parent, text="🎛️ Camera & Image Controls", padding=18)
        controls_frame.grid(row=2, column=0, sticky='ew', pady=(0, 15))

        # Create a grid layout for controls
        controls_container = ttk.Frame(controls_frame, style='Card.TFrame')
        controls_container.pack(fill=tk.X)
        
        # Camera selection section
        camera_frame = ttk.Frame(controls_container, style='Card.TFrame')
        camera_frame.grid(row=0, column=0, columnspan=3, sticky='ew', pady=(0, 15))
        
        ttk.Label(camera_frame, text="📷 Active Camera:", style='Control.TLabel').pack(side=tk.LEFT, padx=(5, 10))
        self.camera_var = tk.StringVar(value="Camera 1")
        self.camera_dropdown = ttk.Combobox(camera_frame, textvariable=self.camera_var, 
                                      values=list(self.cameras.keys()), state='readonly', width=15)
        self.camera_dropdown.pack(side=tk.LEFT, padx=(0, 20))
        self.camera_dropdown.bind('<<ComboboxSelected>>', lambda e: self._switch_camera(self.camera_var.get()))
        
        # Add camera button
        add_cam_btn = ttk.Button(camera_frame, text="➕ Add Camera", 
                                command=self._show_add_camera_dialog, style='TButton')
        add_cam_btn.pack(side=tk.LEFT, padx=(0, 10))

        # Row offset for remaining controls
        row_offset = 1
        
        # Zoom control (for Android camera)
        self._create_control_row(controls_container, "🔍 Zoom", row_offset, 1.0, 10.0, "1.0x", self._on_zoom_change)

        # Exposure control (for Android camera)
        self._create_control_row(controls_container, "☀️ Exposure", row_offset + 1, -12, 12, "0", self._on_exposure_change)

        # Focus control (for Android camera)
        self._create_control_row(controls_container, "🎯 Focus", row_offset + 2, 0.0, 1.0, "0.50", self._on_focus_change)
        
        # Brightness control (for desktop processing)
        self._create_control_row(controls_container, "💡 Brightness", row_offset + 3, 0.0, 2.0, "1.00", self._on_brightness_change)
        
        # Contrast control (for desktop processing)
        self._create_control_row(controls_container, "◐ Contrast", row_offset + 4, 0.0, 2.0, "1.00", self._on_contrast_change)
        
        # Saturation control (for desktop processing)
        self._create_control_row(controls_container, "🎨 Saturation", row_offset + 5, 0.0, 2.0, "1.00", self._on_saturation_change)
        
        # Filter selection
        filter_frame = ttk.Frame(controls_container, style='Card.TFrame')
        filter_frame.grid(row=row_offset + 6, column=0, columnspan=3, sticky='ew', pady=(15, 5))
        
        ttk.Label(filter_frame, text="🎭 Filter:", style='Control.TLabel').pack(side=tk.LEFT, padx=(5, 10))
        self.filter_var = tk.StringVar(value="none")
        filter_dropdown = ttk.Combobox(filter_frame, textvariable=self.filter_var,
                                       values=["none", "grayscale", "blur", "sharpen", "edge_enhance"],
                                       state='readonly', width=15)
        filter_dropdown.pack(side=tk.LEFT)
        filter_dropdown.bind('<<ComboboxSelected>>', lambda e: self._on_filter_change())

        # Reset button with better styling
        button_frame = ttk.Frame(controls_container, style='Card.TFrame')
        button_frame.grid(row=row_offset + 7, column=0, columnspan=3, pady=(18, 5), sticky='ew')

        reset_btn = ttk.Button(button_frame, text="🔄 Reset All Controls",
                              command=self._reset_controls, style='Reset.TButton')
        reset_btn.pack(anchor=tk.CENTER)

    def _create_control_row(self, parent: ttk.Frame, label_text: str, row: int, 
                           min_val: float, max_val: float, default_label: str, callback) -> None:
        """Create a clean control row with label, slider, and value display"""
        # Label with clean styling
        label = ttk.Label(parent, text=label_text, style='Control.TLabel', width=13)
        label.grid(row=row, column=0, sticky='w', padx=(5, 15), pady=10)

        # Slider frame
        slider_frame = ttk.Frame(parent, style='Card.TFrame')
        slider_frame.grid(row=row, column=1, sticky='ew', padx=8, pady=10)

        # Slider
        if isinstance(min_val, int) and isinstance(max_val, int):
            var = tk.IntVar()
            scale = ttk.Scale(slider_frame, from_=min_val, to=max_val, variable=var,
                             orient=tk.HORIZONTAL, command=callback, style='Horizontal.TScale',
                             length=300)
        else:
            var = tk.DoubleVar()
            scale = ttk.Scale(slider_frame, from_=min_val, to=max_val, variable=var,
                             orient=tk.HORIZONTAL, command=callback, style='Horizontal.TScale',
                             length=300)

        scale.pack(fill=tk.X, expand=True)

        # Value label with modern styling
        value_label = ttk.Label(parent, text=default_label, style='ControlValue.TLabel', width=7)
        value_label.grid(row=row, column=2, sticky='e', padx=(15, 5), pady=10)

        # Store references
        attr_name = label_text.lower().split()[1] + '_var'
        label_attr = label_text.lower().split()[1] + '_label'
        scale_attr = label_text.lower().split()[1] + '_scale'

        setattr(self, attr_name, var)
        setattr(self, label_attr, value_label)
        setattr(self, scale_attr, scale)

        # Set default values and add tooltips
        if label_text == "🔍 Zoom":
            var.set(1.0)
            ToolTip(scale, "Digital zoom: 1x to 10x magnification")
        elif label_text == "☀️ Exposure":
            var.set(0)
            ToolTip(scale, "Exposure compensation: -12 (darker) to +12 (brighter)")
        elif label_text == "🎯 Focus":
            var.set(0.5)
            ToolTip(scale, "Manual focus: 0.0 (infinity) to 1.0 (closest)")
        elif label_text == "💡 Brightness":
            var.set(1.0)
            ToolTip(scale, "Image brightness: 0.0 (black) to 2.0 (very bright)")
        elif label_text == "◐ Contrast":
            var.set(1.0)
            ToolTip(scale, "Image contrast: 0.0 (no contrast) to 2.0 (high contrast)")
        elif label_text == "🎨 Saturation":
            var.set(1.0)
            ToolTip(scale, "Color saturation: 0.0 (grayscale) to 2.0 (vivid colors)")

        # Configure grid weights
        parent.grid_columnconfigure(1, weight=1)

    def _build_video_display(self, parent: ttk.Frame) -> None:
        """Build the clean video display area"""
        video_frame = ttk.LabelFrame(parent, text="📺 Live Video Stream", padding=12)
        video_frame.grid(row=1, column=0, sticky='nsew', pady=(0, 15))

        # Video display area with clean design
        self.video_label = tk.Label(video_frame, text="Waiting for video stream...",
                                   font=('Segoe UI', 13), foreground='#64748b',
                                   bg='#f8fafc', relief='solid', borderwidth=1,
                                   cursor='hand2')
        self.video_label.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)

    def _build_status_bar(self, parent: ttk.Frame) -> None:
        """Build the clean status bar with connection status"""
        status_frame = ttk.Frame(parent, style='Card.TFrame', padding=(15, 12))
        status_frame.grid(row=3, column=0, sticky='ew')

        # Left side - Status
        left_frame = ttk.Frame(status_frame, style='Card.TFrame')
        left_frame.pack(side=tk.LEFT)

        status_indicator = ttk.Label(left_frame, text="●", style='Status.TLabel', 
                                    font=('Segoe UI', 12))
        status_indicator.pack(side=tk.LEFT, padx=(0, 8))

        self.status_var = tk.StringVar(value="⏳ Waiting for connection...")
        status_label = ttk.Label(left_frame, textvariable=self.status_var, style='Disconnected.TLabel')
        status_label.pack(side=tk.LEFT)

        # Right side - Stats
        right_frame = ttk.Frame(status_frame, style='Card.TFrame')
        right_frame.pack(side=tk.RIGHT)

        self.fps_var = tk.StringVar(value="FPS: --")
        fps_label = ttk.Label(right_frame, textvariable=self.fps_var, 
                             font=('Segoe UI', 9), foreground='#64748b', background='#ffffff')
        fps_label.pack(side=tk.LEFT, padx=(0, 20))

        self.latency_var = tk.StringVar(value="Latency: --ms")
        latency_label = ttk.Label(right_frame, textvariable=self.latency_var,
                                 font=('Segoe UI', 9), foreground='#64748b', background='#ffffff')
        latency_label.pack(side=tk.LEFT, padx=(0, 20))

        # Stop button
        self.stop_button = ttk.Button(right_frame, text="⏹️ Stop",
                                     command=self._stop_server, style='TButton', width=10)
        self.stop_button.pack(side=tk.RIGHT)

    def _poll_frames(self) -> None:
        try:
            # Get frame from active camera
            if self.active_camera_id and self.active_camera_id in self.cameras:
                camera = self.cameras[self.active_camera_id]
                frame_data, timestamp = camera['queue'].get_nowait()
                self._display_frame(frame_data, timestamp)
        except queue.Empty:
            pass
        finally:
            self.after(8, self._poll_frames)  # ~8ms for 120Hz polling

    def _display_frame(self, frame_data: bytes, timestamp: float) -> None:
        try:
            image = Image.open(io.BytesIO(frame_data)).convert("RGB")
            
            # Apply image processing filters
            image = self._apply_image_processing(image)
            
            # Use BILINEAR for faster resizing (good quality, much faster than LANCZOS)
            display_size = self._compute_display_size(image.size)
            if display_size != image.size:
                image = image.resize(display_size, Image.Resampling.BILINEAR)
            self._photo_image = ImageTk.PhotoImage(image)
            self.video_label.configure(image=self._photo_image, text="")
            now = time.time()
            
            # Update stats for active camera
            if self.active_camera_id and self.active_camera_id in self.cameras:
                stats = self.cameras[self.active_camera_id]['stats']
                stats.latency_ms = max((now - timestamp) * 1000.0, 0.0)
            
            self._current_image_ts = now
        except Exception:
            # Silently ignore frame display errors to avoid disrupting stream
            return

    def _compute_display_size(self, image_size: tuple[int, int]) -> tuple[int, int]:
        label_width = max(self.video_label.winfo_width(), 320)
        label_height = max(self.video_label.winfo_height(), 240)
        
        # Cache display size if label size hasn't changed
        current_label_size = (label_width, label_height)
        if (self._cached_display_size is not None and 
            self._last_label_size == current_label_size):
            return self._cached_display_size
        
        image_width, image_height = image_size
        width_ratio = label_width / image_width
        height_ratio = label_height / image_height
        scale = min(width_ratio, height_ratio)
        
        display_size = (int(image_width * scale), int(image_height * scale))
        self._cached_display_size = display_size
        self._last_label_size = current_label_size
        return display_size
    
    def _apply_image_processing(self, image: Image.Image) -> Image.Image:
        """Apply image processing settings to the frame"""
        try:
            # Apply brightness
            if self.image_settings.brightness != 1.0:
                enhancer = ImageEnhance.Brightness(image)
                image = enhancer.enhance(self.image_settings.brightness)
            
            # Apply contrast
            if self.image_settings.contrast != 1.0:
                enhancer = ImageEnhance.Contrast(image)
                image = enhancer.enhance(self.image_settings.contrast)
            
            # Apply saturation
            if self.image_settings.saturation != 1.0:
                enhancer = ImageEnhance.Color(image)
                image = enhancer.enhance(self.image_settings.saturation)
            
            # Apply filter
            if self.image_settings.filter_type == "grayscale":
                image = image.convert('L').convert('RGB')
            elif self.image_settings.filter_type == "blur":
                image = image.filter(ImageFilter.BLUR)
            elif self.image_settings.filter_type == "sharpen":
                image = image.filter(ImageFilter.SHARPEN)
            elif self.image_settings.filter_type == "edge_enhance":
                image = image.filter(ImageFilter.EDGE_ENHANCE)
            
            return image
        except Exception:
            # Return original image if processing fails
            return image

    def _refresh_stats(self) -> None:
        """Update the FPS and latency display"""
        if self.active_camera_id and self.active_camera_id in self.cameras:
            stats = self.cameras[self.active_camera_id]['stats']
            elapsed = time.time() - stats.last_updated
            if elapsed < 2.0 and stats.fps > 0:
                self.fps_var.set(f"FPS: {stats.fps:.1f}")
                self.latency_var.set(f"Latency: {stats.latency_ms:.0f}ms")
                self.status_var.set(f"🎬 Streaming: {stats.camera_id}")
            else:
                self.fps_var.set("FPS: --")
                self.latency_var.set("Latency: --ms")
                self.status_var.set("⏳ Waiting for stream...")
        else:
            self.fps_var.set("FPS: --")
            self.latency_var.set("Latency: --ms")
            self.status_var.set("⏳ No camera selected...")
        self.after(500, self._refresh_stats)

    def _stop_server(self) -> None:
        for camera in self.cameras.values():
            camera['server'].stop()
        self.status_var.set("All servers stopped")

    def _on_close(self) -> None:
        self._stop_server()
        self.destroy()
    
    def _on_zoom_change(self, value: str) -> None:
        zoom_value = float(value)
        self.zoom_label.config(text=f"{zoom_value:.1f}x")
        if self.active_camera_id and self.active_camera_id in self.cameras:
            self.cameras[self.active_camera_id]['server'].send_control_command(f"ZOOM:{zoom_value:.2f}")
    
    def _on_exposure_change(self, value: str) -> None:
        exposure_value = int(float(value))
        self.exposure_label.config(text=str(exposure_value))
        if self.active_camera_id and self.active_camera_id in self.cameras:
            self.cameras[self.active_camera_id]['server'].send_control_command(f"EXPOSURE:{exposure_value}")
    
    def _on_focus_change(self, value: str) -> None:
        focus_value = float(value)
        self.focus_label.config(text=f"{focus_value:.2f}")
        if self.active_camera_id and self.active_camera_id in self.cameras:
            self.cameras[self.active_camera_id]['server'].send_control_command(f"FOCUS:{focus_value:.2f}")
    
    def _on_brightness_change(self, value: str) -> None:
        brightness_value = float(value)
        self.brightness_label.config(text=f"{brightness_value:.2f}")
        self.image_settings.brightness = brightness_value
    
    def _on_contrast_change(self, value: str) -> None:
        contrast_value = float(value)
        self.contrast_label.config(text=f"{contrast_value:.2f}")
        self.image_settings.contrast = contrast_value
    
    def _on_saturation_change(self, value: str) -> None:
        saturation_value = float(value)
        self.saturation_label.config(text=f"{saturation_value:.2f}")
        self.image_settings.saturation = saturation_value
    
    def _on_filter_change(self) -> None:
        self.image_settings.filter_type = self.filter_var.get()
    
    def _show_add_camera_dialog(self) -> None:
        """Show dialog to add a new camera"""
        dialog = tk.Toplevel(self)
        dialog.title("Add New Camera")
        dialog.geometry("400x250")
        dialog.resizable(False, False)
        dialog.configure(bg='#f5f7fa')
        
        # Center the dialog
        dialog.transient(self)
        dialog.grab_set()
        
        frame = ttk.Frame(dialog, padding=20, style='Card.TFrame')
        frame.pack(fill=tk.BOTH, expand=True)
        
        # Camera name
        ttk.Label(frame, text="Camera Name:", style='TLabel').grid(row=0, column=0, sticky='w', pady=10)
        name_entry = ttk.Entry(frame, width=25)
        name_entry.grid(row=0, column=1, pady=10, padx=10)
        name_entry.insert(0, f"Camera {len(self.cameras) + 1}")
        
        # Host/IP
        ttk.Label(frame, text="Host/IP:", style='TLabel').grid(row=1, column=0, sticky='w', pady=10)
        host_entry = ttk.Entry(frame, width=25)
        host_entry.grid(row=1, column=1, pady=10, padx=10)
        host_entry.insert(0, "0.0.0.0")
        
        # Port
        ttk.Label(frame, text="Port:", style='TLabel').grid(row=2, column=0, sticky='w', pady=10)
        port_entry = ttk.Entry(frame, width=25)
        port_entry.grid(row=2, column=1, pady=10, padx=10)
        port_entry.insert(0, str(5000 + len(self.cameras)))
        
        error_label = ttk.Label(frame, text="", foreground='red', background='#ffffff', style='TLabel')
        error_label.grid(row=4, column=0, columnspan=2, pady=(0, 10))
        
        def add_camera():
            name = name_entry.get().strip()
            host = host_entry.get().strip()
            try:
                port = int(port_entry.get().strip())
                if not name:
                    error_label.config(text="Please enter a camera name")
                    return
                if not host:
                    error_label.config(text="Please enter a host/IP address")
                    return
                if port < 1024 or port > 65535:
                    error_label.config(text="Port must be between 1024 and 65535")
                    return
                if name in self.cameras:
                    error_label.config(text=f"Camera '{name}' already exists")
                    return
                    
                self._add_camera(name, host, port)
                self.cameras[name]['server'].start()
                # Update camera dropdown
                self.camera_dropdown['values'] = list(self.cameras.keys())
                dialog.destroy()
            except ValueError:
                error_label.config(text="Invalid port number")
        
        # Buttons
        button_frame = ttk.Frame(frame, style='Card.TFrame')
        button_frame.grid(row=5, column=0, columnspan=2, pady=20)
        
        ttk.Button(button_frame, text="Add", command=add_camera, style='TButton').pack(side=tk.LEFT, padx=5)
        ttk.Button(button_frame, text="Cancel", command=dialog.destroy, style='TButton').pack(side=tk.LEFT, padx=5)
    
    def _update_camera_label(self) -> None:
        """Update UI to reflect active camera"""
        pass  # Can be extended to show camera info
    
    def _reset_controls(self) -> None:
        self.zoom_var.set(1.0)
        self.exposure_var.set(0)
        self.focus_var.set(0.5)
        self.brightness_var.set(1.0)
        self.contrast_var.set(1.0)
        self.saturation_var.set(1.0)
        self.filter_var.set("none")
        
        self.zoom_label.config(text="1.0x")
        self.exposure_label.config(text="0")
        self.focus_label.config(text="0.50")
        self.brightness_label.config(text="1.00")
        self.contrast_label.config(text="1.00")
        self.saturation_label.config(text="1.00")
        
        self.image_settings.brightness = 1.0
        self.image_settings.contrast = 1.0
        self.image_settings.saturation = 1.0
        self.image_settings.filter_type = "none"
        
        if self.active_camera_id and self.active_camera_id in self.cameras:
            server = self.cameras[self.active_camera_id]['server']
            server.send_control_command("ZOOM:1.0")
            server.send_control_command("EXPOSURE:0")
            server.send_control_command("FOCUS:0.5")


def get_local_ip_addresses() -> list[str]:
    ips: list[str] = []
    try:
        hostname = socket.gethostname()
        ips.extend(
            [addr for addr in socket.gethostbyname_ex(hostname)[2] if not addr.startswith("127.")]
        )
    except socket.gaierror:
        pass

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ips.append(s.getsockname()[0])
    except OSError:
        pass

    return sorted(set(ips)) or ["127.0.0.1"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Laptop listener for ChessAssist streaming client")
    parser.add_argument("--host", default="0.0.0.0", help="Host/IP to bind the server to")
    parser.add_argument("--port", type=int, default=5000, help="Port to listen on")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    app = ViewerApp(args)
    app.mainloop()


if __name__ == "__main__":
    main()
