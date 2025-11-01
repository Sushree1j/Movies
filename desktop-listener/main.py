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
from typing import Optional, Tuple

from PIL import Image, ImageTk

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
        # Don't create multiple tooltip windows
        if self.tooltip_window is not None:
            return
            
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
            try:
                self.tooltip_window.destroy()
            except tk.TclError:
                pass
            self.tooltip_window = None


@dataclass
class FrameStats:
    fps: float = 0.0
    latency_ms: float = 0.0
    last_updated: float = time.time()


class VideoServer:
    def __init__(self, host: str, port: int, frame_queue: queue.Queue, stats: FrameStats):
        self.host = host
        self.port = port
        self.frame_queue = frame_queue
        self.stats = stats
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
            # Increase receive buffer for better throughput
            client_socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 512 * 1024)
            client_socket.settimeout(5.0)
            self._client_output_stream = client_socket
            frame_count = 0
            window_start = time.time()

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
        # Drop old frames if queue is full - more efficient than get/put dance
        if self.frame_queue.full():
            try:
                self.frame_queue.get_nowait()
            except queue.Empty:
                pass
        try:
            self.frame_queue.put_nowait((frame_data, timestamp))
        except queue.Full:
            pass
        self.stats.last_updated = timestamp

    def _recvall(self, client_socket: socket.socket, length: int) -> Optional[bytes]:
        data = bytearray()
        while len(data) < length:
            try:
                chunk = client_socket.recv(length - len(data))
            except socket.timeout:
                return None
            if not chunk:
                return None
            data.extend(chunk)
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
        self.title("🎥 ChessAssist Pro - Video Stream Listener")
        self.geometry("1400x850")
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

        self.frame_queue: queue.Queue = queue.Queue(maxsize=1)
        self.stats = FrameStats()
        self.server = VideoServer(args.host, args.port, self.frame_queue, self.stats)

        self._photo_image: Optional[ImageTk.PhotoImage] = None
        self._current_image_ts: float = 0.0
        self._cached_display_size: Optional[Tuple[int, int]] = None
        self._last_label_size: Tuple[int, int] = (0, 0)
        self._frame_count: int = 0  # Count frames to throttle UI updates

        self._build_ui(args)
        self.server.start()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(10, self._poll_frames)
        self.after(500, self._refresh_stats)

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
        controls_frame = ttk.LabelFrame(parent, text="🎛️ Camera Controls", padding=18)
        controls_frame.grid(row=2, column=0, sticky='ew', pady=(0, 15))

        # Create a grid layout for controls
        controls_container = ttk.Frame(controls_frame, style='Card.TFrame')
        controls_container.pack(fill=tk.X)

        # Zoom control
        self._create_control_row(controls_container, "🔍 Zoom", 0, 1.0, 10.0, "1.0x", self._on_zoom_change)

        # Exposure control
        self._create_control_row(controls_container, "☀️ Exposure", 1, -12, 12, "0", self._on_exposure_change)

        # Focus control
        self._create_control_row(controls_container, "🎯 Focus", 2, 0.0, 1.0, "0.50", self._on_focus_change)

        # Reset button with better styling
        button_frame = ttk.Frame(controls_container, style='Card.TFrame')
        button_frame.grid(row=3, column=0, columnspan=3, pady=(18, 5), sticky='ew')

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
        """Poll for new frames from the queue"""
        try:
            frame_data, timestamp = self.frame_queue.get_nowait()
            self._display_frame(frame_data, timestamp)
        except queue.Empty:
            pass
        
        # Poll at ~60 FPS (every 17ms) to reduce CPU usage
        self.after(17, self._poll_frames)

    def _display_frame(self, frame_data: bytes, timestamp: float) -> None:
        """Display a frame in the video label"""
        try:
            image = Image.open(io.BytesIO(frame_data)).convert("RGB")
            display_size = self._compute_display_size(image.size)
            image = image.resize(display_size, RESAMPLE_LANCZOS)
            self._photo_image = ImageTk.PhotoImage(image)
            self.video_label.configure(image=self._photo_image, text="")
            now = time.time()
            self.stats.latency_ms = max((now - timestamp) * 1000.0, 0.0)
            self._current_image_ts = now
            # Status will be updated by _refresh_stats
        except Exception:
            return

    def _compute_display_size(self, image_size: Tuple[int, int]) -> Tuple[int, int]:
        """Compute optimal display size for image, with caching"""
        label_width = max(self.video_label.winfo_width(), 320)
        label_height = max(self.video_label.winfo_height(), 240)
        current_label_size = (label_width, label_height)
        
        # Cache display size if label size hasn't changed
        if self._cached_display_size is not None and self._last_label_size == current_label_size:
            return self._cached_display_size
        
        image_width, image_height = image_size
        width_ratio = label_width / image_width
        height_ratio = label_height / image_height
        scale = min(width_ratio, height_ratio)
        
        self._last_label_size = current_label_size
        self._cached_display_size = (int(image_width * scale), int(image_height * scale))
        return self._cached_display_size

    def _refresh_stats(self) -> None:
        """Update the FPS and latency display"""
        elapsed = time.time() - self.stats.last_updated
        if elapsed < 2.0 and self.stats.fps > 0:
            self.fps_var.set(f"FPS: {self.stats.fps:.1f}")
            self.latency_var.set(f"Latency: {self.stats.latency_ms:.0f}ms")
            self.status_var.set("🎬 Streaming active")
        else:
            self.fps_var.set("FPS: --")
            self.latency_var.set("Latency: --ms")
            self.status_var.set("⏳ Waiting for stream...")
        self.after(500, self._refresh_stats)

    def _stop_server(self) -> None:
        self.server.stop()
        self.status_var.set("Server stopped")

    def _on_close(self) -> None:
        self._stop_server()
        self.destroy()
    
    def _on_zoom_change(self, value: str) -> None:
        zoom_value = float(value)
        self.zoom_label.config(text=f"{zoom_value:.1f}x")
        self.server.send_control_command(f"ZOOM:{zoom_value:.2f}")
    
    def _on_exposure_change(self, value: str) -> None:
        exposure_value = int(float(value))
        self.exposure_label.config(text=str(exposure_value))
        self.server.send_control_command(f"EXPOSURE:{exposure_value}")
    
    def _on_focus_change(self, value: str) -> None:
        focus_value = float(value)
        self.focus_label.config(text=f"{focus_value:.2f}")
        self.server.send_control_command(f"FOCUS:{focus_value:.2f}")
    
    def _reset_controls(self) -> None:
        self.zoom_var.set(1.0)
        self.exposure_var.set(0)
        self.focus_var.set(0.5)
        self.zoom_label.config(text="1.0x")
        self.exposure_label.config(text="0")
        self.focus_label.config(text="0.50")
        self.server.send_control_command("ZOOM:1.0")
        self.server.send_control_command("EXPOSURE:0")
        self.server.send_control_command("FOCUS:0.5")


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
